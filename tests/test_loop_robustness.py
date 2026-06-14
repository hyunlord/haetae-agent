"""WO#108 — 루프 견고성 배치: (A) self-check right-size · (B) continue-from 시드 자동해소.
(C unit-retries 상향은 test_run.py 기본값 테스트 + 기존 override/escalate 테스트가 커버.)
"""

import json
from pathlib import Path

import pytest

from haetae.gate import CompositeGate
from haetae.intake import extract_evidence_contracts
from haetae.models import ProjectSpec, Verdict
from haetae.run import ContinuationError, _resolve_seed_src, _write_cli_meta, seed_workdir_from_parent

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"

_RUN_PASS = "stdout JSON에 wall_crossings == 0, overlap_pairs == 0 이어야 한다."


def _gate(tmp_path, client=None) -> CompositeGate:
    return CompositeGate(
        workdir=tmp_path, judge_client=client,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10, install_deps=False,
    )


def _harness_spec(cmds: list[str]) -> ProjectSpec:
    """u1=하니스 + N개 run 기준(integration-태그) + 추출 계약. cmds = 각 시나리오 cmd."""
    acs = [
        {"id": f"ac{i}", "desc": "동선 trace", "check": {"type": "run", "cmd": c, "pass": _RUN_PASS}}
        for i, c in enumerate(cmds)
    ]
    spec = ProjectSpec.model_validate({
        "spec_id": "lr-001", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": acs, "non_goals": ["n"], "done_when": "x",
        "decomposition": [{"unit": "u1", "desc": "헤드리스 sim:trace 하니스"}],
    })
    return extract_evidence_contracts(spec)


# ════════════════════ A. self-check right-size (대표 시나리오 1개) ════════════════════


def test_self_check_runs_only_representative(tmp_path):
    """per-unit self-check가 *대표(첫) 시나리오 1개*만 실행 — 둘째 시나리오는 안 돈다."""
    good = "echo '{\"wall_crossings\":0,\"overlap_pairs\":0}'"
    boom = "exit 7"  # 둘째 — 만약 실행되면 fail날 cmd
    spec = _harness_spec([good, boom])
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    runs = [c for c in gr.checks if c.ac_id.startswith("(harness-run:")]
    assert len(runs) == 1                       # 대표 1개만(전 시나리오 아님)
    assert runs[0].ac_id == "(harness-run:ac0)"  # 첫(대표)
    assert runs[0].status == "pass"
    assert gr.verdict is Verdict.pass_           # 둘째(boom) 안 돌아서 pass


def test_self_check_broken_representative_fails(tmp_path):
    """대표 시나리오가 깨지면(exit≠0) per-unit fail(깨진 하니스 조기 차단 보존)."""
    spec = _harness_spec(["this-cmd-xyz-missing"])
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    run = [c for c in gr.checks if c.ac_id.startswith("(harness-run:")][0]
    assert run.status == "fail"
    assert gr.verdict is Verdict.fail_recoverable


def test_self_check_smoke_passes_with_partial_fields(tmp_path):
    """right-size 핵심: 대표 트레이스가 계약 어휘를 *일부만* 내도 구조 smoke pass(전-필드 union은
    통합서). 단일 시나리오가 union을 다 못 내는 멀티-시나리오 거짓 fail 방지."""
    # 계약 union = {wall_crossings, overlap_pairs}; 대표는 그 중 하나만 emit.
    spec = _harness_spec(["echo '{\"wall_crossings\":0}'", "echo '{\"overlap_pairs\":0}'"])
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0]
    assert ec.status == "pass"                  # ≥1 어휘 필드 → 구조 smoke 충족
    assert gr.verdict is Verdict.pass_


def test_self_check_smoke_fails_on_zero_contract_fields(tmp_path):
    """대표 트레이스에 계약 어휘 0개(다른 필드로 대체) → 구조 smoke fail(잘못된 필드 차단 보존)."""
    spec = _harness_spec(["echo '{\"my_own_count\":42}'"])
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0]
    assert ec.status == "fail"
    assert gr.verdict is Verdict.fail_recoverable


