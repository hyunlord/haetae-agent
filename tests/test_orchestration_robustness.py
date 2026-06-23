"""WO#172 약-brain 오케스트레이션 견고화 테스트.

#171 라이브(완전-로컬·zero-codex): 14b 빌더는 옳은 코드를 짰으나 약 brain이 green 완주를 막음 —
(1) 합성 check-cmd `-k`가 생성될 테스트 이름과 불일치(findability, 게이트 0개 발견 exit 5),
(2) replan이 빈 next_order(즉시 막힘). 둘을 *파이프라인 견고성*으로 흡수(모델 교체 0). **가드지
판정 아님 — 바 불완화 0·gate/run_judge 판정 로직 불변·codex 0.**
"""

from __future__ import annotations

from pathlib import Path

from haetae.gate import CompositeGate, aggregate_verdict
from haetae.intake import _strip_pytest_k, align_check_findability, synthesize
from haetae.llm import MockClient
from haetae.loop import (
    _fallback_order,
    _fallback_target_unit,
    _is_degenerate_order,
    MockGate,
)
from haetae.providers.codex import ALLOWED_SANDBOXES
from haetae.replan import degenerate_next_order
from haetae.executors import HumanRelayExecutor
from haetae.models import (
    AcceptanceCriterion,
    Action,
    Check,
    CheckReport,
    CheckType,
    Decision,
    DecompositionUnit,
    NextOrder,
    PlanItem,
    PlanState,
    ProjectSpec,
    State,
    Status,
    Verdict,
)
from haetae.run import run

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


def _spec(criteria, decomposition=None) -> ProjectSpec:
    return ProjectSpec(
        spec_id="s", version=1, order_raw="o", goal="g",
        task_type="feature_impl", verifiability="objective", mode="normal",
        acceptance_criteria=criteria, non_goals=[], done_when="x",
        decomposition=decomposition or [],
    )


# ════════════════════ 1. 합성 findability 정렬 (fragile pytest -k 흡수) ════════════════════


def test_strip_pytest_k_variants():
    assert _strip_pytest_k("pytest test_calculator.py -k 'divide_zero_division'") == "pytest test_calculator.py"
    assert _strip_pytest_k("python -m pytest -k storage") == "python -m pytest"
    assert _strip_pytest_k("pytest -k 'a or b' tests/") == "pytest tests/"
    assert _strip_pytest_k("pytest -kstorage tests/") == "pytest tests/"  # 붙은 폼
    assert _strip_pytest_k("pytest tests/test_x.py") == "pytest tests/test_x.py"  # -k 없음 = no-op
    assert _strip_pytest_k("npm run test -- -k foo") == "npm run test -- -k foo"  # 비-pytest 무관


def test_strip_pytest_k_idempotent_and_unbalanced_safe():
    once = _strip_pytest_k("pytest test_x.py -k 'divide_zero'")
    assert _strip_pytest_k(once) == once  # 멱등
    # 따옴표 불균형 → 보수적으로 원본 유지(크래시 없음)
    assert _strip_pytest_k("pytest -k 'unterminated") == "pytest -k 'unterminated"


def test_align_strips_pytest_k_leaves_run_untouched():
    spec = _spec([
        AcceptanceCriterion(id="a1", desc="d", check=Check(type=CheckType.test, cmd="pytest test_calc.py -k 'divide_zero_division'")),
        AcceptanceCriterion(id="a2", desc="d", check=Check(type=CheckType.run, cmd="npm run sim:trace")),
        AcceptanceCriterion(id="a3", desc="d", check=Check(type=CheckType.test, cmd="pytest test_calc.py")),
    ])
    align_check_findability(spec)
    assert spec.acceptance_criteria[0].check.cmd == "pytest test_calc.py"  # -k stripped
    assert spec.acceptance_criteria[1].check.cmd == "npm run sim:trace"  # run untouched
    assert spec.acceptance_criteria[2].check.cmd == "pytest test_calc.py"  # already clean


