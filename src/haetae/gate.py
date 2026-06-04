"""실제 gate — CheckRunner.

spec의 acceptance_criteria[].check.cmd를 실제로 실행해 객관적으로 verdict를 낸다.
haetae가 "멈출 줄 아는" 차별점의 코드판. LLM/codex 불필요(subprocess만).

v1은 exit-code 기반:
  - 종료코드 0 → 그 check 통과, 아니면 실패.
  - check.pass 값 비교(">=10", "0" 등 기대값 파싱)는 다음 WO(타입별 추출기). 지금은 기록만.

자동 평가 불가(check.type이 human/judge, 또는 cmd 없음)는 "미평가(skipped)"로 표시.

집계:
  - 자동 check 중 하나라도 실패 → fail_recoverable
  - 실패 없지만 미평가가 하나라도 있음 → ambiguous (사람/judge tier 필요)
  - 전부 자동 통과 & 미평가 없음 → pass_
  - gate는 done을 내지 않는다(done_when 충족 판단은 replan 몫).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from haetae.models import CheckReport, CheckType, GateResult, ProjectSpec, Verdict

# 기계로 자동 실행 불가한 check 타입
_UNEVALUATABLE_TYPES = {CheckType.human, CheckType.judge}


class CheckRunner:
    """acceptance_criteria의 check.cmd를 실행해 Verdict를 내는 Gate 구현.

    workdir: check 명령을 실행할 cwd.
    timeout: 각 명령의 최대 실행 시간(초). 행 방지 필수.
    judge는 verdict뿐 아니라 per-check 증거(CheckReport 리스트)를 GateResult에 동봉해
    반환한다 — 근거가 반환 계약이라 루프가 그대로 Event.checks에 실을 수 있다.
    """

    def __init__(self, workdir: str | Path = ".", timeout: float = 120):
        self.workdir = str(workdir)
        self.timeout = timeout

    # ── Gate 인터페이스 ───────────────────────────────────────────────
    def judge(self, result: str, spec: ProjectSpec) -> GateResult:
        report: list[CheckReport] = []
        any_fail = False
        any_skipped = False

        for ac in spec.acceptance_criteria:
            entry = self._run_check(ac.id, ac.check)
            report.append(entry)
            if entry.status == "fail":
                any_fail = True
            elif entry.status == "skipped":
                any_skipped = True

        # 집계 규칙은 불변 — 근거(report)만 반환에 동봉한다.
        if any_fail:
            verdict = Verdict.fail_recoverable
        elif any_skipped:
            verdict = Verdict.ambiguous
        else:
            verdict = Verdict.pass_
        return GateResult(verdict=verdict, checks=report)

    # ── 단일 check 실행 ───────────────────────────────────────────────
    def _run_check(self, ac_id: str, check) -> CheckReport:
        base: dict = {"ac_id": ac_id, "check_type": check.type, "cmd": check.cmd}

        # 미평가: human/judge 타입이거나 cmd가 없음
        if check.type in _UNEVALUATABLE_TYPES or not check.cmd:
            reason = (
                f"{check.type.value} 타입은 자동 평가 불가"
                if check.type in _UNEVALUATABLE_TYPES
                else "cmd 없음"
            )
            return CheckReport(**base, status="skipped", exit_code=None, detail=reason)

        try:
            proc = subprocess.run(
                check.cmd,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return CheckReport(
                **base, status="fail", exit_code=None, detail=f"timeout (>{self.timeout}s)"
            )
        except OSError as e:  # cwd 없음 등 실행 자체 실패
            return CheckReport(**base, status="fail", exit_code=None, detail=f"실행 실패: {e}")

        status = "pass" if proc.returncode == 0 else "fail"
        return CheckReport(
            **base,
            status=status,
            exit_code=proc.returncode,
            detail=_tail(proc.stderr or proc.stdout),
        )


def _tail(s: str, n: int = 300) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else "…" + s[-n:]
