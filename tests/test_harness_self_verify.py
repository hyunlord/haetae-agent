"""WO#82 — 하니스 자기검증 테스트 (mock, codex/네트워크 없음).

#81이 드러낸 hollow-verification 두 곳의 수정:
  A. #78 brittle — 산문 criteria(pass=None)에서 snake_case 토큰 0 → 계약 미부착(no-op).
     → 합성기가 구조화 `evidence_fields` 명시 + 추출이 그 슬롯을 *우선* 읽음(산문에도 견고).
  B. 깨진 하니스 통과 — sim:trace가 통합(자주 미도달)까지 안 잡힘.
     → 하니스 *per-unit* 게이트가 sim:trace를 *실제 실행*(clean-install)해 크래시·필드누락 조기 차단.
분리 유지: per-unit은 *결정적*(실행 exit + 필드 존재). 값/행동은 적대 run-judge(통합, LLM) 그대로.
"""

from pathlib import Path

from haetae.gate import CompositeGate
from haetae.intake import (
    extract_evidence_contracts,
    extract_required_evidence_fields,
)
from haetae.llm import MockClient
from haetae.models import CheckType, ProjectSpec, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"
SYNTH_PROMPT = REPO_ROOT / "prompts" / "synthesizer.md"


def _spec(acs: list[dict], decomp: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate({
        "spec_id": "hsv-001", "version": 1, "order_raw": "x", "goal": "g",
        "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
        "acceptance_criteria": acs, "non_goals": ["n"], "done_when": "통과",
        "decomposition": decomp,
    })


def _gate(tmp_path, client=None) -> CompositeGate:
    return CompositeGate(
        workdir=tmp_path, judge_client=client,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10, install_deps=False,
    )


# ════════════════════ A. 견고한 추출 (산문 brittle 해소) ════════════════════


def test_structured_evidence_fields_extracted_from_prose_criteria():
    """#81 재현 수정: pass=None인 *산문* run 기준이라도 구조화 evidence_fields면 추출됨(이전엔 0)."""
    spec = _spec(
        [{"id": "ac6", "desc": "벽 swept collision 위반이 0건임을 출력한다",  # 산문, snake 토큰 0
          "check": {"type": "run", "cmd": "npm run sim:trace -- --scenario lifecycle"},
          "evidence_fields": ["wall_crossings", "overlap_pairs", "swept_collisions"]}],
        [{"unit": "u7", "desc": "헤드리스 sim:trace 하니스"}],
    )
    fields = extract_required_evidence_fields(spec)
    assert fields == ["overlap_pairs", "swept_collisions", "wall_crossings"]  # 구조화서 추출
    # 계약이 하니스 유닛에 부착(이전 #81에선 0필드 → 미부착)
    s2 = extract_evidence_contracts(spec)
    assert [u.evidence_contract for u in s2.decomposition if u.unit == "u7"][0] == fields


def test_structured_slot_takes_priority_over_scrape():
    """구조화 evidence_fields가 있으면 그것을 권위로(pass의 snake_case 스크레이프보다 우선)."""
    spec = _spec(
        [{"id": "ac6", "desc": "d", "check": {"type": "run", "cmd": "sim",
          "pass": "stdout JSON에 legacy_field == 0"},  # 스크레이프하면 legacy_field
          "evidence_fields": ["wall_crossings"]}],  # 구조화 우선 → 이것만
        [{"unit": "u7", "desc": "trace 하니스"}],
    )
    assert extract_required_evidence_fields(spec) == ["wall_crossings"]  # 구조화 우선
    assert "legacy_field" not in extract_required_evidence_fields(spec)


def test_scrape_fallback_when_no_structured_slot():
    """구조화 슬롯 전무면 기존 #78 동작대로 pass/desc snake_case 스크레이프(back-compat)."""
    spec = _spec(
        [{"id": "ac6", "desc": "d", "check": {"type": "run", "cmd": "sim",
          "pass": "stdout JSON에 wall_crossings == 0, route_cost_samples >= 200"}}],
        [{"unit": "u7", "desc": "trace 하니스"}],
    )
    assert extract_required_evidence_fields(spec) == ["route_cost_samples", "wall_crossings"]


def test_synthesizer_prompt_instructs_evidence_fields():
    """합성기 프롬프트가 run 기준에 evidence_fields를 명시하라고 지시한다(A 원천)."""
    src = SYNTH_PROMPT.read_text(encoding="utf-8")
    assert "evidence_fields" in src
    assert "정확한 키 이름" in src or "정확히 이 필드" in src


# ════════════════════ B. 하니스 per-unit 게이트가 sim:trace 실제 실행 ════════════════════
#
# 핵심(#81 수정): run 기준이 *integration-태그*(통합서만 검사)여도, 하니스 *per-unit* 게이트가
# 그 sim:trace를 실제 실행해 크래시·필드누락을 조기 차단한다(통합 미도달이어도).


def _harness_spec(cmd: str, fields=None) -> ProjectSpec:
    """u7=sim:trace 하니스 + *integration-태그* run 기준(unit 생략) + 구조화 evidence_fields."""
    spec = _spec(
        [{"id": "ac6", "desc": "동선 trace", "check": {"type": "run", "cmd": cmd},
          "evidence_fields": fields or ["wall_crossings", "overlap_pairs"]}],
        [{"unit": "u7", "desc": "헤드리스 sim:trace 하니스"}],
    )
    return extract_evidence_contracts(spec)


def test_broken_harness_fails_at_per_unit_gate(tmp_path):
    """깨진 하니스(sim:trace exit≠0, 예: 미선언 dep 127) → 하니스 per-unit 게이트 fail→재빌드.

    #81 정확 재현: ac6은 integration-태그(per-unit 선택 미포함)이나, u7이 하니스라 self-check가
    sim:trace를 *실제 실행* → 크래시 잡음. 통합 미도달이어도 per-unit서 조기 차단.
    """
    spec = _harness_spec("this-cmd-does-not-exist-xyz --scenario lifecycle")
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u7")
    assert gr.verdict is Verdict.fail_recoverable        # 깨진 하니스 → fail → 재빌드
    runrep = [c for c in gr.checks if c.ac_id == "(harness-run:ac6)"]
    assert runrep and runrep[0].status == "fail"          # 실제 실행 → 크래시 포착
    assert runrep[0].run_evidence is not None and runrep[0].run_evidence.booted is False


def test_valid_harness_passes_per_unit_gate(tmp_path):
    """유효 하니스(실행 OK + 계약 필드 emit) → per-unit 게이트 pass."""
    spec = _harness_spec("echo '{\"wall_crossings\":0,\"overlap_pairs\":0}'")
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u7")
    assert gr.verdict is Verdict.pass_
    assert [c for c in gr.checks if c.ac_id == "(harness-run:ac6)"][0].status == "pass"
    assert [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0].status == "pass"


def test_wrong_fields_harness_fails_per_unit_gate(tmp_path):
    """실행은 되나 *틀린 필드*를 emit하는 하니스 → 계약 체크 fail→재빌드(hollow 차단)."""
    spec = _harness_spec("echo '{\"my_own_count\":42}'")  # 계약 필드 없음
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u7")
    assert gr.verdict is Verdict.fail_recoverable
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0]
    assert ec.status == "fail" and "overlap_pairs" in ec.detail
    # 실행 자체는 booted(exit 0) — 깨진 게 아니라 *틀린 필드*임을 구분
    assert [c for c in gr.checks if c.ac_id == "(harness-run:ac6)"][0].status == "pass"


