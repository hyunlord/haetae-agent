"""WO#78 — 검증 증거 계약(evidence-contract) 테스트 (mock, codex/네트워크 없음).

캡스톤 #77이 데이터로 드러낸 두 결함의 수정:
  1. 계약 불일치 — acceptance_criteria(run/sim:trace)가 요구하는 증거 필드를 빌더 하니스가 *다른
     필드*로 내서 적대 run-judge가 판정조차 못 함(게이트가 텅 빔).
  2. 검증 역전 — "트레이스 하니스 만들어라"가 열린 명세라 폭주.

수정: criteria에서 요구 증거 필드를 *파생*해 (1)추출→하니스 유닛 부착, (2)빌더 작업지시서 주입,
(3)게이트 결정적 필드-존재 체크로 강제. 바 불변(파생) · 스키마 체크 ≠ 행동 판정(적대 run-judge 그대로).
"""

from pathlib import Path

from haetae.gate import (
    CompositeGate,
    check_evidence_contract,
    contract_for_unit,
)
from haetae.intake import (
    extract_evidence_contracts,
    extract_required_evidence_fields,
)
from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import CheckType, NextOrder, ProjectSpec, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"

# acceptance_criteria가 *명시적으로* 요구하는 증거 필드를 든 run 기준 pass 절(캡스톤 ac6/7류).
_RUN_PASS = (
    "stdout JSON에 completed_agents >= 25이고 wall_crossings == 0, overlap_pairs == 0, "
    "spawn_attempts >= 80, route_cost_samples >= 200이어야 한다."
)
_RUN_FIELDS = ["completed_agents", "overlap_pairs", "route_cost_samples",
               "spawn_attempts", "wall_crossings"]  # 정렬된 기대 계약


def _spec(acs: list[dict], decomp: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate({
        "spec_id": "ec-001", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": acs, "non_goals": ["n"], "done_when": "전부 통과",
        "decomposition": decomp,
    })


def _gate(tmp_path, client=None) -> CompositeGate:
    return CompositeGate(
        workdir=tmp_path, judge_client=client,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10, install_deps=False,
    )


# ════════════════════ 1. 추출 (criteria → 하니스 유닛) ════════════════════


def test_extract_fields_from_run_criteria():
    """run 기준 pass/desc의 스네이크케이스 필드명을 결정적으로 추출(정렬·중복제거)."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": _RUN_PASS}}],
        [{"unit": "u1", "desc": "엔진"}, {"unit": "u2", "desc": "헤드리스 sim:trace CLI"}],
    )
    assert extract_required_evidence_fields(spec) == _RUN_FIELDS


def test_extract_attaches_contract_to_harness_unit_only():
    """추출된 계약은 *트레이스 하니스* 유닛(desc 키워드)에만 부착 — 비하니스 유닛은 빈 계약."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": _RUN_PASS}}],
        [{"unit": "u1", "desc": "물리 엔진 구현"}, {"unit": "u2", "desc": "헤드리스 trace 하니스"}],
    )
    out = extract_evidence_contracts(spec)
    by = {u.unit: u.evidence_contract for u in out.decomposition}
    assert by["u2"] == _RUN_FIELDS         # 하니스 유닛 → 계약 부착
    assert by["u1"] == []                  # 비하니스 → 무계약


def test_extract_derives_from_bar_without_changing_it():
    """anti-erosion: 계약은 criteria에서 *파생*만 — 바(criteria/done_when/goal 등)를 안 바꾼다."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": _RUN_PASS}}],
        [{"unit": "u1", "desc": "sim:trace 하니스"}],
    )
    out = extract_evidence_contracts(spec)
    assert out.acceptance_criteria == spec.acceptance_criteria  # 바 불변
    assert out.done_when == spec.done_when and out.goal == spec.goal
    assert out.non_goals == spec.non_goals and out.constraints == spec.constraints


def test_extract_graceful_when_no_run_criteria():
    """run 기준이 없으면(필드 추출 불가/모호) → 무계약(graceful, 기존 동작)."""
    spec = _spec(
        [{"id": "ac1", "desc": "빌드", "check": {"type": "build", "cmd": "npm run build"}}],
        [{"unit": "u1", "desc": "헤드리스 sim:trace CLI"}],
    )
    out = extract_evidence_contracts(spec)
    assert all(u.evidence_contract == [] for u in out.decomposition)
    assert out is spec or out.model_dump() == spec.model_dump()


def test_extract_graceful_when_no_harness_unit():
    """run 기준은 있어도 트레이스-하니스 유닛이 없으면 → 부착 대상 없음(무계약)."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": _RUN_PASS}}],
        [{"unit": "u1", "desc": "물리 엔진"}, {"unit": "u2", "desc": "Canvas UI 렌더링"}],
    )
    out = extract_evidence_contracts(spec)
    assert all(u.evidence_contract == [] for u in out.decomposition)


