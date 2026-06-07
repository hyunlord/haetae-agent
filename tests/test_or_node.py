"""OR-node 헬퍼(WO#41) 단위 테스트 — gate 증거 요약 + bar-불변 대안 지시.

순수 함수(LLM/네트워크 없음). bar 불변(anti-erosion)이 지시 텍스트에 박혀있는지 가드.
"""

import haetae.providers.codex as codex_mod
from haetae.models import CheckReport, CheckType, GateResult, RunEvidence, Verdict
from haetae.or_node import build_alternative_feedback, summarize_gate_evidence


# ──────────────────────────── summarize_gate_evidence ────────────────────────────


def test_summarize_uses_failed_check_detail():
    gr = GateResult(
        verdict=Verdict.fail_recoverable,
        checks=[
            CheckReport(ac_id="ac1", check_type=CheckType.test, status="pass"),
            CheckReport(ac_id="ac2", check_type=CheckType.test, status="fail",
                        detail="충돌이 여전히 발생"),
        ],
    )
    s = summarize_gate_evidence(gr)
    assert s is not None
    assert "ac2" in s and "충돌" in s
    assert "ac1" not in s  # pass한 건 요약 안 함


def test_summarize_prefers_run_judge_reason():
    gr = GateResult(
        verdict=Verdict.fail_recoverable,
        checks=[CheckReport(ac_id="ac3", check_type=CheckType.run, status="fail",
                            detail="exit 0",
                            run_evidence=RunEvidence(booted=False, reason="행동 증거 없음"))],
    )
    s = summarize_gate_evidence(gr)
    assert "행동 증거 없음" in s  # run-judge reason 우선


def test_summarize_no_failed_checks_falls_back_to_verdict():
    gr = GateResult(verdict=Verdict.fail_recoverable, checks=[])
    s = summarize_gate_evidence(gr)
    assert s is not None and "fail_recoverable" in s


def test_summarize_handles_weird_input_no_raise():
    """best-effort: 이상한 입력에도 raise하지 않는다(None/속성없음)."""
    class Bogus:
        pass
    # checks 없음 + verdict 없음 → None(무크래시)
    assert summarize_gate_evidence(Bogus()) is None


# ──────────────────────────── build_alternative_feedback ────────────────────────────


def test_alternative_feedback_is_bar_invariant():
    """**bar 불변 가드**: 대안 지시는 criteria/done_when을 *바꾸지 말라*고 못박는다."""
    fb = build_alternative_feedback("격자-이산 충돌검사", "ac2: 겹침 여전", scope="unit")
    # 접근만 바꾸라는 directive + 기준 불변 못박음
    assert "다른 접근" in fb
    assert "acceptance_criteria" in fb and "바꾸지" in fb
    assert "낮추" in fb or "재정의" in fb  # 기준 약화/재정의 금지 명시
    # 폐기 접근 + 실패 증거가 들어간다(반복 회피)
    assert "격자-이산 충돌검사" in fb
    assert "ac2" in fb


def test_alternative_feedback_integration_scope():
    fb = build_alternative_feedback(None, "통합 깨짐", scope="integration")
    assert "통합" in fb
    assert "acceptance_criteria" in fb and "바꾸지" in fb  # bar 불변은 통합에서도


# ──────────────────────────── 안전 불변 가드 ────────────────────────────


def test_allowed_sandboxes_unchanged_by_or_node():
    """OR-node는 sandbox 권한과 무관 — ALLOWED_SANDBOXES 불변(가드)."""
    assert codex_mod.ALLOWED_SANDBOXES == ("read-only", "workspace-write")
    assert "danger-full-access" not in codex_mod.ALLOWED_SANDBOXES
