"""loop driver 테스트 — mock LLM/executor/gate만 사용(네트워크/시크릿 없음)."""

from pathlib import Path

import pytest

from haetae.llm import MockClient
from haetae.loop import Executor, Gate, MockExecutor, MockGate, run_loop
from haetae.models import State, Status, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

SPEC_YAML = """\
spec_id: loop-001
version: 1
order_raw: "전투 시스템 추가해"
goal: "전투 시스템을 ECS에 추가"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "전투 컴포넌트 등록"
    check: { type: test, cmd: "cargo test combat" }
assumptions: []
non_goals:
  - "공성전"
  - "애니메이션"
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "스켈레톤", deps: [] }
  - { unit: u2, desc: "데미지 로직", deps: [u1] }
open_questions: []
"""


def _next_order(unit: str) -> str:
    return f"""\
verdict: pass
action: next_order
rationale: "{unit} 진행이 done_when에 기여"
next_order:
  unit: {unit}
  goal: "{unit} 구현"
  local_checks: [{{ type: test, cmd: "cargo test {unit}" }}]
  executor: codex
  deliverable: "요약"
"""


DEC_ESCALATE = """\
verdict: ambiguous
action: escalate
rationale: "goal 변경 필요 — 사람 tier"
escalation:
  question: "공성전을 포함할까요?"
  why_now: "goal 경계 변경"
"""


# ──────────────────────────── happy path: done ────────────────────────────


def test_run_loop_completes_on_done_verdict():
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    executor = MockExecutor(["u1 done", "u2 done"])
    gate = MockGate([Verdict.pass_, Verdict.done])  # 2번째에서 done → 종료

    state = run_loop(client=client, order="전투 시스템 추가해",
                     executor=executor, gate=gate, prompt_dir=PROMPT_DIR)

    assert state.status is Status.done
    assert len(state.events) == 2
    assert state.events[0].unit == "u1"
    assert state.events[1].verdict is Verdict.done
    # plan 갱신: u1, u2 모두 done
    plan_state = {p.unit: p.state.value for p in state.plan}
    assert plan_state["u1"] == "done"
    assert plan_state["u2"] == "done"
    assert len(executor.calls) == 2
    assert len(gate.calls) == 2


def test_run_loop_stop_action_completes():
    dec_stop = "verdict: done\naction: stop\nrationale: \"done_when 충족\"\n"
    client = MockClient([SPEC_YAML, dec_stop])
    state = run_loop(order="x", client=client,
                     executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
                     prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert state.events == []  # stop은 executor 호출 없음


# ──────────────────────────── escalate ────────────────────────────


def test_run_loop_escalate():
    client = MockClient([SPEC_YAML, DEC_ESCALATE])
    state = run_loop(order="x", client=client,
                     executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
                     prompt_dir=PROMPT_DIR)
    assert state.status is Status.escalated
    assert len(state.pending_escalations) == 1
    assert "공성전" in state.pending_escalations[0]["question"]


# ──────────────────────────── max_iters 캡 ────────────────────────────


def test_run_loop_max_iters_caps():
    # 끝나지 않는 스크립트(매번 next_order, gate는 pass만) → max_iters에서 종료
    client = MockClient([SPEC_YAML] + [_next_order("u1")] * 3)
    state = run_loop(order="x", client=client,
                     executor=MockExecutor("again"), gate=MockGate(Verdict.pass_),
                     max_iters=3, prompt_dir=PROMPT_DIR)
    assert state.status is Status.stopped_stuck
    assert len(state.events) == 3


# ──────────────────────────── state_path 저장/재로드 ────────────────────────────


def test_run_loop_saves_and_reloads_state(tmp_path):
    out = tmp_path / "state.yaml"
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    state = run_loop(order="x", client=client,
                     executor=MockExecutor(["a", "b"]),
                     gate=MockGate([Verdict.pass_, Verdict.done]),
                     prompt_dir=PROMPT_DIR, state_path=out)
    assert out.exists()
    reloaded = State.from_yaml(out)
    assert reloaded.status is Status.done
    assert reloaded.spec_ref == "loop-001"
    assert len(reloaded.events) == 2


# ──────────────────────────── Protocol 적합성 ────────────────────────────


def test_mocks_satisfy_protocols():
    assert isinstance(MockExecutor("x"), Executor)
    assert isinstance(MockGate(Verdict.pass_), Gate)
