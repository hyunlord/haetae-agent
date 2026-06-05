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

from haetae.deps import Runner, ensure_deps
from haetae.judge import (
    DEFAULT_JUDGE_PROMPT_PATH,
    DEFAULT_RUN_JUDGE_PROMPT_PATH,
    LLMJudge,
)
from haetae.llm import LLMClient
from haetae.models import (
    CheckReport,
    CheckType,
    GateResult,
    ProjectSpec,
    RunEvidence,
    Verdict,
)
from haetae.run_harness import run_artifact

# 기계로 자동 실행 불가한 check 타입
_UNEVALUATABLE_TYPES = {CheckType.human, CheckType.judge}


def select_criteria(spec: ProjectSpec, unit: str | None) -> list:
    """gate가 *어떤* acceptance_criteria를 검사할지 unit 태그로 고른다 (WO#26).

      - unit is None → 전체 검사. 통합 gate(머지된 main)와 순차(N=1) 경로가 쓴다.
        권위 있는 done 판정 — unit 태그 무관하게 전부 실행.
      - unit == "uX" → 그 유닛 per-unit gate(worktree). `ac.unit == "uX"`인 기준만.
        유닛에 자기 기준이 없으면 빈 리스트 → 호출부가 executor-ok(pass)로 흡수한다
        (aggregate_verdict([]) == pass_, 통합 gate가 나중에 전체를 잡음).
    미태그(ac.unit is None) 기준은 통합 기준 → per-unit 선택에서 제외된다(후방호환).
    """
    if unit is None:
        return list(spec.acceptance_criteria)
    return [ac for ac in spec.acceptance_criteria if ac.unit == unit]


def aggregate_verdict(reports: list[CheckReport]) -> Verdict:
    """per-check 보고들을 하나의 verdict로 집계하는 공유 규칙(중복 금지).

    CheckRunner와 CompositeGate가 *동일* 규칙을 쓰도록 일원화한다:
      - fail 하나라도 → fail_recoverable
      - fail 없고 skipped 하나라도 → ambiguous (사람/judge tier 필요)
      - 전부 pass → pass_
    gate는 done을 내지 않는다(done_when 충족 판단은 replan 몫).
    """
    any_fail = any(r.status == "fail" for r in reports)
    any_skipped = any(r.status == "skipped" for r in reports)
    if any_fail:
        return Verdict.fail_recoverable
    if any_skipped:
        return Verdict.ambiguous
    return Verdict.pass_


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
    def judge(self, result: str, spec: ProjectSpec, unit: str | None = None) -> GateResult:
        # unit이 주어지면 그 유닛 태그된 기준만(WO#26). 비면(자기 기준 없음)
        # report=[] → aggregate_verdict([])==pass_ → executor-ok(블록 안 함).
        acs = select_criteria(spec, unit)
        report = [self._run_check(ac.id, ac.check) for ac in acs]
        # 집계 규칙은 공유 헬퍼로 일원화 — 근거(report)만 반환에 동봉한다.
        return GateResult(verdict=aggregate_verdict(report), checks=report)

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


def _run_degrade_detail(ev: RunEvidence) -> str:
    """judge 없이 booted만으로 판정할 때 CheckReport.detail 한 줄(감사용)."""
    if ev.timed_out:
        return f"degrade(booted 판정): 타임아웃 — {ev.reason or 'timed_out'}"
    if not ev.booted:
        return f"degrade(booted 판정): 미부팅 — {ev.reason or _tail(ev.stderr_tail)}"
    return f"degrade(booted 판정): 정상 부팅(exit {ev.exit_code}, {ev.duration_s:.2f}s)"


