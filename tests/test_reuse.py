"""②b 깊은 증분 — 검증된 부모 유닛 명시적 재사용 (WO#71).

continue-from에서 부모 done·바 불변 유닛을 done으로 시드해 재빌드를 생략(delta DAG)하고,
바 바뀐 유닛은 재사용 거부·재빌드+재gate(anti-erosion). 통합 gate는 항상 최종 결과에 실행.
mock LLM/executor/gate + 실 git worktree. gate/judge 판정·ALLOWED_SANDBOXES 불변.
"""

import threading
from pathlib import Path

from haetae.intake import unit_bar_signature
from haetae.loop import ReuseDecision, evaluate_reuse, run_loop
from haetae.models import (
    GateResult,
    PlanState,
    ProjectSpec,
    State,
    Status,
    Verdict,
)
from haetae.run import build_reuse_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


# ──────────────────────────── 순수: evaluate_reuse / manifest / signature ────────────────────────────


def _spec(units_yaml: str, acs_yaml: str) -> ProjectSpec:
    import yaml as _yaml

    doc = f"""\
spec_id: s
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
{acs_yaml}
assumptions: []
non_goals: ["n"]
done_when: "전부 통과"
decomposition:
{units_yaml}
open_questions: []
"""
    return ProjectSpec.model_validate(_yaml.safe_load(doc))


_AC_U1 = '  - { id: ac1, desc: "u1 기능", check: { type: test, cmd: "true" }, unit: u1 }'
_AC_U2 = '  - { id: ac2, desc: "u2 기능", check: { type: test, cmd: "true" }, unit: u2 }'


def test_unit_bar_signature_criteria_and_scope():
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [], scope: ["src/u1.py"] }', _AC_U1
    )
    sig = unit_bar_signature(spec, "u1")
    assert sig["scope"] == ["src/u1.py"]
    assert sig["criteria"] == [("ac1", "u1 기능", "test", "true", None)]


def test_build_reuse_manifest_only_done_units():
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [] }\n  - { unit: u2, desc: b, deps: [] }',
        f"{_AC_U1}\n{_AC_U2}",
    )
    state = State(
        spec_ref="s", spec_version=1, status=Status.escalated,
        plan=[
            __import__("haetae.models", fromlist=["PlanItem"]).PlanItem(unit="u1", state=PlanState.done),
            __import__("haetae.models", fromlist=["PlanItem"]).PlanItem(unit="u2", state=PlanState.failed),
        ],
    )
    manifest = build_reuse_manifest(spec, state)
    assert set(manifest) == {"u1"}  # done만 — failed는 재사용 후보 아님(crashed-parent graceful)


def test_build_reuse_manifest_no_parent_spec_empty():
    assert build_reuse_manifest(None, None) == {}


def test_evaluate_reuse_unchanged_reuses():
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [], scope: ["src/u1.py"], reuse_of: "u1" }', _AC_U1
    )
    manifest = {"u1": unit_bar_signature(spec, "u1")}  # 동일 지문
    decisions = evaluate_reuse(spec, manifest)
    assert len(decisions) == 1 and decisions[0].reused is True
    assert decisions[0].unit == "u1" and decisions[0].parent == "u1"


def test_evaluate_reuse_changed_criteria_rebuilds():
    """anti-erosion: criteria 변경 → 재사용 거부(재빌드)."""
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [], scope: ["src/u1.py"], reuse_of: "u1" }', _AC_U1
    )
    # 부모 지문은 *다른* criteria(desc 변경) → mismatch
    parent_sig = {"criteria": [("ac1", "다른 기능", "test", "true", None)], "scope": ["src/u1.py"]}
    decisions = evaluate_reuse(spec, {"u1": parent_sig})
    assert len(decisions) == 1 and decisions[0].reused is False
    assert "anti-erosion" in decisions[0].reason


def test_evaluate_reuse_changed_scope_rebuilds():
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [], scope: ["src/NEW.py"], reuse_of: "u1" }', _AC_U1
    )
    parent_sig = {"criteria": [("ac1", "u1 기능", "test", "true", None)], "scope": ["src/u1.py"]}
    decisions = evaluate_reuse(spec, {"u1": parent_sig})
    assert decisions[0].reused is False


