"""loop driver — synthesize → replan → dispatch → gate → 반복.

손으로 돌리던 director 루프(1~6번)의 코드판. 이번 WO는 executor/gate를 주입 가능한
Protocol + mock으로 두고 *오케스트레이션 흐름*만 증명한다.
(budget/stuck 정식 처리, governed spec-change 적용, 실제 executor/gate 어댑터는 이후 WO.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import yaml

from haetae import intake, replan as replan_mod
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
    State,
    Status,
    Verdict,
)
from haetae.replan import ReplanError, replan
from haetae.spec_change import apply_spec_change


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


def _save_state(state: State, state_path: str | Path) -> None:
    Path(state_path).write_text(
        yaml.safe_dump(
            state.model_dump(by_alias=True, mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ──────────────────────────── 루프 ────────────────────────────


def run_loop(
    order: str,
    client: LLMClient,
    executor: Executor,
    gate: Gate,
    *,
    max_iters: int = 20,
    replan_retries: int = 2,
    prompt_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> State:
    """주문 한 줄에서 종료 상태까지 루프를 돈다. 최종 State를 반환(필요시 저장).

    내성(WO#12): LLM 출력 하나로 루프가 crash하지 않는다.
      - 합성(synthesize) 실패: traceback 대신 escalated State 반환.
      - replan 출력 검증 실패: 최대 replan_retries회 재시도(직전 에러를 피드백으로
        얹어 self-correction 유도), 소진 시 escalate하고 루프 종료(예외 전파 안 함).

    progress: 단계 진입 시 한 줄 상태를 받는 콜백(WO#13). 기본 None=no-op이라
      테스트엔 아무것도 새지 않는다. CLI는 stderr로 찍는 함수를 주입해 느린 codex
      호출이 "행"으로 안 보이게 한다.
    """

    def emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    syn_prompt = (
        Path(prompt_dir) / "synthesizer.md" if prompt_dir else intake.DEFAULT_PROMPT_PATH
    )
    rep_prompt = (
        Path(prompt_dir) / "replan.md" if prompt_dir else replan_mod.DEFAULT_PROMPT_PATH
    )

    emit("합성 중…")
    try:
        spec = synthesize(order, client, prompt_path=syn_prompt)
    except SynthesisError as e:
        state = _escalated_no_spec(
            "spec 합성 실패 (synthesize 출력 검증 불통과)", e.raw_response
        )
        emit(f"종료: {state.status.value}")
        if state_path is not None:
            _save_state(state, state_path)
        return state

    state = _init_state(spec)
    last_result = "(시작 — 아직 실행 없음)"

    iters = 0
    while iters < max_iters and state.status == Status.running:
        iters += 1

        # replan: 비결정적 LLM 출력 → 검증 실패를 흡수(재시도 → 소진 시 escalate).
        decision = None
        feedback: str | None = None
        last_err: ReplanError | None = None
        for _attempt in range(replan_retries + 1):
            emit(f"replan 중… (재시도 {_attempt})")
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
            emit("작업 실행 중…")
            result = executor.run(no)
            emit("gate 검사 중…")
            gr = gate.judge(result, spec)
            verdict = gr.verdict
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
                last_result = f"(spec-change applied — {outcome.reason})"
            else:
                state.status = Status.escalated
                state.pending_escalations.append(outcome.note)

        else:  # 방어: 미지원 action
            state.status = Status.escalated
            state.pending_escalations.append({"reason": f"미지원 action: {action.value}"})

    # max_iters 도달 등으로 여전히 running이면 임시로 stopped_stuck.
    if state.status == Status.running:
        state.status = Status.stopped_stuck

    emit(f"종료: {state.status.value}")

    if state_path is not None:
        _save_state(state, state_path)

    return state
