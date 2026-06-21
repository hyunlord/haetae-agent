"""LocalAgentExecutor — 약한 *로컬* 모델(OpenAI 호환 엔드포인트)을 유닛 *빌더*로 모는 provider (WO#137).

#136에서 GB10의 llama.cpp가 실용 속도(7B@28 t/s)로 확인돼, haetae가 약한 로컬 모델을
*빌더*로 쓰는 첫 토대다. codex executor와 **동일 인터페이스**(`run(order) -> str` +
`last_usage`)라 provider-pluggable 루프가 codex 자리에 그대로 끼운다(`--executor local`).

설계 핵심:
  - **빌더 전용(적대 분리 sacred)**: 이 모듈은 judge/run-judge/gate/critic 경로에 *절대*
    주입되지 않는다. 약한 빌더라도 판정은 기존 강한 모델(codex)이 독립·적대적으로 한다.
    구조적 보장: 이 클래스는 `run(order)`만 갖고 `complete()`가 *없다* → LLMClient(judge
    client) 프로토콜을 충족하지 못해 judge 경로에 끼울 수 없다(테스트로 단언).
  - **경계 있는 에이전틱 루프(#134: 제약 하 단발>수다)**: work order + workdir 컨텍스트를
    주고, 모델이 *경로-태깅된 편집 블록*을 내면 workdir에 적용한다. 견고 파싱(펜스/프리앰블
    내성), turn 상한(28 t/s 경제성), 선택적 에러-피드백 1턴. gate가 이후 독립 판정한다.
  - **새 의존성 없음**: stdlib `urllib`만으로 OpenAI 호환 `/chat/completions`를 호출한다
    (codex provider의 "stdlib만" 규율과 동일).
  - **안전**: 빌트 코드 실행은 gate의 기존 오프라인 규율 그대로다. 이 모듈의 네트워크는
    *executor 자신의 모델 API 호출*뿐(codex의 API 호출과 동격). 편집 적용은 workdir 밖으로
    *절대* 못 나간다(경로 가둠 — 약한 모델 출력은 신뢰하지 않는다). ALLOWED_SANDBOXES 불변.
"""

from __future__ import annotations

import inspect
import json
import os
import py_compile
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

from haetae.metering import Usage
from haetae.models import NextOrder

# 모델이 "다 끝났다"를 알리는 센티넬. 본문 어디에 있어도(코드블록 밖) 탐지된다.
DONE_MARKER = "<<HAETAE_DONE>>"

# 컨텍스트 주입 시 건너뛸 디렉토리(노이즈/대용량). 트리 나열·파일 첨부 양쪽에서 제외.
_SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv", ".next", "coverage"}

# 빌더 시스템 프롬프트. *편집 프로토콜*을 엄격히 규정한다. 약한 모델이라 단순·반복적으로.
_BUILDER_SYSTEM = (
    "너는 코드 빌더다. 주어진 work order를 이 프로젝트에 **파일 편집**으로 구현한다.\n"
    "\n"
    "출력 규칙(엄격):\n"
    "- 만들거나 덮어쓸 파일마다 **펜스 코드블록**을 내되, 펜스 정보줄에 `path=<상대경로>`를 넣어라:\n"
    "\n"
    "```path=src/foo.ts\n"
    "<파일 전체 내용>\n"
    "```\n"
    "\n"
    "- 경로는 프로젝트 루트 기준 **상대경로**. 절대경로·`..` 금지.\n"
    "- diff가 아니라 **파일 전체 내용**(전체 덮어쓰기)을 내라.\n"
    "- 언어 토큰을 앞에 붙여도 된다(예: ```ts path=src/foo.ts). 둘 다 인식된다.\n"
    "- work order를 만족시키는 데 필요한 파일을 모두 내라.\n"
    f"- 모든 편집이 끝나면 마지막에 정확히 이 한 줄을 출력하라: {DONE_MARKER}\n"
    "- 질문하지 마라. 장황한 설명 금지. 편집 블록과 완료 신호만."
)


class LocalAgentError(RuntimeError):
    """LocalAgentExecutor 실행 실패(엔드포인트 오류/빈 응답/편집 0건 등).

    CodexExecutorError와 동격 — 루프가 일반 유닛 실패로 처리한다. 디버깅용으로 모델 최종
    메시지 일부를 동봉할 수 있다.
    """

    def __init__(self, message: str, detail: str = ""):
        self.detail = detail

        def _tail(s: str, n: int = 800) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else "…" + s[-n:]

        super().__init__(message + (f"\n--- 모델 응답(tail) ---\n{_tail(detail)}" if detail else ""))


class FileEdit(NamedTuple):
    """파싱된 파일 편집 한 건 = (상대경로, 전체 내용)."""

    path: str
    content: str


# ──────────────────────────── 편집 프로토콜 파싱(견고) ────────────────────────────


def _extract_path(info: str) -> str | None:
    """펜스 정보줄에서 편집 대상 경로를 뽑는다(없으면 None — 그 블록은 편집 아님).

    1순위: `path=<token>` (언어 토큰이 앞에 있어도 됨, 예: "ts path=src/foo.ts").
    2순위: 정보줄이 *경로처럼 생긴 단일 토큰*(슬래시나 점 포함, `=`/공백 없음) — bare 경로 폼.
    그 외(예: "typescript", "python")는 단순 언어 태그라 None(편집 아님 → #134 프리앰블 내성).
    """
    info = (info or "").strip()
    if not info:
        return None
    # 1순위: path=<token>
    for tok in info.split():
        if tok.startswith("path="):
            p = tok[len("path="):].strip().strip("\"'")
            return p or None
    # 2순위: bare 경로(토큰 1개 + 경로처럼 생김)
    toks = info.split()
    if len(toks) == 1 and "=" not in toks[0]:
        t = toks[0].strip().strip("\"'")
        if ("/" in t or "." in t) and not t.startswith("`"):
            return t
    return None


def parse_edits(text: str) -> list[FileEdit]:
    """모델 응답에서 *경로-태깅된 펜스 블록*을 모두 뽑아 FileEdit 리스트로 반환한다.

    줄 단위 상태기계라 프리앰블/설명 산문·언어 태그(```typescript)에 견고하다(#134). 경로가
    없는 블록(단순 언어 태그)은 *무시*한다 — 편집이 아니라 예시/설명으로 본다. 같은 경로가
    여러 번 나오면 마지막 것이 이긴다(모델이 수정해 다시 낸 경우). 본문 내 ``` 중첩은 첫 닫는
    펜스에서 끊긴다(약한 모델·작은 파일 전제의 알려진 한계).
    """
    edits: dict[str, str] = {}
    order: list[str] = []
    lines = (text or "").splitlines()
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            info = stripped[3:]
            path = _extract_path(info)
            body: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            # i는 닫는 펜스(또는 EOF)를 가리킨다 — 소비.
            if i < n:
                i += 1
            if path is not None:
                content = "\n".join(body)
                # 파일은 보통 개행으로 끝난다 — 본문이 있으면 끝개행 보장(없으면 빈 파일 그대로).
                if content and not content.endswith("\n"):
                    content += "\n"
                if path not in edits:
                    order.append(path)
                edits[path] = content
            continue
        i += 1
    return [FileEdit(p, edits[p]) for p in order]


