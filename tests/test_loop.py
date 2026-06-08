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


# ──────────── 진행 메시지 풍부화 (WO#18) ────────────


def test_progress_execute_message_includes_unit_and_goal():
    """작업 실행 진행 메시지에 unit과 goal이 보여야 추적 가능하다."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    seen: list[str] = []
    run_loop(order="x", client=client, executor=MockExecutor("a"),
             gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR, progress=seen.append)
    exec_msgs = [s for s in seen if s.startswith("작업 실행")]
    assert exec_msgs, seen
    assert "u1" in exec_msgs[0]
    assert "u1 구현" in exec_msgs[0]  # _next_order의 goal


def test_progress_gate_message_includes_verdict_and_check_summary():
    """gate 진행 메시지에 verdict와 체크 통과 요약(N/M)이 보여야 한다."""
    evidence = [_report("ac1", "pass")]
    client = MockClient([SPEC_YAML, _next_order("u1")])
    gate = MockGate(Verdict.done, checks=evidence)
    seen: list[str] = []
    run_loop(order="x", client=client, executor=MockExecutor("a"), gate=gate,
             prompt_dir=PROMPT_DIR, progress=seen.append)
    summary = [s for s in seen if s.startswith("gate:")]
    assert summary, seen
    assert "done" in summary[0]
    assert "1/1 통과" in summary[0]


def test_progress_gate_message_reports_first_failing_check():
    """실패 시 첫 실패 체크의 cmd/exit_code가 한 줄로 드러난다."""
    evidence = [_report("ac1", "pass"), _report("ac2", "fail")]
    client = MockClient([SPEC_YAML, _next_order("u1")])
    gate = MockGate(Verdict.done, checks=evidence)
    seen: list[str] = []
    run_loop(order="x", client=client, executor=MockExecutor("a"), gate=gate,
             prompt_dir=PROMPT_DIR, progress=seen.append)
    summary = [s for s in seen if s.startswith("gate:")]
    assert summary, seen
    assert "pytest ac2" in summary[0]
    assert "exit 1" in summary[0]


def test_progress_replan_retry_message_shows_reason():
    """replan 재시도 메시지에 직전 검증 에러 요약이 붙는다."""
    client = MockClient([SPEC_YAML, DEC_INVALID, _next_order("u1")])
    seen: list[str] = []
    run_loop(order="x", client=client, executor=MockExecutor("a"),
             gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR, progress=seen.append)
    retry = [s for s in seen if s.startswith("replan 재시도")]
    assert retry, seen
    assert "replan 재시도 1" in retry[0]


def test_progress_final_label_includes_escalation_reason():
    """escalated 종료 라벨에 사유가 한 줄 붙는다."""
    client = MockClient([SPEC_YAML, DEC_ESCALATE])
    seen: list[str] = []
    run_loop(order="x", client=client, executor=MockExecutor("noop"),
             gate=MockGate(Verdict.pass_), prompt_dir=PROMPT_DIR, progress=seen.append)
    final = [s for s in seen if s.startswith("종료")]
    assert final, seen
    assert "escalated" in final[-1]
    assert "공성전" in final[-1]  # escalation question이 사유로


# ──────────── state 저장 견고화: 비치명적 + 증분 (WO#18) ────────────


def test_save_failure_is_non_fatal_and_warns(tmp_path):
    """state_path가 디렉토리(write 불가)여도 run은 예외 없이 State를 반환하고 경고한다."""
    bad_path = tmp_path / "as_dir"
    bad_path.mkdir()  # 디렉토리 → write_text가 IsADirectoryError
    client = MockClient([SPEC_YAML, _next_order("u1")])
    seen: list[str] = []
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR,
                     state_path=bad_path, progress=seen.append)
    # 성공한 run을 저장 실패가 죽이지 않는다
    assert state.status is Status.done
    warns = [s for s in seen if s.startswith("⚠ state 저장 실패")]
    assert warns, seen
    assert str(bad_path) in warns[0]


def test_save_failure_non_fatal_without_progress(tmp_path):
    """progress 없이도 저장 실패가 run을 죽이지 않는다(경고는 그냥 흡수)."""
    bad_path = tmp_path / "as_dir"
    bad_path.mkdir()
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR,
                     state_path=bad_path)
    assert state.status is Status.done


def test_incremental_save_persists_events_during_run(tmp_path):
    """이벤트마다 증분 저장 → max_iters로 중단돼도 그때까지의 이벤트가 파일에 남는다."""
    out = tmp_path / "state.yaml"
    # 끝나지 않는 스크립트: 매번 next_order, gate는 pass만 → max_iters에서 stopped_stuck
    client = MockClient([SPEC_YAML] + [_next_order("u1")] * 3)
    run_loop(order="x", client=client, executor=MockExecutor("again"),
             gate=MockGate(Verdict.pass_), max_iters=3, prompt_dir=PROMPT_DIR,
             state_path=out)
    assert out.exists()
    reloaded = State.from_yaml(out)
    # 증분으로 3 이벤트가 모두 보존됨(라운드트립)
    assert len(reloaded.events) == 3
    assert reloaded.status is Status.stopped_stuck


def test_incremental_save_preserves_audit_log_on_midrun_crash(tmp_path):
    """후반 실패(executor가 2번째에 raise)에도 1번째 이벤트는 이미 파일에 남는다.

    최종 저장이 아니라 *증분* 저장이 동작함을 증명한다 — 루프가 예외로 죽어
    최종 저장에 도달하지 못해도 그때까지의 감사 로그가 보존되어야 한다.
    """
    out = tmp_path / "state.yaml"

    class _CrashOnSecond:
        def __init__(self):
            self.n = 0

        def run(self, order):
            self.n += 1
            if self.n >= 2:
                raise RuntimeError("executor 폭발")
            return "first ok"

    client = MockClient([SPEC_YAML] + [_next_order("u1")] * 3)
    with pytest.raises(RuntimeError):
        run_loop(order="x", client=client, executor=_CrashOnSecond(),
                 gate=MockGate(Verdict.pass_), prompt_dir=PROMPT_DIR, state_path=out)
    # 루프는 예외로 죽었지만 1번째 이벤트는 증분 저장으로 디스크에 남아야 한다
    assert out.exists()
    reloaded = State.from_yaml(out)
    assert len(reloaded.events) == 1
    assert reloaded.events[0].unit == "u1"


# ──────────── spec critic 통합 (WO#19) ────────────

_CRIT_SOFT = """\
verdict: soft
gaps:
  - area: "ac1"
    cheap_path: "격자 칸 점유만 막으면 통과 — 진짜 어려움 회피"
    strengthening: "연속공간 고밀도 충돌까지 검사하도록 ac 강화"
