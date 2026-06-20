"""WO#97 — OR 통합-대안이 연루 유닛만 리셋 + #91 seeded-done 보존.

#92서 #41/#52 OR 통합-대안이 *전체* 유닛(seeded-done 포함)을 리셋 → #91 reuse 상실·병렬
머지충돌 재발 → escalate. 이 WO: 통합 실패에 *연루된* 유닛(+의존)만 리셋, 연루 안 된
seeded-done은 보존. 순수 implicated_units + 실루프(#91 재개 + 통합 OR) 통합 검증.
mock LLM/executor + 실 git worktree. 통합 gate·run-judge·OR 생성·#71·#91·ALLOWED_SANDBOXES 불변.
"""

import threading
from pathlib import Path

import yaml

from haetae.or_node import contract_consuming_siblings, implicated_units
from haetae.loop import run_loop
from haetae.models import (
    CheckReport,
    CheckType,
    GateResult,
    PlanItem,
    PlanState,
    ProjectSpec,
    State,
    Status,
    Verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

# 4유닛: u_seed(기반)·u_dnd(드래그앤드롭)·u_persist(localStorage)·u_trace(헤드리스 하니스).
# 통합 run 기준 ac_dnd/ac_persist는 unit=integration(=직접 태그 없음) — desc 고유 토큰으로 #72 매핑.
_SPEC_YAML = """\
spec_id: or-imp-001
version: 1
order_raw: "보드를 만들어줘"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - { id: ac_seed, desc: "기반 상태 모델 단위 테스트", check: { type: test, cmd: "true" }, unit: u_seed }
  - { id: ac_dnd, desc: "브라우저에서 드래그앤드롭으로 카드 이동", check: { type: run, cmd: "true" }, unit: integration }
  - { id: ac_persist, desc: "localStorage 직렬화 저장과 reload 복원", check: { type: run, cmd: "true" }, unit: integration }
assumptions: []
non_goals: ["n"]
done_when: "전부 통과"
decomposition:
  - { unit: u_seed, desc: "기반 상태 모델과 엔티티", deps: [], scope: ["src/state.ts"] }
  - { unit: u_dnd, desc: "포인터 드래그앤드롭 UI", deps: [u_seed], scope: ["src/dnd.ts"] }
  - { unit: u_persist, desc: "localStorage 저장 로드 복원", deps: [u_seed], scope: ["src/persist.ts"] }
  - { unit: u_trace, desc: "헤드리스 트레이스 진입점", deps: [u_dnd, u_persist], scope: ["scripts/trace.ts"] }
open_questions: []
"""


def _spec() -> ProjectSpec:
    return ProjectSpec.model_validate(yaml.safe_load(_SPEC_YAML))


# ──────────────────────────── 순수: implicated_units ────────────────────────────


def test_implicated_by_direct_unit_tag_26():
    """#26: ac.unit이 실제 유닛이면 그 유닛 + 전이 dependents."""
    spec = _spec()
    # ac_seed→u_seed; u_seed는 기반이라 dependents가 전부 → 전체.
    assert implicated_units(spec, {"ac_seed"}) == {"u_seed", "u_dnd", "u_persist", "u_trace"}


def test_implicated_by_distinctive_token_72():
    """#72: unit=integration criterion이 *고유 토큰*으로 feature 유닛에 매핑(+의존)."""
    spec = _spec()
    # ac_dnd desc '드래그앤드롭' → u_dnd(고유) + dependent u_trace. u_seed/u_persist 보존.
    assert implicated_units(spec, {"ac_dnd"}) == {"u_dnd", "u_trace"}
    # ac_persist desc 'localStorage' → u_persist + u_trace. u_seed/u_dnd 보존.
    assert implicated_units(spec, {"ac_persist"}) == {"u_persist", "u_trace"}


def test_implicated_harness_excluded_from_owner_but_is_dependent():
    """하니스(트레이스) 유닛은 owner 매칭서 제외 — 단 연루 유닛의 dependent로 리셋된다."""
    spec = _spec()
    imp = implicated_units(spec, {"ac_dnd"})
    assert "u_trace" in imp           # dependent로 포함
    # 하니스가 owner로 광범위 매칭되지 않음(ac_dnd가 u_trace를 *직접* owner로 잡지 않음):
    # u_persist는 미연루(드래그앤드롭과 무관) → 보존
    assert "u_persist" not in imp


def test_implicated_unmappable_returns_none_fallback():
    """매핑 불가(없는 ac / 토큰 매칭 0) → None → 호출부 전체 리셋 폴백."""
    spec = _spec()
    assert implicated_units(spec, {"does-not-exist"}) is None
    assert implicated_units(spec, set()) is None
    assert implicated_units(spec, None) is None


def test_implicated_no_raise_on_weird_input():
    """예외 흡수: 이상한 spec이어도 raise 없이 None."""
    assert implicated_units(object(), {"x"}) is None
    assert implicated_units(None, {"x"}) is None


# ──────────────────────── 순수: contract_consuming_siblings (WO#123, #97 확장) ────────────────────────

# scope-겹침 케이스용 spec(_SPEC_YAML은 scope 전부 disjoint라 별도).
_OVERLAP_YAML = """\
spec_id: ccs-001
version: 1
order_raw: x
goal: g
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - { id: ac1, desc: d, check: { type: test, cmd: "true" } }
assumptions: []
non_goals: [n]
done_when: d
decomposition:
  - { unit: u_a, desc: a, deps: [], scope: ["src/shared.ts", "src/a.ts"] }
  - { unit: u_b, desc: b, deps: [], scope: ["src/shared.ts", "src/b.ts"] }
  - { unit: u_c, desc: c, deps: [], scope: ["src/c.ts"] }
open_questions: []
"""


def _overlap_spec() -> ProjectSpec:
    return ProjectSpec.model_validate(yaml.safe_load(_OVERLAP_YAML))


def test_consumer_by_scope_overlap_72():
    """#72: 형제 scope가 unit scope와 파일 공유(src/shared.ts) → 소비 형제. 무관(u_c) 보존."""
    spec = _overlap_spec()
    assert contract_consuming_siblings(spec, "u_a", ["u_b", "u_c"]) == {"u_b"}
    assert contract_consuming_siblings(spec, "u_b", ["u_a", "u_c"]) == {"u_a"}


def test_consumer_by_direct_dep_either_direction_no_transitive():
    """직접 선언 의존(양방향) → 소비 형제. *전이*는 제외(기반 유닛 과다-리셋 방지)."""
    spec = _spec()
    # u_dnd·u_persist는 u_seed에 *직접* 의존 → u_seed 재작성의 소비자. u_trace는 u_seed에
    # 직접 의존 아님(u_dnd/u_persist 경유=전이) → 헬퍼는 미식별(전이 여파는 호출부 cut이 처리).
    assert contract_consuming_siblings(spec, "u_seed", ["u_dnd", "u_persist", "u_trace"]) == {
        "u_dnd",
        "u_persist",
    }


def test_consumer_build_error_file_attribution():
    """(c) conflict_files를 scope로 *소유*한 형제 → 소비자(의존·scope-겹침 없어도)."""
    spec = _spec()
    # u_persist(src/persist.ts)·u_dnd 사이엔 직접 의존·scope 겹침 없음 → conflict_files로만 귀속.
    assert contract_consuming_siblings(
        spec, "u_dnd", ["u_persist"], conflict_files=["src/persist.ts"]
    ) == {"u_persist"}
    # 귀속 파일이 아무 형제 scope도 아니면 미식별.
    assert contract_consuming_siblings(
        spec, "u_dnd", ["u_persist"], conflict_files=["src/nobody.ts"]
    ) == set()


def test_consumer_excludes_self_and_unmerged():
    """`unit` 자신 제외 · 머지 형제 없으면 빈 집합(미머지는 어차피 재빌드 예정)."""
    spec = _spec()
    assert contract_consuming_siblings(spec, "u_dnd", ["u_dnd"]) == set()  # self 제외
    assert contract_consuming_siblings(spec, "u_dnd", []) == set()
    assert contract_consuming_siblings(spec, "u_dnd", None) == set()


def test_consumer_unrelated_preserved():
    """의존·scope·귀속 무관 형제는 소비자 아님 → 보존(좁은 식별)."""
    spec = _spec()
    # u_persist vs u_dnd: 둘 다 u_seed 의존(서로 직접 의존 아님)·scope disjoint → 미식별.
    assert contract_consuming_siblings(spec, "u_dnd", ["u_persist"]) == set()


def test_consumer_no_raise_on_weird_input():
    """예외 흡수: 이상한 입력이어도 raise 없이 빈 집합(graceful 폴백)."""
    assert contract_consuming_siblings(None, "u1", ["u2"]) == set()
    assert contract_consuming_siblings(object(), "u1", ["u2"]) == set()
    assert contract_consuming_siblings(_spec(), "does-not-exist", ["u_dnd"]) == set()


# ──────────────────────────── 실루프: #91 재개 + 통합 OR (연루만 리셋·seeded 보존) ────────────────────────────

_DEC = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: placeholder
  goal: "구현"
  deliverable: "요약"
"""


class _ReplanOnlyClient:
    """순수 재개라 synthesize 미호출 — replan(_DEC)만."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, system, user, **opts):
        self.calls.append({"system": system, "user": user})
        return _DEC


def _record_factory(built, lock):
    class _Rec:
        def __init__(self, wt):
            self.wt = Path(wt)

        def run(self, order):
            with lock:
                built.append(order.unit)
            return f"{order.unit} built"

    return lambda wt: _Rec(wt)


class _PassGate:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.pass_)


