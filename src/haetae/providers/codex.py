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
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from haetae.metering import Usage

# provider가 선언하는 실행 폼 옵션 디스크립터(WO#45) — 실행 로직과 분리된 *메타데이터*.
# 엔진-free 리프에 정의하고 여기서 re-export해 "codex provider가 자기 launch 옵션을
# 선언한다"는 표면을 제공한다(additive). 대시보드는 이 리프를 직접 import한다(격리 유지).
from haetae.providers.launch_options import (  # noqa: F401 — 공개 re-export
    LaunchOption,
    codex_launch_options as launch_options,
)

CODEX_BIN = "codex"

# 허용 sandbox 화이트리스트. danger-full-access는 코드 레벨에서 차단한다
# (자율 executor가 LLM이 만든 명령을 쓰기 권한으로 실행하므로 — WO#13 SAFETY).
ALLOWED_SANDBOXES = ("read-only", "workspace-write")

# 허용 추론 강도 화이트리스트(WO#38). codex의 `model_reasoning_effort` config 값과 일치.
# **sandbox 권한과 무관** — 이 화이트리스트는 ALLOWED_SANDBOXES를 절대 건드리지 않는다.
# codex exec엔 전용 플래그(-e 등)가 없어 `-c model_reasoning_effort=<effort>`로 넘긴다.
ALLOWED_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


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


class CodexStalled(RuntimeError):
    """codex 호출이 *무진행(idle)* 으로 멈춤 — idle_timeout초 동안 새 `--json` 이벤트가
    하나도 안 옴(무음). hung 프로세스(+자식)를 정리한 뒤 던진다.

    **총 시간 cap이 아니다**: 진행 중인(이벤트를 계속 뱉는) 긴 호출은 절대 죽이지 않는다 —
    "마지막 이벤트 이후 침묵"만 잰다. 신호는 *진행*이지 경과시간이 아니다.

    **CodexError의 하위가 아니다(의도적)**: CodexExecutor의 `except CodexError` 래핑에
    걸리지 않고 그대로 전파돼, 호출부가 멈춤을 타입으로 라우팅한다(필수=재시도/escalate,
    best-effort=degrade). stdout/stderr 일부를 디버깅용으로 동봉한다.
    """

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