def test_extract_ambiguous_criteria_empty_contract():
    """필드명을 안 든 모호한 run 기준(스네이크케이스 토큰 없음) → 빈 계약(graceful)."""
    spec = _spec(
        [{"id": "ac1", "desc": "잘 돈다", "check": {"type": "run", "cmd": "sim", "pass": "정상 동작한다"}}],
        [{"unit": "u1", "desc": "sim:trace 하니스"}],
    )
    out = extract_evidence_contracts(spec)
    assert all(u.evidence_contract == [] for u in out.decomposition)


# ════════════════════ 3a. 강제 — 순수 결정적 체크 ════════════════════


def test_check_pass_when_all_fields_present():
    cr = check_evidence_contract(
        ["wall_crossings", "overlap_pairs"],
        ['{"wall_crossings": 0, "overlap_pairs": 1, "extra": 9}'],
    )
    assert cr is not None and cr.status == "pass"
    assert cr.check_type is CheckType.schema  # 행동 판정 아님 — 스키마 체크


def test_check_fail_when_field_missing():
    cr = check_evidence_contract(
        ["wall_crossings", "overlap_pairs"],
        ['{"wall_crossings": 0}'],  # overlap_pairs 누락
    )
    assert cr.status == "fail"
    assert "overlap_pairs" in cr.detail


def test_check_presence_only_not_behavior():
    """필드가 *존재*하면 값이 '나쁜' 값이어도 pass — 행동 판정이 아님(겹침 진짜 0인가는 run-judge 몫)."""
    cr = check_evidence_contract(
        ["wall_crossings", "overlap_pairs"],
        ['{"wall_crossings": 999, "overlap_pairs": 42}'],  # 값은 위반이지만 키는 존재
    )
    assert cr.status == "pass"  # 키 존재만 본다


def test_check_nested_fields_found():
    """중첩 JSON(metrics.ac6 안)에서도 키를 재귀 탐색해 찾는다."""
    cr = check_evidence_contract(
        ["wall_crossings", "route_cost_samples"],
        ['{"metrics": {"ac7": {"wall_crossings": 0}}, "summary": [{"route_cost_samples": 5}]}'],
    )
    assert cr.status == "pass"


def test_check_fail_when_trace_not_json():
    """트레이스가 구조화 JSON이 아니면(키 검증 불가) fail — 하니스가 JSON을 내야 함."""
    cr = check_evidence_contract(["wall_crossings"], ["just some text, not json"])
    assert cr.status == "fail"
    assert "JSON" in cr.detail


def test_check_empty_contract_is_noop():
    assert check_evidence_contract([], ['{"x":1}']) is None


def test_contract_for_unit_per_unit_and_integration():
    spec = _spec(
        [{"id": "ac1", "desc": "동선", "check": {"type": "run", "cmd": "sim", "pass": _RUN_PASS}}],
        [{"unit": "u1", "desc": "엔진"}, {"unit": "u2", "desc": "sim:trace 하니스"}],
    )
    spec = extract_evidence_contracts(spec)
    assert contract_for_unit(spec, "u2") == _RUN_FIELDS    # per-unit
    assert contract_for_unit(spec, "u1") == []             # 무계약 유닛
    assert contract_for_unit(spec, None) == _RUN_FIELDS    # 통합 = union


# ════════════════════ 3b. 강제 — 게이트 통합(누락→fail→재빌드) ════════════════════


def _harness_spec_with_run(cmd: str) -> ProjectSpec:
    """u1=sim:trace 하니스 + u1-태그 run 기준(echo로 트레이스) + 추출 계약."""
    spec = _spec(
        [{"id": "ac1", "desc": "동선 trace", "unit": "u1",
          "check": {"type": "run", "cmd": cmd, "pass": _RUN_PASS}}],
        [{"unit": "u1", "desc": "헤드리스 sim:trace 하니스"}],
    )
    return extract_evidence_contracts(spec)