def test_synthesize_strips_fragile_k_end_to_end():
    """#171c 실패 재현: 약 brain이 -k 'divide_zero_division' 냄 → synthesize가 결정적으로 제거."""
    spec_yaml = (
        "spec_id: calc-001\nversion: 1\norder_raw: o\ngoal: g\n"
        "task_type: feature_impl\nverifiability: objective\nmode: normal\n"
        "acceptance_criteria:\n"
        "  - id: ac1\n    desc: funcs\n    unit: u1\n"
        "    check: { type: test, cmd: \"pytest test_calculator.py -k 'divide_zero_division'\" }\n"
        "non_goals: [x]\ndone_when: all pass\n"
        "decomposition:\n  - { unit: u1, desc: impl, deps: [] }\n"
    )
    sp = synthesize("calc", MockClient([spec_yaml]), prompt_path=str(PROMPT_DIR / "synthesizer.md"))
    assert sp.acceptance_criteria[0].check.cmd == "pytest test_calculator.py"  # findable now


def test_findability_strip_is_bar_safe_failing_test_still_fails(tmp_path):
    """가드≠봐주기: -k 제거는 *더 많은* 테스트를 돌릴 뿐 — 발견된 테스트가 fail이면 여전히 fail."""
    # 실패하는 테스트 파일을 만들고, -k 제거된 cmd로 게이트를 돌리면 여전히 fail이어야 한다.
    (tmp_path / "test_x.py").write_text("def test_fails():\n    assert False\n")
    spec = _spec([AcceptanceCriterion(id="a1", desc="d", check=Check(type=CheckType.test, cmd="python -m pytest test_x.py -k 'nonexistent_keyword'"))])
    align_check_findability(spec)  # cmd → "python -m pytest test_x.py"
    assert spec.acceptance_criteria[0].check.cmd == "python -m pytest test_x.py"
    gate = CompositeGate(workdir=str(tmp_path), install_deps=False)
    gr = gate.judge("r", spec)
    assert gr.verdict == Verdict.fail_recoverable  # 발견된 실패 테스트는 여전히 fail(바 불완화 0)


# ════════════════════ 2. replan 빈-산출 견고화 (재프롬프트 → fallback) ════════════════════


def test_degenerate_next_order_detection():
    mk = lambda action, no: Decision(verdict=Verdict.pass_, action=action, rationale="r", next_order=no)
    assert degenerate_next_order(mk(Action.next_order, None)) is not None
    assert degenerate_next_order(mk(Action.retry, NextOrder(unit="", goal="x"))) is not None
    assert degenerate_next_order(mk(Action.next_order, NextOrder(unit="u1", goal="  "))) is not None
    # 완전한 order = None(정상)
    assert degenerate_next_order(mk(Action.next_order, NextOrder(unit="u1", goal="do"))) is None
    # next_order/retry 아닌 action = 무관(None)
    assert degenerate_next_order(Decision(verdict=Verdict.pass_, action=Action.stop, rationale="r")) is None
    assert degenerate_next_order(None) is None


def test_is_degenerate_order():
    assert _is_degenerate_order(None)
    assert _is_degenerate_order(NextOrder(unit="u1", goal=""))
    assert _is_degenerate_order(NextOrder(unit="", goal="x"))
    assert not _is_degenerate_order(NextOrder(unit="u1", goal="do it"))


def test_fallback_order_from_pinned_spec():
    spec = _spec(
        [AcceptanceCriterion(id="a1", desc="d", unit="u1", check=Check(type=CheckType.test, cmd="pytest test_x.py"))],
        decomposition=[DecompositionUnit(unit="u1", desc="calc 구현", deps=[], scope=["calc.py", "test_x.py"])],
    )
    fb = _fallback_order(spec, "u1")
    assert fb is not None and fb.unit == "u1" and fb.goal == "calc 구현"
    assert len(fb.local_checks) == 1 and fb.local_checks[0].cmd == "pytest test_x.py"
    assert "calc.py" in fb.scope
    assert _fallback_order(spec, "nope") is None  # 유닛 없음
    assert _fallback_order(spec, None) is None


def test_fallback_target_unit():
    st = State(spec_ref="s", spec_version=1, status=Status.running, plan=[
        PlanItem(unit="u1", state=PlanState.in_progress),
        PlanItem(unit="u2", state=PlanState.pending),
    ])
    assert _fallback_target_unit(st, "u1") == "u1"  # worked still in_progress → retry it
    assert _fallback_target_unit(st, None) in ("u1", "u2")  # 첫 ready
    done = State(spec_ref="s", spec_version=1, status=Status.running, plan=[PlanItem(unit="u1", state=PlanState.done)])
    assert _fallback_target_unit(done, None) is None  # ready 없음 → None(호출부 escalate)


