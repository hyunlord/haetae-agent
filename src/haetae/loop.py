"""loop driver — synthesize → replan → dispatch → gate → 반복.

손으로 돌리던 director 루프(1~6번)의 코드판. 이번 WO는 executor/gate를 주입 가능한
Protocol + mock으로 두고 *오케스트레이션 흐름*만 증명한다.
(budget/stuck 정식 처리, governed spec-change 적용, 실제 executor/gate 어댑터는 이후 WO.)
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import yaml

from haetae import intake, replan as replan_mod, spec_critic as critic_mod
from haetae.intake import SynthesisError, synthesize
from haetae.llm import LLMClient
from haetae.models import (
    Action,
    CheckReport,
    Event,
    GateResult,
    NextOrder,
    PlanItem,
    PlanState,
    ProjectSpec,
    SpecCritique,
    State,
    Status,
    Verdict,
)
from haetae.replan import ReplanError, replan
from haetae.scheduler import all_done, is_stuck, ready_units
from haetae.spec_change import apply_spec_change
from haetae.spec_critic import synthesize_with_critique
from haetae.worktree import WorktreeError, WorktreeManager

# 병렬 모드에서 worktree 경로를 받아 그 경로에 묶인 Executor/Gate를 만드는 팩토리.
# (Executor/Gate는 생성 시 workdir이 고정되므로 unit마다 worktree로 다시 묶어야 한다.)
ExecutorFactory = Callable[[Path], "Executor"]
GateFactory = Callable[[Path], "Gate"]


# ──────────────────────────── 주입 인터페이스 ────────────────────────────


@runtime_checkable
class Executor(Protocol):
    """work order를 실행하고 결과 요약 텍스트를 반환한다."""

    def run(self, order: NextOrder) -> str: ...


@runtime_checkable
class Gate(Protocol):
    """실행 결과를 spec 기준으로 판정해 verdict + 근거(GateResult)를 반환한다.

    근거(per-check 증거)는 mutable 속성이 아니라 반환 계약이다 — 루프가 그대로
    Event.checks에 실어 state 파일을 진짜 감사 로그로 만든다.
    """

    def judge(self, result: str, spec: ProjectSpec) -> GateResult: ...


class MockExecutor:
    """테스트용. 스크립트된 결과를 순서대로(소진 시 마지막을 반복) 반환."""

    def __init__(self, results: list[str] | str):
        self._results = [results] if isinstance(results, str) else list(results)
        self._i = 0
        self.calls: list[NextOrder] = []

    def run(self, order: NextOrder) -> str:
        self.calls.append(order)
        r = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return r


class MockGate:
    """테스트용. 스크립트된 Verdict를 순서대로(소진 시 마지막을 반복) GateResult로 반환.

    checks: 매 judge 호출이 동봉할 per-check 증거(선택). 모든 호출에 같은 리스트를
    재사용한다. 기본 None=빈 근거. 근거가 Event.checks까지 흐르는지 검증용.
    """

    def __init__(
        self,
        verdicts: list[Verdict] | Verdict,
        checks: list[CheckReport] | None = None,
    ):
        self._v = [verdicts] if isinstance(verdicts, Verdict) else list(verdicts)
        self._checks = checks
        self._i = 0
        self.calls: list[str] = []

    def judge(self, result: str, spec: ProjectSpec) -> GateResult:
        self.calls.append(result)
        v = self._v[min(self._i, len(self._v) - 1)]
        self._i += 1
        return GateResult(verdict=v, checks=list(self._checks or []))


# ──────────────────────────── 내부 헬퍼 ────────────────────────────

# verdict → 해당 unit의 plan 상태 매핑
_VERDICT_TO_PLAN = {
    Verdict.pass_: PlanState.done,
    Verdict.done: PlanState.done,
    Verdict.fail_recoverable: PlanState.in_progress,
    Verdict.fail_replan: PlanState.failed,
    Verdict.stuck: PlanState.failed,
}


def _init_state(spec: ProjectSpec) -> State:
    plan = [
        PlanItem(unit=u.unit, state=PlanState.pending, deps=(u.deps or None))
        for u in spec.decomposition
    ]
    return State(
        spec_ref=spec.spec_id,
        spec_version=spec.version,
        status=Status.running,
        plan=plan,
    )


def _escalated_no_spec(reason: str, raw_response: str | None) -> State:
    """spec이 없을 때(합성 실패) 구성하는 최소 escalated State.

    spec_ref는 placeholder. 진짜 원인은 pending_escalations에 raw와 함께 남긴다.
    """
    note: dict = {"reason": reason}
    if raw_response is not None:
        note["raw_response"] = raw_response
    return State(
        spec_ref="(synthesis-failed)",
        spec_version=0,
        status=Status.escalated,
        pending_escalations=[note],
    )


def _update_plan(state: State, unit: str, verdict: Verdict) -> None:
    new = _VERDICT_TO_PLAN.get(verdict)
    if new is None:
        return
    for item in state.plan:
        if item.unit == unit:
            item.state = new
            return
    state.plan.append(PlanItem(unit=unit, state=new))


def _set_plan_state(state: State, unit: str, plan_state: PlanState) -> None:
    """병렬 스케줄러용 직접 plan 상태 설정(verdict 매핑 없이 — 스케줄러가 직접 제어).

    재dispatch는 failed/in_progress를 다시 pending으로 되돌려 frontier에 재진입시킨다.
    """
    for item in state.plan:
        if item.unit == unit:
            item.state = plan_state
            return


def _exec_and_gate(
    executor: "Executor", gate: "Gate", order: NextOrder, spec: ProjectSpec
) -> tuple[str, GateResult]:
    """ThreadPoolExecutor 워커 — 느린 부분(executor 실행 + unit gate)만 병렬화한다.

    brain(work order 생성)과 worktree 생성/머지는 main 스레드에서 직렬·결정적으로
    처리하므로 여기엔 mock 시퀀스 race가 없다. 예외는 fut.result()로 전파된다.
    """
    result = executor.run(order)
    return result, gate.judge(result, spec)


def _save_state(state: State, state_path: str | Path) -> None:
    Path(state_path).write_text(
        yaml.safe_dump(
            state.model_dump(by_alias=True, mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ──────────────────────────── 진행 표시 헬퍼 ────────────────────────────


def _truncate(text: str, limit: int = 60) -> str:
    """여러 줄/공백을 한 줄로 접고 limit자로 자른다(진행 메시지용)."""
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _summarize_gate(gr: GateResult) -> str:
    """gate 판정을 한 줄로: verdict + 체크 요약(첫 실패 체크의 cmd/exit 포함)."""
    v = gr.verdict.value
    checks = gr.checks
    failed = [c for c in checks if c.status == "fail"]
    if failed:
        c = failed[0]
        what = c.cmd or c.ac_id or c.check_type.value
        ec = "" if c.exit_code is None else f" (exit {c.exit_code})"
        return f"gate: {v} — {_truncate(what, 40)}{ec}"
    if checks:
        passed = sum(1 for c in checks if c.status == "pass")
        return f"gate: {v} ({passed}/{len(checks)} 통과)"
    return f"gate: {v}"


def _critique_label(crit: SpecCritique) -> str:
    """spec critic 비평을 한 줄 progress로: 재합성/평가불가/soft/adequate."""
    if crit.resynthesized:
        return "spec critic: soft — 1회 재합성"
    if crit.note and "평가 불가" in crit.note:
        return "spec critic: (평가 불가)"
    if crit.verdict == "soft":
        # soft지만 재합성이 안 일어남(구체 gap 없음 or 재합성 폴백) → 원본 유지.
        return "spec critic: soft — 원본 유지"
    return "spec critic: adequate"


def _final_label(state: State) -> str:
    """종료 라벨: escalated면 직전 escalation 사유를 한 줄로 덧붙인다."""
    if state.status == Status.escalated and state.pending_escalations:
        last = state.pending_escalations[-1]
        reason = None
        if isinstance(last, dict):
            reason = last.get("reason") or last.get("question")
        else:
            reason = str(last)
        if reason:
            return f"종료: {state.status.value} — {_truncate(str(reason))}"
    return f"종료: {state.status.value}"


# ──────────────────────────── 루프 ────────────────────────────


def run_loop(
    order: str,
    client: LLMClient,
    executor: Executor,
    gate: Gate,
    *,
    critic_client: LLMClient | None = None,
    max_iters: int = 20,
    replan_retries: int = 2,
    prompt_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    max_parallel: int = 1,
    workdir: str | Path | None = None,
    executor_factory: ExecutorFactory | None = None,
    gate_factory: GateFactory | None = None,
    unit_retries: int = 1,
    worktree_manager: WorktreeManager | None = None,
) -> State:
    """주문 한 줄에서 종료 상태까지 루프를 돈다. 최종 State를 반환(필요시 저장).

    내성(WO#12): LLM 출력 하나로 루프가 crash하지 않는다.
      - 합성(synthesize) 실패: traceback 대신 escalated State 반환.
      - replan 출력 검증 실패: 최대 replan_retries회 재시도(직전 에러를 피드백으로
        얹어 self-correction 유도), 소진 시 escalate하고 루프 종료(예외 전파 안 함).

    progress: 단계 진입 시 한 줄 상태를 받는 콜백(WO#13). 기본 None=no-op이라
      테스트엔 아무것도 새지 않는다. CLI는 stderr로 찍는 함수를 주입해 느린 codex
      호출이 "행"으로 안 보이게 한다.

    병렬 실행(WO#21): max_parallel>1이면 git worktree 격리 + 결정적 DAG 스케줄러로
      ready unit들을 동시에 굴린다. **max_parallel<=1이면 기존 순차 경로 그대로**
      (worktree·동시성 미사용 → 무회귀). 선형 deps DAG는 자연히 직렬화되어
      순차와 동일한 최종 결과를 낸다.
      - workdir: 통합(main) 브랜치가 사는 git 루트. worktree들이 여기서 분기/머지.
      - executor_factory/gate_factory: worktree 경로를 받아 그 경로에 묶인 unit용
        executor/gate를 만든다(기본: 주입된 executor/gate를 경로 무시하고 재사용).
        통합 gate(머지된 main 1회 검사)는 항상 인자로 받은 `gate`를 쓴다.
      - unit_retries: gate 실패/머지 충돌 시 그 unit을 재dispatch하는 최대 횟수.
      - 이벤트는 완료 타이밍과 무관하게 (unit-id, attempt)로 정렬해 결정성을 봉인.
    """

    def emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    def try_save() -> None:
        """비치명적 + 증분 저장. write 실패해도 run을 죽이지 않고 경고만 흘린다.

        이벤트 append/spec 변경/종료마다 호출 → Ctrl-C나 후반 실패에도 그때까지의
        감사 로그가 파일에 남는다. state_path가 없으면 no-op.
        """
        if state_path is None:
            return
        try:
            _save_state(state, state_path)
        except Exception as e:  # noqa: BLE001 — 저장 실패는 run을 죽이면 안 된다
            emit(f"⚠ state 저장 실패: {state_path} ({e}) — run은 정상 완료됨")

    syn_prompt = (
        Path(prompt_dir) / "synthesizer.md" if prompt_dir else intake.DEFAULT_PROMPT_PATH
    )
    rep_prompt = (
        Path(prompt_dir) / "replan.md" if prompt_dir else replan_mod.DEFAULT_PROMPT_PATH
    )
    critic_prompt = (
        Path(prompt_dir) / "spec_critic.md"
        if prompt_dir
        else critic_mod.DEFAULT_CRITIC_PROMPT_PATH
    )

    emit("합성 중…")
    critique: SpecCritique | None = None
    try:
        if critic_client is not None:
            # opt-in 적대적 critic: 비평 surface + 구체 gap이면 바운드 1회 재합성.
            spec, critique = synthesize_with_critique(
                order, client, critic_client,
                syn_prompt_path=syn_prompt, critic_prompt_path=critic_prompt,
            )
        else:
            spec = synthesize(order, client, prompt_path=syn_prompt)
    except SynthesisError as e:
        state = _escalated_no_spec(
            "spec 합성 실패 (synthesize 출력 검증 불통과)", e.raw_response
        )
        emit(_final_label(state))
        try_save()
        return state

    state = _init_state(spec)
    if critique is not None:
        state.spec_critique = critique  # 감사 기록(재합성 발생 여부 포함)
        emit(_critique_label(critique))
        try_save()

    # 병렬 모드: worktree 격리 + 결정적 DAG 스케줄러로 분기.
    # max_parallel<=1은 아래 순차 경로 그대로(현행 동작 불변 — 무회귀).
    if max_parallel > 1:
        return _parallel_loop(
            spec,
            state,
            client,
            integration_gate=gate,
            executor_factory=executor_factory or (lambda wt: executor),
            gate_factory=gate_factory or (lambda wt: gate),
            wm=worktree_manager or WorktreeManager(workdir or "."),
            max_parallel=max_parallel,
            max_iters=max_iters,
            replan_retries=replan_retries,
            unit_retries=unit_retries,
            rep_prompt=rep_prompt,
            emit=emit,
            try_save=try_save,
        )

    last_result = "(시작 — 아직 실행 없음)"

    iters = 0
    while iters < max_iters and state.status == Status.running:
        iters += 1

        # replan: 비결정적 LLM 출력 → 검증 실패를 흡수(재시도 → 소진 시 escalate).
        decision = None
        feedback: str | None = None
        last_err: ReplanError | None = None
        for _attempt in range(replan_retries + 1):
            if _attempt == 0:
                emit("replan 중…")
            else:
                emit(
                    f"replan 재시도 {_attempt}: "
                    f"{_truncate(feedback or 'Decision 검증 실패')}"
                )
            try:
                decision = replan(
                    spec, state, last_result, client,
                    prompt_path=rep_prompt, feedback=feedback,
                )
                break
            except ReplanError as e:
                last_err = e
                feedback = e.message  # raw는 빼고 검증 메시지만 다시 태운다
        if decision is None:
            state.status = Status.escalated
            state.pending_escalations.append(
                {
                    "reason": f"replan 출력 {replan_retries + 1}회 검증 실패",
                    "raw_response": last_err.raw_response if last_err else None,
                }
            )
            break

        action = decision.action

        if action in (Action.next_order, Action.retry):
            no = decision.next_order
            if no is None:
                # 방어: next_order/retry인데 본문이 없음 → 사람 tier로 올림
                state.status = Status.escalated
                state.pending_escalations.append(
                    {"reason": "next_order 본문 없음", "action": action.value}
                )
                break
            emit(f"작업 실행 중: {no.unit} — {_truncate(no.goal)}")
            result = executor.run(no)
            emit("gate 검사 중…")
            gr = gate.judge(result, spec)
            verdict = gr.verdict
            emit(_summarize_gate(gr))
            state.events.append(
                Event(
                    seq=len(state.events) + 1,
                    unit=no.unit,
                    work_order_ref=no.goal,
                    result=result,
                    verdict=verdict,
                    checks=gr.checks,
                )
            )
            _update_plan(state, no.unit, verdict)
            try_save()  # 증분: 매 이벤트마다 감사 로그 보존(비치명적)
            last_result = f"unit={no.unit} verdict={verdict.value} :: {result}"
            if verdict == Verdict.done:
                state.status = Status.done

        elif action == Action.stop:
            state.status = Status.done

        elif action == Action.escalate:
            state.status = Status.escalated
            if decision.escalation is not None:
                state.pending_escalations.append(
                    decision.escalation.model_dump(by_alias=True, mode="json")
                )

        elif action == Action.replan_approach:
            # executor 호출 없이 다음 루프 — 다음 replan이 계획을 다시 짠다.
            last_result = "(approach reset — 이전 접근 폐기, 재계획 요청됨)"

        elif action == Action.propose_spec_change:
            # governed 적용(mutability gradient): assumptions+evidence는 자율 적용,
            # 성공 정의(goal/criteria/done_when)·anchor(order_raw)는 자율 변경 불가.
            proposal = decision.spec_change
            if proposal is None:
                state.status = Status.escalated
                state.pending_escalations.append(
                    {"reason": "propose_spec_change인데 spec_change 본문 없음"}
                )
                break
            outcome = apply_spec_change(spec, state, proposal)
            if outcome.applied:
                # 감사 이벤트만 남기고 루프 계속 — 다음 replan이 갱신된 spec을 본다.
                state.events.append(
                    Event(
                        seq=len(state.events) + 1,
                        verdict=decision.verdict,
                        result=f"spec-change applied: {outcome.reason}",
                        learnings=outcome.reason,
                    )
                )
                emit(f"spec 변경 적용: {proposal.target} (v{spec.version})")
                try_save()  # 증분: spec 변경도 즉시 보존(비치명적)
                last_result = f"(spec-change applied — {outcome.reason})"
            else:
                state.status = Status.escalated
                state.pending_escalations.append(outcome.note)
                emit(f"spec 변경 escalate: {_truncate(outcome.reason)}")

        else:  # 방어: 미지원 action
            state.status = Status.escalated
            state.pending_escalations.append({"reason": f"미지원 action: {action.value}"})

    # max_iters 도달 등으로 여전히 running이면 임시로 stopped_stuck.
    if state.status == Status.running:
        state.status = Status.stopped_stuck

    emit(_final_label(state))
    try_save()

    return state


# ──────────────────────────── 병렬 루프 (WO#21) ────────────────────────────


def _parallel_loop(
    spec: ProjectSpec,
    state: State,
    client: LLMClient,
    *,
    integration_gate: Gate,
    executor_factory: ExecutorFactory,
    gate_factory: GateFactory,
    wm: WorktreeManager,
    max_parallel: int,
    max_iters: int,
    replan_retries: int,
    unit_retries: int,
    rep_prompt: str | Path,
    emit: Callable[[str], None],
    try_save: Callable[[], None],
) -> State:
    """결정적 DAG 스케줄러 + git worktree 격리로 ready unit들을 동시에 굴린다.

    설계 요점:
      - 결정성: ready set은 unit-id 정렬. brain(work order 생성)·worktree 생성·머지는
        **main 스레드에서 직렬** 처리 → mock 시퀀스 race 없음, 선형 DAG=순차 동일.
      - 병렬: 느린 executor 실행 + unit gate만 ThreadPoolExecutor로 동시 실행.
      - 머지 직렬화: 완료된 future를 main 스레드가 (unit-id 순으로) 하나씩 처리하며
        머지하므로 자연히 직렬. 충돌이면 unit을 pending으로 되돌려 갱신된 main 위에
        재dispatch(바운드). 소진 시 escalate.
      - 뒷정리: try/finally로 모든 종료 경로(done/escalate/예외)에서 cleanup_all().
      - 이벤트: (unit-id, attempt)로 정렬해 완료 타이밍 비결정성을 봉인.
    """
    # ── 상태 봉투(closure로 공유) ──────────────────────────────────────────
    terminal: str | None = None  # None | "done"(brain stop) | "escalated" | "stuck"
    last_result = "(시작 — 아직 실행 없음)"
    total_attempts = 0
    attempts_of: dict[str, int] = {u.unit: 0 for u in spec.decomposition}
    buf: list[dict] = []  # 결정적 정렬 전 이벤트 수집 버퍼

    def record(unit: str, goal: str | None, result: str, verdict: Verdict,
               checks: list[CheckReport]) -> None:
        buf.append({
            "unit": unit, "attempt": attempts_of.get(unit, 0), "goal": goal,
            "result": result, "verdict": verdict, "checks": list(checks),
        })

    def materialize(integration_ev: Event | None = None) -> None:
        ordered = sorted(buf, key=lambda e: (e["unit"], e["attempt"]))
        evs: list[Event] = []
        for i, e in enumerate(ordered, start=1):
            evs.append(Event(
                seq=i, unit=e["unit"], work_order_ref=e["goal"],
                result=e["result"], verdict=e["verdict"], checks=e["checks"],
            ))
        if integration_ev is not None:
            integration_ev.seq = len(evs) + 1
            evs.append(integration_ev)
        state.events = evs

    def persist() -> None:
        materialize()
        try_save()

    def gen_order(unit: str) -> NextOrder | None:
        """brain(replan/Decision 머신 재사용)으로 이 unit의 work order를 만든다.

        스케줄러가 unit을 권위적으로 정했으므로 결과 next_order.unit을 unit으로 고정.
        escalate/stop/replan-소진은 terminal을 세팅하고 None을 반환(이 unit 미dispatch).
        """
        nonlocal terminal, last_result
        ctx = (
            f"스케줄러가 unit '{unit}'를 ready로 선택했다(deps 충족). "
            f"이 unit의 work order만 생성하라(action=next_order, unit={unit}).\n"
            f"# 직전 진행\n{last_result}"
        )
        decision = None
        feedback: str | None = None
        last_err: ReplanError | None = None
        for attempt in range(replan_retries + 1):
            emit("replan 중…" if attempt == 0
                 else f"replan 재시도 {attempt}: {_truncate(feedback or 'Decision 검증 실패')}")
            try:
                decision = replan(spec, state, ctx, client,
                                  prompt_path=rep_prompt, feedback=feedback)
                break
            except ReplanError as e:
                last_err = e
                feedback = e.message
        if decision is None:
            terminal = "escalated"
            state.pending_escalations.append({
                "reason": f"unit {unit} replan {replan_retries + 1}회 검증 실패",
                "raw_response": last_err.raw_response if last_err else None,
            })
            return None

        action = decision.action
        if action in (Action.next_order, Action.retry):
            no = decision.next_order
            if no is None:
                terminal = "escalated"
                state.pending_escalations.append(
                    {"reason": "next_order 본문 없음", "unit": unit})
                return None
            no.unit = unit  # 스케줄러 권위 — 어떤 unit인지는 스케줄러가 정한다
            emit(f"작업 실행 중: {unit} — {_truncate(no.goal)}")
            return no
        if action == Action.stop:
            terminal = "done"
            return None
        if action == Action.escalate:
            terminal = "escalated"
            if decision.escalation is not None:
                state.pending_escalations.append(
                    decision.escalation.model_dump(by_alias=True, mode="json"))
            return None
        # parallel v1 바운드: 똑똑한 in-flight replan/spec-change 저글링은 안 한다
        terminal = "escalated"
        state.pending_escalations.append(
            {"reason": f"parallel v1 미지원 action: {action.value}", "unit": unit})
        return None

    def handle_outcome(unit: str, order: NextOrder, result: str, gr: GateResult) -> None:
        """unit gate 결과를 처리: 성공→머지, 충돌/실패→바운드 재dispatch 또는 escalate."""
        nonlocal terminal, last_result
        verdict = gr.verdict
        emit(_summarize_gate(gr))

        if verdict in (Verdict.pass_, Verdict.done):
            outcome = wm.merge(unit)
            if outcome == "ok":
                record(unit, order.goal, result, verdict, gr.checks)
                _set_plan_state(state, unit, PlanState.done)
                wm.cleanup(unit)
                last_result = f"unit={unit} verdict={verdict.value} merged"
                persist()
                return
            # 머지 충돌 → 직렬화 재dispatch(갱신된 main 위), 소진 시 escalate
            wm.discard(unit)
            if attempts_of[unit] < unit_retries:
                attempts_of[unit] += 1
                emit(f"머지 충돌 → 직렬화 재dispatch: {unit} (재시도 {attempts_of[unit]})")
                _set_plan_state(state, unit, PlanState.pending)
            else:
                record(unit, order.goal, result, Verdict.fail_replan, gr.checks)
                _set_plan_state(state, unit, PlanState.failed)
                terminal = "escalated"
                state.pending_escalations.append(
                    {"reason": f"unit {unit} 머지 충돌 {unit_retries}회 후 미해소 — escalate",
                     "unit": unit})
                persist()
            return

        # gate 실패 → 바운드 재시도(재dispatch) 또는 escalate
        wm.discard(unit)
        if attempts_of[unit] < unit_retries:
            attempts_of[unit] += 1
            emit(f"unit gate 실패({verdict.value}) → 재시도: {unit} ({attempts_of[unit]})")
            _set_plan_state(state, unit, PlanState.pending)
        else:
            record(unit, order.goal, result, verdict, gr.checks)
            _set_plan_state(state, unit, PlanState.failed)
            terminal = "escalated"
            state.pending_escalations.append(
                {"reason": f"unit {unit} gate {unit_retries}회 실패 — escalate", "unit": unit})
            persist()

    # ── 실행: 모든 경로에서 cleanup_all 보장(try/finally) ─────────────────
    try:
        try:
            wm.ensure_repo()
        except WorktreeError as e:
            state.status = Status.escalated
            state.pending_escalations.append({"reason": f"git repo 준비 실패: {e}"})
            materialize()
            emit(_final_label(state))
            try_save()
            return state

        if not state.plan:
            state.status = Status.escalated
            state.pending_escalations.append(
                {"reason": "병렬 실행에는 decomposition(units)이 필요하다 — 비어있음"})
            emit(_final_label(state))
            try_save()
            return state

        in_flight: set[str] = set()

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures: dict[Future, tuple[str, NextOrder, Path]] = {}

            def dispatch_ready() -> None:
                if terminal:
                    return
                for u in ready_units(state.plan, in_flight):
                    if terminal or len(futures) >= max_parallel:
                        break
                    order = gen_order(u)  # main 스레드 — 직렬·결정적
                    if order is None:  # escalate/stop/replan-소진 → terminal 세팅됨
                        break
                    wt = wm.create(u)
                    _set_plan_state(state, u, PlanState.in_progress)
                    in_flight.add(u)
                    fut = pool.submit(
                        _exec_and_gate, executor_factory(wt), gate_factory(wt), order, spec)
                    futures[fut] = (u, order, wt)

            dispatch_ready()
            while futures and not terminal:
                done_set, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                # 완료분을 unit-id 순으로 처리 → 머지 직렬화 + 처리 순서 결정적
                for fut in sorted(done_set, key=lambda f: futures[f][0]):
                    unit, order, _wt = futures.pop(fut)
                    in_flight.discard(unit)
                    total_attempts += 1
                    try:
                        result, gr = fut.result()
                    except Exception as e:  # noqa: BLE001 — executor/gate 예외=그 unit 실패
                        result = f"(executor/gate 예외: {e})"
                        gr = GateResult(verdict=Verdict.fail_recoverable)
                    handle_outcome(unit, order, result, gr)
                    if total_attempts >= max_iters and not terminal:
                        terminal = "stuck"
                if terminal:
                    break
                dispatch_ready()

        # ── finalize: 통합 gate + 최종 status ────────────────────────────
        integration_ev: Event | None = None
        if terminal == "escalated":
            state.status = Status.escalated
        elif terminal == "done":  # brain이 stop으로 done_when 충족 선언
            state.status = Status.done
        elif terminal == "stuck":
            state.status = Status.stopped_stuck
        elif all_done(state.plan):
            # 통합 gate: 머지된 main에서 spec 체크 1회 → cross-unit 깨짐 포착
            emit("통합 gate 검사 중…")
            igr = integration_gate.judge("(integration — 머지된 main 통합 검사)", spec)
            emit(_summarize_gate(igr))
            integration_ev = Event(
                seq=0, unit=None, work_order_ref="(integration)",
                result="통합 gate(머지된 main)", verdict=igr.verdict, checks=igr.checks)
            if igr.verdict in (Verdict.pass_, Verdict.done):
                state.status = Status.done
            else:
                state.status = Status.escalated
                state.pending_escalations.append(
                    {"reason": "통합 gate 실패 — cross-unit breakage",
                     "verdict": igr.verdict.value})
        elif is_stuck(state.plan, in_flight):
            state.status = Status.stopped_stuck
        else:
            state.status = Status.stopped_stuck

        materialize(integration_ev)
        emit(_final_label(state))
        try_save()
        return state
    finally:
        # 타협 불가: done/escalate/예외/Ctrl-C 어떤 경로든 흔적 0.
        wm.cleanup_all()
