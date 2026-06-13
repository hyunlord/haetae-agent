"""WO#91 — continue-from 안정 재개: 부모 plan/criteria 보존(재합성 skip → #71 reuse 살림).

순수 재개(같은 order로 미완 run 마저 끝내기)는 부모 spec.yaml/state.yaml을 *직접 로드*해
재합성을 skip한다 → done 유닛 보존(시드)·미완만 *부모 criteria 그대로* 재빌드(rebuild-all 회피).
증분 연속(새 order)은 기존 재합성 경로 그대로(back-compat). 부모 spec.yaml 없음/--resynthesize는
증분 폴백. mock LLM/executor/gate + 실 git worktree. 통합 gate·#71 reuse-match·ALLOWED_SANDBOXES 불변.
"""

import json
import subprocess
import threading
from pathlib import Path

import yaml

import haetae.loop as loop_mod
import haetae.run as run_mod
from haetae.intake import unit_bar_signature
from haetae.loop import run_loop
from haetae.models import (
    GateResult,
    PlanItem,
    PlanState,
    ProjectSpec,
    State,
    Status,
    Verdict,
)
from haetae.run import ResumePlan, is_pure_resume, load_resume

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

# 부모 spec(3 유닛, 독립). order_raw가 부모 order의 폴백 비교 기준이 된다.
_RESUME_SPEC_YAML = """\
spec_id: parent-001
version: 1
order_raw: "보드 게임을 만들어라"
goal: "보드 게임 엔진+UI"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - { id: ac1, desc: "u1 기능", check: { type: test, cmd: "true" }, unit: u1 }
  - { id: ac2, desc: "u2 기능", check: { type: test, cmd: "true" }, unit: u2 }
  - { id: ac3, desc: "u3 기능", check: { type: test, cmd: "true" }, unit: u3 }
assumptions: []
non_goals: ["n"]
done_when: "ac1·ac2·ac3 전부 통과"
decomposition:
  - { unit: u1, desc: a, deps: [], scope: ["src/u1.py"] }
  - { unit: u2, desc: b, deps: [], scope: ["src/u2.py"] }
  - { unit: u3, desc: c, deps: [], scope: ["src/u3.py"] }
open_questions: []
"""

# 부모 state.yaml: u1·u2 done, u3 미완(pending) — 미완만 재빌드돼야 한다.
_PARENT_STATE_YAML = (
    "spec_ref: parent-001\nspec_version: 1\nstatus: stopped_budget\n"
    "plan:\n"
    "  - {unit: u1, state: done}\n"
    "  - {unit: u2, state: done}\n"
    "  - {unit: u3, state: pending}\n"
)

# replan 결정(합성이 아니라 replan만 — 순수 재개서 brain은 replan에만 쓰인다).
_DEC = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: placeholder
  goal: "구현"
  deliverable: "요약"