def is_done(text: str) -> bool:
    """모델이 완료 신호를 냈는지. DONE_MARKER가 있거나, 단독 'DONE' 줄(관대)."""
    if DONE_MARKER in (text or ""):
        return True
    for line in (text or "").splitlines():
        if line.strip() in ("DONE", "<<DONE>>"):
            return True
    return False


# ──────────────────────────── workdir 안전 적용(경로 가둠) ────────────────────────────


def safe_target(workdir: Path, rel: str) -> Path | None:
    """workdir 기준 상대경로를 *workdir 안으로 가둔* 절대경로로 해소한다(밖이면 None).

    약한 모델 출력은 신뢰하지 않는다 — 절대경로·`..` 탈출은 거부(None)한다. 빈 경로도 None.
    이건 *안전 경계*다: 빌더가 workdir 밖 파일을 못 건드린다(codex의 cwd=workdir 가둠과 동격).
    """
    rel = (rel or "").strip()
    if not rel:
        return None
    # 절대경로는 상대로 강등(앞 슬래시 제거) — 그래도 .. 탈출은 아래서 막힌다.
    rel = rel.lstrip("/")
    if not rel:
        return None
    base = workdir.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None  # workdir 밖으로 탈출 → 거부
    return target


def apply_edits(workdir: Path, edits: list[FileEdit]) -> list[str]:
    """편집들을 workdir에 적용(전체 덮어쓰기)한다. 적용된 *상대경로* 리스트를 반환.

    workdir 밖으로 탈출하는 경로(safe_target=None)는 *건너뛴다*(안전). 부모 디렉토리는 생성한다.
    """
    applied: list[str] = []
    for edit in edits:
        target = safe_target(workdir, edit.path)
        if target is None:
            continue  # 탈출 시도 — 무시(거부)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit.content, encoding="utf-8")
        applied.append(edit.path)
    return applied


# ──────────────────────────── OpenAI 호환 호출(stdlib urllib) ────────────────────────────


def post_chat(endpoint: str, payload: dict, timeout: float) -> dict:
    """OpenAI 호환 `/chat/completions`에 POST하고 JSON 응답(dict)을 반환한다(테스트 seam).

    endpoint는 `.../v1` 형태(끝 슬래시 무관). 여기서 `/chat/completions`를 붙인다. 네트워크/HTTP/
    JSON 오류는 LocalAgentError로 변환한다. **이 함수가 유일한 네트워크 표면**이라 테스트는 이걸
    monkeypatch해 라이브 엔드포인트 없이 단위 검증한다(codex 테스트가 subprocess.run을 패치하듯).
    """
    url = endpoint.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
        raise LocalAgentError(f"로컬 엔드포인트 HTTP {e.code} 오류 ({url})", body) from e
    except (urllib.error.URLError, OSError) as e:
        raise LocalAgentError(f"로컬 엔드포인트 연결 실패 ({url}): {e}") from e
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise LocalAgentError(f"로컬 엔드포인트 응답이 JSON이 아님 ({url})", raw) from e
    if not isinstance(obj, dict):
        raise LocalAgentError(f"로컬 엔드포인트 응답이 객체가 아님 ({url})", raw)
    return obj


def _message_text(resp: dict) -> str:
    """chat completion 응답에서 assistant 메시지 텍스트를 뽑는다(없으면 빈 문자열)."""
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
    # 일부 서버는 text 필드를 쓴다(completions 폼) — best-effort 폴백.
    text = choices[0].get("text") if isinstance(choices[0], dict) else None
    return text if isinstance(text, str) else ""


def _usage_from_resp(resp: dict, model: str | None) -> Usage | None:
    """chat completion 응답의 usage(prompt/completion tokens)를 Usage로(없으면 None — 날조 금지)."""
    u = resp.get("usage")
    if not isinstance(u, dict):
        return None
    inp = u.get("prompt_tokens")
    out = u.get("completion_tokens")
    if not isinstance(inp, int):
        inp = None
    if not isinstance(out, int):
        out = None
    if inp is None and out is None:
        return None
    return Usage(input_tokens=inp, output_tokens=out, model=model)


# ──────────────────────────── work order/컨텍스트 포맷 ────────────────────────────


def _format_order(order: NextOrder) -> str:
    """NextOrder를 빌더용 work order 텍스트로 렌더한다(자족적 — executors.py 미의존, 순환 회피)."""
    out: list[str] = [f"# WORK ORDER — unit {order.unit}", "", "## goal", order.goal]
    if order.scope:
        out += ["", "## scope", order.scope]
    if order.context_refs:
        out += ["", "## context_refs"] + [f"- {c}" for c in order.context_refs]
    if order.local_checks:
        out += ["", "## local_checks"]
        for c in order.local_checks:
            extra = f"  (기대 pass: {c.pass_})" if c.pass_ else ""
            out.append(f"- [{c.type.value}] {c.cmd or '(cmd 없음)'}{extra}")
    if order.deliverable:
        out += ["", "## deliverable", order.deliverable]
    return "\n".join(out)


def workdir_context(workdir: Path, order: NextOrder, *, max_files: int = 60, max_chars: int = 8000) -> str:
    """workdir의 파일 트리(+ context_refs로 지목된 기존 파일 내용)를 빌더 컨텍스트로 만든다.

    약한 모델에 *무엇이 이미 있는지* 알려준다. 노이즈 디렉토리(_SKIP_DIRS)·과대 출력은 cap한다.
    best-effort: 읽기 실패는 흡수(컨텍스트는 보조일 뿐 — 실패가 빌드를 죽이지 않는다).
    """
    base = workdir.resolve()
    rel_files: list[str] = []
    try:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(root, f), base)
                rel_files.append(rel)
                if len(rel_files) >= max_files:
                    break
            if len(rel_files) >= max_files:
                break
    except OSError:
        pass
    parts: list[str] = []
    if rel_files:
        parts.append("## 현재 workdir 파일들")
        parts += [f"- {p}" for p in sorted(rel_files)]
    # context_refs가 실제 파일 경로면 내용 일부를 첨부(best-effort, cap).
    attached = 0
    for ref in order.context_refs or []:
        target = safe_target(base, ref)
        if target is None or not target.is_file():
            continue
        try:
            body = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…(잘림)"
        parts += ["", f"## 기존 파일: {ref}", "```", body, "```"]
        attached += 1
        if attached >= 5:
            break
    return "\n".join(parts)


