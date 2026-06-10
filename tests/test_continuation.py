"""WO#58 — run 이어가기(②a) + 계보 테스트. 실제 codex/네트워크 없음(mock).

검증축:
- spec 사이드카(spec.yaml) 기록·라운드트립·best-effort.
- 부모 해석·workdir 시딩(추적 파일만, node_modules 제외)·증분 context.
- 적대적 분리(부모 context는 합성기만 — critic 무수신).
- anti-erosion(기준 *확장*만, 약화 금지 문구).
- 계보 사이드카·CLI 배선.
"""

import json
import subprocess
from pathlib import Path

import pytest

import haetae.loop as loop_mod
import haetae.run as run_mod
from haetae.intake import build_continuation_context
from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import ProjectSpec, State, Status, Verdict
from haetae.run import (
    ContinuationError,
    load_continuation,
    resolve_parent_dir,
    seed_workdir_from_parent,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

SPEC_YAML = """\
spec_id: cont-001
version: 1
order_raw: "리테일 시뮬레이터를 만들어라"
goal: "리테일 체크아웃 시뮬레이터"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: ["오프라인 동작"]
acceptance_criteria:
  - id: ac1
    desc: "체크아웃 흐름 동작"
    check: { type: test, cmd: "npm test" }
assumptions: []
non_goals: ["결제 PG 연동"]
done_when: "ac1 통과 + 라이브 차트"
decomposition:
  - { unit: u1, desc: "엔진", deps: [] }
  - { unit: u2, desc: "대시보드", deps: [u1] }
open_questions: []
"""

_STOP = "verdict: done\naction: stop\nrationale: \"done_when 충족\"\n"
_ADEQUATE = "verdict: adequate\ngaps: []\n"


def _parent_spec() -> ProjectSpec:
    return ProjectSpec.from_yaml  # placeholder unused


# ──────────────────────────── 증분 context 빌더 ────────────────────────────


def test_continuation_context_has_parent_summary_and_anti_erosion():
    spec = ProjectSpec.model_validate_json(
        ProjectSpec(
            spec_id="p", version=1, order_raw="o", goal="리테일 시뮬레이터",
            task_type="feature_impl", verifiability="objective", mode="normal",
            acceptance_criteria=[{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "t"}}],
            non_goals=["x", "y"], done_when="ac1 통과",
            decomposition=[{"unit": "u1", "desc": "엔진", "deps": []}],
        ).model_dump_json()
    )
    state = State(spec_ref="p", spec_version=1, status=Status.done)
    state.plan = []
    ctx = build_continuation_context(spec, state)
    # 부모 요약
    assert "리테일 시뮬레이터" in ctx
    assert "ac1 통과" in ctx
    assert "u1" in ctx
    # 증분 신호
    assert "delta" in ctx
    assert "다시 짓지 마라" in ctx
    # anti-erosion: *확장*만, 약화 금지를 명시
    assert "확장" in ctx
    assert "약화" in ctx  # "절대 약화/완화하지 마라" 금지 문구


def test_continuation_context_does_not_instruct_weakening():
    """약화/완화는 *금지 문구*로만 등장 — '완화하라' 같은 지시는 없어야 한다(anti-erosion)."""
    spec = ProjectSpec(
        spec_id="p", version=1, order_raw="o", goal="g",
        task_type="feature_impl", verifiability="objective", mode="normal",
        acceptance_criteria=[{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "t"}}],
        non_goals=["x", "y"], done_when="dw",
    )
    ctx = build_continuation_context(spec, None)
    assert "완화하라" not in ctx
    assert "약화하라" not in ctx
    assert "기준을 낮춰" not in ctx


def test_continuation_context_degrades_to_order_without_spec():
    """spec.yaml 없는 옛 부모 → parent_order로 degrade(여전히 증분 신호)."""
    ctx = build_continuation_context(None, None, parent_order="원래 주문 문장")
    assert "원래 주문 문장" in ctx
    assert "delta" in ctx


# ──────────────────────────── 부모 해석 / 시딩 ────────────────────────────


