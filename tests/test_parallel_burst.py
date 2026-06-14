"""WO#110(OMC #1) — disjoint 병렬 burst: scope 입증 disjoint 유닛은 자원 cap(--max-parallel-burst)
까지 동시 실행, 비-disjoint/미선언은 --max-parallel 한정, 충돌 backstop(serialize-on-conflict)은
가정이 틀려도 그대로 안전망, opt-in 기본 보수적(burst 미지정=기존 동작).

스케줄링만 검증 — gate/judge/run-judge 무접촉. mock/실git(codex 없음).
"""

import threading
import time
from pathlib import Path

from haetae.llm import MockClient  # noqa: F401  (harness 일관성)
from haetae.loop import run_loop
from haetae.models import GateResult, Status, Verdict
from haetae.worktree import WorktreeManager

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"

DEC = """\
verdict: pass
action: next_order
rationale: "build unit"
next_order:
  unit: placeholder
  goal: "unit 구현"
  deliverable: "요약"
"""


class BrainClient:
    """call#1=synthesize(spec) / 이후=replan(DEC). main 스레드 직렬 호출."""

    def __init__(self, spec_yaml: str):
        self.spec = spec_yaml
        self.n = 0

    def complete(self, system: str, user: str, **opts) -> str:
        self.n += 1
        return self.spec if self.n == 1 else DEC


class PassGate:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.pass_)


def _spec(units: list[tuple[str, list[str], list[str]]]) -> str:
    """units: [(unit, deps, scope)] → spec yaml(disjoint 판정에 쓸 scope 포함)."""
    lines = []
    for u, deps, scope in units:
        deps_s = "[" + ", ".join(deps) + "]"
        scope_s = "[" + ", ".join(f'"{s}"' for s in scope) + "]"
        lines.append(f"  - {{ unit: {u}, desc: {u}, deps: {deps_s}, scope: {scope_s} }}")
    decomp = "\n".join(lines)
    return f"""\
spec_id: burst-001
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
    check: {{ type: test, cmd: "true" }}
assumptions: []
non_goals: ["n"]
done_when: "ac1"
decomposition:
{decomp}
open_questions: []
"""


class PeakProbe:
    """동시 실행 peak를 기록(작은 hold로 오버랩 창 확보). barrier 없음 — cap/음성 검증용."""

    def __init__(self, hold: float = 0.1):
        self.hold = hold
        self.cur = 0
        self.peak = 0
        self.lock = threading.Lock()

    def factory(self, wt):
        probe = self

        class E:
            def run(self, order):
                with probe.lock:
                    probe.cur += 1
                    probe.peak = max(probe.peak, probe.cur)
                time.sleep(probe.hold)  # 오버랩 창(동시 허용 시 peak↑, 직렬이면 1)
                with probe.lock:
                    probe.cur -= 1
                return f"{order.unit} done"
        return E()


# ──────────────────── 1. disjoint 유닛이 base를 넘어 burst ────────────────────


def test_disjoint_units_burst_beyond_base_cap(tmp_path):
    """scope disjoint 유닛 3개: base=1이어도 burst=3까지 동시 실행(Barrier로 3-동시 증명)."""
    barrier = threading.Barrier(3, timeout=5)
    seen: list[str] = []
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    seen.append(order.unit)
                barrier.wait()  # 3이 동시 도달 못 하면 timeout→실패
                return f"{order.unit} done"
        return E()

    spec = _spec([("u1", [], ["a.ts"]), ("u2", [], ["b.ts"]), ("u3", [], ["c.ts"])])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(),
        max_parallel=1, max_parallel_burst=3, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert set(seen) == {"u1", "u2", "u3"}  # 3 동시 실행 성공


# ──────────────────── 2. 비-disjoint(겹치는 scope)는 base cap 한정 ────────────────────


def test_overlapping_scope_units_stay_at_base_cap(tmp_path):
    """scope 겹치는 유닛들: base=1이면 burst=3여도 동시 1(직렬) — 충돌 리스크 있어 burst 안 함."""
    probe = PeakProbe()
    # 셋 다 shared.ts 공유 → 서로 비-disjoint.
    spec = _spec([
        ("u1", [], ["shared.ts"]),
        ("u2", [], ["shared.ts"]),
        ("u3", [], ["shared.ts"]),
    ])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=probe.factory, gate_factory=lambda wt: PassGate(),
        max_parallel=1, max_parallel_burst=3, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert probe.peak == 1  # base=1 한정 — 겹침 유닛은 burst 슬롯 못 받음