"""
_CRIT_ADEQUATE = "verdict: adequate\ngaps: []\n"


def test_run_loop_critic_off_by_default():
    """critic_client 미주입 → critic 호출 0, critique 미기록(기존 동작 불변)."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert state.spec_critique is None
    assert len(client.calls) == 2  # synthesize + replan만, critic 재합성 없음


def test_run_loop_critic_adequate_surfaces_and_persists():
    """critic_client 주입 + adequate → progress surface + State.spec_critique 기록."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    critic = MockClient([_CRIT_ADEQUATE])
    seen: list[str] = []
    # decomp_critic=False: 이 테스트는 *spec* critic을 격리 검증 — 공유 critic-model을
    #   분해 critic이 추가 호출하지 않도록 끔(분해 critic은 별도 테스트에서 검증).
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), critic_client=critic,
                     decomp_critic=False, prompt_dir=PROMPT_DIR, progress=seen.append)
    assert state.status is Status.done
    assert state.spec_critique is not None
    assert state.spec_critique.verdict == "adequate"
    assert state.spec_critique.resynthesized is False
    assert any(s == "spec critic: adequate" for s in seen), seen
    assert len(critic.calls) == 1


def test_run_loop_critic_soft_triggers_resynthesis_and_records():
    """soft(구체 gap) → 1회 재합성, progress·State에 재합성 사실이 남는다."""
    # client: synthesize → 재합성 → replan
    client = MockClient([SPEC_YAML, SPEC_YAML, _next_order("u1")])
    critic = MockClient([_CRIT_SOFT])
    seen: list[str] = []
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), critic_client=critic,
                     prompt_dir=PROMPT_DIR, progress=seen.append)
    assert state.status is Status.done
    assert state.spec_critique is not None
    assert state.spec_critique.resynthesized is True
    assert any(s.startswith("spec critic: soft") and "재합성" in s for s in seen), seen
    assert len(client.calls) == 3  # synthesize + 재합성 + replan


class _RaisingClient:
    """complete()에서 예외를 던지는 critic mock — codex 다운/인증/잘못된 모델 등."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls.append({"system": system, "user": user, "opts": opts})
        raise self._exc