def _git(cmd, cwd):
    subprocess.run(["git", *cmd], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_parent_repo(parent_work: Path) -> None:
    parent_work.mkdir(parents=True, exist_ok=True)
    (parent_work / "src").mkdir()
    (parent_work / "src" / "engine.ts").write_text("export const engine = 1;\n", encoding="utf-8")
    (parent_work / "package.json").write_text('{"name":"sim","version":"1.0.0"}\n', encoding="utf-8")
    (parent_work / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    # node_modules는 추적 안 됨(gitignore) → 시딩되면 안 됨
    (parent_work / "node_modules").mkdir()
    (parent_work / "node_modules" / "dep.js").write_text("// huge\n", encoding="utf-8")
    _git(["init", "-q"], parent_work)
    _git(["config", "user.email", "t@t"], parent_work)
    _git(["config", "user.name", "t"], parent_work)
    _git(["add", "-A"], parent_work)
    _git(["commit", "-q", "-m", "init"], parent_work)


def test_seed_workdir_copies_tracked_excludes_node_modules(tmp_path):
    parent_work = tmp_path / "parent" / "work"
    _make_parent_repo(parent_work)
    new_work = tmp_path / "child" / "work"
    n = seed_workdir_from_parent(parent_work, new_work)
    assert (new_work / "src" / "engine.ts").is_file()  # 추적 파일 시딩됨
    assert (new_work / "package.json").is_file()
    assert not (new_work / "node_modules").exists()     # gitignore → 시딩 안 됨
    assert n >= 3


def test_resolve_parent_dir_by_path_and_id(tmp_path):
    runs = tmp_path / "runs"
    rdir = runs / "20260610-100000-x"
    rdir.mkdir(parents=True)
    (rdir / "state.yaml").write_text("spec_ref: x\nspec_version: 1\nstatus: done\n", encoding="utf-8")
    # 직접 경로
    assert resolve_parent_dir(str(rdir), runs) == rdir
    # run-id (runs_dir 아래)
    assert resolve_parent_dir("20260610-100000-x", runs) == rdir


def test_resolve_parent_dir_missing_raises(tmp_path):
    with pytest.raises(ContinuationError):
        resolve_parent_dir("nope", tmp_path / "runs")


def test_load_continuation_seeds_and_builds_context(tmp_path):
    runs = tmp_path / "runs"
    rdir = runs / "20260610-100000-p"
    (rdir / "work").mkdir(parents=True)
    _make_parent_repo(rdir / "work")
    (rdir / "state.yaml").write_text("spec_ref: cont-001\nspec_version: 1\nstatus: done\n", encoding="utf-8")
    (rdir / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
    (rdir / "meta.json").write_text(json.dumps({"id": "p", "order": "부모주문"}), encoding="utf-8")

    new_work = tmp_path / "child" / "work"
    ctx, parent_dir, n = load_continuation("20260610-100000-p", runs, new_work)
    assert parent_dir == rdir
    assert (new_work / "src" / "engine.ts").is_file()  # 시딩됨
    assert "리테일 체크아웃 시뮬레이터" in ctx           # 부모 spec goal
    assert "확장" in ctx and "약화" in ctx               # anti-erosion


# ──────────────────────────── spec 사이드카(loop) ────────────────────────────


def test_run_loop_writes_spec_sidecar(tmp_path):
    sp = tmp_path / "state.yaml"
    run_loop(
        order="x", client=MockClient([SPEC_YAML, _STOP]),
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR, state_path=sp,
    )
    spec_path = tmp_path / "spec.yaml"
    assert spec_path.exists()
    loaded = ProjectSpec.from_yaml(spec_path)  # 라운드트립
    assert loaded.goal == "리테일 체크아웃 시뮬레이터"
    assert loaded.done_when == "ac1 통과 + 라이브 차트"


def test_spec_sidecar_write_failure_is_best_effort(tmp_path, monkeypatch):
    """spec.yaml 쓰기 실패가 run을 죽이지 않는다(best-effort)."""
    def boom(spec, state_path):
        raise OSError("disk full")
    monkeypatch.setattr(loop_mod, "_save_spec", boom)
    state = run_loop(
        order="x", client=MockClient([SPEC_YAML, _STOP]),
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR, state_path=tmp_path / "state.yaml",
    )
    assert state.status is Status.done  # 사이드카 실패에도 정상 완료


# ──────────────────────────── 적대적 분리 + 증분 주입(loop) ────────────────────────────


def test_synth_context_reaches_synthesizer_not_critic(tmp_path):
    """이어가기 context는 *합성기*에만 — critic(적대)엔 절대 안 간다."""
    MARKER = "PARENT_MARKER_XYZ_12345"
    brain = MockClient([SPEC_YAML, _STOP])
    critic = MockClient([_ADEQUATE])
    run_loop(
        order="delta 주문", client=brain,
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        critic_client=critic, prompt_dir=PROMPT_DIR,
        state_path=tmp_path / "state.yaml",
        synth_context=MARKER,
    )
    # 합성기(brain) 첫 호출 user엔 마커가 있다(증분 주입됨).
    assert any(MARKER in c["user"] for c in brain.calls)
    # critic 호출 어디에도 마커 없음(적대 분리 — 부모 done 맹신 안 함).
    assert all(MARKER not in c["user"] for c in critic.calls)
    assert critic.calls, "critic이 호출되긴 해야 한다(분리 단언이 공허하지 않게)"


# ──────────────────────────── seeded deps 설치(loop) ────────────────────────────


def test_seeded_continuation_installs_deps(tmp_path):
    """이어가기(seeded)면 scaffold 없이도 시딩된 package.json에 deps를 host-install."""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    calls = []

    def recorder(cmd, cwd, timeout):
        calls.append((cmd, cwd))
        return (0, "ok")

    run_loop(
        order="delta", client=MockClient([SPEC_YAML, _STOP]),
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR, state_path=tmp_path / "state.yaml",
        workdir=wd, seeded=True, install_deps=True, deps_runner=recorder,
    )
    assert calls, "seeded면 deps install runner가 호출돼야 한다"
    assert str(wd) in calls[0][1]


def test_non_seeded_no_scaffold_skips_deps(tmp_path):
    """무회귀: seeded=False(기존)면 scaffold 없을 때 deps install 안 함(기존 동작)."""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    calls = []

    def recorder(cmd, cwd, timeout):
        calls.append(cmd)
        return (0, "ok")

    run_loop(
        order="x", client=MockClient([SPEC_YAML, _STOP]),
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR, state_path=tmp_path / "state.yaml",
        workdir=wd, seeded=False, install_deps=True, deps_runner=recorder,
    )
    assert calls == []  # scaffold 없고 seeded 아니면 deps 미설치(기존 경로 불변)


# ──────────────────────────── CLI --continue-from 배선 ────────────────────────────


def _capture_run(monkeypatch):
    captured = {}

    def fake_run(order, **kwargs):
        captured["order"] = order
        captured.update(kwargs)
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)
    return captured


def test_main_continue_from_wires_context_seeded_and_skips_scaffold(tmp_path, monkeypatch):
    # 부모 run 준비(state+spec+work git repo).
    runs = tmp_path / "runs"
    pdir = runs / "20260610-090000-parent"
    (pdir / "work").mkdir(parents=True)
    _make_parent_repo(pdir / "work")
    (pdir / "state.yaml").write_text("spec_ref: cont-001\nspec_version: 1\nstatus: done\n", encoding="utf-8")
    (pdir / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")

    new_work = tmp_path / "child-work"
    captured = _capture_run(monkeypatch)
    rc = run_mod.main([
        "--order", "세일 이벤트 모드 추가",
        "--workdir", str(new_work),
        "--state-path", str(tmp_path / "child-state.yaml"),
        "--continue-from", str(pdir),
        "--runs-dir", str(runs),
    ])
    assert rc == 0
    assert captured["seeded"] is True
    assert captured["scaffold_client"] is None          # 이어가기 = scaffold 스킵
    assert "리테일 체크아웃 시뮬레이터" in captured["synth_context"]  # 증분 context
    assert (new_work / "src" / "engine.ts").is_file()     # 부모 코드 시딩됨
    # 계보 사이드카 기록
    lineage = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["parent_run_id"] == "20260610-090000-parent"


def test_main_continue_from_missing_parent_clean_error(tmp_path, monkeypatch, capsys):
    _capture_run(monkeypatch)
    rc = run_mod.main([
        "--order", "x", "--workdir", str(tmp_path / "w"),
        "--state-path", str(tmp_path / "s.yaml"),
        "--continue-from", "does-not-exist",
        "--runs-dir", str(tmp_path / "runs"),
    ])
    assert rc == 2  # 명확한 에러(조용한 폴백 아님)
    assert "이어가기 실패" in capsys.readouterr().err


def test_main_continue_from_in_help(capsys):
    with pytest.raises(SystemExit):
        run_mod.main(["--help"])
    assert "--continue-from" in capsys.readouterr().out