def test_per_unit_check_is_presence_not_behavior(tmp_path):
    """분리: per-unit 계약 체크는 *필드 존재*만(결정적, schema 타입) — 값이 나빠도 pass.

    값/행동(겹침이 진짜 0인가)은 적대 run-judge(통합, LLM) 몫 — per-unit이 그걸 덮지 않는다.
    """
    # 필드는 다 있으나 값은 위반(wall_crossings=999) — per-unit은 존재만 보므로 pass.
    spec = _harness_spec("echo '{\"wall_crossings\":999,\"overlap_pairs\":42}'")
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u7")
    ec = [c for c in gr.checks if c.ac_id == "(harness-evidence-contract)"][0]
    assert ec.status == "pass"                    # 존재만 — 값 판정 아님
    assert ec.check_type is CheckType.schema       # 결정적 스키마 체크(LLM 아님)
    assert gr.verdict is Verdict.pass_


def test_harness_self_check_is_deterministic_no_llm(tmp_path):
    """per-unit self-check는 LLM run-judge를 호출하지 않는다(결정적). judge_client이 있어도 무호출."""
    client = MockClient("verdicts:\n  - ac_id: ac6\n    status: fail\n    reason: x\n")
    spec = _harness_spec("echo '{\"wall_crossings\":0,\"overlap_pairs\":0}'")
    gr = _gate(tmp_path, client=client).judge("결과", spec, unit="u7")
    # self-check가 결정적으로 실행/필드 판정 → run-judge LLM 미호출(ac6은 integration-태그라
    # per-unit 선택에 없음 → LLMJudge로 안 감). client.calls 비어야(per-unit 결정적).
    assert not client.calls
    assert [c for c in gr.checks if c.ac_id == "(harness-run:ac6)"][0].status == "pass"


def test_non_harness_unit_no_self_check(tmp_path):
    """무회귀: 하니스 아닌 유닛(계약 없음)은 self-check 미실행 → 기존 동작(pass)."""
    spec = _spec(
        [{"id": "ac6", "desc": "trace", "check": {"type": "run", "cmd": "echo x"},
          "evidence_fields": ["wall_crossings"]}],
        [{"unit": "u1", "desc": "물리 엔진"}],  # 하니스 키워드 없음 → 계약 미부착
    )
    spec = extract_evidence_contracts(spec)
    gr = _gate(tmp_path, client=None).judge("결과", spec, unit="u1")
    assert not [c for c in gr.checks if c.ac_id.startswith("(harness-")]  # self-check 없음
    assert gr.verdict is Verdict.pass_


def test_integration_gate_unchanged_uses_run_judge(tmp_path):
    """무회귀: 통합 게이트(unit=None)는 self-check가 아니라 기존 run-judge(LLM) 경로(분리 보존)."""
    client = MockClient("verdicts:\n  - ac_id: ac6\n    status: fail\n    reason: 그리드락\n")
    spec = _harness_spec("echo '{\"wall_crossings\":0,\"overlap_pairs\":0}'")
    gr = _gate(tmp_path, client=client).judge("(integration)", spec, unit=None)
    # 통합은 run 기준을 LLM run-judge로 판정(self-check 아님) → client 호출됨, ac6 행동 fail.
    assert client.calls
    assert not [c for c in gr.checks if c.ac_id == "(harness-run:ac6)"]  # self-check는 per-unit 전용
    assert [c for c in gr.checks if c.ac_id == "ac6"][0].status == "fail"  # 행동 판정(LLM)
