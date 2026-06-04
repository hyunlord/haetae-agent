"""loop driver 테스트 — mock LLM/executor/gate만 사용(네트워크/시크릿 없음)."""

from pathlib import Path

import pytest

from haetae.llm import MockClient
from haetae.loop import Executor, Gate, MockExecutor, MockGate, run_loop
from haetae.models import CheckReport, State, Status, Verdict

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


# ──────────────────────────── 진행 표시 (WO#13) ────────────────────────────


def test_run_loop_emits_progress_labels():
    """progress 콜백에 synthesize/replan/execute/gate/종료 라벨이 흘러오는지."""
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    executor = MockExecutor(["u1 done", "u2 done"])
    gate = MockGate([Verdict.pass_, Verdict.done])

    seen: list[str] = []
    state = run_loop(order="x", client=client, executor=executor, gate=gate,
                     prompt_dir=PROMPT_DIR, progress=seen.append)

    assert state.status is Status.done
    assert any(s.startswith("합성 중") for s in seen)
    assert any(s.startswith("replan 중") for s in seen)
    assert any(s.startswith("작업 실행 중") for s in seen)
    assert any(s.startswith("gate 검사 중") for s in seen)
    assert any(s.startswith("종료") for s in seen)


def test_run_loop_progress_defaults_to_noop(capsys):
    """progress 기본 None → 표준출력/에러로 아무것도 새지 않는다."""
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    run_loop(order="x", client=client,
             executor=MockExecutor(["u1 done", "u2 done"]),
             gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR)
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


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


# ──────────────────── gate 근거 → Event.checks 저장 (WO#14) ────────────────────


def _report(ac_id: str, status: str = "pass") -> CheckReport:
    return CheckReport(
        ac_id=ac_id, check_type="test", cmd=f"pytest {ac_id}",
        status=status, exit_code=0 if status == "pass" else 1,
    )


def test_run_loop_persists_gate_evidence_into_events():
    """gate가 GateResult(checks 포함)를 반환하면 그 근거가 Event.checks에 실린다."""
    evidence = [_report("ac1")]
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    gate = MockGate([Verdict.pass_, Verdict.done], checks=evidence)
    state = run_loop(order="x", client=client,
                     executor=MockExecutor(["a", "b"]), gate=gate,
                     prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    # 두 이벤트 모두 근거가 비어있지 않고 CheckReport로 채워졌는지
    assert all(len(ev.checks) == 1 for ev in state.events)
    c = state.events[0].checks[0]
    assert c.ac_id == "ac1"
    assert c.check_type.value == "test"
    assert c.status == "pass"
    assert c.exit_code == 0


def test_run_loop_event_checks_roundtrip_through_yaml(tmp_path):
    """Event.checks(CheckReport)가 YAML로 저장되고 다시 State로 로드되는지(감사 로그)."""
    out = tmp_path / "state.yaml"
    evidence = [_report("ac1", "pass"), _report("ac2", "fail")]
    client = MockClient([SPEC_YAML, _next_order("u1")])
    run_loop(order="x", client=client,
             executor=MockExecutor("a"),
             gate=MockGate(Verdict.done, checks=evidence),
             prompt_dir=PROMPT_DIR, state_path=out)
    reloaded = State.from_yaml(out)
    checks = reloaded.events[0].checks
    assert [c.ac_id for c in checks] == ["ac1", "ac2"]
    assert checks[1].status == "fail"
    assert checks[1].exit_code == 1
    assert checks[0].check_type.value == "test"


# ──────────────────────────── Protocol 적합성 ────────────────────────────


def test_mocks_satisfy_protocols():
    assert isinstance(MockExecutor("x"), Executor)
    assert isinstance(MockGate(Verdict.pass_), Gate)


# ──────────────────── 루프 내성 (WO#12 — LLM 출력으로 crash 금지) ────────────────────

# 정규화로도 못 고치는 검증 실패(action enum 위반) → replan이 ReplanError를 낸다.
DEC_INVALID = """\
verdict: pass
action: teleport
rationale: "미지원 action — 검증 실패용"
"""


def test_run_loop_replan_retries_then_succeeds():
    """첫 replan이 검증 실패해도 재시도로 정상 출력을 얻으면 crash 없이 진행한다."""
    # iter1: replan attempt1=DEC_INVALID(실패) → attempt2=정상 next_order → done
    client = MockClient([SPEC_YAML, DEC_INVALID, _next_order("u1")])
    state = run_loop(order="x", client=client,
                     executor=MockExecutor("u1 done"), gate=MockGate(Verdict.done),
                     prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert len(state.events) == 1
    assert state.events[0].unit == "u1"
    assert state.pending_escalations == []  # 재시도로 흡수 → escalate 없음


def test_run_loop_escalates_when_replan_retries_exhausted():
    """replan이 계속 검증 실패하면 crash 대신 escalated로 종료하고 raw를 보존한다."""
    # replan_retries=2 → iter1에서 3회 시도 모두 실패 → escalate
    client = MockClient([SPEC_YAML, DEC_INVALID, DEC_INVALID, DEC_INVALID])
    state = run_loop(order="x", client=client,
                     executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
                     replan_retries=2, prompt_dir=PROMPT_DIR)
    assert state.status is Status.escalated
    assert len(state.pending_escalations) == 1
    esc = state.pending_escalations[0]
    assert "검증 실패" in esc["reason"]
    assert "teleport" in esc["raw_response"]  # raw 응답 보존
    assert state.events == []  # 실행까지 못 감


def test_run_loop_feeds_validation_error_back_on_retry():
    """재시도 시 직전 검증 에러를 피드백으로 프롬프트에 얹어 self-correction을 유도한다."""
    client = MockClient([SPEC_YAML, DEC_INVALID, _next_order("u1")])
    run_loop(order="x", client=client,
             executor=MockExecutor("u1 done"), gate=MockGate(Verdict.done),
             prompt_dir=PROMPT_DIR)
    # calls: [0]=synthesize, [1]=replan attempt1(피드백 없음), [2]=replan retry(피드백 있음)
    assert "검증 실패" not in client.calls[1]["user"]
    assert "직전 응답이 검증에 실패" in client.calls[2]["user"]


def test_run_loop_synthesis_failure_returns_escalated_without_traceback():
    """합성 실패 시 traceback 대신 escalated State를 반환한다(spec 없음)."""
    # 매핑(dict)이 아닌 출력 → SynthesisError
    client = MockClient(["이건 spec이 아니라 그냥 문장이다"])
    state = run_loop(order="x", client=client,
                     executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
                     prompt_dir=PROMPT_DIR)
    assert state.status is Status.escalated
    assert state.spec_ref == "(synthesis-failed)"
    assert len(state.pending_escalations) == 1
    esc = state.pending_escalations[0]
    assert "합성 실패" in esc["reason"]
    assert "그냥 문장" in esc["raw_response"]  # raw 보존
    assert state.events == []


def test_run_loop_synthesis_failure_saves_state(tmp_path):
    """합성 실패로 끝나도 state_path가 주어지면 escalated State를 저장한다."""
    out = tmp_path / "state.yaml"
    client = MockClient(["not a mapping"])
    run_loop(order="x", client=client,
             executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
             prompt_dir=PROMPT_DIR, state_path=out)
    assert out.exists()
    reloaded = State.from_yaml(out)
    assert reloaded.status is Status.escalated
