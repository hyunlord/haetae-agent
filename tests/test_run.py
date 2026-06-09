"""run() 배선 테스트 — 실제 codex 없이 MockClient + 주입 executor/gate."""

from pathlib import Path

import haetae.run as run_mod
from haetae.executors import CodexExecutor, HumanRelayExecutor
from haetae.llm import MockClient
from haetae.loop import MockGate
from haetae.models import State, Status, Verdict
from haetae.run import format_summary, main, run

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


# ──────────────────────────── --executor 배선 (WO#13) ────────────────────────────


def _capture_main_run(monkeypatch):
    """main()이 run()에 넘기는 kwargs를 캡처하고, 실제 루프는 돌리지 않는다."""
    captured = {}

    def fake_run(order, **kwargs):
        captured["order"] = order
        captured.update(kwargs)
        return State(spec_ref="x", spec_version=1, status=Status.done)

    monkeypatch.setattr(run_mod, "run", fake_run)
    return captured


def test_main_executor_codex_wires_codexexecutor(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    rc = main(["--order", "x", "--executor", "codex", "--workdir", "/tmp/scratch"])
    assert rc == 0
    ex = captured["executor"]
    assert isinstance(ex, CodexExecutor)
    # gate와 같은 --workdir로 범위가 한정됐는지
    assert str(ex.workdir) == "/tmp/scratch"
    # 가장 좁은 쓰기 sandbox 기본
    assert ex.sandbox == "workspace-write"
    # progress 함수가 주입됐는지
    assert callable(captured.get("progress"))


def test_main_executor_defaults_to_human(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    rc = main(["--order", "x"])
    assert rc == 0
    assert isinstance(captured["executor"], HumanRelayExecutor)


# ──────────────────────────── --reasoning-effort 배선 (WO#38) ────────────────────────────


def test_main_reasoning_effort_wires_into_codexexecutor(monkeypatch):
    """--reasoning-effort가 CodexExecutor로 전달된다(설정 시)."""
    captured = _capture_main_run(monkeypatch)
    rc = main(["--order", "x", "--executor", "codex", "--workdir", "/tmp/s",
               "--reasoning-effort", "xhigh"])
    assert rc == 0
    ex = captured["executor"]
    assert isinstance(ex, CodexExecutor)
    assert ex.reasoning_effort == "xhigh"


def test_main_reasoning_effort_default_is_none(monkeypatch):
    """미설정(기본)이면 None → codex 기본(medium) 그대로(기존 동작 불변)."""
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--executor", "codex", "--workdir", "/tmp/s"])
    assert captured["executor"].reasoning_effort is None


def test_main_rejects_bad_reasoning_effort(monkeypatch):
    """화이트리스트 밖 값은 argparse(choices)가 SystemExit으로 거부."""
    import pytest
    _capture_main_run(monkeypatch)
    with pytest.raises(SystemExit):
        main(["--order", "x", "--executor", "codex", "--reasoning-effort", "ultra"])


# ──────────────────────────── 헤드룸 기본값/배선 (WO#24 Part B) ────────────────────────────


def test_main_max_iters_default_is_30(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x"])
    assert captured["max_iters"] == 30  # 캡스톤 헤드룸: 10/20 → 30


def test_main_unit_retries_default_is_2(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x"])
    assert captured["unit_retries"] == 2  # 큰 빌드: 유닛 한 번 더 시도 후 escalate


def test_main_unit_retries_override(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--unit-retries", "5"])
    assert captured["unit_retries"] == 5


def test_main_max_iters_override(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--max-iters", "7"])
    assert captured["max_iters"] == 7


# ──────────────────────────── graceful stop / SIGINT (WO#43) ────────────────────────────


def test_main_keyboardinterrupt_clean_exit(monkeypatch, capsys):
    """main()이 KeyboardInterrupt를 잡아 코드 0(=stopped, failed 아님)으로 클린 종료 +
    raw traceback 대신 '중단됨' 한 줄."""
    def fake_run(order, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(run_mod, "run", fake_run)
    rc = main(["--order", "x"])
    assert rc == 0  # 종료코드 0 → 대시보드가 "failed"로 오해하지 않게 정합
    err = capsys.readouterr().err
    assert "중단됨" in err


# ──────────────────────────── codex idle timeout 배선 (WO#54) ────────────────────────────


def test_main_codex_idle_timeout_default_and_essential_stall_retries(monkeypatch):
    """기본 --codex-idle-timeout=300. brain은 *필수* → stall_retries=1로 배선."""
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x"])
    client = captured["client"]
    assert client.idle_timeout == 300.0
    assert client.stall_retries == 1  # 합성/replan/scaffold = 필수 → bounded 재시도


def test_main_codex_idle_timeout_override(monkeypatch):
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--codex-idle-timeout", "45"])
    assert captured["client"].idle_timeout == 45.0


def test_main_codex_max_duration_default_off(monkeypatch):
    """절대 backstop 기본 off(None) — 주 메커니즘은 idle."""
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x"])
    assert captured["client"].max_duration is None


def test_main_codex_idle_timeout_wires_into_codexexecutor(monkeypatch):
    """빌드(executor)도 idle_timeout + stall_retries=1(필수)로 배선."""
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--executor", "codex", "--workdir", "/tmp/s",
          "--codex-idle-timeout", "77"])
    ex = captured["executor"]
    assert isinstance(ex, CodexExecutor)
    assert ex.idle_timeout == 77.0
    assert ex.stall_retries == 1


def test_main_critic_client_best_effort_stall_retries_zero(monkeypatch):
    """critic은 *best-effort* → stall_retries=0(멈추면 degrade/진행, 재시도 안 함)."""
    captured = _capture_main_run(monkeypatch)
    main(["--order", "x", "--critic-model", "some-critic"])
    critic = captured["critic_client"]
    assert critic is not None
    assert critic.idle_timeout == 300.0
    assert critic.stall_retries == 0


def test_main_help_lists_codex_idle_timeout(capsys):
    """--help에 --codex-idle-timeout 노출(관측 가능)."""
    import pytest
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "--codex-idle-timeout" in out
