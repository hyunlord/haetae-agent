"""RunHarness — 산출물을 실제로 *실행*해 동적 행동 증거(RunEvidence)를 캡처한다 (WO#22).

gate가 지금까지 본 건 정적 증거뿐이다 — 기계 체크(exit code)와 judge(파일 읽기). 둘 다
*실행 시 행동*을 못 봐서 "테스트는 통과하는데 동작은 틀림"이 그대로 통과됐다. 이 모듈은
산출물(헤드리스 트레이스 진입점 등)을 호스트에서 돌려 부팅/에러 + 구조화 트레이스를 잡고,
그 증거를 judge가 적대적으로 판정하게 길을 연다.

실행 위치는 **호스트**(네트워크 O, codex sandbox 아님), **타임아웃 바운드**. 신뢰수준은 이미
`pytest`/`npm test`를 돌리는 것과 동일(스크래치 한정). 컨테이너 격리는 v1 스코프 밖.

원칙(loop/save/critic resilience와 동일): **이 함수는 절대 raise하지 않는다.**
타임아웃·실행불가·예외는 전부 RunEvidence(booted=False, reason/timed_out)로 흡수한다.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from haetae.models import RunEvidence

# judge 입력을 바운드하는 출력 캡처 상한(바이트). trace는 머리, stderr는 꼬리를 남긴다.
MAX_TRACE_BYTES = 64_000
MAX_STDERR_BYTES = 8_000


def _to_text(s) -> str:
    """str|bytes|None을 안전하게 텍스트로(타임아웃 예외의 부분 출력이 bytes일 수 있음)."""
    if s is None:
        return ""
    if isinstance(s, bytes):
        return s.decode("utf-8", errors="replace")
    return str(s)


def _head(s: str, limit: int) -> str:
    """앞부분 limit 바이트만 남긴다(구조화 trace는 머리가 중요)."""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", errors="ignore") + "\n…(truncated)"


def _tail(s: str, limit: int) -> str:
    """끝부분 limit 바이트만 남긴다(에러는 보통 마지막에 찍힘)."""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return "…(truncated)\n" + b[-limit:].decode("utf-8", errors="ignore")


def run_artifact(
    cmd: str, workdir: str | Path = ".", timeout: float = 120.0
) -> RunEvidence:
    """workdir에서 cmd를 실행해 RunEvidence를 캡처한다. **절대 raise하지 않는다.**

    booted = 크래시/타임아웃 없이 정상 종료(exit 0). 타임아웃/실행불가/예외는
    booted=False + reason/timed_out으로 흡수된다. 출력은 cap을 적용해 judge 입력을 바운드.
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        # 타임아웃: 그때까지의 부분 출력이라도 캡처(진단/judge용). 행/무한루프 신호.
        return RunEvidence(
            booted=False,
            exit_code=None,
            trace=_head(_to_text(e.stdout), MAX_TRACE_BYTES),
            stderr_tail=_tail(_to_text(e.stderr), MAX_STDERR_BYTES),
            timed_out=True,
            duration_s=time.monotonic() - start,
            reason=f"timeout (>{timeout}s)",
        )
    except OSError as e:
        # cwd 없음 / 실행 자체 실패 등 — 크래시로 간주.
        return RunEvidence(
            booted=False,
            exit_code=None,
            trace="",
            stderr_tail="",
            timed_out=False,
            duration_s=time.monotonic() - start,
            reason=f"실행 실패: {e}",
        )
    except Exception as e:  # noqa: BLE001 — never-raise 보장(예상 못한 오류도 흡수)
        return RunEvidence(
            booted=False,
            exit_code=None,
            trace="",
            stderr_tail="",
            timed_out=False,
            duration_s=time.monotonic() - start,
            reason=f"예외: {e}",
        )

    return RunEvidence(
        booted=(proc.returncode == 0),
        exit_code=proc.returncode,
        trace=_head(_to_text(proc.stdout), MAX_TRACE_BYTES),
        stderr_tail=_tail(_to_text(proc.stderr), MAX_STDERR_BYTES),
        timed_out=False,
        duration_s=time.monotonic() - start,
        reason=None if proc.returncode == 0 else f"비정상 종료 (exit {proc.returncode})",
    )