class CompositeGate:
    """기계 체크(CheckRunner)와 LLM judge를 기준별로 라우팅해 하나의 GateResult로 합친다.

    라우팅(check.type 기준):
      - 기계(test/bench/lint/build/schema, cmd 있음) → CheckRunner._run_check 재사용.
      - judge 타입 → 모아서 LLMJudge.judge_criteria로 *한 번에* 평가(느린 호출 절감).
      - human / cmd 없음 → skipped(CheckRunner._run_check이 그대로 처리).

    비용/행동 불변 보장: judge_client=None 이거나 judge 타입 기준이 0개면 LLMJudge를
    아예 만들지 않아 judge 호출 0회. judge 타입은 그때 _run_check으로 흘러 skipped가
    되므로(=기존 CheckRunner와 동일), 기계 전용 spec의 verdict는 무회귀다.

    집계는 aggregate_verdict 공유 헬퍼로 일원화한다(CheckRunner와 동일 규칙).
    """

    def __init__(
        self,
        workdir: str | Path = ".",
        judge_client: LLMClient | None = None,
        *,
        timeout: float = 120,
        judge_prompt_path: str | Path = DEFAULT_JUDGE_PROMPT_PATH,
        run_judge_prompt_path: str | Path = DEFAULT_RUN_JUDGE_PROMPT_PATH,
        run_timeout: float = 120,
        install_deps: bool = True,
        install_timeout: int = 300,
        deps_runner: Runner | None = None,
        max_file_bytes: int = 64_000,
        max_total_bytes: int = 200_000,
    ):
        self.workdir = str(workdir)
        self.judge_client = judge_client
        self.judge_prompt_path = judge_prompt_path
        self.run_judge_prompt_path = run_judge_prompt_path
        self.run_timeout = run_timeout
        self.install_deps = install_deps
        self.install_timeout = install_timeout
        self.deps_runner = deps_runner
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        # 기계 per-check 평가는 CheckRunner에 위임(중복 로직 금지).
        self._runner = CheckRunner(workdir=workdir, timeout=timeout)

    # ── Gate 인터페이스 ───────────────────────────────────────────────
    def judge(self, result: str, spec: ProjectSpec, unit: str | None = None) -> GateResult:
        # WO#26: unit이 주어지면(per-unit gate, worktree) 그 유닛 태그된 기준만 검사한다.
        # 자기 기준이 하나도 없으면 install/run/judge 모두 건너뛰고 executor-ok(pass).
        # 통합 gate·순차 경로는 unit=None → 전체 검사(권위 있는 done 판정, 무회귀).
        acs = select_criteria(spec, unit)
        if unit is not None and not acs:
            return GateResult(verdict=Verdict.pass_, checks=[])

        # 호스트-사이드 install(WO#23): 기계 체크(npm test)·run-harness가 설치된 deps로
        # 동작하도록 체크 평가 *전에* 1회. ensure_deps 자체는 여전히 raise 안 함(non-fatal).
        # 해시 캐시로 매니페스트 불변이면 스킵. 매니페스트 없으면 no-op.
        install_res = None
        if self.install_deps:
            install_res = ensure_deps(
                self.workdir, timeout=self.install_timeout, runner=self.deps_runner
            )

        # run 타입: judge 유무와 무관하게 항상 harness로 실행해 RunEvidence 캡처.
        run_acs = [ac for ac in acs if ac.check.type == CheckType.run]
        run_reports = self._judge_run_acs(run_acs, result, spec)

        # judge 타입은 judge_client이 있을 때만 LLM 경로로. 없으면 _run_check→skipped.
        route_to_judge = self.judge_client is not None
        judge_acs = [
            ac
            for ac in acs
            if route_to_judge and ac.check.type == CheckType.judge
        ]

        judged: dict[str, CheckReport] = {}
        if judge_acs:  # 비면 LLMJudge 생성·호출 0회.
            llm_judge = LLMJudge(
                self.judge_client,
                workdir=self.workdir,
                prompt_path=self.judge_prompt_path,
                max_file_bytes=self.max_file_bytes,
                max_total_bytes=self.max_total_bytes,
            )
            for rep in llm_judge.judge_criteria(judge_acs, result, spec):
                judged[rep.ac_id] = rep

        # spec 순서를 보존하며 run/judge/기계 보고를 합친다(run·judge 우선, 나머지는 기계).
        report: list[CheckReport] = []
        for ac in acs:
            if ac.id in run_reports:
                report.append(run_reports[ac.id])
            elif ac.id in judged:
                report.append(judged[ac.id])
            else:
                report.append(self._runner._run_check(ac.id, ac.check))

        # WO#25 Part B: 매니페스트가 있었고 호스트 install이 *실제로 실패*하면(skipped/none
        # 아님) 명시적 fail 체크를 추가 → aggregate_verdict가 pass를 못 내고 replan이
        # 매니페스트를 고치게 한다. "clean에서 설치됨"이 매니페스트 있을 때 암묵 기준.
        if (
            install_res is not None
            and install_res.manager != "none"
            and not install_res.skipped_cached
            and not install_res.ok
        ):
            report.append(
                CheckReport(
                    ac_id="(install)",
                    check_type=CheckType.build,
                    cmd=None,
                    status="fail",
                    exit_code=None,
                    detail=f"의존성 설치 실패: {install_res.reason}",
                )
            )

        return GateResult(verdict=aggregate_verdict(report), checks=report)

    # ── run 타입 라우팅: harness 실행 + (judge | booted degrade) ─────────
    def _judge_run_acs(
        self, run_acs: list, result: str, spec: ProjectSpec
    ) -> dict[str, CheckReport]:
        """run 타입 ac를 harness로 실행해 RunEvidence 캡처 후 판정한다.

          - cmd 없음 → skipped(run인데 실행할 게 없음).
          - judge_client 있음 → run-judge로 동적 행동 적대 판정(pass/fail/skipped).
          - judge_client 없음 → **graceful degrade**: pass = evidence.booted
            (크래시/타임아웃 없이 부팅됐는가).
        모든 보고에 RunEvidence를 실어 감사한다.
        """
        reports: dict[str, CheckReport] = {}
        if not run_acs:
            return reports

        items: list[tuple] = []  # (ac, RunEvidence) — cmd 있는 것만
        for ac in run_acs:
            if not ac.check.cmd:
                reports[ac.id] = CheckReport(
                    ac_id=ac.id, check_type=CheckType.run, cmd=None,
                    status="skipped", exit_code=None, detail="run 체크에 cmd 없음",
                )
                continue
            ev = run_artifact(ac.check.cmd, workdir=self.workdir, timeout=self.run_timeout)
            items.append((ac, ev))

        if not items:
            return reports

        if self.judge_client is not None:
            llm_judge = LLMJudge(
                self.judge_client,
                workdir=self.workdir,
                prompt_path=self.judge_prompt_path,
                run_prompt_path=self.run_judge_prompt_path,
                max_file_bytes=self.max_file_bytes,
                max_total_bytes=self.max_total_bytes,
            )
            for rep in llm_judge.judge_run_criteria(items, result, spec):
                reports[rep.ac_id] = rep
        else:
            # degrade: 비전/judge 없을 때 "크래시·타임아웃 없이 부팅됨" 여부만으로 판정.
            for ac, ev in items:
                reports[ac.id] = CheckReport(
                    ac_id=ac.id, check_type=CheckType.run, cmd=ac.check.cmd,
                    status=("pass" if ev.booted else "fail"),
                    exit_code=ev.exit_code,
                    detail=_run_degrade_detail(ev),
                    run_evidence=ev,
                )
        return reports
