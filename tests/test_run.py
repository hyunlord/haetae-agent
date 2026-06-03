"""run() 배선 테스트 — 실제 codex 없이 MockClient + 주입 executor/gate."""

from pathlib import Path

from haetae.executors import HumanRelayExecutor
from haetae.llm import MockClient
from haetae.loop import MockGate
from haetae.models import State, Status, Verdict
from haetae.run import format_summary, run

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

SPEC_YAML = """\
spec_id: run-001
version: 1
order_raw: "전투 추가"
goal: "전투 시스템 추가"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "컴포넌트 등록"
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["공성전", "애니메이션"]
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "스켈레톤", deps: [] }
  - { unit: u2, desc: "로직", deps: [u1] }
open_questions: []
"""


def _next_order(unit: str) -> str:
    return f"""\
verdict: pass
action: next_order
rationale: "{unit} 진행"
next_order:
  unit: {unit}
  goal: "{unit} 구현"
  deliverable: "요약"
"""


def test_run_wires_full_loop_to_done():
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    # present는 무시, collect는 캔된 결과 → 실제 stdin/codex 불필요
    executor = HumanRelayExecutor(present=lambda t: None, collect=lambda: "사람 실행 결과")
    gate = MockGate([Verdict.pass_, Verdict.done])

    state = run(
        "전투 추가",
        client=client,
        executor=executor,
        gate=gate,
        prompt_dir=PROMPT_DIR,
    )

    assert isinstance(state, State)
    assert state.status is Status.done
    assert len(state.events) == 2
    assert state.spec_ref == "run-001"
    # executor가 사람 결과를 루프에 전달했는지(이벤트 result에 반영)
    assert "사람 실행 결과" in state.events[0].result


def test_format_summary_contains_status_and_plan():
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    executor = HumanRelayExecutor(present=lambda t: None, collect=lambda: "r")
    gate = MockGate([Verdict.pass_, Verdict.done])
    state = run("x", client=client, executor=executor, gate=gate, prompt_dir=PROMPT_DIR)

    summary = format_summary(state)
    assert "status" in summary
    assert "done" in summary
    assert "u1=" in summary