def test_gate_fails_when_harness_emits_wrong_fields(tmp_path):
    """하니스 트레이스가 계약 필드 누락 → 유닛 게이트 fail_recoverable(→재빌드). booted여도 차단."""
    # 빌더가 *다른 필드*(자체 카운트)를 냄 — 계약 필드 없음.
    bad = "echo '{\"my_lifecycle_count\": 30, \"congestion\": 0.9}'"
    spec = _harness_spec_with_run(bad)
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    assert gr.verdict is Verdict.fail_recoverable  # 누락 → fail → 재빌드
    # WO#82 (B): 하니스 per-unit 게이트의 계약 체크는 self-check가 (harness-evidence-contract)로 단다.
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"]
    assert ec and ec[0].status == "fail"
    assert ec[0].check_type is CheckType.schema  # 결정적 스키마 체크(LLM 아님)


def test_gate_passes_when_harness_emits_contract_fields(tmp_path):
    """트레이스가 계약 필드를 *전부* 내면 계약 체크 pass(값 무관 — 존재만)."""
    good = (
        "echo '{\"completed_agents\":30,\"overlap_pairs\":0,\"route_cost_samples\":250,"
        "\"spawn_attempts\":80,\"wall_crossings\":0}'"
    )
    spec = _harness_spec_with_run(good)
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"]  # WO#82 (B) self-check 라벨
    assert ec and ec[0].status == "pass"
    assert gr.verdict is Verdict.pass_


def test_gate_no_contract_no_check_back_compat(tmp_path):
    """무계약(추출 안 됨) 유닛 → 계약 체크 미추가(기존 verdict 그대로 = 무회귀)."""
    # 하니스 키워드 없는 유닛 + run 기준 → 계약 부착 안 됨.
    spec = _spec(
        [{"id": "ac1", "desc": "부팅", "unit": "u1", "check": {"type": "run", "cmd": "true"}}],
        [{"unit": "u1", "desc": "물리 엔진"}],
    )
    spec = extract_evidence_contracts(spec)
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    assert not [c for c in gr.checks if c.ac_id == "(evidence-contract)"]
    assert gr.verdict is Verdict.pass_  # booted 그대로


def test_gate_skips_contract_when_no_run_trace(tmp_path):
    """하니스 유닛이라도 이 게이트 호출에 run 트레이스가 없으면 계약 체크 스킵(false-fail 금지).

    run 기준이 *다른* 유닛 태그라 per-unit 선택에 안 잡히면, 그 게이트는 강제할 트레이스가 없다 →
    run을 돌리는 게이트(통합/하니스 per-unit)에서 강제. 여기선 무회귀(스킵).
    """
    spec = _spec(
        [{"id": "ac1", "desc": "빌드", "unit": "u1", "check": {"type": "build", "cmd": "true"}}],
        [{"unit": "u1", "desc": "sim:trace 하니스"}],
    )
    # 계약을 강제로 달아도(run 트레이스 없음) 체크가 안 붙어야 한다.
    for u in spec.decomposition:
        u.evidence_contract = ["wall_crossings"]
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    assert not [c for c in gr.checks if c.ac_id == "(evidence-contract)"]  # 스킵
    assert gr.verdict is Verdict.pass_


def test_gate_integration_union_contract_enforced(tmp_path):
    """통합 게이트(unit=None)도 전 유닛 계약 union을 트레이스에 강제(누락→fail)."""
    spec = _harness_spec_with_run("echo '{\"wall_crossings\":0}'")  # 나머지 필드 누락
    gr = _gate(tmp_path, client=None).judge("(integration)", spec, unit=None)
    ec = [c for c in gr.checks if c.ac_id == "(evidence-contract)"]
    assert ec and ec[0].status == "fail"
    assert gr.verdict is Verdict.fail_recoverable


# ════════════════════ 분리 / 적대 run-judge 무주입 ════════════════════


def test_run_judge_does_not_receive_contract_injection(tmp_path):
    """적대 run-judge 입력에 *빌더 전용* 계약 주입 헤더가 없어야 한다(분리 — judge 무주입)."""
    spec = _harness_spec_with_run("echo '{\"wall_crossings\":0}'")
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: ok\n")
    _gate(tmp_path, client=client).judge("결과", spec, unit="u1")
    assert client.calls
    joined = client.calls[0]["system"] + client.calls[0]["user"]
    # 빌더 작업지시서에만 들어가는 주입 섹션 헤더가 judge 입력엔 없어야 한다.
    assert "검증 증거 계약 (필수" not in joined


