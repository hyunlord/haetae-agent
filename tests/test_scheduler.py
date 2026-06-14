"""결정적 DAG 스케줄러 테스트 — 순수 함수(ready_units/all_done/is_stuck/is_disjoint_from)."""

from haetae.models import PlanItem, PlanState
from haetae.scheduler import all_done, is_disjoint_from, is_stuck, ready_units


def _plan(spec: dict[str, tuple[str, list[str]]]) -> list[PlanItem]:
    """{unit: (state, deps)} → PlanItem 리스트."""
    return [
        PlanItem(unit=u, state=PlanState(st), deps=(deps or None))
        for u, (st, deps) in spec.items()
    ]


def test_no_deps_all_pending_are_ready():
    plan = _plan({"u1": ("pending", []), "u2": ("pending", [])})
    assert ready_units(plan, set()) == ["u1", "u2"]


def test_ready_is_sorted_for_determinism():
    # 삽입 순서가 뒤섞여도 unit-id 사전순으로 정렬된다(동시 dispatch 결정성).
    plan = _plan({"b": ("pending", []), "a": ("pending", []), "c": ("pending", [])})
    assert ready_units(plan, set()) == ["a", "b", "c"]


def test_dep_not_done_blocks_unit():
    plan = _plan({"u1": ("pending", []), "u2": ("pending", ["u1"])})
    # u1 미완 → u2는 ready 아님
    assert ready_units(plan, set()) == ["u1"]


def test_dep_done_unblocks_unit():
    plan = _plan({"u1": ("done", []), "u2": ("pending", ["u1"])})
    assert ready_units(plan, set()) == ["u2"]


def test_in_flight_excluded():
    plan = _plan({"u1": ("pending", []), "u2": ("pending", [])})
    assert ready_units(plan, {"u1"}) == ["u2"]


def test_in_progress_and_done_and_failed_not_ready():
    plan = _plan({
        "u1": ("in_progress", []),
        "u2": ("done", []),
        "u3": ("failed", []),
        "u4": ("pending", []),
    })
    assert ready_units(plan, set()) == ["u4"]


def test_all_done():
    assert all_done(_plan({"u1": ("done", []), "u2": ("done", [])})) is True
    assert all_done(_plan({"u1": ("done", []), "u2": ("pending", [])})) is False
    assert all_done([]) is False  # 빈 plan은 done 아님


def test_is_stuck_when_dep_failed():
    # u1 failed → u2(=deps u1)는 영영 ready 안 됨, in-flight도 없음 → stuck
    plan = _plan({"u1": ("failed", []), "u2": ("pending", ["u1"])})
    assert is_stuck(plan, set()) is True


def test_not_stuck_with_inflight_or_ready():
    plan = _plan({"u1": ("pending", []), "u2": ("pending", ["u1"])})
    assert is_stuck(plan, set()) is False  # u1 ready
    assert is_stuck(plan, {"u1"}) is False  # u1 in-flight


def test_not_stuck_when_all_done():
    assert is_stuck(_plan({"u1": ("done", [])}), set()) is False


# ──────────────────── is_disjoint_from (WO#110 disjoint burst 술어) ────────────────────


def test_disjoint_scopes_are_disjoint():
    scope_of = {"u1": ["a.ts"], "u2": ["b.ts"], "u3": ["c.ts"]}
    assert is_disjoint_from("u3", {"u1", "u2"}, scope_of) is True


def test_overlapping_scope_not_disjoint():
    scope_of = {"u1": ["a.ts", "shared.ts"], "u2": ["shared.ts"]}
    assert is_disjoint_from("u2", {"u1"}, scope_of) is False


def test_missing_own_scope_not_disjoint():
    # 자기 scope 미선언 → 미입증 → 보수적 False(burst 불가).
    scope_of = {"u1": ["a.ts"], "u2": []}
    assert is_disjoint_from("u2", {"u1"}, scope_of) is False


def test_missing_other_scope_not_disjoint():
    # 상대가 scope 미선언 → 그 상대와 겹치는지 입증 불가 → 보수적 False.
    scope_of = {"u1": [], "u2": ["b.ts"]}
    assert is_disjoint_from("u2", {"u1"}, scope_of) is False


def test_disjoint_from_empty_inflight_with_own_scope():
    # in-flight 없음 + 자기 scope 선언 → vacuously disjoint(첫 burst 후보).
    assert is_disjoint_from("u1", set(), {"u1": ["a.ts"]}) is True


def test_exact_string_match_no_fuzzy():
    # 정확-문자열 매칭(퍼지/glob 없음 — intake._scope_overlaps와 동형). 다른 문자열은 겹침 아님.
    scope_of = {"u1": ["src/a.ts"], "u2": ["src/a.tsx"]}
    assert is_disjoint_from("u2", {"u1"}, scope_of) is True