"""


def _spec_from_yaml(text: str) -> ProjectSpec:
    return ProjectSpec.model_validate(yaml.safe_load(text))


def _parent_state() -> State:
    return State(
        spec_ref="parent-001", spec_version=1, status=Status.stopped_budget,
        plan=[
            PlanItem(unit="u1", state=PlanState.done),
            PlanItem(unit="u2", state=PlanState.done),
            PlanItem(unit="u3", state=PlanState.pending),
        ],
    )


class _ReplanOnlyClient:
    """순수 재개: synthesize는 절대 안 불려야 한다 → replan(_DEC)만 돌려준다.

    합성이 (잘못) 호출되면 _DEC가 spec으로 파싱돼 SynthesisError→escalate → 테스트가 잡는다.
    (synthesize 스파이 단언과 더불어 이중으로 '재합성 0'을 보장.)
    """

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, system, user, **opts):
        self.calls.append({"system": system, "user": user})
        return _DEC


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


def _git(cmd, cwd):
    subprocess.run(["git", *cmd], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_parent_repo(parent_work: Path) -> None:
    parent_work.mkdir(parents=True, exist_ok=True)
    (parent_work / "src").mkdir(exist_ok=True)
    (parent_work / "src" / "u1.py").write_text("x = 1\n", encoding="utf-8")
    (parent_work / "seed.txt").write_text("parent code\n", encoding="utf-8")
    (parent_work / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(["init", "-q"], parent_work)
    _git(["config", "user.email", "t@t"], parent_work)
    _git(["config", "user.name", "t"], parent_work)
    _git(["add", "-A"], parent_work)
    _git(["commit", "-q", "-m", "init"], parent_work)


def _make_parent_run(pdir: Path, *, with_spec=True, with_meta=True) -> None:
    (pdir / "work").mkdir(parents=True, exist_ok=True)
    _make_parent_repo(pdir / "work")
    (pdir / "state.yaml").write_text(_PARENT_STATE_YAML, encoding="utf-8")
    if with_spec:
        (pdir / "spec.yaml").write_text(_RESUME_SPEC_YAML, encoding="utf-8")
    if with_meta:
        (pdir / "meta.json").write_text(
            json.dumps({"id": pdir.name, "order": "보드 게임을 만들어라"}), encoding="utf-8"
        )


def _criteria_dump(spec: ProjectSpec) -> str:
    """acceptance_criteria의 *정규 직렬화*(byte 비교용 — 결정적 dump)."""
    return yaml.safe_dump(
        [ac.model_dump(by_alias=True, mode="json") for ac in spec.acceptance_criteria],
        allow_unicode=True, sort_keys=False,
    )


# ──────────────────────────── 순수 재개 판별(is_pure_resume) ────────────────────────────


def test_is_pure_resume_same_order_true():
    spec = _spec_from_yaml(_RESUME_SPEC_YAML)
    # meta order 우선 — 같으면 순수 재개.
    assert is_pure_resume("보드 게임을 만들어라", "보드 게임을 만들어라", spec) is True
    # 공백만 다른 건 같은 것으로(strip).
    assert is_pure_resume("  보드 게임을 만들어라 ", "보드 게임을 만들어라", spec) is True


def test_is_pure_resume_new_order_false():
    spec = _spec_from_yaml(_RESUME_SPEC_YAML)
    assert is_pure_resume("세일 이벤트 모드 추가", "보드 게임을 만들어라", spec) is False


def test_is_pure_resume_falls_back_to_order_raw_when_no_meta():
    """meta order 없으면 부모 spec.order_raw로 폴백 비교."""
    spec = _spec_from_yaml(_RESUME_SPEC_YAML)  # order_raw="보드 게임을 만들어라"
    assert is_pure_resume("보드 게임을 만들어라", None, spec) is True
    assert is_pure_resume("다른 주문", None, spec) is False


def test_is_pure_resume_no_parent_spec_false():
    """부모 spec 없으면 plan 로딩 불가 → False(증분 폴백)."""
    assert is_pure_resume("x", "x", None) is False


def test_is_pure_resume_resynthesize_forces_false():
    """--resynthesize면 같은 order여도 순수 재개 아님(강제 재합성 opt-in)."""
    spec = _spec_from_yaml(_RESUME_SPEC_YAML)
    assert is_pure_resume("보드 게임을 만들어라", "보드 게임을 만들어라", spec, resynthesize=True) is False


# ──────────────────────────── load_resume 분기 ────────────────────────────


def test_load_resume_pure_resume_loads_parent_spec_and_state(tmp_path):
    runs = tmp_path / "runs"
    pdir = runs / "20260610-100000-p"
    _make_parent_run(pdir)
    new_work = tmp_path / "child" / "work"

    rp = load_resume(str(pdir), runs, new_work, order="보드 게임을 만들어라")
    assert isinstance(rp, ResumePlan)
    assert rp.mode == "pure_resume"
    assert rp.resume_spec is not None and rp.resume_spec.spec_id == "parent-001"
    assert rp.resume_state is not None
    assert {p.unit: p.state for p in rp.resume_state.plan} == {
        "u1": PlanState.done, "u2": PlanState.done, "u3": PlanState.pending,
    }
    # 순수 재개는 증분 합성 페이로드를 만들지 않는다(재합성 skip).
    assert rp.synth_context is None and rp.reuse_manifest is None
    # 부모 코드는 여전히 시딩된다(done 유닛 코드 필요).
    assert (new_work / "seed.txt").is_file()


def test_load_resume_new_order_is_incremental(tmp_path):
    """새 order → 증분 연속(기존 재합성 경로) — resume_spec 없음, synth_context 있음."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-100000-q"
    _make_parent_run(pdir)
    new_work = tmp_path / "child" / "work"

    rp = load_resume(str(pdir), runs, new_work, order="세일 이벤트 모드 추가")
    assert rp.mode == "incremental"
    assert rp.resume_spec is None and rp.resume_state is None
    assert rp.synth_context is not None and "보드 게임 엔진+UI" in rp.synth_context  # 증분 context
    assert rp.reuse_manifest is not None  # #71 재사용 manifest(부모 done 유닛 지문)