# ── 통합: 빈 산출이 escalate-empty로 *조용히 막지 않고* 재프롬프트/fallback으로 진행 ──

_SPEC_YAML = """\
spec_id: orch-001
version: 1
order_raw: "유틸 추가"
goal: "유틸 구현"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "동작"
    unit: u1
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["x"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "유틸 모듈 구현", deps: [], scope: ["util.py", "test_util.py"] }
open_questions: []
"""

_DEGEN = "verdict: pass\naction: next_order\nrationale: 빈산출\nnext_order: null\n"


def _valid_order(unit: str) -> str:
    return (
        f"verdict: pass\naction: next_order\nrationale: \"{unit}\"\n"
        f"next_order:\n  unit: {unit}\n  goal: \"{unit} 구현\"\n  deliverable: \"요약\"\n"
    )


def test_replan_empty_output_reprompts_and_recovers():
    """빈 next_order → 에러-피드백 재프롬프트(#31)로 *흡수* → 유효 order로 회복 → done. escalate-empty 0."""
    # synth → replan attempt0=DEGEN(재프롬프트) → attempt1=valid u1 → dispatch → gate pass
    #       → replan=valid u2 → dispatch → gate done.
    client = MockClient([_SPEC_YAML, _DEGEN, _valid_order("u1"), _valid_order("u2")])
    executor = HumanRelayExecutor(present=lambda t: None, collect=lambda: "결과")
    state = run("유틸 추가", client=client, executor=executor,
                gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert not any("본문 없음" in str(e) for e in state.pending_escalations)  # escalate-empty 아님


def test_replan_persistent_empty_falls_back_to_pinned_spec_order():
    """재프롬프트 소진 후에도 빈 → pinned spec서 결정적 fallback work order 합성·dispatch(escalate-empty 0)."""
    # synth + replan 3시도(replan_retries=2 → attempt 0,1,2) 전부 DEGEN → fallback(u1) → dispatch → gate done.
    client = MockClient([_SPEC_YAML, _DEGEN, _DEGEN, _DEGEN])
    executor = HumanRelayExecutor(present=lambda t: None, collect=lambda: "결과")
    state = run("유틸 추가", client=client, executor=executor,
                gate=MockGate([Verdict.done]), prompt_dir=PROMPT_DIR)
    # fallback이 u1 work order를 합성·dispatch → gate done → 완주(escalate-empty로 막히지 않음).
    assert not any("본문 없음" in str(e) for e in state.pending_escalations)
    assert state.status is Status.done
    assert any(e.unit == "u1" for e in state.events)  # fallback이 u1을 dispatch함


# ════════════════════ 적대 분리·바 (가드≠판정, 불변) ════════════════════


def test_aggregate_verdict_logic_unchanged():
    """가드는 gate/run_judge 판정 로직을 안 건드린다 — aggregate_verdict 규칙 byte-identical."""
    t = CheckType.test
    assert aggregate_verdict([CheckReport(ac_id="a", check_type=t, status="pass")]) == Verdict.pass_
    assert aggregate_verdict([CheckReport(ac_id="a", check_type=t, status="fail")]) == Verdict.fail_recoverable
    assert aggregate_verdict([CheckReport(ac_id="a", check_type=t, status="skipped")]) == Verdict.ambiguous
    assert aggregate_verdict([]) == Verdict.pass_


def test_allowed_sandboxes_unchanged():
    assert ALLOWED_SANDBOXES == ("read-only", "workspace-write")


def test_fallback_order_is_builder_guidance_not_verdict():
    """fallback order는 NextOrder(빌더 가이드)일 뿐 — verdict/Decision 권위가 아니다(가드≠판정)."""
    spec = _spec(
        [AcceptanceCriterion(id="a1", desc="d", unit="u1", check=Check(type=CheckType.test, cmd="pytest"))],
        decomposition=[DecompositionUnit(unit="u1", desc="impl", deps=[])],
    )
    fb = _fallback_order(spec, "u1")
    assert isinstance(fb, NextOrder)  # work order(빌더 입력)지 Verdict/GateResult 아님
    assert not hasattr(fb, "verdict")