# ──────────────────────────── 빌더-측 구조적 스모크 (WO#139) ────────────────────────────
#
# #138 진단: 7B가 minimax는 맞췄으나 impl↔test 불일치(test가 자기 impl엔 없는 API import →
# collection error)를 4시도 내내 못 고침 — *에러를 못 봐서*. 해법: 편집 적용 후 빌더가 *스스로*
# 구조적 스모크(컴파일 + pytest --collect-only)를 돌려 정확한 에러를 보고 bounded 턴 내 self-fix.
#
# 적대 분리(sacred): 스모크 = **빌더-측 자기검사**(구조적: 컴파일 가능·임포트(collect) 가능만).
# *판정 아님* — 정답성/완결성/criteria 충족은 여전히 독립 적대 run-judge/gate가 판정한다(불변).
# collect-only는 테스트를 *실행/채점하지 않는다*(import 가능 여부만) → #82 self-verification·
# #108 harness-smoke와 동형. judge/run-judge/gate/critic 코드·경로엔 일절 닿지 않는다.


def _smoke_run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """스모크용 subprocess 실행 seam(테스트 monkeypatch 지점). (returncode, output_tail) 반환.

    빌트 코드 *오프라인 규율*은 gate와 동일(네트워크 미부여) — collect-only는 import-time만 돌린다.
    툴 부재/타임아웃은 특별 코드로 반환해 호출부가 graceful 처리한다. ALLOWED_SANDBOXES 무관.
    """
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return (127, "")  # 인터프리터/툴 없음 → 호출부 skip
    except subprocess.TimeoutExpired:
        return (124, "smoke timeout")
    return (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _py_files(workdir: Path, cap: int = 300) -> list[Path]:
    """workdir의 .py 파일들(노이즈 디렉토리 제외, cap). 컴파일/스모크 대상 수집."""
    files: list[Path] = []
    for root, dirs, names in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for n in names:
            if n.endswith(".py"):
                files.append(Path(root) / n)
                if len(files) >= cap:
                    return files
    return files


# ──────────────────────────── 스택 감지 + JS/node 미러 (WO#146) ────────────────────────────
#
# #145: #144 자기-테스트가 u1 수렴 실증(메커니즘 작동)했으나 Python(pytest/unittest) 전용 —
# 합성기가 게임에 흔히 고르는 JS/vitest 스택엔 inert(_py_files 비어 → no-op으로 "통과"). 북극성
# 태스크(snake/crowd-sim/platformer)가 다 JS/Canvas이므로 증명된 lift 메커니즘을 JS로 확장한다.
# 스모크(findability)·자기-테스트를 *스택에 맞게 라우팅* — Python 경로는 분기 가드로 **무회귀**.
# 적대 분리 sacred·불변: JS도 빌더-측(자기 테스트 green) — 독립 적대 gate가 진짜 바(gate 무접촉).

# JS 테스트 러너 마커(check_cmds에 있으면 JS 스택 — gate 러너를 권위 신호로). 'npm test'류 포함.
_JS_TEST_RUNNER_MARKERS = (
    "vitest", "jest", "npm test", "npm run test", "pnpm test", "pnpm run test",
    "yarn test", "node --test",
)
# JS/TS 소스 확장자(.d.ts 타입선언은 제외 — 컴파일/실행 대상 아님).
_JS_SRC_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")


def _detect_stack(workdir: Path, check_cmds: list[str] | None = None) -> str:
    """빌더 워크트리/체크명령으로 스택을 감지: "python" | "js"(기본 "python" — 기존 동작 안전).

    1순위 = check_cmds(gate가 실제로 돌릴 러너 — 가장 신뢰): pytest/unittest → python,
    vitest/jest/npm-test류 → js. 2순위 = 파일: package.json 있으면 js. 그 외 → python(기본).
    pytest/unittest가 있으면 package.json이 있어도 python(혼합 워크트리서 Python 의도 우선).
    """
    blob = " ".join(c for c in (check_cmds or []) if c).lower()
    if "pytest" in blob or "unittest" in blob:
        return "python"
    if any(m in blob for m in _JS_TEST_RUNNER_MARKERS):
        return "js"
    if (Path(workdir) / "package.json").is_file():
        return "js"
    return "python"


def _js_files(workdir: Path, cap: int = 400) -> list[Path]:
    """workdir의 JS/TS 소스 파일들(노이즈 디렉토리 제외, .d.ts 제외, cap). 구문/스모크 대상."""
    files: list[Path] = []
    for root, dirs, names in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for n in names:
            if n.endswith(_JS_SRC_EXTS) and not n.endswith(".d.ts"):
                files.append(Path(root) / n)
                if len(files) >= cap:
                    return files
    return files


def _js_test_files(workdir: Path) -> list[Path]:
    """JS/TS 테스트 파일들(*.test.* / *.spec.*) — 디스커버리/자기-테스트 존재 판정."""
    return [f for f in _js_files(workdir) if ".test." in f.name or ".spec." in f.name]


def _smoke_node_check(workdir: Path, timeout: float) -> str | None:
    """`node --check`로 평문 JS(.js/.mjs/.cjs) *구문만* 검사(실행 아님). node 부재면 skip(None).

    .ts/.tsx는 node가 못 파싱하므로 제외(_smoke_tsc 소관). py_compile의 JS 동형 — 컴파일/문법만.
    """
    for f in _js_files(workdir):
        if f.suffix not in (".js", ".mjs", ".cjs"):
            continue
        rc, out = _smoke_run(["node", "--check", str(f)], workdir, timeout)
        if rc in (127, 124):
            return None  # node 부재/타임아웃 → skip(빌드 막지 않음)
        if rc != 0:
            rel = os.path.relpath(str(f), str(workdir))
            return f"[smoke node --check 실패] {rel} (구문 오류):\n{out.strip()[-1500:]}"
    return None


def _smoke_tsc(workdir: Path, timeout: float) -> str | None:
    """tsconfig.json + 설치된 tsc가 있으면 `tsc --noEmit`(타입/컴파일 검사, *실행 아님*). 없으면 skip.

    오프라인 규율: 설치된 `node_modules/.bin/tsc`만 쓴다(네트워크 install 안 함). 부재면 None(skip).
    """
    wd = Path(workdir)
    if not (wd / "tsconfig.json").is_file():
        return None
    tsc = wd / "node_modules" / ".bin" / "tsc"
    if not tsc.exists():
        return None  # tsc 미설치 → skip(오프라인; 네트워크 install 금지)
    rc, out = _smoke_run([str(tsc), "--noEmit"], workdir, timeout)
    if rc in (127, 124):
        return None
    if rc != 0:
        return f"[smoke tsc --noEmit 실패] (타입/컴파일 오류):\n{out.strip()[-2000:]}"
    return None


def _smoke_vitest_collect(workdir: Path, timeout: float) -> str | None:
    """`vitest list` = 테스트 *수집/발견만*(실행/채점 아님 — gate 소관·불변). 미설치면 skip(None).

    설치된 `node_modules/.bin/vitest`를 우선, 없으면 `npx --no-install vitest`(오프라인). 수집 실패
    (import/컴파일 에러 → exit≠0)를 findability 실패로 빌더-측서 잡는다(#138-JS 동형, would-fail
    테스트도 수집되면 통과 — 실행 안 하므로 채점 아님).
    """
    wd = Path(workdir)
    vbin = wd / "node_modules" / ".bin" / "vitest"
    cmd = [str(vbin), "list"] if vbin.exists() else ["npx", "--no-install", "vitest", "list"]
    rc, out = _smoke_run(cmd, workdir, timeout)
    low = out.lower()
    if rc in (127, 124) or "not found" in low or "could not determine" in low or "npm error" in low:
        return None  # vitest 부재 → skip
    if rc != 0:
        return (
            f"[smoke vitest 수집 실패] vitest list (exit {rc}) — 테스트 발견/임포트 실패"
            "(테스트가 import하는 모듈 경로/이름이 impl과 정확히 일치하는지 확인):\n"
            + out.strip()[-2000:]
        )
    return None


def _builder_smoke_js(
    workdir: Path, *, check_cmds: list[str] | None = None, timeout: float = 60.0
) -> str | None:
    """빌더-측 구조적 스모크(JS): (1) node --check 구문 + tsc --noEmit + (2) vitest 수집(발견만).

    builder_smoke의 JS 분기 — Python 경로와 동형(컴파일/문법 → 테스트 있으면 디스커버리). **판정
    아님**(실행/채점은 독립 적대 gate 소관·불변). 통과면 None, 실패면 정확한 에러(다음 턴 주입).
    툴 부재면 그 단계 skip(best-effort — 진짜 검증은 gate).
    """
    err = _smoke_node_check(workdir, timeout)
    if err:
        return err
    err = _smoke_tsc(workdir, timeout)
    if err:
        return err
    if not _js_test_files(workdir):
        return None  # 테스트 파일 없음 → 구문 OK로 구조적 충분(mirror Python clean-pass)
    return _smoke_vitest_collect(workdir, timeout)


def _parse_unittest_discovery(cmds: list[str]) -> tuple[str, str] | None:
    """gate 체크 명령들에 `unittest discover`가 있으면 그 -s(start)·-p(pattern)을 뽑는다(WO#141).

    예: "python -m unittest discover -s tests -p test_*.py" → ("tests", "test_*.py").
    -s/-p 없으면 unittest 기본(".", "test*.py"). unittest discover가 없으면 None.
    """
    for cmd in cmds:
        low = cmd.lower()
        if "unittest" in low and "discover" in low:
            toks = cmd.split()
            start, pattern = ".", "test*.py"
            for i, t in enumerate(toks):
                nxt = toks[i + 1].strip("'\"") if i + 1 < len(toks) else None
                if t in ("-s", "--start-directory") and nxt is not None:
                    start = nxt
                elif t.startswith("-s") and len(t) > 2:
                    start = t[2:].strip("'\"")
                elif t in ("-p", "--pattern") and nxt is not None:
                    pattern = nxt
                elif t.startswith("-p") and len(t) > 2:
                    pattern = t[2:].strip("'\"")
            return (start, pattern)
    return None


def _smoke_conventions(
    workdir: Path, check_cmds: list[str] | None
) -> tuple[bool, tuple[str, str] | None]:
    """검사할 디스커버리 컨벤션 결정: (pytest collect 할까, unittest discover (start,pattern)|None).

    gate 체크 명령(check_cmds)을 알면 *그 러너를 미러*한다(WO#141 — over-constraint 회피):
      - unittest discover 명령 → unittest 디스커버리(그 -s/-p). pytest는 항상(브로드 import 안전망).
      - pytest만 → unittest 강제 안 함(None) — bare-function pytest 테스트 false-fail 방지.
      - 모름(빈 cmds) → 양쪽(pytest collect + 기본 unittest discover) — gate가 어느 러너든 찾게.
    pytest collect는 *항상* 켠다: TestCase도 수집해 정상 테스트엔 false-fail 없고, import 에러를
    브로드하게 잡는다.
    """
    cmds = [c for c in (check_cmds or []) if c]
    blob = " ".join(cmds).lower()
    has_unittest = "unittest" in blob and "discover" in blob
    has_pytest = "pytest" in blob
    if has_unittest:
        return (True, _parse_unittest_discovery(cmds) or (".", "test*.py"))
    if has_pytest:
        return (True, None)  # gate가 pytest만 → unittest findability 강제 안 함
    start = "tests" if (workdir / "tests").is_dir() else "."  # 모름 → 양쪽
    return (True, (start, "test*.py"))


def _parse_pytest_k(cmds: list[str]) -> str | None:
    """gate 체크 명령들에 pytest `-k <expr>`가 있으면 그 keyword expr 반환(없으면 None, WO#144 wart#2).

    예: "python -m pytest -k board_rules" → "board_rules". `-k "a and b"`(따옴표 묶인 표현식)도
    shlex로 처리. pytest 명령이 아니거나 -k가 없으면 None.
    """
    for cmd in cmds or []:
        if "pytest" not in cmd.lower():
            continue
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        for i, t in enumerate(toks):
            if t == "-k" and i + 1 < len(toks):
                return toks[i + 1].strip() or None
            if t.startswith("-k") and len(t) > 2:
                return t[2:].strip().strip("'\"") or None
    return None


def _smoke_pytest_collect(workdir: Path, timeout: float, k_expr: str | None = None) -> str | None:
    """pytest --collect-only(import/collection 검사). 미설치/타임아웃이면 None(skip). 채점 아님.

    WO#144 wart#2: gate가 `pytest -k <expr>`면 그 -k를 *미러*(--collect-only -k expr)해, 수집은
    되나 -k 매칭 0인 경우(=#143 exit-5형, gate 러너가 못 찾음)를 findability 실패로 빌더-측서 잡는다.
    *발견만* — collect-only는 테스트를 실행/채점하지 않는다(적대 분리 불변).
    """
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if k_expr:
        cmd += ["-k", k_expr]
    rc, out = _smoke_run(cmd, workdir, timeout)
    low = out.lower()
    if rc in (127, 124) or "no module named pytest" in low or "no module named \"pytest\"" in low:
        return None
    if k_expr and rc == 5:  # -k 매칭 0 → gate(`pytest -k`)가 테스트를 못 찾는다(findability).
        return (
            f"[smoke pytest -k 발견 실패] --collect-only -k {k_expr} → 매칭 테스트 0개. gate가 "
            f"`pytest -k {k_expr}`로 실행하니 테스트 노드ID(파일/클래스/함수명)에 '{k_expr}' 키워드가 "
            f"들어가게 해라(예: tests/test_{k_expr}.py 또는 def test_{k_expr}_*())."
        )
    if rc not in (0, 5):  # 0=수집됨, 5=테스트 없음 → 구조적 OK. 그 외(2=collection error)=실패.
        return f"[smoke pytest collect 실패] pytest --collect-only (exit {rc}):\n{out.strip()[-2000:]}"
    return None


# unittest 디스커버리를 *발견만* 하는 스니펫: TestLoader.discover().countTestCases()는 테스트를
# *실행/채점하지 않고* 수집만 한다(#139 단언 유지). 발견 0개(=#140 exit-5형)·로드 에러를 잡는다.
_UT_DISCOVER_SNIPPET = (
    "import unittest,sys\n"
    "ld=unittest.TestLoader()\n"
    "try:\n"
    "    s=ld.discover(START, pattern=PAT)\n"
    "except Exception as e:\n"
    "    print('DISCOVER_ERROR', repr(e)); sys.exit(3)\n"
    "errs=getattr(ld,'errors',None) or []\n"
    "cnt=s.countTestCases()\n"
    "if errs:\n"
    "    [print(str(x)[:600]) for x in errs]; sys.exit(2)\n"
    "print('COUNT', cnt); sys.exit(0 if cnt>0 else 5)\n"
)


def _smoke_unittest_discover(
    workdir: Path, start_dir: str, pattern: str, timeout: float
) -> str | None:
    """gate의 `unittest discover -s START -p PAT` 컨벤션을 *미러*해 테스트 *발견 가능성*만 검사한다.

    `TestLoader.discover().countTestCases()` = *수집만*(실행/채점 아님 — gate 소관·불변). 발견 0개
    (=#140 exit-5형, gate 러너가 못 찾음)·디스커버리 import 에러를 빌더-측서 미리 잡아 피드백.
    """
    snippet = _UT_DISCOVER_SNIPPET.replace("START", repr(start_dir)).replace("PAT", repr(pattern))
    rc, out = _smoke_run([sys.executable, "-c", snippet], workdir, timeout)
    if rc in (127, 124):
        return None  # 인터프리터 부재/타임아웃 → skip
    if rc == 5:
        return (
            f"[smoke unittest 발견 실패] unittest discover -s {start_dir} -p {pattern} → 테스트 0개 "
            f"발견. gate 러너가 못 찾는다 — 테스트를 {start_dir}/ 아래 {pattern} 파일에 "
            "unittest.TestCase 하위클래스로 둬라(자유 함수 def test_*()는 unittest가 못 찾음)."
        )
    if rc in (2, 3):
        return f"[smoke unittest 발견 에러] discover(-s {start_dir} -p {pattern}):\n{out.strip()[-1500:]}"
    return None  # rc 0 = 발견 OK


def builder_smoke(
    workdir: Path, *, check_cmds: list[str] | None = None, timeout: float = 60.0
) -> str | None:
    """빌더-측 구조적 스모크 v2(WO#141): (1) py_compile 문법 + (2) *gate 디스커버리 컨벤션 미러*.

    통과(구조적 자기-정합·gate-findable)면 None, 실패면 *정확한 에러*(다음 빌더 턴에 주입).
    **판정 아님** — 컴파일·import·*디스커버리(findability)* 만(테스트 실행/채점 아님, gate 소관·불변).
    check_cmds(유닛 gate 체크 명령)를 알면 그 러너(pytest/unittest discover)를 미러해 #140의 exit-5
    (스모크 통과인데 gate가 테스트를 못 찾던 불일치)를 빌더-측서 잡는다. 모르면 양쪽 디스커버리.
    best-effort: 툴 부재면 그 단계 skip(빌드 막지 않음 — 진짜 검증은 gate).
    WO#146: JS/node 스택이면 JS 분기로 라우팅(node --check/tsc/vitest 수집). Python 경로 무회귀.
    """
    workdir = Path(workdir)
    if _detect_stack(workdir, check_cmds) == "js":
        return _builder_smoke_js(workdir, check_cmds=check_cmds, timeout=timeout)
    # 1) 컴파일/문법 — py_compile은 *실행하지 않고* 파싱/바이트컴파일만(안전).
    for f in _py_files(workdir):
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            rel = os.path.relpath(str(f), str(workdir))
            return f"[smoke 컴파일 실패] {rel}:\n{e}"[:2000]
        except (OSError, ValueError):
            continue
    # 테스트 파일이 없으면 디스커버리 검사 불필요(컴파일만으로 구조적 OK).
    if not any("test" in f.name for f in _py_files(workdir)):
        return None
    # 2) gate 디스커버리 컨벤션 미러(collect/discover — *발견만*, 실행/채점 아님).
    do_pytest, ut_spec = _smoke_conventions(workdir, check_cmds)
    k_expr = _parse_pytest_k(check_cmds or [])  # WO#144 wart#2: gate의 pytest -k 키워드 미러
    if do_pytest:
        err = _smoke_pytest_collect(workdir, timeout, k_expr=k_expr)
        if err:
            return err
    if ut_spec is not None:
        err = _smoke_unittest_discover(workdir, ut_spec[0], ut_spec[1], timeout)
        if err:
            return err
    return None


def _call_verify(verify, workdir: Path, check_cmds: list[str]):
    """verify 콜백 호출(back-compat): check_cmds kwarg를 받으면 넘기고(builder_smoke), 아니면
    workdir만(테스트 모킹 `lambda wd: ...`). 시그니처로 분기 — 1-arg 콜백 무회귀.
    """
    try:
        if "check_cmds" in inspect.signature(verify).parameters:
            return verify(workdir, check_cmds=check_cmds)
    except (ValueError, TypeError):
        pass
    return verify(workdir)


# ──────────────────────────── 빌더-측 정밀 자기-테스트 (WO#144) ────────────────────────────
#
# #143 진단: Qwen3.6는 정답성 테스트까지 *도달*하나(7B보다 멀리) u1 미수렴 — gate의 맨 exit-1
# (어느 assertion이 깨졌는지 detail 無)만 보고 빌더가 *풀-재생성*→발산. 해법: 스모크(findability)
# 통과 *후*, 빌더가 *자기 유닛 테스트를 실행*(TDD 자기검사)해 실패 시 *정확한 detail*(테스트명·
# assertion·traceback)을 다음 턴에 주입 → *타겟 수정*(전체 재생성 아님) → 자기 테스트 green까지 반복.
#
# 적대 분리(sacred·최우선): 빌더가 *자기가 쓴* 유닛 테스트를 green으로 모는 건 빌더-측 TDD
# 자기검사다. **독립 적대 gate는 불변**: 행동 run-judge(유닛 테스트 *너머* 행동 검증)·hollow-테스트
# 탐지(#98 시나리오 계약)·통합 판정이 *진짜 바*. 빌더 자기-테스트 green = 필요조건이지 *충분조건
# 아님*(gate가 hollow green을 잡는다). judge/run-judge/gate/critic 코드·경로엔 일절 닿지 않는다
# (import·호출 0). 빌트코드 실행은 스모크와 동일 오프라인 posture(_smoke_run). ALLOWED_SANDBOXES 불변.


def _selftest_cmd(check_cmds: list[str] | None) -> list[str] | None:
    """check_cmds에서 *테스트 실행* 명령(pytest/unittest)을 골라 venv `python -m` argv로 정규화한다.

    gate가 실제로 돌릴 체크 명령(예: "python -m pytest -k board_rules", "pytest -q",
    "python -m unittest discover -s tests")을 그대로 미러하되 인터프리터는 현재 venv로 고정한다
    (cwd가 sys.path에 들어가 빌더 워크트리 모듈이 import된다). 테스트 명령이 없으면 None(skip).
    """
    for cmd in check_cmds or []:
        low = cmd.lower()
        if "pytest" not in low and "unittest" not in low:
            continue
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        runner = "pytest" if "pytest" in low else "unittest"
        idx = next((j for j, t in enumerate(toks) if runner in t.lower()), None)
        if idx is None:
            continue
        return [sys.executable, "-m", runner, *toks[idx + 1:]]
    return None


def _selftest_js_cmd(check_cmds: list[str] | None) -> list[str] | None:
    """check_cmds에서 *JS 테스트 실행* 명령(vitest/jest/npm test)을 골라 실행 가능 argv로 정규화(WO#146).

    gate가 돌릴 러너를 미러하되: bare `vitest`/`jest`는 `npx --no-install` 접두(워크트리 .bin 사용·
    오프라인), vitest는 one-shot `run` 보장(watch-mode hang 회피; 타임아웃도 백스톱). `npm test`류는
    그대로(npm on PATH). JS 러너 명령이 없으면 None(skip — findability는 스모크 소관).
    """
    for cmd in check_cmds or []:
        low = cmd.lower()
        if not any(m in low for m in _JS_TEST_RUNNER_MARKERS):
            continue
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        if not toks:
            continue
        if "vitest" in low:
            vi = next((j for j, t in enumerate(toks) if "vitest" in t.lower()), None)
            if vi is not None and "run" not in toks[vi:] and "list" not in toks[vi:]:
                toks = toks[: vi + 1] + ["run"] + toks[vi + 1:]  # one-shot 강제
            if toks[0].lower() == "vitest":
                toks = ["npx", "--no-install", *toks]
        elif toks[0].lower() == "jest":
            toks = ["npx", "--no-install", *toks]
        return toks
    return None


def _builder_selftest_js(
    workdir: Path, *, check_cmds: list[str] | None = None, timeout: float = 60.0
) -> str | None:
    """빌더-측 정밀 자기-테스트(JS) — builder_selftest의 JS 분기(Python 경로와 동형, WO#146).

    빌더가 *자기 vitest/jest 테스트를 실행*해 실패 시 정확한 detail(실패 테스트명·expected vs
    received·stack)을 반환 → 호출부가 다음 턴에 주입 → *타겟 수정* → green까지 반복. 테스트 파일
    있을 때만(없으면 None=skip; findability는 스모크 소관). 툴 부재(127)/타임아웃(124) → None.
    **적대 분리**: 빌더가 *자기 테스트*를 green으로 모는 TDD 자기검사 — judge/gate 무접촉. 독립
    적대 gate(행동 run-judge·hollow·통합)가 진짜 바(불변): green = 필요조건이지 충분조건 아님.
    """
    if not _js_test_files(workdir):
        return None  # 테스트 파일 없음 → 실행할 자기-테스트 없음
    cmd = _selftest_js_cmd(check_cmds)
    if cmd is None:
        return None  # JS 테스트 명령 모름 → skip(findability는 스모크 안전망)
    rc, out = _smoke_run(cmd, workdir, timeout)
    if rc in (127, 124):
        return None  # 툴 부재/타임아웃 → skip(빌드 막지 않음 — 진짜 검증은 gate)
    if rc != 0:  # vitest/jest 비0 = 테스트 실패(테스트 파일 존재 → "no tests" 아님) → detail 피드백
        body = out.strip()[-3000:]
        return (
            "[빌더 자기-테스트 실패] " + " ".join(cmd) + f" (exit {rc}). 아래 *정확한 실패 detail*"
            "(실패 테스트명·expected vs received·stack)을 보고 *실패 지점만 타겟 수정*하라"
            "(전체 재생성 말고 — 통과 중인 부분은 유지):\n" + body
        )
    return None  # rc 0 = green


def builder_selftest(
    workdir: Path, *, check_cmds: list[str] | None = None, timeout: float = 60.0
) -> str | None:
    """빌더-측 *정밀 자기-테스트*(WO#144): 빌더가 *자기 유닛 테스트를 실행*해 실패 detail을 받는다.

    스모크(findability) 통과 *후* 호출. gate 체크 명령(check_cmds)을 venv `python -m`으로 정규화해
    빌더 워크트리서 실행:
      - 통과(exit 0) → None(green).
      - 실패(exit 1)/collection 에러(exit 2) → *정확한 실패 detail*(테스트명·assertion·expected vs
        actual·traceback tail) 반환 → 호출부가 다음 bounded 턴에 주입 → 빌더가 *타겟 수정*.
      - 테스트/매칭 0(exit 5)·툴 부재(127)·타임아웃(124) → None(best-effort; findability는 스모크 소관).
    **적대 분리(sacred)**: 빌더가 *자기가 쓴* 유닛 테스트를 green으로 모는 TDD 자기검사 — judge/gate
    *아님*(import·호출 0). 독립 적대 gate(행동 run-judge·hollow 탐지·통합)가 *진짜 바*로 불변:
    빌더 자기-테스트 green = 필요조건이지 충분조건 아님(gate가 hollow green을 잡는다). 빌트코드 실행은
    스모크와 동일 오프라인 posture(_smoke_run, 네트워크 미부여). ALLOWED_SANDBOXES 불변.
    WO#146: JS/node 스택이면 JS 분기로 라우팅(vitest/jest 실행→detail). Python 경로 무회귀.
    """
    workdir = Path(workdir)
    if _detect_stack(workdir, check_cmds) == "js":
        return _builder_selftest_js(workdir, check_cmds=check_cmds, timeout=timeout)
    if not any("test" in f.name for f in _py_files(workdir)):
        return None  # 테스트 파일 없음 → 실행할 자기-테스트 없음
    cmd = _selftest_cmd(check_cmds)
    if cmd is None:
        return None  # 실행할 테스트 명령 모름 → skip(스모크 findability가 안전망)
    rc, out = _smoke_run(cmd, workdir, timeout)
    low = out.lower()
    if rc in (127, 124) or "no module named pytest" in low or "no module named \"pytest\"" in low:
        return None  # 툴 부재/타임아웃 → skip(빌드 막지 않음 — 진짜 검증은 gate)
    if rc in (1, 2):  # 1=테스트 실패, 2=collection 에러 → *정확한 실패 detail*을 빌더에 피드백.
        body = out.strip()[-3000:]
        return (
            "[빌더 자기-테스트 실패] " + " ".join(cmd) + f" (exit {rc}). 아래 *정확한 실패 detail*"
            "(실패 테스트명·assertion·expected vs actual·traceback)을 보고 *실패 지점만 타겟 수정*하라"
            "(전체 재생성 말고 — 통과 중인 부분은 유지):\n" + body
        )
    return None  # rc 0 = green / rc 5 = 테스트·매칭 0(findability는 스모크 소관)


# ──────────────────────────── LocalAgentExecutor ────────────────────────────


class LocalAgentExecutor:
    """work order를 *약한 로컬 모델*(OpenAI 호환 엔드포인트)에 줘 workdir에서 구현시키는 Executor.

    CodexExecutor와 **동일 인터페이스**(`run(order) -> str` + `last_usage`)라 run_loop에 그대로 끼운다.

    endpoint:    OpenAI 호환 베이스 URL(예: "http://100.70.109.50:8089/v1"). post_chat가
                 "/chat/completions"를 붙인다.
    model:       서빙 모델명(예: "qwen2.5-coder:7b"). 엔드포인트가 alias면 그대로 통과.
    workdir:     빌더 작업 루트. 편집은 이 폴더 안으로 *가둬진다*(safe_target).
    max_turns:   에이전틱 루프 상한(기본 3 — 28 t/s 경제성, #134 단발 선호). done 신호나
                 더 낼 편집이 없으면 조기 종료.
    timeout:     한 번의 모델 호출 타임아웃(초).
    temperature/max_tokens: 생성 파라미터(코드라 낮은 temp 기본).
    verify:      빌더-측 *구조적 스모크* 콜백(예: `builder_smoke` = 컴파일 + *gate 디스커버리 컨벤션
                 미러*(pytest collect / unittest discover findability), WO#139/#141). 편집 적용 후
                 호출해 에러를 받으면 *그 정확한 에러를 다음 턴에 주입*해 self-fix(턴 상한 내 반복).
                 통과(None)면 구조적 자기-정합·gate-findable 달성 → *즉시 반환*(속도). None이면 미사용.
                 **판정 아님** — 정답/완결은 독립 적대 gate가 한다(불변). 주입형이라 테스트서 모킹 가능.
                 콜백이 `check_cmds` kwarg를 받으면 유닛 gate 체크 명령이 주입된다(1-arg 콜백 back-compat).
    heartbeat/transcript: 라이브 텔레메트리 sink(duck-typed). None이면 off(무회귀).
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        workdir: str | Path = ".",
        max_turns: int = 3,
        timeout: float = 180.0,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        verify: Callable[[Path], str | None] | None = None,
        selftest: Callable[[Path], str | None] | None = None,
        heartbeat=None,
        transcript=None,
    ):
        if max_turns < 1:
            raise ValueError(f"max_turns는 1 이상이어야 함: {max_turns}")
        self.endpoint = endpoint
        self.model = model
        self.workdir = Path(workdir)
        self.max_turns = max_turns
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.verify = verify
        # WO#144: 스모크(findability) 통과 *후* 돌리는 빌더-측 *정밀 자기-테스트* 콜백
        # (예: `builder_selftest` = 빌더가 자기 유닛 테스트를 실행→실패 detail 주입→타겟 수정→green).
        # **판정 아님** — 빌더 자기-테스트 green = 필요조건이지 충분조건 아님(독립 적대 gate 불변).
        # verify가 set일 때만 동작(스모크 통과 *후*). None이면 미사용(#141 즉시-반환 무회귀).
        self.selftest = selftest
        self.heartbeat = heartbeat
        self.transcript = transcript
        # 빌드 전체(여러 턴)의 누적 token usage(WO#33 동형). 미노출이면 None(날조 금지).
        self.last_usage: Usage | None = None
        # 직전 run의 적용 파일/턴 수(보고/테스트용).
        self.last_applied: list[str] = []
        self.last_turns: int = 0
        # WO#139 빌더-측 스모크 상태(보고/테스트용).
        self.last_smoke_error: str | None = None
        self.last_smoke_passed: bool = False
        self.smoke_feedback_count: int = 0
        # WO#144 빌더-측 정밀 자기-테스트 상태(보고/테스트용).
        self.last_selftest_error: str | None = None
        self.last_selftest_passed: bool = False
        self.selftest_feedback_count: int = 0
        # WO#141: 직전 run의 벽시계(초) — 빌드 요약에 표면화(속도 튜닝 가시성).
        self.last_elapsed_s: float = 0.0

    # ── Executor 인터페이스 ────────────────────────────────────────────
    def run(self, order: NextOrder) -> str:
        """work order를 경계 있는 에이전틱 루프로 구현하고 결과 요약을 반환한다(gate가 독립 판정)."""
        self.last_usage = None
        self.last_applied = []
        self.last_turns = 0
        self.last_smoke_error = None
        self.last_smoke_passed = False
        self.smoke_feedback_count = 0
        self.last_selftest_error = None
        self.last_selftest_passed = False
        self.selftest_feedback_count = 0
        self.last_elapsed_s = 0.0
        _t0 = time.monotonic()

        user_msg = _format_order(order)
        ctx = workdir_context(self.workdir, order)
        if ctx:
            user_msg += "\n\n" + ctx
        messages = [
            {"role": "system", "content": _BUILDER_SYSTEM},
            {"role": "user", "content": user_msg},
        ]

        applied_all: list[str] = []
        agg_in: int | None = None
        agg_out: int | None = None
        last_text = ""

        for turn in range(self.max_turns):
            text, usage = self._chat(messages)
            self.last_turns = turn + 1
            last_text = text
            agg_in, agg_out = _accumulate_tokens(agg_in, agg_out, usage)
            self._set_usage(agg_in, agg_out)

            edits = parse_edits(text)
            applied = apply_edits(self.workdir, edits)
            for p in applied:
                if p not in applied_all:
                    applied_all.append(p)
            self.last_applied = list(applied_all)
            done = is_done(text)

            # 빌더-측 스모크(WO#139/#141): 편집 적용 후 *구조적 자기검사*(컴파일 + gate 디스커버리
            # 컨벤션 미러). 실패하면 정확한 에러를 다음 턴에 주입해 self-fix(턴 상한 내 반복).
            # 통과(구조적 자기-정합·gate-findable)면 *즉시 반환*(WO#141 속도: 불필요 턴 0).
            # 정답/완결 판정은 이후 독립 적대 gate가 한다(불변).
            if self.verify is not None and applied:
                err = self._safe_verify(order)
                if err:
                    self.last_smoke_error = err
                    self.smoke_feedback_count += 1
                    if turn < self.max_turns - 1:
                        messages.append({"role": "assistant", "content": text})
                        messages.append({
                            "role": "user",
                            "content": (
                                "빌더 스모크(컴파일/디스커버리) 실패. 아래 *정확한 에러*를 보고 같은 "
                                f"편집 프로토콜(path= 펜스 블록)로 고쳐라(파일 전체 재출력):\n{err}\n"
                                f"고친 뒤 {DONE_MARKER}."
                            ),
                        })
                        continue
                    # 턴 소진 — 스모크 미통과인 채 반환(이후 독립 gate가 판정).
                    break
                # 구조적(findability) 통과.
                self.last_smoke_passed = True
                # WO#144: 스모크 통과 *후* 빌더-측 *정밀 자기-테스트*(자기 유닛 테스트 실행). 실패 시
                # *정확한 detail*(테스트명·assertion·traceback)을 다음 턴에 주입 → *타겟 수정* → green까지
                # 반복. green/off면 반환(독립 적대 gate가 *행동·hollow·통합*을 진짜 바로 판정 — 불변).
                if self.selftest is not None:
                    sterr = self._safe_selftest(order)
                    if sterr:
                        self.last_selftest_error = sterr
                        self.selftest_feedback_count += 1
                        if turn < self.max_turns - 1:
                            messages.append({"role": "assistant", "content": text})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "빌더 자기-테스트 실패. 아래 *정확한 실패 detail*(테스트명·assertion·"
                                    "expected vs actual·traceback)을 보고 *실패 지점만 타겟 수정*하라"
                                    f"(전체 재생성 말고 — 통과 중인 부분은 유지):\n{sterr}\n고친 뒤 {DONE_MARKER}."
                                ),
                            })
                            continue
                        # 턴 소진 — 자기-테스트 미green인 채 반환(이후 독립 gate가 판정).
                        break
                    self.last_selftest_passed = True
                # 스모크(+자기-테스트) 통과 → 반환(gate가 정답성·행동·통합 독립 판정).
                break

            if done or not edits:
                break

            # 아직 진행 중 — 계속 진행(같은 프로토콜) 또는 완료 신호 유도.
            if turn < self.max_turns - 1:
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"계속. 남은 파일이 있으면 같은 프로토콜로 내고, 끝났으면 {DONE_MARKER}만 출력하라.",
                })

        self.last_elapsed_s = time.monotonic() - _t0
        if not applied_all:
            raise LocalAgentError(
                "로컬 빌더가 적용 가능한 편집을 내지 못함(경로-태깅 펜스 블록 0건)", last_text
            )
        return self._summary(applied_all, last_text)

    # ── 한 턴 모델 호출(텔레메트리 감싸기) ──────────────────────────────
    def _chat(self, messages: list[dict]) -> tuple[str, Usage | None]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        hb_handle = self._hb_start()
        try:
            resp = post_chat(self.endpoint, payload, self.timeout)
        finally:
            self._hb_finish(hb_handle)
        text = _message_text(resp)
        usage = _usage_from_resp(resp, self.model)
        self._tr_output(text)
        return text, usage

    # ── 내부 헬퍼 ──────────────────────────────────────────────────────
    def _set_usage(self, agg_in: int | None, agg_out: int | None) -> None:
        if agg_in is None and agg_out is None:
            self.last_usage = None
        else:
            self.last_usage = Usage(input_tokens=agg_in, output_tokens=agg_out, model=self.model)

    def _safe_verify(self, order: NextOrder | None = None) -> str | None:
        """verify(빌더 스모크) 콜백을 best-effort로 부른다(예외는 흡수 — 검증 실패가 빌드를 안 죽임).

        유닛 gate 체크 명령(order.local_checks)을 check_cmds로 넘겨 스모크가 *gate 디스커버리
        컨벤션*을 미러하게 한다(WO#141). _call_verify가 1-arg 콜백(테스트 모킹)엔 back-compat.
        """
        if self.verify is None:
            return None
        cmds = [c.cmd for c in order.local_checks if c.cmd] if order is not None else []
        try:
            return _call_verify(self.verify, self.workdir, cmds)
        except Exception:  # noqa: BLE001 — 주입 체크의 크래시가 빌더를 죽이지 않는다
            return None

    def _safe_selftest(self, order: NextOrder | None = None) -> str | None:
        """selftest(빌더 정밀 자기-테스트) 콜백을 best-effort로 부른다(예외 흡수 — 검증이 빌드 안 죽임).

        유닛 gate 체크 명령(order.local_checks)을 check_cmds로 넘겨 *gate 러너를 미러*해 빌더가
        자기 유닛 테스트를 실행한다(WO#144). _call_verify가 1-arg 콜백(테스트 모킹)엔 back-compat.
        **적대 분리**: gate/run-judge를 부르지 않는다 — 빌더 워크트리서 자기 테스트만 돌린다.
        """
        if self.selftest is None:
            return None
        cmds = [c.cmd for c in order.local_checks if c.cmd] if order is not None else []
        try:
            return _call_verify(self.selftest, self.workdir, cmds)
        except Exception:  # noqa: BLE001 — 자기-테스트 크래시가 빌더를 죽이지 않는다
            return None

    def _summary(self, applied: list[str], last_text: str) -> str:
        tail = (last_text or "").strip()
        if len(tail) > 600:
            tail = "…" + tail[-600:]
        files = ", ".join(applied) if applied else "(없음)"
        if self.last_smoke_passed:
            smoke = "스모크 pass(구조적 자기-정합)"
        elif self.smoke_feedback_count:
            smoke = f"스모크 미통과({self.smoke_feedback_count}회 피드백 후 턴 소진)"
        else:
            smoke = "스모크 off"
        segs = [f"턴 {self.last_turns}/{self.max_turns}", smoke]
        # WO#144: 자기-테스트가 *동작했을 때만* 상태 표면화(off 노이즈 회피).
        if self.last_selftest_passed:
            segs.append("자기-테스트 green")
        elif self.selftest_feedback_count:
            segs.append(f"자기-테스트 미green({self.selftest_feedback_count}회 피드백)")
        segs.append(f"{self.last_elapsed_s:.0f}s")
        return (
            f"로컬 빌더({self.model}) 완료 — 적용 파일 {len(applied)}개: {files}\n"
            + " · ".join(segs) + "\n"
            + f"--- 모델 최종 메시지(tail) ---\n{tail}"
        )

    # 텔레메트리(duck-typed, best-effort — 절대 run을 죽이지 않는다; #55/#67 패턴).
    def _hb_start(self):
        if self.heartbeat is None:
            return None
        try:
            kind, unit = self.heartbeat.get_context()
        except Exception:  # noqa: BLE001
            kind, unit = None, None
        try:
            return self.heartbeat.start(kind or "빌드", unit, idle_timeout=None)
        except Exception:  # noqa: BLE001
            return None

    def _hb_finish(self, handle) -> None:
        if self.heartbeat is None or handle is None:
            return
        try:
            self.heartbeat.finish(handle)
        except Exception:  # noqa: BLE001
            pass

    def _tr_output(self, text: str) -> None:
        if self.transcript is None or not text:
            return
        try:
            tr_id = self.transcript.start(kind="빌드", unit=None, input_text="")
            self.transcript.output(tr_id, text)
            self.transcript.finish(tr_id, "done")
        except Exception:  # noqa: BLE001
            pass


def _accumulate_tokens(
    agg_in: int | None, agg_out: int | None, usage: Usage | None
) -> tuple[int | None, int | None]:
    """여러 턴의 token usage를 누적한다(알려진 값만 더함 — 미상은 보존). 순수 함수(테스트 용이)."""
    if usage is None:
        return agg_in, agg_out
    if usage.input_tokens is not None:
        agg_in = (agg_in or 0) + usage.input_tokens
    if usage.output_tokens is not None:
        agg_out = (agg_out or 0) + usage.output_tokens
    return agg_in, agg_out