def test_self_check_smoke_fails_on_non_json(tmp_path):
    """대표 트레이스가 JSON 아님(stdout 위생 위반) → 구조 smoke fail."""
    spec = _harness_spec(["echo 'not json at all'"])
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0]
    assert ec.status == "fail"


def test_integration_still_enforces_full_union(tmp_path):
    """통합 게이트(unit=None)는 전-필드 union을 *그대로* 강제(누락→fail) — 커버리지 보존(무변경)."""
    # 통합서 단일 트레이스가 union의 일부만 → 누락 fail(per-unit smoke와 달리 union 강제).
    spec = _harness_spec(["echo '{\"wall_crossings\":0}'"])  # overlap_pairs 누락
    gr = _gate(tmp_path, client=None).judge("(integration)", spec, unit=None)
    ec = [c for c in gr.checks if c.ac_id == "(evidence-contract)"]
    assert ec and ec[0].status == "fail"        # 통합 union 강제 그대로
    assert gr.verdict is Verdict.fail_recoverable


# ════════════════════ B. continue-from 시드 자동해소 ════════════════════


def test_resolve_seed_src_from_meta_workdir(tmp_path):
    """meta.json의 workdir(별도 위치)을 시드원으로 자동 해소 — 수동 symlink 불필요."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sep_work = tmp_path / "elsewhere" / "code"
    sep_work.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"id": "run", "order": "x", "workdir": str(sep_work)}), encoding="utf-8")
    assert _resolve_seed_src(run_dir) == sep_work


def test_resolve_seed_src_falls_back_to_work(tmp_path):
    """meta workdir 없으면 <run-dir>/work 폴백."""
    run_dir = tmp_path / "run"
    (run_dir / "work").mkdir(parents=True)
    assert _resolve_seed_src(run_dir) == run_dir / "work"


def test_resolve_seed_src_falls_back_to_rundir(tmp_path):
    """meta workdir·work 둘 다 없으면 <run-dir> 폴백(기존 동작)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert _resolve_seed_src(run_dir) == run_dir


def test_resolve_seed_src_ignores_missing_meta_workdir(tmp_path):
    """meta workdir이 *존재하지 않는* 경로면 무시하고 폴백(크래시 0)."""
    run_dir = tmp_path / "run"
    (run_dir / "work").mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps({"workdir": str(tmp_path / "gone")}), encoding="utf-8")
    assert _resolve_seed_src(run_dir) == run_dir / "work"   # 없는 workdir → work 폴백


def test_seed_auto_resolves_separate_workdir(tmp_path):
    """통합: 부모가 *별도 workdir*(다른 위치)여도 시드가 자동 해소해 코드를 복사(symlink 없이)."""
    run_dir = tmp_path / "parent-run"
    run_dir.mkdir()
    sep_work = tmp_path / "parent-code"
    sep_work.mkdir()
    (sep_work / "app.js").write_text("console.log(1)", encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps({"workdir": str(sep_work)}), encoding="utf-8")
    new_work = tmp_path / "child-work"
    n = seed_workdir_from_parent(_resolve_seed_src(run_dir), new_work)
    assert n >= 1 and (new_work / "app.js").is_file()       # 별도 workdir서 코드 복사됨


def test_seed_missing_clear_error(tmp_path):
    """시드원이 없으면 명확한 ContinuationError(크래시 0)."""
    with pytest.raises(ContinuationError):
        seed_workdir_from_parent(tmp_path / "nonexistent", tmp_path / "new")


def test_cli_meta_records_workdir(tmp_path):
    """_write_cli_meta가 workdir를 *절대경로*로 meta.json에 기록(시드 자동해소 원천)."""
    sp = tmp_path / "run" / "state.yaml"
    sp.parent.mkdir(parents=True)
    wd = tmp_path / "mywork"
    wd.mkdir()
    _write_cli_meta(sp, "주문", workdir=wd)
    meta = json.loads((sp.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["workdir"] == str(wd.resolve())
    assert meta["order"] == "주문"
