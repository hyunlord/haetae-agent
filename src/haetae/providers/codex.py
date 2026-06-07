"""CodexClient — 첫 실제 provider. 로컬 `codex exec`를 subprocess로 호출.

Codex는 chat 엔드포인트가 아니라 agentic 코딩 에이전트라, 순수 생성만 원하는
우리는 격리해서 실행한다:
  - 격리된 cwd(기본: 임시 디렉토리)에서 실행 → 실제 repo를 건드리지 않음
  - `-s read-only` 샌드박스 → 모델이 만든 셸 명령의 쓰기 차단
  - `--ephemeral` → 세션 파일을 디스크에 남기지 않음
  - `--skip-git-repo-check` → 임시 디렉토리(=non-git)에서도 실행 허용
  - `-o <file>` → 최종 메시지만 파일로 받아 stdout 이벤트 노이즈 파싱 회피
  - `-m <model>` → 지정 시에만. 미지정이면 codex 설정 기본을 따름(하드코딩 금지)

새 의존성 없음(stdlib subprocess만). LLMClient는 구조적 프로토콜이라 상속하지 않는다.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from haetae.metering import Usage

CODEX_BIN = "codex"

# 허용 sandbox 화이트리스트. danger-full-access는 코드 레벨에서 차단한다
# (자율 executor가 LLM이 만든 명령을 쓰기 권한으로 실행하므로 — WO#13 SAFETY).
ALLOWED_SANDBOXES = ("read-only", "workspace-write")


class CodexError(RuntimeError):
    """codex exec 실행 실패. 디버깅용으로 stdout/stderr 일부를 동봉한다."""

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr

        def _tail(s: str, n: int = 800) -> str:
            s = (s or "").strip()
            return s if len(s) <= n else "…" + s[-n:]

        detail = ""
        if stdout:
            detail += f"\n--- stdout(tail) ---\n{_tail(stdout)}"
        if stderr:
            detail += f"\n--- stderr(tail) ---\n{_tail(stderr)}"
        super().__init__(message + detail)


def _parse_usage(stdout: str, model: str | None) -> Usage | None:
    """codex `--json` stdout(JSONL)에서 마지막 `turn.completed.usage`를 파싱한다.

    *읽기 전용* 계측 — sandbox/실행 권한과 무관하다(WO#33 안전 불변). usage 라인이
    없거나 JSON이 깨지면 None(무크래시). 여러 턴이면 마지막 usage를 채택한다.
    codex usage는 input_tokens(=cached 포함)/output_tokens를 노출한다.
    """
    last: dict | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and ev.get("type") == "turn.completed":
            u = ev.get("usage")
            if isinstance(u, dict):
                last = u
    if last is None:
        return None
    inp = last.get("input_tokens")
    out = last.get("output_tokens")
    return Usage(
        input_tokens=inp if isinstance(inp, int) else None,
        output_tokens=out if isinstance(out, int) else None,
        model=model,
    )


def exec_codex(
    prompt: str,
    *,
    sandbox: str,
    cwd: str | Path | None,
    model: str | None = None,
    timeout: float | None = None,
    ephemeral: bool = True,
) -> str:
    """`codex exec`를 한 턴 돌려 `-o` 최종 메시지 파일을 읽어 반환하는 공유 헬퍼.

    후방호환 표면: text(str)만 반환한다. usage까지 필요한 호출부(CodexClient/
    CodexExecutor)는 `exec_codex_with_usage`를 쓴다.
    """
    text, _usage = exec_codex_with_usage(
        prompt, sandbox=sandbox, cwd=cwd, model=model, timeout=timeout, ephemeral=ephemeral
    )
    return text


def exec_codex_with_usage(
    prompt: str,
    *,
    sandbox: str,
    cwd: str | Path | None,
    model: str | None = None,
    timeout: float | None = None,
    ephemeral: bool = True,
) -> tuple[str, Usage | None]:
    """`codex exec`를 한 턴 돌려 (최종 메시지, token usage)를 반환하는 저수준 헬퍼.

    CodexClient(읽기전용 생성)와 CodexExecutor(쓰기 자율 실행)가 공유한다.
    차이는 sandbox/cwd 두 파라미터뿐 — 나머지 플러밍(stdin 프롬프트, `-o` 캡처,
    `-m` 조건부, `--skip-git-repo-check`)은 동일.

    usage(WO#33): `--json`으로 이벤트 JSONL을 stdout에 받아 `turn.completed.usage`를
    *읽기만* 한다. 최종 메시지는 여전히 `-o` 파일에서 읽으므로 반환 텍스트는 불변.
    파싱 실패/미노출이면 usage=None(날조 금지). **sandbox 권한은 절대 불변.**

    sandbox: ALLOWED_SANDBOXES 중 하나. danger-full-access는 ValueError로 거부.
    cwd:     codex 작업 루트(`-C`). None이면 임시 디렉토리(완전 격리).
             CodexExecutor는 여기에 `--workdir`를 넘겨 실행 범위를 그 폴더로 한정한다.
    """
    if sandbox not in ALLOWED_SANDBOXES:
        raise ValueError(
            f"허용되지 않은 sandbox: {sandbox!r} "
            f"(허용: {ALLOWED_SANDBOXES}; danger-full-access 금지)"
        )
    with tempfile.TemporaryDirectory(prefix="haetae-codex-") as tmp:
        run_cwd = cwd or tmp
        # 최종 메시지 캡처 파일은 tmp에 둬서 작업 디렉토리(cwd)를 오염시키지 않는다.
        out_path = Path(tmp) / "last_message.txt"
        # `--json`: 이벤트(usage 포함)를 stdout JSONL로. 최종 메시지는 여전히 `-o` 파일.
        cmd = [CODEX_BIN, "exec", "--skip-git-repo-check", "--json"]
        if ephemeral:
            cmd.append("--ephemeral")
        cmd += ["-s", sandbox, "-C", str(run_cwd), "-o", str(out_path)]
        if model:
            cmd += ["-m", model]
        cmd.append("-")  # 프롬프트는 stdin으로

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise CodexError(
                f"codex CLI를 찾을 수 없음 ('{CODEX_BIN}' on PATH?)"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise CodexError(
                f"codex exec 타임아웃 ({timeout}s 초과)",
                e.stdout or "",
                e.stderr or "",
            ) from e

        if proc.returncode != 0:
            raise CodexError(
                f"codex exec 비정상 종료 (exit {proc.returncode})",
                proc.stdout,
                proc.stderr,
            )

        text = ""
        if out_path.exists():
            text = out_path.read_text(encoding="utf-8").strip()
        if not text:
            raise CodexError(
                "codex exec가 빈 최종 메시지를 반환함", proc.stdout, proc.stderr
            )
        # usage 파싱은 best-effort 부가물 — 실패해도 text 반환은 영향 없음.
        usage = None
        try:
            usage = _parse_usage(proc.stdout, model)
        except Exception:  # noqa: BLE001 — 계측은 run을 죽이지 않는다
            usage = None
        return text, usage


class CodexClient:
    """`codex exec`를 한 턴 돌려 최종 텍스트를 반환하는 LLMClient.

    model:   override할 모델. None이면 codex 설정 기본.
    workdir: codex의 작업 루트. None이면 호출마다 임시 디렉토리(완전 격리).
    """

    def __init__(self, model: str | None = None, workdir: str | None = None, **default_opts):
        self.model = model
        self.workdir = workdir
        self.default_opts = default_opts  # 향후 플래그 확장용으로 보존
        # 직전 호출의 token usage(WO#33). 미노출/파싱 실패면 None(날조 금지).
        self.last_usage: Usage | None = None

    # ── LLMClient 인터페이스 ──────────────────────────────────────────
    def complete(self, system: str, user: str, **opts) -> str:
        prompt = self._merge_prompt(system, user)
        return self._run(prompt)

    # ── 합치기 ────────────────────────────────────────────────────────
    @staticmethod
    def _merge_prompt(system: str, user: str) -> str:
        """codex는 단일 프롬프트만 받으므로 system을 preamble로 앞에 붙인다."""
        system = (system or "").strip()
        user = (user or "").strip()
        if not system:
            return user
        return f"{system}\n\n========\n\n{user}"

    # ── 테스트 seam: 실제 subprocess 실행은 공유 헬퍼로 격리 ────────────
    def _run(self, prompt: str) -> str:
        # 생성(읽기전용) 용도 → read-only sandbox. workdir 미지정이면 헬퍼가 격리.
        text, usage = exec_codex_with_usage(
            prompt,
            sandbox="read-only",
            cwd=self.workdir,
            model=self.model,
        )
        self.last_usage = usage  # 읽기만 — sandbox 권한 불변
        return text
