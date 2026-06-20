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

import json
import os
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
    verify:      선택적 경량 체크 `(workdir: Path) -> str | None`. 편집 적용 후 호출해 에러
                 문자열을 받으면 *한 번* 모델에 피드백해 재시도시킨다(에러-피드백 1턴). None이면
                 미사용(gate가 진짜 검증). **빌트 코드 실행 정책은 gate 소관** — 여기선 주입만.
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
        self.heartbeat = heartbeat
        self.transcript = transcript
        # 빌드 전체(여러 턴)의 누적 token usage(WO#33 동형). 미노출이면 None(날조 금지).
        self.last_usage: Usage | None = None
        # 직전 run의 적용 파일/턴 수(보고/테스트용).
        self.last_applied: list[str] = []
        self.last_turns: int = 0

    # ── Executor 인터페이스 ────────────────────────────────────────────
    def run(self, order: NextOrder) -> str:
        """work order를 경계 있는 에이전틱 루프로 구현하고 결과 요약을 반환한다(gate가 독립 판정)."""
        self.last_usage = None
        self.last_applied = []
        self.last_turns = 0

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
        feedback_used = False
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

            # 선택적 경량 체크 → 실패 시 에러-피드백 1턴(딱 한 번).
            if self.verify is not None and applied and not feedback_used:
                err = self._safe_verify()
                if err:
                    feedback_used = True
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "적용 후 체크가 실패했다. 같은 편집 프로토콜(path= 펜스 블록)로 고쳐라:\n"
                            f"{err}\n끝나면 {DONE_MARKER}."
                        ),
                    })
                    continue

            if done or not edits:
                break

            # 아직 진행 중 — 계속 진행(같은 프로토콜) 또는 완료 신호 유도.
            if turn < self.max_turns - 1:
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"계속. 남은 파일이 있으면 같은 프로토콜로 내고, 끝났으면 {DONE_MARKER}만 출력하라.",
                })

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

    def _safe_verify(self) -> str | None:
        """verify 콜백을 best-effort로 부른다(예외는 흡수 — 검증 실패가 빌드를 죽이지 않는다)."""
        if self.verify is None:
            return None
        try:
            return self.verify(self.workdir)
        except Exception:  # noqa: BLE001 — 주입 체크의 크래시가 빌더를 죽이지 않는다
            return None

    def _summary(self, applied: list[str], last_text: str) -> str:
        tail = (last_text or "").strip()
        if len(tail) > 600:
            tail = "…" + tail[-600:]
        files = ", ".join(applied) if applied else "(없음)"
        return (
            f"로컬 빌더({self.model}) 완료 — 적용 파일 {len(applied)}개: {files}\n"
            f"턴 {self.last_turns}/{self.max_turns}\n"
            f"--- 모델 최종 메시지(tail) ---\n{tail}"
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
