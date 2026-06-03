"""replan 러너 테스트 — mock LLM만 사용(네트워크/시크릿 없음)."""

from pathlib import Path

import pytest

from haetae.llm import MockClient
from haetae.models import Action, Decision, ProjectSpec, State, Verdict
from haetae.replan import ReplanError, replan

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "spec"
PROMPT_PATH = REPO_ROOT / "prompts" / "replan.md"

VALID_DECISION_YAML = """\
verdict: pass
action: next_order
rationale: "u1 통과로 골격 확보 → u2가 done_when의 ac2에 기여"
next_order:
  unit: u2
  goal: "데미지/판정 로직 구현"
  scope: "판정 규칙만. 연동(u3)은 제외"
  context_refs: ["spec.ac2", "state.u1 산출물"]
  local_checks: [{ type: test, cmd: "cargo test combat_damage" }]
  executor: codex
  deliverable: "변경 파일 목록 + 요약"
"""


def _spec() -> ProjectSpec:
    return ProjectSpec.from_yaml(SPEC_DIR / "projectspec.schema.yaml")


def _state() -> State:
    return State.from_yaml(SPEC_DIR / "state.schema.yaml")


def _replan(response: str) -> Decision:
    return replan(
        _spec(), _state(), "u1 결과: 통과 (bench 11 tps)",
        MockClient(response), prompt_path=PROMPT_PATH,
    )


# ──────────────────────────── positive ────────────────────────────


def test_replan_returns_validated_decision():
    d = _replan(VALID_DECISION_YAML)
    assert isinstance(d, Decision)
    assert d.verdict is Verdict.pass_
    assert d.action is Action.next_order
    assert d.next_order is not None
    assert d.next_order.unit == "u2"
    assert d.next_order.local_checks[0].type.value == "test"
    assert d.next_order.executor == "codex"


def test_replan_serializes_spec_and_state_into_user():
    client = MockClient(VALID_DECISION_YAML)
    replan(_spec(), _state(), "방금 결과 요약", client, prompt_path=PROMPT_PATH)
    user = client.calls[0]["user"]
    assert "spec" in user and "state" in user
    assert "방금 결과 요약" in user
    # spec의 핵심 식별자가 직렬화돼 들어갔는지
    assert "ws-combat-001" in user


def test_replan_escalate_decision():
    yaml_text = """\
verdict: ambiguous
action: escalate
rationale: "goal 변경이 필요해 보임 — 사람 tier"
escalation:
  question: "전투에 공성전을 포함할까요?"
  why_now: "goal 경계가 바뀌는 결정"
"""
    d = _replan(yaml_text)
    assert d.action is Action.escalate
    assert d.escalation is not None
    assert "공성전" in d.escalation.question
    assert d.next_order is None


# ──────────────────────────── negative ────────────────────────────


def test_replan_rejects_invalid_action():
    bad = VALID_DECISION_YAML.replace("action: next_order", "action: teleport")
    with pytest.raises(ReplanError) as ei:
        _replan(bad)
    assert "teleport" in ei.value.raw_response


def test_replan_rejects_invalid_verdict():
    bad = VALID_DECISION_YAML.replace("verdict: pass", "verdict: vibes")
    with pytest.raises(ReplanError):
        _replan(bad)


def test_replan_rejects_broken_yaml():
    with pytest.raises(ReplanError):
        _replan("verdict: [unclosed\n : : :")