class _IntegFailDnDThenPass:
    """통합 gate: 1차는 ac_dnd fail(checks 동봉) → 2차부터 pass. (main 스레드 직렬 — lock 불필요)"""

    def __init__(self):
        self.n = 0

    def judge(self, result, spec, unit=None):
        self.n += 1
        if self.n == 1:
            return GateResult(
                verdict=Verdict.fail_recoverable,
                checks=[CheckReport(ac_id="ac_dnd", check_type=CheckType.run, status="fail")],
            )
        return GateResult(verdict=Verdict.pass_)


def _resume_state() -> State:
    return State(
        spec_ref="or-imp-001", spec_version=1, status=Status.stopped_budget,
        plan=[
            PlanItem(unit="u_seed", state=PlanState.done),       # 부모서 검증 (seeded)
            PlanItem(unit="u_dnd", state=PlanState.pending),
            PlanItem(unit="u_persist", state=PlanState.pending),
            PlanItem(unit="u_trace", state=PlanState.pending),
        ],
    )


def test_integration_or_resets_only_implicated_preserves_seeded_done(tmp_path):
    """#92 재현 fix: 통합 ac_dnd fail → OR이 연루(u_dnd,u_trace)만 리셋, seeded-done u_seed +
    미연루 u_persist 보존 → reuse 유지·done 재빌드 최소."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()

    state = run_loop(
        "보드를 만들어줘", _ReplanOnlyClient(), executor=None, gate=_IntegFailDnDThenPass(),
        executor_factory=_record_factory(built, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, resume_spec=_spec(), resume_state=_resume_state(),
        or_alternatives=1, unit_retries=0,
    )
    assert state.status is Status.done
    # seeded-done u_seed: 한 번도 빌드 안 됨(시드 보존 — OR 리셋서도 제외).
    assert "u_seed" not in built, f"seeded-done u_seed는 보존돼야(빌드 0): {built}"
    # 연루 안 된 u_persist: 최초 1회만(OR 리셋 제외 → 재빌드 0).
    assert built.count("u_persist") == 1, f"미연루 u_persist 재빌드되면 안 됨: {built}"
    # 연루된 u_dnd: 최초 + OR 재빌드 = 2회 이상.
    assert built.count("u_dnd") >= 2, f"연루 u_dnd는 재빌드돼야: {built}"
    # 연루 dependent u_trace: 최초 + OR 재빌드 = 2회 이상.
    assert built.count("u_trace") >= 2, f"연루 dependent u_trace는 재빌드돼야: {built}"
    # OR 통합 대안 기록(투명성).
    assert any(a.scope == "integration" and a.outcome == "abandoned" for a in state.approaches)


def test_integration_or_full_reset_fallback_when_no_checks(tmp_path):
    """폴백(back-compat): 통합 gate가 checks 없이 fail → 연루 판정 불가 → 전체 리셋(기존 동작)."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()

    class _IntegNoChecks:
        def __init__(self): self.n = 0
        def judge(self, result, spec, unit=None):
            self.n += 1
            return GateResult(verdict=Verdict.fail_recoverable if self.n == 1 else Verdict.pass_)

    state = run_loop(
        "보드를 만들어줘", _ReplanOnlyClient(), executor=None, gate=_IntegNoChecks(),
        executor_factory=_record_factory(built, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, resume_spec=_spec(), resume_state=_resume_state(),
        or_alternatives=1, unit_retries=0,
    )
    assert state.status is Status.done
    # checks 없음 → implicated None → 전체 리셋 폴백: 연루 안정 판정 불가라 u_persist도 재빌드(보수적).
    assert built.count("u_persist") >= 2, f"폴백은 전체 리셋(기존 동작)이어야: {built}"
    # u_seed는 #91 seeded-done이라 폴백 전체 리셋서도 시드 상태 유지(plan done) — 빌드는 될 수 있음(폴백).
    assert any(a.scope == "integration" for a in state.approaches)


def test_anti_erosion_preserved_unit_criteria_byte_identical(tmp_path):
    """보존된 seeded-done 유닛의 criteria는 부모 spec 그대로(리셋 범위만 좁힘 — bar 무관)."""
    spec = _spec()
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("x", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "보드를 만들어줘", _ReplanOnlyClient(), executor=None, gate=_IntegFailDnDThenPass(),
        executor_factory=_record_factory(built, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        state_path=tmp_path / "state.yaml",
        seeded=True, resume_spec=spec, resume_state=_resume_state(),
        or_alternatives=1, unit_retries=0,
    )
    assert state.status is Status.done
    # 저장된 spec.yaml의 criteria == 입력 spec criteria(보존, byte 동일 — OR이 바를 안 건드림).
    saved = ProjectSpec.from_yaml(tmp_path / "spec.yaml")
    assert saved.acceptance_criteria == spec.acceptance_criteria
    assert saved.done_when == spec.done_when
