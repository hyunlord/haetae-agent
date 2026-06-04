"""loop driver — synthesize → replan → dispatch → gate → 반복.

손으로 돌리던 director 루프(1~6번)의 코드판. 이번 WO는 executor/gate를 주입 가능한
Protocol + mock으로 두고 *오케스트레이션 흐름*만 증명한다.
(budget/stuck 정식 처리, governed spec-change 적용, 실제 executor/gate 어댑터는 이후 WO.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from haetae import intake, replan as replan_mod
from haetae.intake import SynthesisError, synthesize
from haetae.llm import LLMClient
from haetae.models import (
    Action,
    Event,
    NextOrder,
    PlanItem,
    PlanState,
    ProjectSpec,
    State,
    Status,
    Verdict,
)
from haetae.replan import ReplanError, replan


# ──────────────────────────── 주입 인터페이스 ────────────────────────────


@runtime_checkable
class Executor(Protocol):
    """work order를 실행하고 결과 요약 텍스트를 반환한다."""

    def run(self, order: NextOrder) -> str: ...


@runtime_checkable
class Gate(Protocol):
    """실행 결과를 spec 기준으로 판정해 Verdict를 반환한다."""

    def judge(self, result: str, spec: ProjectSpec) -> Verdict: ...


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
    """테스트용. 스크립트된 Verdict를 순서대로(소진 시 마지막을 반복) 반환."""

    def __init__(self, verdicts: list[Verdict] | Verdict):
        self._v = [verdicts] if isinstance(verdicts, Verdict) else list(verdicts)
        self._i = 0
        self.calls: list[str] = []

    def judge(self, result: str, spec: ProjectSpec) -> Verdict:
        self.calls.append(result)
        v = self._v[min(self._i, len(self._v) - 1)]
        self._i += 1
        return v


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
) -> State:
    """주문 한 줄에서 종료 상태까지 루프를 돈다. 최종 State를 반환(필요시 저장).

    내성(WO#12): LLM 출력 하나로 루프가 crash하지 않는다.
      - 합성(synthesize) 실패: traceback 대신 escalated State 반환.
      - replan 출력 검증 실패: 최대 replan_retries회 재시도(직전 에러를 피드백으로
        얹어 self-correction 유도), 소진 시 escalate하고 루프 종료(예외 전파 안 함).
    """
    syn_prompt = (
        Path(prompt_dir) / "synthesizer.md" if prompt_dir else intake.DEFAULT_PROMPT_PATH
    )
    rep_prompt = (
        Path(prompt_dir) / "replan.md" if prompt_dir else replan_mod.DEFAULT_PROMPT_PATH
    )

    try:
        spec = synthesize(order, client, prompt_path=syn_prompt)
    except SynthesisError as e:
        state = _escalated_no_spec(
            "spec 합성 실패 (synthesize 출력 검증 불통과)", e.raw_response
        )
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
            result = executor.run(no)
            verdict = gate.judge(result, spec)
            state.events.append(
                Event(
                    seq=len(state.events) + 1,
                    unit=no.unit,
                    work_order_ref=no.goal,
                    result=result,
                    verdict=verdict,
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
            # 이번 WO는 governed 적용 미구현 → escalate로 라우팅(다음 WO에서 정식 처리).
            state.status = Status.escalated
            note: dict = {"reason": "propose_spec_change (governed 적용은 다음 WO)"}
            if decision.spec_change is not None:
                note["spec_change"] = decision.spec_change.model_dump(
                    by_alias=True, mode="json"
                )
            state.pending_escalations.append(note)

        else:  # 방어: 미지원 action
            state.status = Status.escalated
            state.pending_escalations.append({"reason": f"미지원 action: {action.value}"})

    # max_iters 도달 등으로 여전히 running이면 임시로 stopped_stuck.
    if state.status == Status.running:
        state.status = Status.stopped_stuck

    if state_path is not None:
        _save_state(state, state_path)

    return state