def test_evaluate_reuse_parent_not_done_rebuilds():
    """crashed-parent: 부모 manifest에 없음(non-done) → 재사용 거부."""
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [], reuse_of: "u1" }', _AC_U1
    )
    decisions = evaluate_reuse(spec, {})  # 부모 done 없음
    assert decisions[0].reused is False and "미검증" in decisions[0].reason


def test_evaluate_reuse_no_marker_no_decision():
    spec = _spec('  - { unit: u1, desc: a, deps: [] }', _AC_U1)
    assert evaluate_reuse(spec, {"u1": {}}) == []  # reuse_of 없음 → 결정 없음(정상 빌드)


def test_evaluate_reuse_off_or_no_manifest():
    spec = _spec(
        '  - { unit: u1, desc: a, deps: [], reuse_of: "u1" }', _AC_U1
    )
    sig = {"u1": unit_bar_signature(spec, "u1")}
    assert evaluate_reuse(spec, sig, reuse_on=False) == []  # --no-reuse
    assert evaluate_reuse(spec, None) == []                  # continue-from 아님


# ──────────────────────────── 병렬 통합: 재사용 skip + delta DAG ────────────────────────────


_DEC = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: placeholder
  goal: "구현"
  deliverable: "요약"
"""


class _BrainClient:
    """call#1=합성(주어진 child spec) / 이후=replan(DEC)."""

    def __init__(self, spec_yaml: str):
        self.spec = spec_yaml
        self.n = 0

    def complete(self, system, user, **opts):
        self.n += 1
        return self.spec if self.n == 1 else _DEC


class _PassGate:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.pass_)


def _record_factory(built, seed_seen, lock):
    """executor_factory(wt) — 빌드된 unit + 그 worktree에 시딩 코드 존재 여부 기록."""

    class _Rec:
        def __init__(self, wt):
            self.wt = Path(wt)

        def run(self, order):
            with lock:
                built.append(order.unit)
                seed_seen[order.unit] = (self.wt / "seed.txt").is_file()
            return f"{order.unit} built"

    return lambda wt: _Rec(wt)


# 부모 u1과 동일한 child u1(reuse_of) + 신규 u2(deps=[u1]).
_CHILD_SPEC = """\
spec_id: child-001
version: 2
order_raw: "x"
goal: "g+delta"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - { id: ac1, desc: "u1 기능", check: { type: test, cmd: "true" }, unit: u1 }
  - { id: ac2, desc: "u2 기능", check: { type: test, cmd: "true" }, unit: u2 }
assumptions: []
non_goals: ["n"]
done_when: "전부 통과"
decomposition:
  - { unit: u1, desc: a, deps: [], scope: ["src/u1.py"], reuse_of: "u1" }
  - { unit: u2, desc: b, deps: [u1], scope: ["src/u2.py"] }
open_questions: []
"""


def _parent_manifest_matching():
    """child u1과 *동일* 지문의 부모 manifest(재사용 매칭되도록)."""
    parent = _spec(
        '  - { unit: u1, desc: a, deps: [], scope: ["src/u1.py"] }', _AC_U1
    )
    return {"u1": unit_bar_signature(parent, "u1")}


def test_reuse_skips_rebuild_and_downstream_builds_on_seeded(tmp_path):
    """재사용 skip: 부모 done·불변 u1 → done 시드 → 빌드 0; u2만 빌드(시딩 main 위)."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")  # 부모서 시딩된 코드

    built: list[str] = []
    seed_seen: dict[str, bool] = {}
    lock = threading.Lock()
    state = run_loop(
        "세일 모드 추가", _BrainClient(_CHILD_SPEC), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, seed_seen, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, reuse_manifest=_parent_manifest_matching(),
    )
    assert state.status is Status.done
    # u1 재사용 → 빌드 안 함(토큰 재소모 0). u2만 빌드.
    assert built == ["u2"], f"u1은 재빌드되면 안 됨: {built}"
    # u1 plan은 done으로 시드됨
    by_state = {p.unit: p.state for p in state.plan}
    assert by_state["u1"] is PlanState.done and by_state["u2"] is PlanState.done
    # 하류 u2는 *시딩된 main* 위에서 빌드됨(worktree에 seed.txt 존재)
    assert seed_seen.get("u2") is True, "u2 worktree가 시딩된 부모 코드를 상속해야 한다"


def test_reuse_records_transparency_event(tmp_path):
    """투명성: 재사용 결정이 이벤트(stage=reuse)로 기록."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("x", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "x", _BrainClient(_CHILD_SPEC), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, reuse_manifest=_parent_manifest_matching(),
    )
    reuse_evs = [e for e in state.events if e.stage == "reuse"]
    assert len(reuse_evs) == 1 and reuse_evs[0].unit == "u1"
    assert reuse_evs[0].verdict is Verdict.pass_
    assert "reuse_of=u1" in (reuse_evs[0].work_order_ref or "")
    # transition에도 reuse 기록
    assert any(t.stage == "reuse" and t.unit == "u1" for t in state.transitions)