def test_run_loop_critic_client_exception_does_not_kill_run():
    """critic 클라이언트가 던져도 run은 원본 spec으로 정상 완료(WO#20).

    critic은 advisory라 그 실패가 본 run을 죽이면 안 된다. "(평가 불가)"로 surface되고
    spec_critique는 기록되며 예외는 전혀 전파되지 않는다. 재합성도 없다."""
    from haetae.llm import CodexError

    client = MockClient([SPEC_YAML, _next_order("u1")])  # synthesize + replan만
    critic = _RaisingClient(CodexError("모델 'bogus' 없음"))
    seen: list[str] = []
    # decomp_critic=False: *spec* critic 격리(공유 critic-model 추가 호출 방지).
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), critic_client=critic,
                     decomp_critic=False, prompt_dir=PROMPT_DIR, progress=seen.append)
    assert state.status is Status.done  # 정상 완료
    assert state.spec_critique is not None
    assert state.spec_critique.verdict == "adequate"
    assert state.spec_critique.resynthesized is False
    assert any(s == "spec critic: (평가 불가)" for s in seen), seen
    assert len(critic.calls) == 1  # critic 호출은 일어남(그리고 실패 흡수)
    assert len(client.calls) == 2  # synthesize + replan, 재합성 없음


def test_run_loop_critic_persists_through_yaml(tmp_path):
    """State.spec_critique가 YAML로 저장/재로드되는지(감사 로그)."""
    out = tmp_path / "state.yaml"
    # soft면 synthesize가 2회 소비됨(합성 + 재합성) → client 시퀀스도 그만큼.
    client = MockClient([SPEC_YAML, SPEC_YAML, _next_order("u1")])
    critic = MockClient([_CRIT_SOFT])
    run_loop(order="x", client=client, executor=MockExecutor("a"),
             gate=MockGate(Verdict.done), critic_client=critic,
             prompt_dir=PROMPT_DIR, state_path=out)
    reloaded = State.from_yaml(out)
    assert reloaded.spec_critique is not None
    assert reloaded.spec_critique.verdict == "soft"
    assert reloaded.spec_critique.resynthesized is True
    assert len(reloaded.spec_critique.gaps) == 1


# ──────────────────── 분해 critic at replan (WO#40, Phase C) ────────────────────

# 분해 critic 응답(spec critic과 별개 — 독립 critic-model이 매 replan마다 판정).
_DC_PROGRESS = "verdict: progress\nreason: \"한 조각만 좁힘 — 진전\"\n"
_DC_WEAK = "verdict: weak\nreason: \"전체 goal 재진술 — 무진전\"\n"


def _write_skill(base: Path, name: str, triggers, body: str) -> None:
    sk = base / name
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text(
        f"---\ntriggers: [{', '.join(triggers)}]\n---\n\n{body}\n", encoding="utf-8"
    )


