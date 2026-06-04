"""결정적 DAG 스케줄러 — plan(units + deps)에서 ready set을 계산한다.

순수 함수만 둔다(LLM·subprocess·동시성 없음). 동시성 비결정성은 호출부(loop)가
ready set을 정렬된 순서로 dispatch하고 이벤트를 결정적으로 정렬해 봉인한다.
여기서는 "지금 돌릴 수 있는 unit이 무엇인가"만 결정적으로 답한다.
"""

from __future__ import annotations

from haetae.models import PlanItem, PlanState


def ready_units(plan: list[PlanItem], in_flight: set[str]) -> list[str]:
    """deps가 전부 done이고, 자신은 미완(pending)이며, in-flight도 아닌 unit들.

    - deps 전부 done && self pending && not in_flight → ready.
    - failed/in_progress/done은 ready가 아니다(재dispatch는 loop가 pending으로 되돌린다).
    - 반환은 **unit id 사전순 정렬** — 동시 dispatch라도 brain 호출/처리 순서를 결정적으로.
    """
    done = {p.unit for p in plan if p.state == PlanState.done}
    ready: list[str] = []
    for p in plan:
        if p.state != PlanState.pending:
            continue
        if p.unit in in_flight:
            continue
        deps = p.deps or []
        if all(d in done for d in deps):
            ready.append(p.unit)
    return sorted(ready)


def all_done(plan: list[PlanItem]) -> bool:
    """plan이 비어있지 않고 모든 unit이 done인가."""
    return bool(plan) and all(p.state == PlanState.done for p in plan)


def is_stuck(plan: list[PlanItem], in_flight: set[str]) -> bool:
    """진행 불가 상태: in-flight도 ready도 없는데 아직 done이 아닌 unit이 남음.

    deps가 failed unit에 막혔거나(영영 done 안 됨) 모든 잔여가 failed인 경우.
    """
    if in_flight:
        return False
    if ready_units(plan, in_flight):
        return False
    return not all_done(plan)
