"""루프 계측 테스트 (WO#33) — 토큰/코스트 누적·귀속, ts, 단계 전이, 라이브 activity.

mock LLM/executor/gate만. best-effort 불변(계측 예외가 run을 안 죽임)도 검증.
"""

import threading
from pathlib import Path

from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.metering import Usage
from haetae.models import State, Status, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

SPEC_YAML = """\
spec_id: instr-001
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
  - { unit: u2, desc: b, deps: [] }
open_questions: []
"""


def _next_order(unit: str) -> str:
    return f"""\
verdict: pass
action: next_order
rationale: "{unit}"
next_order:
  unit: {unit}
  goal: "{unit} 구현"
  deliverable: "요약"
"""


# ──────────────────────────── Part A: 토큰/코스트 ────────────────────────────


def test_budget_accumulates_and_event_cost_and_ts_filled():
    """usage 반환 → budget.spent 누적 + event cost 귀속 + ts 채움. 가격표로 usd."""
    # call#1=synthesize(1000/500), call#2=replan(2000/800)
    client = MockClient(
        [SPEC_YAML, _next_order("u1")],
        usages=[Usage(1000, 500, "m"), Usage(2000, 800, "m")],
    )
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)}, clock=lambda: "T0",
    )
    assert state.status is Status.done
    # budget = synth(1500) + replan(2800)
    assert state.budget.spent.tokens == 1500 + 2800
    assert state.budget.spent.input == 1000 + 2000
    assert state.budget.spent.output == 500 + 800
    # usd = (1500 + 2800) / 1e6 (rate 1/1)
    assert abs(state.budget.spent.usd - 4300 / 1_000_000) < 1e-12
    # event cost = replan(orchestration) 귀속 + ts
    ev = state.events[0]
    assert ev.cost is not None
    assert ev.cost.tokens == 2800
    assert ev.cost.source == "orchestration"
    assert ev.ts == "T0"


def test_unknown_model_usd_none_tokens_only():
    """미상 모델 → usd=None, tokens만(날조 금지)."""
    client = MockClient(
        [SPEC_YAML, _next_order("u1")],
        usages=[Usage(100, 50, "unknown"), Usage(100, 50, "unknown")],
    )
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    assert state.budget.spent.tokens == 300
    assert state.budget.spent.usd is None


def test_usage_absent_no_crash_cost_none():
    """usage 미노출(usages 미주입) → cost None, 무크래시."""
    client = MockClient([SPEC_YAML, _next_order("u1")])  # usages 없음
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert state.events[0].cost is None
    assert state.budget.spent.tokens is None