def test_reuse_integration_gate_still_runs(tmp_path):
    """통합 게이트 보존: 재사용+신규 섞인 run서 통합 gate가 최종 결과에 실행됨."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("x", encoding="utf-8")
    lock = threading.Lock()
    state = run_loop(
        "x", _BrainClient(_CHILD_SPEC), executor=None, gate=_PassGate(),
        executor_factory=_record_factory([], {}, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, reuse_manifest=_parent_manifest_matching(),
    )
    # 통합 event(unit=None)가 최종 결과에 실린다(개별 재사용≠통합 생략).
    integ = [e for e in state.events if e.unit is None]
    assert integ, "통합 gate event가 있어야 한다"
    assert integ[-1].work_order_ref == "(integration)"


# 바 바뀐 child u1(criteria desc 변경) → 재사용 거부 → 재빌드.
_CHILD_SPEC_CHANGED = _CHILD_SPEC.replace('desc: "u1 기능"', 'desc: "u1 기능 대폭 변경"')


def test_anti_erosion_changed_criteria_rebuilds(tmp_path):
    """anti-erosion: 새 합성이 u1 criteria 변경 → 재사용 거부 → 재빌드+재gate(도장 금지)."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("x", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "x", _BrainClient(_CHILD_SPEC_CHANGED), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, reuse_manifest=_parent_manifest_matching(),
    )
    assert state.status is Status.done
    # u1이 *재빌드*됨(부모 통과로 도장 안 함) — 둘 다 빌드.
    assert set(built) == {"u1", "u2"}, f"바 변경된 u1은 재빌드돼야 함: {built}"
    # rebuild transition 기록(투명성)
    assert any(t.stage == "rebuild" and t.unit == "u1" for t in state.transitions)
    # reuse 이벤트는 없다(도장 안 함)
    assert [e for e in state.events if e.stage == "reuse"] == []


def test_crashed_parent_non_done_rebuilds(tmp_path):
    """crashed-parent: 부모 u1이 done 아님(manifest 없음) → 재사용 안 됨(재빌드)."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("x", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "x", _BrainClient(_CHILD_SPEC), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, reuse_manifest={},  # 부모 done 유닛 없음(crashed/half)
    )
    assert state.status is Status.done
    assert set(built) == {"u1", "u2"}  # u1도 재빌드(검증 가능 done 아님)
    assert any(t.stage == "rebuild" and t.unit == "u1" for t in state.transitions)


def test_no_reuse_flag_rebuilds_all(tmp_path):
    """--no-reuse(escape): manifest 있어도 재사용 끔 → 전부 재빌드."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("x", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "x", _BrainClient(_CHILD_SPEC), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, reuse_manifest=_parent_manifest_matching(), reuse=False,
    )
    assert state.status is Status.done
    assert set(built) == {"u1", "u2"}  # 재사용 off → u1도 빌드
    assert [e for e in state.events if e.stage == "reuse"] == []


def test_back_compat_no_manifest_no_reuse(tmp_path):
    """back-compat: reuse_manifest 없으면(continue-from 아님) reuse_of 라벨 있어도 무변경(정상 빌드)."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "x", _BrainClient(_CHILD_SPEC), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        # seeded/reuse_manifest 없음 = greenfield 경로
    )
    assert state.status is Status.done
    assert set(built) == {"u1", "u2"}  # manifest 없으면 reuse_of 무시 → 전부 빌드
    assert [e for e in state.events if e.stage == "reuse"] == []