def test_decomp_critic_weak_then_progress_rereplans_then_dispatches():
    """weak → 피드백 주고 재replan → progress면 dispatch. reject가 state에 기록된다."""
    # 공유 critic-model: 호출0=spec critic(adequate), 호출1=분해 weak, 호출2=분해 progress.
    critic = MockClient([_CRIT_ADEQUATE, _DC_WEAK, _DC_PROGRESS])
    # brain: synthesize + replan + 재replan(분해 reject 후).
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u1")])
    ex = MockExecutor("a")
    state = run_loop(order="x", client=client, executor=ex, gate=MockGate(Verdict.done),
                     critic_client=critic, prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert len(critic.calls) == 3      # spec + 분해 2회(weak→progress)
    assert len(client.calls) == 3      # synthesize + replan + 재replan
    assert len(ex.calls) == 1          # progress 후에야 dispatch
    # reject가 감사 로그에 기록(rejected=True) + 전이에 decomp-reject 단계.
    rejected = [c for c in state.decomp_critiques if c.rejected]
    assert len(rejected) == 1 and rejected[0].verdict == "weak"
    assert any(t.stage == "decomp-reject" for t in state.transitions)


def test_decomp_critic_exhausted_proceeds_no_deadlock():
    """재시도 소진(decomp_retries=1) 후에도 weak면 *진행*(데드락 금지) + critique 기록."""
    critic = MockClient([_CRIT_ADEQUATE, _DC_WEAK, _DC_WEAK])  # 분해는 계속 weak
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u1")])
    ex = MockExecutor("a")
    state = run_loop(order="x", client=client, executor=ex, gate=MockGate(Verdict.done),
                     critic_client=critic, prompt_dir=PROMPT_DIR, decomp_retries=1)
    assert state.status is Status.done   # 소진 후 진행 → 정상 종료(데드락 안 함)
    assert len(critic.calls) == 3        # 바운드: spec + 분해 2회(무한 아님)
    assert len(ex.calls) == 1            # 소진 후 그 order를 dispatch함
    # 2건 기록: reject(True) + 소진-진행(False).
    assert len(state.decomp_critiques) == 2
    assert state.decomp_critiques[0].rejected is True
    assert state.decomp_critiques[1].rejected is False


def test_decomp_critic_progress_dispatches_without_extra_replan():
    """progress 판정이면 추가 replan 없이 바로 dispatch(기록 없음)."""
    critic = MockClient([_CRIT_ADEQUATE, _DC_PROGRESS])
    client = MockClient([SPEC_YAML, _next_order("u1")])
    ex = MockExecutor("a")
    state = run_loop(order="x", client=client, executor=ex, gate=MockGate(Verdict.done),
                     critic_client=critic, prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert len(critic.calls) == 2   # spec + 분해 1회
    assert len(client.calls) == 2   # synthesize + replan(재replan 없음)
    assert len(ex.calls) == 1
    assert state.decomp_critiques == []   # progress는 기록 안 함


def test_decomp_critic_client_exception_degrades_and_dispatches():
    """best-effort: 분해 critic이 던져도 progress로 흡수 → 정상 dispatch(루프 안 죽음)."""
    from haetae.llm import CodexError

    critic = _RaisingClient(CodexError("codex 다운"))  # spec+분해 모두 흡수
    client = MockClient([SPEC_YAML, _next_order("u1")])
    ex = MockExecutor("a")
    state = run_loop(order="x", client=client, executor=ex, gate=MockGate(Verdict.done),
                     critic_client=critic, prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert len(critic.calls) == 2   # spec critic + 분해 critic 둘 다 호출(둘 다 흡수)
    assert len(ex.calls) == 1       # 흡수 후 진행
    assert state.decomp_critiques == []  # 평가불가-degrade는 weak 아님 → 기록 안 함


def test_decomp_critic_off_not_called_during_replan():
    """--no-decomp-critic: 분해 critic 미호출(critic-model은 spec critic에만). 기존 동작."""
    critic = MockClient([_CRIT_ADEQUATE])  # spec critic 1회만 소비
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), critic_client=critic,
                     decomp_critic=False, prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert len(critic.calls) == 1         # spec critic만(분해 critic 미호출)
    assert state.decomp_critiques == []
    assert not any(t.stage == "decomp-reject" for t in state.transitions)


def test_decomp_critic_no_critic_model_means_off():
    """critic_client=None(=--critic-model 미설정)이면 분해 critic 자동 OFF(미설정→진행)."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=MockGate(Verdict.done), critic_client=None,
                     prompt_dir=PROMPT_DIR)  # decomp_critic 기본 True지만 client 없음
    assert state.status is Status.done
    assert state.decomp_critiques == []


def test_decomp_critic_gets_no_skill_injection(tmp_path):
    """**적대적 분리**: 분해 critic은 스킬 미주입 *원본* order를 본다(executor엔 주입)."""
    from haetae.skills import SKILL_SECTION_HEADER

    _write_skill(tmp_path, "frontend-build", ["u1"], body="SKILL_BODY_MARKER")
    critic = MockClient([_CRIT_ADEQUATE, _DC_PROGRESS])  # spec + 분해 progress
    client = MockClient([SPEC_YAML, _next_order("u1")])  # goal "u1 구현" → 트리거 u1 매칭
    ex = MockExecutor("a")
    state = run_loop(order="x", client=client, executor=ex, gate=MockGate(Verdict.done),
                     critic_client=critic, prompt_dir=PROMPT_DIR, skills_dir=tmp_path)
    assert state.status is Status.done
    # 분해 critic이 받은 user 프롬프트엔 스킬 섹션/본문이 없다(빌더 전용과 분리).
    decomp_user = critic.calls[1]["user"]  # calls[0]=spec critic, calls[1]=분해 critic
    assert SKILL_SECTION_HEADER not in decomp_user
    assert "SKILL_BODY_MARKER" not in decomp_user
    # 반면 executor가 받은 order.scope엔 스킬이 주입됐다(분리 증명).
    assert SKILL_SECTION_HEADER in (ex.calls[0].scope or "")


# ──────────────────────────── Protocol 적합성 ────────────────────────────


def test_mocks_satisfy_protocols():
    assert isinstance(MockExecutor("x"), Executor)
    assert isinstance(MockGate(Verdict.pass_), Gate)


# ──────────── plan unit-state 현실 반영 (WO#25 Part A) ────────────

SPEC3_YAML = """\
spec_id: plan-001
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac1
    desc: "d"
    check: { type: test, cmd: "true" }
assumptions: []
non_goals: ["a", "b"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
  - { unit: u2, desc: b, deps: [u1] }
  - { unit: u3, desc: c, deps: [u2] }
open_questions: []
"""


def _plan_states(state) -> dict:
    return {p.unit: p.state.value for p in state.plan}


def test_advance_marks_prev_unit_done_and_terminal_catch_all():
    """전체-spec gate가 중간 유닛에 fail_recoverable을 줘도, advance + 종료 done catch-all로
    작업된 유닛이 전부 done이 된다('전부 in_progress 고착' 회귀 방지)."""
    client = MockClient([SPEC3_YAML, _next_order("u1"), _next_order("u2"), _next_order("u3")])
    # 중간 유닛은 부분 통과(fail_recoverable), 마지막에 done — 예전이면 u1·u2가 in_progress 고착.
    gate = MockGate([Verdict.fail_recoverable, Verdict.fail_recoverable, Verdict.done])
    state = run_loop(order="x", client=client, executor=MockExecutor(["a", "b", "c"]),
                     gate=gate, prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert _plan_states(state) == {"u1": "done", "u2": "done", "u3": "done"}


def test_escalate_marks_worked_unit_failed():
    """brain이 작업 중이던 유닛을 포기(escalate)하면 그 유닛은 failed, 나머지는 pending 보존."""
    client = MockClient([SPEC3_YAML, _next_order("u1"), DEC_ESCALATE])
    gate = MockGate(Verdict.fail_recoverable)
    state = run_loop(order="x", client=client, executor=MockExecutor("a"),
                     gate=gate, prompt_dir=PROMPT_DIR)
    assert state.status is Status.escalated
    ps = _plan_states(state)
    assert ps["u1"] == "failed"
    assert ps["u2"] == "pending"  # 도달 안 한 유닛은 보존
    assert ps["u3"] == "pending"


def test_advance_done_preserved_when_later_unit_escalates():
    """u1 advance로 done 후 u2에서 escalate → u1=done 보존, u2=failed."""
    client = MockClient([SPEC3_YAML, _next_order("u1"), _next_order("u2"), DEC_ESCALATE])
    gate = MockGate([Verdict.pass_, Verdict.fail_recoverable])
    state = run_loop(order="x", client=client, executor=MockExecutor(["a", "b"]),
                     gate=gate, prompt_dir=PROMPT_DIR)
    assert state.status is Status.escalated
    ps = _plan_states(state)
    assert ps["u1"] == "done"  # advance로 수용된 유닛 보존
    assert ps["u2"] == "failed"  # escalate된 작업 유닛
    assert ps["u3"] == "pending"


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


# ──────────────────────────── graceful stop / SIGINT (WO#43) ────────────────────────────


class _InterruptExec:
    """첫 run에서 KeyboardInterrupt(=웹 stop/SIGINT 모사) 발생."""

    def run(self, order):
        raise KeyboardInterrupt()


def test_sequential_interrupt_saves_state_clean_exit(tmp_path):
    """순차 루프 도중 KeyboardInterrupt → 클린 반환(traceback 없음) + state 저장 + '중단됨' 로그."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    sp = tmp_path / "state.yaml"
    msgs: list[str] = []
    # KeyboardInterrupt가 run_loop 밖으로 새지 않고 State로 마무리되면 클린 종료.
    state = run_loop(
        order="x", client=client, executor=_InterruptExec(),
        gate=MockGate(Verdict.pass_), prompt_dir=PROMPT_DIR,
        state_path=sp, progress=msgs.append,
    )
    assert isinstance(state, State)
    # 종료 상태 봉인: running 아님(stopped로 해석 → 대시보드에 "중단됨").
    assert state.status is Status.stopped_stuck
    assert any("중단됨" in m for m in msgs)
    # state 저장 + 부분 진행 보존(u1 dispatch까지 반영).
    assert sp.exists()
    saved = State.from_yaml(sp)
    assert saved.status is Status.stopped_stuck
    assert saved.spec_ref == "loop-001"  # 합성된 spec 보존(부분 진행)


def test_interrupt_during_synthesis_uses_placeholder_state(tmp_path):
    """합성(spec 생성) 전 KeyboardInterrupt → placeholder state로 클린 마무리·저장."""
    class KIClient:
        def complete(self, system, user, **opts):
            raise KeyboardInterrupt()

    sp = tmp_path / "state.yaml"
    msgs: list[str] = []
    state = run_loop(
        order="x", client=KIClient(), executor=MockExecutor("noop"),
        gate=MockGate(Verdict.pass_), prompt_dir=PROMPT_DIR,
        state_path=sp, progress=msgs.append,
    )
    assert isinstance(state, State)
    assert state.status is Status.stopped_stuck
    assert state.spec_ref == "(interrupted)"  # placeholder 사용됨
    assert any("중단됨" in m for m in msgs)
    assert sp.exists()


def test_finalize_interrupt_absorbs_emit_and_save_errors():
    """정리/저장 콜백이 예외를 던져도 흡수 — 2차 크래시 없이 상태만 봉인(best-effort)."""
    from haetae.loop import _finalize_interrupt

    st = State(spec_ref="x", spec_version=1, status=Status.running)

    def bad_emit(_msg):
        raise RuntimeError("emit 폭발")

    def bad_save():
        raise RuntimeError("save 폭발")

    # 예외를 raise하지 않고 정상 반환해야 한다(2차 크래시 금지).
    _finalize_interrupt(st, bad_emit, bad_save)
    assert st.status is Status.stopped_stuck


def test_no_interrupt_normal_done_unchanged():
    """인터럽트 없을 때 정상 done 경로는 불변(graceful-stop 래핑 무회귀)."""
    client = MockClient([SPEC_YAML, _next_order("u1"), _next_order("u2")])
    state = run_loop(order="x", client=client, executor=MockExecutor(["a", "b"]),
                     gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR)
    assert state.status is Status.done
    assert len(state.events) == 2