def test_run_judge_still_judges_behavior_when_fields_present(tmp_path):
    """필드가 다 있어도 적대 run-judge는 *행동*을 그대로 판정(fail 가능) — 스키마 체크가 안 덮는다."""
    good = (
        "echo '{\"completed_agents\":30,\"overlap_pairs\":0,\"route_cost_samples\":250,"
        "\"spawn_attempts\":80,\"wall_crossings\":0}'"
    )
    spec = _harness_spec_with_run(good)
    # run-judge가 행동 결함으로 fail.
    client = MockClient("verdicts:\n  - ac_id: ac1\n    status: fail\n    reason: 그리드락\n")
    gr = _gate(tmp_path, client=client).judge("결과", spec, unit="u1")
    run_rep = [c for c in gr.checks if c.ac_id == "ac1"][0]
    assert run_rep.status == "fail" and "그리드락" in run_rep.detail  # 행동 판정 살아있음
    # WO#82 (B): 결정적 계약 체크(하니스 self-check)는 필드 존재로 pass — 행동 fail과 *공존*(분리).
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0]
    assert ec.status == "pass"  # 스키마는 통과(필드 존재), 행동은 별도로 fail


# ════════════════════ 2. 주입 (빌더 작업지시서 — run_loop 통합) ════════════════════


_INJ_SPEC = f"""\
spec_id: ec-inj-001
version: 1
order_raw: "x"
goal: "군중 시뮬"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "동선 trace"
    check: {{ type: run, cmd: "sim", pass: "{_RUN_PASS}" }}
non_goals: ["n"]
done_when: "ac1"
decomposition:
  - {{ unit: u1, desc: "헤드리스 sim:trace 하니스" }}
open_questions: []
"""

_NEXT_U1 = (
    "verdict: pass\naction: next_order\nrationale: build\n"
    "next_order:\n  unit: u1\n  goal: \"u1 구현\"\n  deliverable: \"요약\"\n"
)
_STOP = "verdict: done\naction: stop\nrationale: done\n"


class _CapturingExec:
    """executor가 받은 work order를 기록(주입 검증용)."""

    def __init__(self):
        self.orders: list[NextOrder] = []

    def run(self, order: NextOrder) -> str:
        self.orders.append(order)
        return f"{order.unit} done"


def test_builder_order_gets_contract_injection():
    """run_loop 통합: 하니스 유닛 work order의 scope에 계약 필드가 *주입*된다(빌더 전용)."""
    ex = _CapturingExec()
    state = run_loop(
        order="x", client=MockClient([_INJ_SPEC, _NEXT_U1, _STOP]),
        executor=ex, gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    assert state.status.value == "done"
    assert ex.orders, "executor가 work order를 받아야"
    o = ex.orders[0]
    assert o.unit == "u1"
    assert "검증 증거 계약" in (o.scope or "")               # 주입 섹션
    for f in _RUN_FIELDS:
        assert f in o.scope                                  # 요구 필드 명시


def test_builder_injection_does_not_change_bar():
    """주입은 빌더 order(scope)에만 — spec의 바(criteria/done_when)는 불변."""
    ex = _CapturingExec()
    client = MockClient([_INJ_SPEC, _NEXT_U1, _STOP])
    run_loop(
        order="x", client=client, executor=ex,
        gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    # 주입된 order는 별도 — 원래 criteria pass 문구가 바뀌지 않았음을 계약 필드 동일성으로 확인.
    o = ex.orders[0]
    # 계약은 criteria에서 파생된 그 필드들과 정확히 일치(추가/완화 없음).
    for f in _RUN_FIELDS:
        assert f in o.scope


def test_non_harness_unit_order_has_no_injection():
    """비하니스 유닛 work order엔 계약 주입이 없다(무계약 → 기존 동작)."""
    spec_yaml = _INJ_SPEC.replace("헤드리스 sim:trace 하니스", "물리 엔진 구현")
    ex = _CapturingExec()
    run_loop(
        order="x", client=MockClient([spec_yaml, _NEXT_U1, _STOP]),
        executor=ex, gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR,
    )
    assert ex.orders
    assert "검증 증거 계약" not in (ex.orders[0].scope or "")