def test_load_resume_missing_spec_falls_back_to_incremental(tmp_path):
    """폴백: 부모 spec.yaml 없음(옛 부모) → 같은 order여도 증분 재합성 폴백(크래시 0)."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-100000-r"
    _make_parent_run(pdir, with_spec=False)  # spec.yaml 없음
    new_work = tmp_path / "child" / "work"

    rp = load_resume(str(pdir), runs, new_work, order="보드 게임을 만들어라")
    assert rp.mode == "incremental"
    assert rp.resume_spec is None
    assert rp.synth_context is not None  # 증분 context로 degrade(graceful)


def test_load_resume_resynthesize_forces_incremental(tmp_path):
    """--resynthesize면 순수 재개여도 증분 재합성으로 강제(opt-in escape)."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-100000-s"
    _make_parent_run(pdir)
    new_work = tmp_path / "child" / "work"

    rp = load_resume(str(pdir), runs, new_work, order="보드 게임을 만들어라", resynthesize=True)
    assert rp.mode == "incremental"
    assert rp.resume_spec is None and rp.synth_context is not None


# ──────────────────────────── anti-erosion: 부모 criteria byte 보존 ────────────────────────────


def test_pure_resume_preserves_parent_criteria_byte_identical(tmp_path):
    """재개 plan의 criteria == 부모 criteria(byte 동일) — anti-erosion by construction."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-100000-t"
    _make_parent_run(pdir)
    new_work = tmp_path / "child" / "work"

    rp = load_resume(str(pdir), runs, new_work, order="보드 게임을 만들어라")
    parent_spec = ProjectSpec.from_yaml(pdir / "spec.yaml")
    # 모델 동등 + 정규 직렬화 byte 동일.
    assert rp.resume_spec.acceptance_criteria == parent_spec.acceptance_criteria
    assert rp.resume_spec.done_when == parent_spec.done_when
    assert _criteria_dump(rp.resume_spec) == _criteria_dump(parent_spec)
    # #71 바 지문(재사용 대조 기준)도 유닛별 동일 — drift 0.
    for u in ("u1", "u2", "u3"):
        assert unit_bar_signature(rp.resume_spec, u) == unit_bar_signature(parent_spec, u)


def test_pure_resume_child_spec_sidecar_criteria_byte_identical(tmp_path):
    """run_loop가 새 run의 spec.yaml에 부모 criteria를 byte-동일하게 영속화한다(보존)."""
    parent_spec = _spec_from_yaml(_RESUME_SPEC_YAML)
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    child_state = tmp_path / "state.yaml"
    lock = threading.Lock()

    run_loop(
        "보드 게임을 만들어라", _ReplanOnlyClient(), executor=None, gate=_PassGate(),
        executor_factory=_record_factory([], {}, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR, state_path=child_state,
        seeded=True, resume_spec=parent_spec, resume_state=_parent_state(),
    )
    child_spec = ProjectSpec.from_yaml(tmp_path / "spec.yaml")
    assert child_spec.acceptance_criteria == parent_spec.acceptance_criteria
    assert _criteria_dump(child_spec) == _criteria_dump(parent_spec)
    assert child_spec.done_when == parent_spec.done_when


# ──────────────────────────── 순수 재개: 시드 + 미완만 재빌드 + synthesize 0회 ────────────────────────────


def test_pure_resume_seeds_done_rebuilds_incomplete_no_synthesis(tmp_path, monkeypatch):
    """순수 재개: synthesize 0회. done(u1,u2) 시드(재빌드 0)·미완(u3)만 재빌드(부모 criteria로)."""
    calls = {"synth": 0}
    real_syn = loop_mod.synthesize

    def spy(*a, **k):
        calls["synth"] += 1
        return real_syn(*a, **k)

    monkeypatch.setattr(loop_mod, "synthesize", spy)

    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    built: list[str] = []
    seed_seen: dict[str, bool] = {}
    lock = threading.Lock()

    state = run_loop(
        "보드 게임을 만들어라", _ReplanOnlyClient(), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, seed_seen, lock),
        gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, resume_spec=_spec_from_yaml(_RESUME_SPEC_YAML), resume_state=_parent_state(),
    )
    assert calls["synth"] == 0, "순수 재개는 synthesize를 호출하면 안 된다(재합성 skip)"
    assert state.status is Status.done
    # done 유닛은 재빌드 0(토큰 절약), 미완 u3만 재빌드.
    assert built == ["u3"], f"done(u1,u2) 재빌드 0·미완(u3)만 재빌드여야 함: {built}"
    by_state = {p.unit: p.state for p in state.plan}
    assert by_state == {"u1": PlanState.done, "u2": PlanState.done, "u3": PlanState.done}
    # u3 worktree는 시딩된 부모 코드를 상속한다(done 유닛 산출물 위에서 빌드).
    assert seed_seen.get("u3") is True


def test_pure_resume_records_seed_events_and_integration_gate(tmp_path):
    """투명성+안전: done 유닛은 시드 이벤트(stage=reuse)로 기록되고 통합 gate는 그대로 실행된다."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    lock = threading.Lock()

    state = run_loop(
        "보드 게임을 만들어라", _ReplanOnlyClient(), executor=None, gate=_PassGate(),
        executor_factory=_record_factory([], {}, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, resume_spec=_spec_from_yaml(_RESUME_SPEC_YAML), resume_state=_parent_state(),
    )
    # done 유닛(u1,u2) 시드 이벤트(stage=reuse) — 도장 아님(통합 gate가 최종 판정).
    reuse_evs = [e for e in state.events if e.stage == "reuse"]
    assert {e.unit for e in reuse_evs} == {"u1", "u2"}
    assert all(e.verdict is Verdict.pass_ for e in reuse_evs)
    # transition에도 reuse 기록.
    assert {t.unit for t in state.transitions if t.stage == "reuse"} == {"u1", "u2"}
    # 통합 gate(unit=None)가 최종 결과에 실행됨(개별 시드 ≠ 통합 생략).
    integ = [e for e in state.events if e.unit is None]
    assert integ and integ[-1].work_order_ref == "(integration)"


def test_pure_resume_all_done_still_runs_integration(tmp_path):
    """부모가 전 유닛 done(통합 전 멈춤)이면 재빌드 0 + 통합 gate만 실행(예산 내 통합 도달)."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    all_done_state = State(
        spec_ref="parent-001", spec_version=1, status=Status.stopped_budget,
        plan=[PlanItem(unit=u, state=PlanState.done) for u in ("u1", "u2", "u3")],
    )
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "보드 게임을 만들어라", _ReplanOnlyClient(), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, resume_spec=_spec_from_yaml(_RESUME_SPEC_YAML), resume_state=all_done_state,
    )
    assert state.status is Status.done
    assert built == [], f"전 유닛 done이면 재빌드 0: {built}"
    integ = [e for e in state.events if e.unit is None]
    assert integ and integ[-1].work_order_ref == "(integration)"


def test_pure_resume_corrupt_parent_state_rebuilds_all_graceful(tmp_path):
    """폴백(crash 0): 부모 state 없음 → 전부 pending(전체 재빌드)·여전히 done 도달."""
    wd = tmp_path / "work"
    wd.mkdir(parents=True)
    (wd / "seed.txt").write_text("parent code", encoding="utf-8")
    built: list[str] = []
    lock = threading.Lock()
    state = run_loop(
        "보드 게임을 만들어라", _ReplanOnlyClient(), executor=None, gate=_PassGate(),
        executor_factory=_record_factory(built, {}, lock), gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=wd, prompt_dir=PROMPT_DIR,
        seeded=True, resume_spec=_spec_from_yaml(_RESUME_SPEC_YAML), resume_state=None,
    )
    assert state.status is Status.done
    assert set(built) == {"u1", "u2", "u3"}  # 부모 state 없음 → 전부 재빌드(graceful)


# ──────────────────────────── CLI --continue-from 배선(main) ────────────────────────────


def _capture_run(monkeypatch):
    captured = {}

    def fake_run(order, **kwargs):
        captured["order"] = order
        captured.update(kwargs)
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)
    return captured


def test_main_pure_resume_wires_resume_spec_no_synth_context(tmp_path, monkeypatch):
    """같은 order → 순수 재개: resume_spec/resume_state 전달, synth_context 없음(재합성 skip)."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-090000-parent"
    _make_parent_run(pdir)
    captured = _capture_run(monkeypatch)

    rc = run_mod.main([
        "--order", "보드 게임을 만들어라",
        "--workdir", str(tmp_path / "child-work"),
        "--state-path", str(tmp_path / "child-state.yaml"),
        "--continue-from", str(pdir), "--runs-dir", str(runs),
    ])
    assert rc == 0
    assert captured["seeded"] is True
    assert captured["scaffold_client"] is None            # 이어가기 = scaffold 스킵
    assert captured["synth_context"] is None              # 순수 재개 = 재합성 skip(증분 context 없음)
    assert captured["resume_spec"] is not None and captured["resume_spec"].spec_id == "parent-001"
    n_done = sum(1 for p in captured["resume_state"].plan if p.state is PlanState.done)
    assert n_done == 2
    # 부모 코드 시딩됨.
    assert (tmp_path / "child-work" / "seed.txt").is_file()
    # 계보 사이드카 기록.
    lineage = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["parent_run_id"] == "20260610-090000-parent"


def test_main_new_order_is_incremental(tmp_path, monkeypatch):
    """새 order → 증분 연속(기존 경로): synth_context 있음, resume_spec 없음(back-compat)."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-090000-parent2"
    _make_parent_run(pdir)
    captured = _capture_run(monkeypatch)

    rc = run_mod.main([
        "--order", "세일 이벤트 모드 추가",
        "--workdir", str(tmp_path / "child-work"),
        "--state-path", str(tmp_path / "child-state.yaml"),
        "--continue-from", str(pdir), "--runs-dir", str(runs),
    ])
    assert rc == 0
    assert captured["resume_spec"] is None
    assert captured["synth_context"] is not None and "보드 게임 엔진+UI" in captured["synth_context"]


def test_main_resynthesize_forces_incremental(tmp_path, monkeypatch):
    """--resynthesize: 같은 order여도 증분 재합성(opt-in escape)."""
    runs = tmp_path / "runs"
    pdir = runs / "20260610-090000-parent3"
    _make_parent_run(pdir)
    captured = _capture_run(monkeypatch)

    rc = run_mod.main([
        "--order", "보드 게임을 만들어라",
        "--workdir", str(tmp_path / "child-work"),
        "--state-path", str(tmp_path / "child-state.yaml"),
        "--continue-from", str(pdir), "--runs-dir", str(runs),
        "--resynthesize",
    ])
    assert rc == 0
    assert captured["resume_spec"] is None
    assert captured["synth_context"] is not None


def test_main_resynthesize_in_help(capsys):
    import pytest
    with pytest.raises(SystemExit):
        run_mod.main(["--help"])
    assert "--resynthesize" in capsys.readouterr().out
