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


def is_disjoint_from(
    unit: str, others: set[str], scope_of: dict[str, list[str]]
) -> bool:
    """unit의 file-scope가 `others`(보통 in-flight 유닛들) 전부와 *입증된 disjoint*인가 (WO#110, OMC #1).

    disjoint 병렬 burst의 안전 술어 — 보수적 cap의 *주 이유는 머지충돌 리스크*인데, scope가
    입증된 disjoint면 그 리스크가 부재하므로 base cap을 넘겨 burst해도 안전하다. 입증 기준은
    intake._scope_overlaps(#59/#72)와 동형으로 보수적:
      - **양쪽 다 scope 선언** + **정확-문자열 겹침 0** → 입증된 disjoint(True).
      - 한쪽이라도 scope 미선언 → *미입증* → False(보수적: burst 안 함, 기존 cap 유지).
      - 어느 상대와든 scope 겹침 → 충돌 리스크 → False.
    퍼지/glob 매칭 없음(오탐 회피 — ready 유닛은 deps가 이미 충족돼 서로 의존 없으므로 충돌
    리스크는 *오직 file-scope 겹침*뿐, 이 술어가 그것만 본다). 순수 함수(결정적·부작용 0).

    **충돌 backstop 불변**: 이 판정이 (모델이 잘못 단 scope로) 틀려 실제 머지충돌이 나도
    #21 serialize-on-conflict·#48 충돌적응 재빌드가 그대로 잡는다 — burst는 안전망 위의 최적화다.
    """
    su = set(scope_of.get(unit) or [])
    if not su:
        return False  # scope 미선언 = 미입증 → 보수적(burst 불가)
    for o in others:
        so = set(scope_of.get(o) or [])
        if not so:
            return False  # 상대 미선언 → 미입증(보수적)
        if su & so:
            return False  # scope 겹침 → 충돌 리스크 → burst 불가
    return True


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