def test_executor_usage_missing_yields_null_and_note():
    """usage-capable executor가 usage를 안 주면 null+노트(날조 금지)."""

    class UsagelessExec:
        last_usage = None  # usage-capable 표식이지만 값 없음

        def run(self, order):
            return "a"

    client = MockClient(
        [SPEC_YAML, _next_order("u1")], usages=[Usage(10, 10, "m"), Usage(10, 10, "m")]
    )
    state = run_loop(
        "x", client, executor=UsagelessExec(), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    ev = state.events[0]
    assert ev.cost is not None
    assert ev.cost.source == "mixed"  # orchestration + executor(note)
    assert "미노출" in (ev.cost.note or "")
    # orchestration 토큰은 잡혔지만 executor는 미상 → tokens = replan(20)만
    assert ev.cost.tokens == 20


def test_executor_usage_captured_and_attributed():
    """executor가 usage를 주면 event cost에 합산(source=mixed)."""

    class UsageExec:
        def __init__(self):
            self.last_usage = None

        def run(self, order):
            self.last_usage = Usage(3000, 1000, "m")
            return "a"

    client = MockClient(
        [SPEC_YAML, _next_order("u1")], usages=[Usage(10, 10, "m"), Usage(20, 0, "m")]
    )
    state = run_loop(
        "x", client, executor=UsageExec(), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    ev = state.events[0]
    assert ev.cost.source == "mixed"
    # replan(20) + executor(4000) = 4020
    assert ev.cost.tokens == 4020


def test_non_llm_executor_no_executor_cost():
    """MockExecutor(last_usage 없음=비-LLM)는 executor 비용 귀속 안 함(노트도 없음)."""
    client = MockClient(
        [SPEC_YAML, _next_order("u1")], usages=[Usage(10, 10, "m"), Usage(20, 0, "m")]
    )
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    ev = state.events[0]
    assert ev.cost.source == "orchestration"
    assert ev.cost.note is None
    assert ev.cost.tokens == 20  # replan만


# ──────────────────────────── Part B: 단계 전이 + activity ────────────────────────────


def test_stage_transitions_recorded_with_order():
    """synthesize/build/verify 단계가 시간순으로 transitions에 기록된다."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, clock=lambda: "T",
    )
    stages = [t.stage for t in state.transitions]
    assert "synthesize" in stages
    assert "build" in stages
    assert "verify" in stages
    pairs = [(t.stage, t.unit) for t in state.transitions]
    assert pairs.index(("build", "u1")) < pairs.index(("verify", "u1"))
    # ts가 채워졌다
    assert all(t.ts == "T" for t in state.transitions)


def test_activity_cleared_at_end_and_observer_sees_build_then_verify():
    """dispatch→build, gate→verify가 라이브 activity에 보이고, 완료 시 비워진다."""
    seen: list[list] = []
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR,
        activity_observer=lambda snap: seen.append([(a.unit, a.stage) for a in snap]),
    )
    assert state.activity == []  # 완료 시 제거
    assert any(("u1", "build") in s for s in seen)
    assert any(("u1", "verify") in s for s in seen)
    assert seen[-1] == []  # 마지막 스냅샷은 비어있음


# ──────────────────────────── best-effort 불변 ────────────────────────────


def test_clock_exception_absorbed():
    """clock이 던져도 run은 정상 완료(ts만 None)."""

    def boom():
        raise RuntimeError("clock boom")

    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, clock=boom,
    )
    assert state.status is Status.done
    assert state.events[0].ts is None


def test_activity_observer_exception_absorbed():
    """activity_observer가 던져도 run은 정상 완료."""

    def boom(snap):
        raise RuntimeError("observer boom")

    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, activity_observer=boom,
    )
    assert state.status is Status.done


# ──────────────────────────── 새 필드 YAML 라운드트립 ────────────────────────────


def test_new_state_fields_roundtrip_through_yaml(tmp_path):
    out = tmp_path / "state.yaml"
    client = MockClient(
        [SPEC_YAML, _next_order("u1")], usages=[Usage(100, 50, "m"), Usage(100, 50, "m")]
    )
    run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, state_path=out, pricing={"m": (1.0, 1.0)}, clock=lambda: "T",
    )
    reloaded = State.from_yaml(out)
    assert reloaded.budget.spent.tokens == 300
    assert reloaded.events[0].ts == "T"
    assert reloaded.activity == []
    assert any(t.stage == "build" for t in reloaded.transitions)


# ──────────────────────────── 병렬: 동시 activity + 예산 ────────────────────────────


class _BrainClient:
    """call#1=spec, 이후=replan(DEC). usage를 호출마다 노출."""

    DEC = (
        "verdict: pass\naction: next_order\nrationale: r\n"
        "next_order:\n  unit: placeholder\n  goal: g\n  deliverable: s\n"
    )

    def __init__(self, spec_yaml):
        self.spec = spec_yaml
        self.n = 0
        self.last_usage = None
        self.calls = []

    def complete(self, system, user, **opts):
        self.calls.append({"system": system, "user": user})
        self.n += 1
        self.last_usage = Usage(100, 50, "m")
        return self.spec if self.n == 1 else self.DEC


def test_parallel_concurrent_activity_and_budget(tmp_path):
    """병렬: 두 유닛이 동시에 in-flight면 activity가 동시에 ≥2, 끝나면 비워지고 예산 누적."""
    barrier = threading.Barrier(2, timeout=5)
    snaps: list[int] = []
    lock = threading.Lock()

    def observer(snap):
        with lock:
            snaps.append(len(snap))

    def make_ex(wt):
        class E:
            def __init__(self):
                self.last_usage = None

            def run(self, order):
                barrier.wait()  # 둘이 동시에 도달해야 통과
                self.last_usage = Usage(500, 100, "m")
                return f"{order.unit} done"

        return E()

    class _PassGate:
        def judge(self, result, spec, unit=None):
            from haetae.models import GateResult

            return GateResult(verdict=Verdict.pass_)

    state = run_loop(
        "x", _BrainClient(SPEC_YAML), executor=None, gate=_PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: _PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        pricing={"m": (1.0, 1.0)}, activity_observer=observer,
    )
    assert state.status is Status.done
    assert max(snaps) >= 2  # 동시에 두 유닛이 in-flight였다
    assert state.activity == []  # 끝나면 비워짐
    # 예산 누적: 최소한 orchestration + executor 토큰이 쌓였다
    assert state.budget.spent.tokens is not None
    assert state.budget.spent.tokens > 0
    # 각 유닛 event에 cost와 ts가 붙는다(executor usage 포함 → mixed)
    unit_events = [e for e in state.events if e.unit in ("u1", "u2")]
    assert unit_events
    assert all(e.cost is not None for e in unit_events)