def _terminate_process(proc: subprocess.Popen) -> None:
    """멈춘 codex 프로세스와 *그 자식들*까지 정리한다(좀비 금지). best-effort.

    Popen을 start_new_session=True로 띄워 새 세션/프로세스그룹 리더로 만들었으므로
    killpg(SIGKILL)로 codex가 띄운 하위 셸/모델 프로세스까지 한 번에 보낸다. 프로세스가
    이미 죽었거나 그룹 조회가 실패하면 단일 kill로 폴백하고, 마지막에 wait로 거둔다.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)  # 거둬서 좀비 방지
    except Exception:  # noqa: BLE001 — 정리는 best-effort, 2차 크래시 금지
        pass


def _event_summary(line: str) -> str | None:
    """codex `--json` 이벤트 한 줄에서 사람이 읽을 *최근 액션* 요약을 best-effort로 뽑는다(WO#55).

    editing X / running <cmd> / reasoning 같은 한 줄. 못 뽑으면 이벤트 *종류*로 폴백,
    JSON이 깨지면 None(호출부가 직전 요약 유지). 순수 텔레메트리 — 절대 raise하지 않는다.
    """
    s = (line or "").strip()
    if not s.startswith("{"):
        return None
    try:
        ev = json.loads(s)
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    item = ev.get("item") if isinstance(ev.get("item"), dict) else None

    def _short(x: str, n: int = 60) -> str:
        x = " ".join((x or "").split())
        return x if len(x) <= n else x[: n - 1] + "…"

    if item is not None:
        it = item.get("type")
        if it == "command_execution":
            return f"running: {_short(item.get('command') or '')}"
        if it in ("file_change", "file_update", "patch", "edit"):
            # 경로 후보 키를 폭넓게 시도(코덱스 버전별 변종). 없으면 종류로.
            path = (
                item.get("path")
                or item.get("file")
                or item.get("filename")
                or item.get("target")
            )
            return f"editing: {_short(str(path))}" if path else "editing files"
        if it == "agent_message":
            return "reasoning/message"
        if it == "reasoning":
            return "reasoning"
        if it == "error":
            return f"error: {_short(item.get('message') or '')}"
        return str(it) if it else (str(t) if t else None)
    if t == "turn.completed":
        return "turn completed"
    if t == "turn.started":
        return "turn started"
    if t == "thread.started":
        return "thread started"
    if t == "turn.failed":
        return "turn failed"
    return str(t) if t else None


def _event_output_text(line: str) -> str | None:
    """codex `--json` 이벤트 한 줄에서 모델 *출력 텍스트*를 best-effort로 뽑는다(WO#67 트랜스크립트).

    '모델이 지금 뱉는 것' — reasoning/agent_message의 본문, command_execution의 명령,
    file_change의 경로 등을 한 조각으로. 완료된 item(`item.completed`)만 취해 start/completed
    중복을 피한다. 못 뽑으면 None(트랜스크립트에 안 더함). 순수 텔레메트리 — 절대 raise 안 함.
    """
    s = (line or "").strip()
    if not s.startswith("{"):
        return None
    try:
        ev = json.loads(s)
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict):
        return None
    if ev.get("type") != "item.completed":
        return None  # 완료된 item만(중복/부분 방지)
    item = ev.get("item") if isinstance(ev.get("item"), dict) else None
    if item is None:
        return None
    it = item.get("type")
    if it == "command_execution":
        cmd = item.get("command")
        return f"$ {cmd}" if cmd else None
    if it in ("file_change", "file_update", "patch", "edit"):
        path = (
            item.get("path") or item.get("file")
            or item.get("filename") or item.get("target")
        )
        return f"[edit] {path}" if path else None
    txt = item.get("text") or item.get("message")
    if isinstance(txt, str) and txt.strip():
        prefix = "" if it in ("agent_message", "assistant_message", None) else f"[{it}] "
        return prefix + txt.strip()
    return None


def _stream_codex(
    cmd: list[str],
    prompt: str,
    *,
    idle_timeout: float,
    max_duration: float | None,
    on_event: Callable[[str | None], None] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """`codex --json`을 Popen으로 띄워 stdout JSONL을 *줄 단위*로 읽으며 idle을 감시한다.

    토대(WO#54): subprocess.run(끝나고 캡처) 대신 Popen + readline 루프. codex --json은
    이벤트가 일어나는 대로 JSONL을 한 줄씩 흘리므로(검증됨: thread.started→turn.started→
    item.completed…→turn.completed) "마지막 줄 이후 idle_timeout초 침묵"을 멈춤 신호로 쓴다.

    반환: (returncode, stdout_text, stderr_text). stdout_text는 그때까지 받은 전체 JSONL이라
    usage 파싱(#33–34)이 스트리밍 경로에서도 동일하게 동작한다(부분 출력도 파싱 가능).
    멈추면 hung 프로세스(+자식)를 정리하고 CodexStalled를 던진다.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # 새 프로세스그룹 → 멈춤 시 자식까지 killpg
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    q: queue.Queue = queue.Queue()
    _EOF = object()

    def _pump_stdin() -> None:
        # 큰 프롬프트로 메인이 블록되지 않게 별도 스레드에서 쓰고 닫는다.
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def _read_stdout() -> None:
        try:
            for line in proc.stdout:  # 이벤트 도착마다 한 줄
                stdout_lines.append(line)
                q.put(line)  # idle 타이머 리셋 신호
        except (ValueError, OSError):
            pass
        finally:
            q.put(_EOF)

    def _read_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except (ValueError, OSError):
            pass

    threading.Thread(target=_pump_stdin, daemon=True).start()
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    start = time.monotonic()
    while True:
        try:
            item = q.get(timeout=idle_timeout)
        except queue.Empty:
            # idle_timeout초간 새 이벤트 0 = 무음 = 멈춤. 정리 후 CodexStalled.
            _terminate_process(proc)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            raise CodexStalled(
                f"codex 무진행(idle): {idle_timeout}s 동안 새 이벤트 없음 — 멈춘 프로세스 정리",
                "".join(stdout_lines),
                "".join(stderr_lines),
            )
        if item is _EOF:
            break
        # WO#55: 진행 이벤트를 라이브 하트비트로 표면화(순수 텔레메트리 — 절대 run 안 죽임).
        if on_event is not None:
            try:
                on_event(_event_summary(item))
            except Exception:  # noqa: BLE001 — 텔레메트리가 codex 호출을 죽이지 않는다
                pass
        # WO#67: 모델 출력 텍스트(완료 item)를 트랜스크립트 tail로 흘린다(순수 텔레메트리).
        if on_output is not None:
            try:
                otext = _event_output_text(item)
                if otext:
                    on_output(otext)
            except Exception:  # noqa: BLE001 — 트랜스크립트가 codex 호출을 죽이지 않는다
                pass
        # 진행 중 — (선택) 아주 넉넉한 절대 backstop. 진행해도 pathological하게 길면 차단.
        if max_duration is not None and (time.monotonic() - start) > max_duration:
            _terminate_process(proc)
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            raise CodexStalled(
                f"codex 절대 시간(max_duration) {max_duration}s 초과 — 정리",
                "".join(stdout_lines),
                "".join(stderr_lines),
            )

    rc = proc.wait()
    t_err.join(timeout=2)
    return rc, "".join(stdout_lines), "".join(stderr_lines)


def _run_streaming_with_retries(
    cmd: list[str],
    prompt: str,
    *,
    idle_timeout: float,
    max_duration: float | None,
    stall_retries: int,
    on_event: Callable[[str | None], None] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> tuple[int, str, str]:
    """스트리밍 실행을 *bounded* 재시도로 감싼다. 멈춤이 지속되면 마지막 CodexStalled 전파.

    stall_retries=0(기본·best-effort)이면 첫 멈춤에서 즉시 전파(degrade 빠르게).
    stall_retries≥1(필수: 빌드·replan·합성)이면 그만큼 재시도 후에도 멈추면 전파 →
    호출부(run_loop)가 escalate한다. CLI 미설치(FileNotFoundError)는 CodexError로 변환.
    """
    last: CodexStalled | None = None
    for _ in range(stall_retries + 1):
        try:
            return _stream_codex(
                cmd, prompt, idle_timeout=idle_timeout, max_duration=max_duration,
                on_event=on_event, on_output=on_output,
            )
        except FileNotFoundError as e:
            raise CodexError(
                f"codex CLI를 찾을 수 없음 ('{CODEX_BIN}' on PATH?)"
            ) from e
        except CodexStalled as e:
            last = e
    assert last is not None
    raise last


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
    reasoning_effort: str | None = None,
    idle_timeout: float | None = None,
    max_duration: float | None = None,
    stall_retries: int = 0,
    on_event: Callable[[str | None], None] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> str:
    """`codex exec`를 한 턴 돌려 `-o` 최종 메시지 파일을 읽어 반환하는 공유 헬퍼.

    후방호환 표면: text(str)만 반환한다. usage까지 필요한 호출부(CodexClient/
    CodexExecutor)는 `exec_codex_with_usage`를 쓴다.
    """
    text, _usage = exec_codex_with_usage(
        prompt, sandbox=sandbox, cwd=cwd, model=model, timeout=timeout,
        ephemeral=ephemeral, reasoning_effort=reasoning_effort,
        idle_timeout=idle_timeout, max_duration=max_duration, stall_retries=stall_retries,
        on_event=on_event, on_output=on_output,
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
    reasoning_effort: str | None = None,
    idle_timeout: float | None = None,
    max_duration: float | None = None,
    stall_retries: int = 0,
    on_event: Callable[[str | None], None] | None = None,
    on_output: Callable[[str], None] | None = None,
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
    reasoning_effort(WO#38): 설정 시 `-c model_reasoning_effort=<effort>`로 codex 추론
             강도를 건다. None(기본)이면 플래그 미부착 → codex 기본(medium) 그대로(후방호환).
             ALLOWED_REASONING_EFFORTS 화이트리스트 밖이면 ValueError. **sandbox 불변.**

    idle_timeout(WO#54): 설정 시 *스트리밍 경로*(Popen + 줄 단위 idle 감시)로 실행한다 —
             `--json` 이벤트가 idle_timeout초간 끊기면(무음=멈춤) hung 프로세스(+자식)를
             정리하고 CodexStalled. **진행 중인 긴 호출은 안 죽임**(총 시간 아님). None(기본)
             이면 기존 subprocess.run 경로 그대로(무회귀 — 기존 테스트 seam 보존).
    max_duration: idle_timeout 경로의 *선택적* 절대 backstop(기본 None=off). 진행해도
             pathological하게 길면 차단. 주 메커니즘은 idle.
    stall_retries: 멈춤 시 bounded 재시도 횟수(기본 0). 필수 호출(빌드·replan·합성)은 1+,
             best-effort(critic·judge)는 0(degrade 빠르게). idle_timeout 경로에서만 의미.
    """
    if sandbox not in ALLOWED_SANDBOXES:
        raise ValueError(
            f"허용되지 않은 sandbox: {sandbox!r} "
            f"(허용: {ALLOWED_SANDBOXES}; danger-full-access 금지)"
        )
    if reasoning_effort is not None and reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise ValueError(
            f"허용되지 않은 reasoning_effort: {reasoning_effort!r} "
            f"(허용: {ALLOWED_REASONING_EFFORTS})"
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
        if reasoning_effort:
            # codex exec엔 전용 추론강도 플래그가 없어 config override로 넘긴다(검증된 형식).
            cmd += ["-c", f"model_reasoning_effort={reasoning_effort}"]
        cmd.append("-")  # 프롬프트는 stdin으로

        if idle_timeout is not None:
            # 스트리밍 경로(WO#54): Popen + 줄 단위 idle 감시. 멈춤은 CodexStalled로 전파
            # (호출부가 라우팅). CodexStalled는 의도적으로 여기서 안 잡는다.
            returncode, stdout_text, stderr_text = _run_streaming_with_retries(
                cmd, prompt,
                idle_timeout=idle_timeout, max_duration=max_duration,
                stall_retries=stall_retries, on_event=on_event, on_output=on_output,
            )
        else:
            # 기존 경로(무회귀): subprocess.run(끝나고 캡처). 기존 테스트 seam(subprocess.run) 보존.
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
            returncode, stdout_text, stderr_text = proc.returncode, proc.stdout, proc.stderr

        if returncode != 0:
            raise CodexError(
                f"codex exec 비정상 종료 (exit {returncode})",
                stdout_text,
                stderr_text,
            )

        text = ""
        if out_path.exists():
            text = out_path.read_text(encoding="utf-8").strip()
        if not text:
            raise CodexError(
                "codex exec가 빈 최종 메시지를 반환함", stdout_text, stderr_text
            )
        # WO#67: 비스트리밍 경로(idle_timeout=None)는 이벤트 콜백이 안 불리므로 최종 텍스트를
        # 한 번 트랜스크립트 출력으로 흘린다(스트리밍 경로는 item별로 이미 흘렸음 — 중복 방지).
        if idle_timeout is None and on_output is not None and text:
            try:
                on_output(text)
            except Exception:  # noqa: BLE001 — 트랜스크립트는 run을 죽이지 않는다
                pass
        # usage 파싱은 best-effort 부가물 — 실패해도 text 반환은 영향 없음.
        # 스트리밍 경로에서도 동일: stdout_text는 그때까지 받은 전체 JSONL(부분 포함).
        usage = None
        try:
            usage = _parse_usage(stdout_text, model)
        except Exception:  # noqa: BLE001 — 계측은 run을 죽이지 않는다
            usage = None
        return text, usage


def heartbeat_wrapped(heartbeat, default_kind, idle_timeout, run_fn):
    """heartbeat가 있으면 start/finish로 감싸고 on_event를 주입해 run_fn(on_event)를 실행한다(WO#55).

    순수 텔레메트리·best-effort: 하트비트 관련 *어떤 예외도* codex 호출을 죽이지 않는다.
    call_kind/unit은 루프가 set_context로 깔아둔 스레드별 컨텍스트에서 읽고(없으면 default_kind),
    이벤트는 명시 handle로 보고한다. run_fn이 던지는 예외(CodexStalled 등)는 그대로 전파된다.
    """
    if heartbeat is None:
        return run_fn(None)
    try:
        kind, unit = heartbeat.get_context()
    except Exception:  # noqa: BLE001
        kind, unit = None, None
    kind = kind or default_kind
    try:
        handle = heartbeat.start(kind, unit, idle_timeout=idle_timeout)
    except Exception:  # noqa: BLE001 — 텔레메트리 실패 → 그냥 실행(하트비트 없이)
        return run_fn(None)

    def on_event(summary):
        try:
            heartbeat.beat(handle, summary)
        except Exception:  # noqa: BLE001
            pass

    try:
        return run_fn(on_event)
    finally:
        try:
            heartbeat.finish(handle)
        except Exception:  # noqa: BLE001
            pass


def observe_call(heartbeat, transcript, default_kind, idle_timeout, prompt, run_fn):
    """heartbeat(요약 한 줄) + transcript(입력+출력 tail) 사이드카를 함께 감싸 호출을 관측한다(WO#67).

    run_fn은 `(on_event, on_output)`를 받는다 — on_event는 #55 하트비트 beat, on_output은 #67
    트랜스크립트 출력 tail. 순수 텔레메트리·best-effort: 하트비트/트랜스크립트 관련 *어떤
    예외도* codex 호출을 죽이지 않는다(#55/#43 패턴). call_kind/unit은 heartbeat가 깔아둔
    스레드별 컨텍스트에서 읽고(없으면 default_kind), 입력(prompt)은 트랜스크립트가 head cap.
    run_fn이 던지는 예외(CodexStalled 등)는 그대로 전파하되, 트랜스크립트 status를 error로 남긴다.
    """
    kind, unit = None, None
    if heartbeat is not None:
        try:
            kind, unit = heartbeat.get_context()
        except Exception:  # noqa: BLE001
            kind, unit = None, None
    kind = kind or default_kind

    hb_handle = None
    if heartbeat is not None:
        try:
            hb_handle = heartbeat.start(kind, unit, idle_timeout=idle_timeout)
        except Exception:  # noqa: BLE001 — 텔레메트리 실패 → 하트비트 없이 진행
            hb_handle = None

    tr_id = None
    if transcript is not None:
        try:
            tr_id = transcript.start(kind=kind, unit=unit, input_text=prompt)
        except Exception:  # noqa: BLE001 — 트랜스크립트 실패 → 캡처 없이 진행
            tr_id = None

    def on_event(summary):
        if hb_handle is not None:
            try:
                heartbeat.beat(hb_handle, summary)
            except Exception:  # noqa: BLE001
                pass

    def on_output(text):
        if tr_id is not None:
            try:
                transcript.output(tr_id, text)
            except Exception:  # noqa: BLE001
                pass

    status = "done"
    try:
        return run_fn(on_event, on_output)
    except BaseException:
        status = "error"
        raise
    finally:
        if hb_handle is not None:
            try:
                heartbeat.finish(hb_handle)
            except Exception:  # noqa: BLE001
                pass
        if tr_id is not None:
            try:
                transcript.finish(tr_id, status)
            except Exception:  # noqa: BLE001
                pass


class CodexClient:
    """`codex exec`를 한 턴 돌려 최종 텍스트를 반환하는 LLMClient.

    model:   override할 모델. None이면 codex 설정 기본.
    workdir: codex의 작업 루트. None이면 호출마다 임시 디렉토리(완전 격리).
    """

    def __init__(
        self,
        model: str | None = None,
        workdir: str | None = None,
        *,
        idle_timeout: float | None = None,
        max_duration: float | None = None,
        stall_retries: int = 0,
        heartbeat=None,
        transcript=None,
        default_call_kind: str | None = None,
        **default_opts,
    ):
        self.model = model
        self.workdir = workdir
        # WO#54: idle(무진행) timeout. None(기본)이면 기존 subprocess.run 경로(무회귀).
        self.idle_timeout = idle_timeout
        self.max_duration = max_duration
        self.stall_retries = stall_retries
        # WO#55: 라이브 하트비트 sink(HeartbeatWriter, duck-typed). None이면 텔레메트리 off(무회귀).
        # call_kind/unit은 루프가 set_context로 깔고 _run이 읽는다. default_call_kind는 폴백.
        self.heartbeat = heartbeat
        # WO#67: 라이브 트랜스크립트 sink(TranscriptWriter, duck-typed). None이면 캡처 off(무회귀).
        self.transcript = transcript
        self.default_call_kind = default_call_kind
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
        def call(on_event, on_output):
            return exec_codex_with_usage(
                prompt,
                sandbox="read-only",
                cwd=self.workdir,
                model=self.model,
                idle_timeout=self.idle_timeout,
                max_duration=self.max_duration,
                stall_retries=self.stall_retries,
                on_event=on_event,
                on_output=on_output,
            )

        text, usage = observe_call(
            self.heartbeat, self.transcript, self.default_call_kind,
            self.idle_timeout, prompt, call,
        )
        self.last_usage = usage  # 읽기만 — sandbox 권한 불변
        return text
