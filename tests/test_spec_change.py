"""governed spec-change 테스트 — mutability gradient. 결정적, mock만."""

from pathlib import Path

from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import (
    ProjectSpec,
    SpecChangeProposal,
    State,
    Status,
    Verdict,
)
from haetae.spec_change import apply_spec_change

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


# ──────────────────────────── 픽스처 ────────────────────────────


def _spec() -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "sc-001",
            "version": 1,
            "order_raw": "원래 주문",
            "goal": "목표 달성",
            "task_type": "feature_impl",
            "verifiability": "objective",
            "mode": "normal",
            "constraints": ["c1"],
            "acceptance_criteria": [
                {"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "true"}}
            ],
            "assumptions": [
                {"id": "as1", "text": "DB는 postgres", "confidence": 0.6, "checkpoint": False}
            ],
            "non_goals": ["ng1"],
            "done_when": "ac1 통과",
        }
    )


def _state(spec: ProjectSpec) -> State:
    return State(spec_ref=spec.spec_id, spec_version=spec.version, status=Status.running)


def _proposal(**kw) -> SpecChangeProposal:
    # alias "from" 입력을 흉내 내려고 model_validate 사용(populate_by_name).
    return SpecChangeProposal.model_validate(kw)


# ──────────────────── 직접 단위 테스트: apply_spec_change ────────────────────


def test_assumption_with_evidence_and_matching_from_applies():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec,
        state,
        _proposal(
            target="assumptions.as1",
            **{"from": "DB는 postgres"},
            to="DB는 sqlite",
            reason="postgres 불가 환경 확인",
            evidence="docker 로그: postgres 미설치, sqlite로 전환",
        ),
    )
    assert out.applied is True
    # spec 갱신
    assert spec.assumptions[0].text == "DB는 sqlite"
    # 버전업(spec + state 동기)
    assert spec.version == 2
    assert state.spec_version == 2
    # 감사 기록 1건
    assert len(state.spec_changes) == 1
    rec = state.spec_changes[0]
    assert rec.target == "assumptions.as1"
    assert rec.version == "2"
    assert rec.evidence and "sqlite" in rec.evidence


def test_assumption_without_evidence_escalates():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec,
        state,
        _proposal(
            target="assumptions.as1",
            **{"from": "DB는 postgres"},
            to="DB는 sqlite",
            reason="그냥 바꾸고 싶음",
        ),  # evidence 없음
    )
    assert out.applied is False
    assert "evidence 없음" in out.reason
    # 적용 안 됨 — spec 불변
    assert spec.assumptions[0].text == "DB는 postgres"
    assert spec.version == 1
    assert state.spec_changes == []


def test_assumption_with_stale_from_escalates():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec,
        state,
        _proposal(
            target="assumptions.as1",
            **{"from": "DB는 mysql"},  # 현재(postgres)와 불일치 → stale
            to="DB는 sqlite",
            reason="r",
            evidence="e",
        ),
    )
    assert out.applied is False
    assert "stale" in out.reason
    assert spec.assumptions[0].text == "DB는 postgres"
    assert spec.version == 1


def test_goal_change_escalates():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec, state, _proposal(target="goal", to="더 쉬운 목표", reason="r", evidence="e")
    )
    assert out.applied is False
    assert spec.goal == "목표 달성"  # 불변
    assert spec.version == 1


def test_done_when_change_escalates():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec, state, _proposal(target="done_when", to="대충 됨", reason="r", evidence="e")
    )
    assert out.applied is False
    assert spec.done_when == "ac1 통과"  # 불변


def test_acceptance_criteria_change_escalates():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec,
        state,
        _proposal(
            target="acceptance_criteria.ac1", to="더 느슨한 기준", reason="r", evidence="e"
        ),
    )
    assert out.applied is False
    # 합격선 불변
    assert spec.acceptance_criteria[0].desc == "d"
    assert spec.version == 1


def test_order_raw_change_rejected_immutable():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec, state, _proposal(target="order_raw", to="다른 주문", reason="r", evidence="e")
    )
    assert out.applied is False
    assert "불변" in out.reason
    assert spec.order_raw == "원래 주문"  # anchor 불변
    assert spec.version == 1


def test_constraints_and_non_goals_escalate():
    for tgt in ("constraints", "non_goals"):
        spec = _spec()
        state = _state(spec)
        out = apply_spec_change(
            spec, state, _proposal(target=tgt, to="x", reason="r", evidence="e")
        )
        assert out.applied is False
        assert spec.version == 1


def test_unknown_target_escalates():
    spec = _spec()
    state = _state(spec)
    out = apply_spec_change(
        spec, state, _proposal(target="whatever.xyz", to="x", reason="r", evidence="e")
    )
    assert out.applied is False
    assert spec.version == 1


# ──────────────────── 루프 통합: applied → 계속 / escalated → 종료 ────────────────────

_SPEC_YAML = """\
spec_id: sc-loop
version: 1
order_raw: "원래 주문"
goal: "목표 달성"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "d"
    check: { type: test, cmd: "true" }
assumptions:
  - { id: as1, text: "DB는 postgres", confidence: 0.6, checkpoint: false }
non_goals: ["ng1"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "스켈레톤", deps: [] }
open_questions: []
"""

_DEC_PROPOSE_ASSUMPTION = """\
verdict: ambiguous
action: propose_spec_change
rationale: "가정 갱신 — 증거 있음"
spec_change:
  target: "assumptions.as1"
  from: "DB는 postgres"
  to: "DB는 sqlite"
  reason: "postgres 미설치 확인"
  evidence: "docker 로그상 sqlite로 전환됨"
  version_bump: true
"""

_DEC_PROPOSE_GOAL = """\
verdict: ambiguous
action: propose_spec_change
rationale: "goal 낮추기 시도"
spec_change:
  target: "goal"
  from: "목표 달성"
  to: "쉬운 목표"
  reason: "어려움"
  evidence: "시도해보니 어렵더라"
"""

_DEC_STOP = 'verdict: done\naction: stop\nrationale: "done_when 충족"\n'


def test_loop_applies_assumption_change_and_continues():
    # iter1: propose(assumptions, evidence) → applied → 계속
    # iter2: stop → done
    client = MockClient([_SPEC_YAML, _DEC_PROPOSE_ASSUMPTION, _DEC_STOP])
    state = run_loop(
        order="x",
        client=client,
        executor=MockExecutor("noop"),
        gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done  # escalate 아님 — 계속 돌아 done
    # 감사 기록 1건 + 버전업
    assert len(state.spec_changes) == 1
    assert state.spec_changes[0].target == "assumptions.as1"
    assert state.spec_version == 2
    # applied 이벤트가 남았는지
    assert any("spec-change applied" in (e.result or "") for e in state.events)
    # 3개 응답 모두 소비(루프가 propose 후 계속해 다음 replan을 호출했다는 증거)
    assert len(client.calls) == 3


def test_loop_escalates_on_goal_change():
    client = MockClient([_SPEC_YAML, _DEC_PROPOSE_GOAL])
    state = run_loop(
        order="x",
        client=client,
        executor=MockExecutor("noop"),
        gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    assert len(state.pending_escalations) == 1
    note = state.pending_escalations[0]
    assert "goal" in note["reason"]
    # spec_change 본문이 escalation에 보존됐는지
    assert note["spec_change"]["target"] == "goal"
    # 자율 적용 안 됨 → 감사 기록 없음
    assert state.spec_changes == []