def test_unscoped_units_stay_at_base_cap(tmp_path):
    """scope 미선언 유닛: 미입증 → 보수적으로 base=1 한정(자동 burst 안 함)."""
    probe = PeakProbe()
    spec = _spec([("u1", [], []), ("u2", [], []), ("u3", [], [])])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=probe.factory, gate_factory=lambda wt: PassGate(),
        max_parallel=1, max_parallel_burst=3, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert probe.peak == 1  # 미선언 = 미입증 → burst 불가


# ──────────────────── 3. burst cap(자원 한계) 존중 ────────────────────


def test_burst_cap_is_respected(tmp_path):
    """disjoint 4개라도 burst=2면 동시 최대 2 — 자원 하드 상한 초과 0."""
    probe = PeakProbe()
    spec = _spec([
        ("u1", [], ["a.ts"]), ("u2", [], ["b.ts"]),
        ("u3", [], ["c.ts"]), ("u4", [], ["d.ts"]),
    ])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=probe.factory, gate_factory=lambda wt: PassGate(),
        max_parallel=1, max_parallel_burst=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert probe.peak <= 2  # burst cap=2 절대 초과 안 함
    assert probe.peak == 2  # disjoint니까 base(1)는 넘김(burst 동작 확인)


# ──────────────────── 4. opt-in 기본 보수적(burst 미지정 = 기존 동작) ────────────────────


def test_default_no_burst_is_back_compat(tmp_path):
    """burst 미지정(기본 0): disjoint 유닛 3개여도 max_parallel(=2) 한정 — 자동 burst 안 함(opt-in)."""
    probe = PeakProbe()
    spec = _spec([("u1", [], ["a.ts"]), ("u2", [], ["b.ts"]), ("u3", [], ["c.ts"])])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=probe.factory, gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,  # burst 미지정(=2)
    )
    assert state.status is Status.done
    assert probe.peak == 2  # disjoint여도 기본은 max_parallel(2)에서 멈춤 — 자동 burst 없음


# ──────────────────── 5. 충돌 backstop: disjoint 판정이 틀려도 안전 ────────────────────


def test_conflict_backstop_holds_under_burst(tmp_path):
    """scope를 disjoint로 *선언*했지만 실제론 같은 파일을 써 머지충돌 → serialize-on-conflict가
    burst여도 그대로 잡아 run 완료. 잘못된 disjoint 판정의 안전망 검증."""
    def make_ex(wt):
        class E:
            def run(self, order):
                # 선언 scope는 disjoint(a.ts/b.ts)지만 실제 산출물은 같은 파일 → 충돌 유발.
                (Path(wt) / "shared.txt").write_text(f"content-{order.unit}\n")
                return f"{order.unit} wrote"
        return E()

    seen_progress: list[str] = []
    spec = _spec([("u1", [], ["a.ts"]), ("u2", [], ["b.ts"])])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(),
        max_parallel=1, max_parallel_burst=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, progress=seen_progress.append,
    )
    assert state.status is Status.done  # backstop이 충돌 해소 → 완료
    assert {p.unit: p.state.value for p in state.plan} == {"u1": "done", "u2": "done"}
    assert any("머지 충돌" in s for s in seen_progress), seen_progress  # 직렬화 backstop 작동


def test_persistent_conflict_still_escalates_under_burst(tmp_path):
    """burst 켜도 머지가 계속 충돌하면(해소 불가) 여전히 escalate — backstop 의미 불변."""
    class AlwaysConflict(WorktreeManager):
        def merge(self, unit_id):
            return "conflict"

    spec = _spec([("u1", [], ["a.ts"]), ("u2", [], ["b.ts"])])
    state = run_loop(
        "x", BrainClient(spec), executor=None, gate=PassGate(),
        executor_factory=lambda wt: type("E", (), {"run": lambda s, o: f"{o.unit} ok"})(),
        gate_factory=lambda wt: PassGate(),
        max_parallel=1, max_parallel_burst=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, worktree_manager=AlwaysConflict(tmp_path),
    )
    assert state.status is Status.escalated
    assert any("머지 충돌" in str(e) for e in state.pending_escalations), state.pending_escalations
