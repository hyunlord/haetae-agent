"""병렬 executor 테스트 (WO#21) — 동시 dispatch / deps 준수 / 머지 충돌 직렬화 /
N=1=순차 / 선형DAG=동일결과 / 뒷정리 보장 / 통합 gate.

codex 없이 mock/실git. brain·executor·gate는 thread-safe한 unit-aware mock으로 둔다
(brain은 main 스레드에서 직렬 호출되지만 executor/gate는 워커에서 동시 호출되므로
시퀀스 인덱스를 공유하는 MockExecutor/MockGate 대신 상태 없는 mock을 쓴다).
"""

import subprocess
import threading
from pathlib import Path

import pytest

from haetae.gate import CheckRunner
from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import GateResult, State, Status, Verdict
from haetae.worktree import ROOT_NAME, WorktreeManager

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


def _spec(decomp: str) -> str:
    return f"""\
spec_id: par-001
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


SPEC_TWO_INDEP = _spec("  - { unit: u1, desc: a, deps: [] }\n  - { unit: u2, desc: b, deps: [] }")
SPEC_LINEAR = _spec("  - { unit: u1, desc: a, deps: [] }\n  - { unit: u2, desc: b, deps: [u1] }")
SPEC_SINGLE = _spec("  - { unit: u1, desc: a, deps: [] }")

# brain Decision 템플릿 — unit은 loop이 스케줄러 권위로 덮어쓰므로 placeholder면 됨.
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
    """call#1=synthesize(spec) / 이후=replan(DEC). 재dispatch로 호출이 늘어도 안전.

    brain은 main 스레드에서 직렬 호출되므로 호출 카운트는 결정적이다.
    """

    def __init__(self, spec_yaml: str, dec_yaml: str = DEC):
        self.spec = spec_yaml
        self.dec = dec_yaml
        self.n = 0
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls.append({"system": system, "user": user})
        self.n += 1
        return self.spec if self.n == 1 else self.dec


class PassExec:
    """상태 없는 executor — 워커에서 동시 호출돼도 안전. 변경 없음."""

    def run(self, order):
        return f"{order.unit} done"


class PassGate:
    """상태 없는 gate — 항상 같은 verdict. unit/통합 gate 양쪽에 쓰임."""

    def __init__(self, verdict: Verdict = Verdict.pass_):
        self.verdict = verdict

    def judge(self, result, spec, unit=None):
        return GateResult(verdict=self.verdict)


class SpyGate:
    """통합 gate 스파이 — judge 호출 result를 기록(스레드 안전 lock)."""

    def __init__(self, verdict: Verdict = Verdict.pass_):
        self.verdict = verdict
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def judge(self, result, spec, unit=None):
        with self._lock:
            self.calls.append(result)
        return GateResult(verdict=self.verdict)


def assert_clean(workdir):
    wl = subprocess.run(["git", "worktree", "list"], cwd=workdir, capture_output=True, text=True)
    assert len([ln for ln in wl.stdout.splitlines() if ln.strip()]) == 1
    br = subprocess.run(["git", "branch", "--list", "haetae/*"], cwd=workdir, capture_output=True, text=True)
    assert br.stdout.strip() == ""
    assert not (Path(workdir) / ROOT_NAME).exists()


# ──────────────────────── 1. 동시 dispatch (오버랩) ────────────────────────


def test_two_independent_units_dispatch_concurrently(tmp_path):
    """무deps unit 둘이 진짜 동시에 실행된다(Barrier로 오버랩 강제 증명)."""
    barrier = threading.Barrier(2, timeout=5)
    seen: list[str] = []
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    seen.append(order.unit)
                barrier.wait()  # 둘이 동시에 도달 못 하면 timeout→BrokenBarrier(=실패)
                return f"{order.unit} done"
        return E()

    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    # 동시 실행이 아니면 barrier timeout→executor 예외→재시도 소진→escalated.
    assert state.status is Status.done
    assert set(seen) == {"u1", "u2"}
    assert_clean(tmp_path)


# ──────────────────────── 2. deps 준수 ────────────────────────


def test_dependent_unit_waits_for_dep_merge(tmp_path):
    """u2(deps u1)는 u1 done+merge 전에 dispatch되지 않는다."""
    seen: list[str] = []
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    seen.append(order.unit)
                return f"{order.unit} ok"
        return E()

    state = run_loop(
        "x", BrainClient(SPEC_LINEAR), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(),
        max_parallel=4, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert seen == ["u1", "u2"]  # 순서 강제(u1 done 후에야 u2)
    assert_clean(tmp_path)


# ──────────────────────── 3. 머지 충돌 → 직렬화 ────────────────────────


def test_merge_conflict_serializes_then_succeeds(tmp_path):
    """같은 파일을 건드리는 두 unit: 하나 머지, 다른 하나 충돌→갱신 main 위 재dispatch→성공."""
    def make_ex(wt):
        class E:
            def run(self, order):
                (Path(wt) / "shared.txt").write_text(f"content-{order.unit}\n")
                return f"{order.unit} wrote"
        return E()

    seen_progress: list[str] = []
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, progress=seen_progress.append,
    )
    assert state.status is Status.done
    assert {p.unit: p.state.value for p in state.plan} == {"u1": "done", "u2": "done"}
    # 충돌 직렬화가 실제로 일어났다(progress에 흔적)
    assert any("머지 충돌" in s for s in seen_progress), seen_progress
    # 최종 파일은 둘 중 나중에 머지된 쪽(순서는 비결정적이라 둘 중 하나)
    assert (tmp_path / "shared.txt").read_text().strip() in ("content-u1", "content-u2")
    assert_clean(tmp_path)


def test_persistent_merge_conflict_escalates(tmp_path):
    """재시도 후에도 머지가 계속 충돌하면 escalate(+뒷정리)."""

    class AlwaysConflict(WorktreeManager):
        def merge(self, unit_id):
            return "conflict"  # 항상 충돌(직렬화로도 해소 불가 시뮬)

    wm = AlwaysConflict(tmp_path)
    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, worktree_manager=wm,
    )
    assert state.status is Status.escalated
    assert any("머지 충돌" in str(e) for e in state.pending_escalations), state.pending_escalations
    assert_clean(tmp_path)


# ──────────────────────── 4. N=1 → 순차(현행 경로) ────────────────────────


def _seq_next_order(unit: str) -> str:
    return f"""\
verdict: pass
action: next_order
rationale: "{unit}"
next_order:
  unit: {unit}
  goal: "{unit} 구현"
  deliverable: "요약"
"""


def test_max_parallel_one_uses_sequential_path(tmp_path):
    """N=1 → 순차 경로 그대로: worktree·git 미사용, 기존 동작과 동일 결과."""
    client = MockClient([SPEC_LINEAR, _seq_next_order("u1"), _seq_next_order("u2")])
    state = run_loop(
        "x", client, MockExecutor(["a", "b"]), MockGate([Verdict.pass_, Verdict.done]),
        max_parallel=1, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert len(state.events) == 2
    # 순차 경로는 worktree/git을 건드리지 않는다(현행 경로 보존)
    assert not (tmp_path / ROOT_NAME).exists()
    assert not (tmp_path / ".git").exists()


# ──────────────────────── 5. 선형 DAG = 순차 동일 결과 ────────────────────────


def test_linear_dag_parallel_matches_sequential(tmp_path):
    """선형 deps DAG는 병렬(N=4)이어도 자연 직렬화 → 순차와 동일한 최종 결과."""
    seq = run_loop(
        "x", MockClient([SPEC_LINEAR, _seq_next_order("u1"), _seq_next_order("u2")]),
        MockExecutor(["a", "b"]), MockGate([Verdict.pass_, Verdict.done]),
        max_parallel=1, prompt_dir=PROMPT_DIR,
    )
    par = run_loop(
        "x", BrainClient(SPEC_LINEAR), executor=None, gate=PassGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=4, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert seq.status == par.status == Status.done
    assert {p.unit: p.state for p in seq.plan} == {p.unit: p.state for p in par.plan}
    # unit 이벤트 순서 동일(통합 이벤트는 병렬에만 존재하므로 unit 있는 것만 비교)
    assert [e.unit for e in seq.events] == [e.unit for e in par.events if e.unit] == ["u1", "u2"]
    assert_clean(tmp_path)


# ──────────────────────── 6. 뒷정리 보장 (핵심) ────────────────────────


def test_cleanup_after_normal_run(tmp_path):
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=PassGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert_clean(tmp_path)


def test_unit_retries_exhausted_then_escalate(tmp_path):
    """유닛 gate가 계속 실패하면 unit_retries회 재시도(=총 retries+1 시도) 후 escalate."""
    calls: list[str] = []
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    calls.append(order.unit)
                return "ran"
        return E()

    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(Verdict.fail_recoverable),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR, unit_retries=2,
    )
    assert state.status is Status.escalated
    assert len(calls) == 3  # 최초 1 + 재시도 2 = unit_retries + 1
    assert_clean(tmp_path)


def test_unit_retries_zero_single_attempt(tmp_path):
    """unit_retries=0이면 첫 실패에서 바로 escalate(시도 1회)."""
    calls: list[str] = []
    lock = threading.Lock()

    def make_ex(wt):
        class E:
            def run(self, order):
                with lock:
                    calls.append(order.unit)
                return "ran"
        return E()

    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(Verdict.fail_recoverable),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR, unit_retries=0,
    )
    assert state.status is Status.escalated
    assert len(calls) == 1  # 재시도 없음
    assert_clean(tmp_path)


def test_cleanup_after_inflight_executor_exception(tmp_path):
    """워커(executor) 예외 → 그 unit 실패로 흡수·재시도 소진→escalate, 흔적 0."""
    def make_ex(wt):
        class E:
            def run(self, order):
                raise RuntimeError("executor 폭발")
        return E()

    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGate(),
        executor_factory=make_ex, gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR, unit_retries=1,
    )
    assert state.status is Status.escalated
    assert_clean(tmp_path)


def test_cleanup_on_propagating_exception(tmp_path):
    """main 스레드 예외(gate 팩토리 폭발)가 전파돼도 finally가 worktree를 청소한다."""
    def bad_gate_factory(wt):
        raise RuntimeError("gate 생성 폭발")

    with pytest.raises(RuntimeError):
        run_loop(
            "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGate(),
            executor_factory=lambda wt: PassExec(), gate_factory=bad_gate_factory,
            max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        )
    # 예외가 전파됐지만(=run 크래시) worktree·브랜치·루트는 청소됨
    assert_clean(tmp_path)


# ──────────────────────── 7. 통합 gate ────────────────────────


def test_integration_gate_runs_once_on_main(tmp_path):
    """전부 done이면 머지된 main에서 통합 gate를 정확히 1회 돌린다."""
    integ = SpyGate(Verdict.pass_)
    state = run_loop(
        "x", BrainClient(SPEC_LINEAR), executor=None, gate=integ,
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=4, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    assert len(integ.calls) == 1
    assert "integration" in integ.calls[0]
    assert any(e.unit is None and e.work_order_ref == "(integration)" for e in state.events)
    assert_clean(tmp_path)


def test_integration_gate_failure_escalates(tmp_path):
    """통합 gate 실패(cross-unit breakage) → escalate."""
    integ = SpyGate(Verdict.fail_recoverable)
    state = run_loop(
        "x", BrainClient(SPEC_LINEAR), executor=None, gate=integ,
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=4, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    assert any("통합 gate" in str(e) for e in state.pending_escalations)
    assert_clean(tmp_path)


# ──────────────────────── 8. 이벤트 결정적 정렬 ────────────────────────


# ──────────────────────── 9. per-unit acceptance criteria (WO#26) ────────────────────────


def _tagged_spec(ac_int_cmd: str) -> str:
    """u1·u2 + 유닛별 ac(true) + 통합 ac(ac_int_cmd). 진짜 CheckRunner로 gate한다."""
    return f"""\
spec_id: par-tag-001
version: 1
order_raw: "x"
goal: "g"
task_type: feature_impl
verifiability: objective
mode: normal
constraints: []
acceptance_criteria:
  - id: ac_u1
    desc: "u1 자기 기준"
    unit: u1
    check: {{ type: test, cmd: "true" }}
  - id: ac_u2
    desc: "u2 자기 기준"
    unit: u2
    check: {{ type: test, cmd: "true" }}
  - id: ac_int
    desc: "전체-시스템 기준"
    unit: integration
    check: {{ type: test, cmd: "{ac_int_cmd}" }}
assumptions: []
non_goals: ["n"]
done_when: "ac_u1~ac_int 전부"
decomposition:
  - {{ unit: u1, desc: a, deps: [] }}
  - {{ unit: u2, desc: b, deps: [] }}
open_questions: []
"""


def test_per_unit_gate_runs_only_its_own_criteria(tmp_path):
    """per-unit gate(real CheckRunner)는 자기 ac만, 통합 gate는 전체 ac를 돌린다."""
    state = run_loop(
        "x", BrainClient(_tagged_spec("true")), executor=None,
        gate=CheckRunner(workdir=tmp_path),  # 통합 gate(머지된 main)
        executor_factory=lambda wt: PassExec(),
        gate_factory=lambda wt: CheckRunner(workdir=wt),  # per-unit gate
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    # per-unit 이벤트: 그 유닛 ac만 평가됨(u1 gate가 u2/integration ac를 안 돌림)
    by_unit = {e.unit: [c.ac_id for c in e.checks] for e in state.events if e.unit}
    assert by_unit == {"u1": ["ac_u1"], "u2": ["ac_u2"]}
    # 통합 이벤트: 전체 ac 평가됨(권위 done 판정)
    integ = [e for e in state.events if e.unit is None][0]
    assert {c.ac_id for c in integ.checks} == {"ac_u1", "ac_u2", "ac_int"}
    assert_clean(tmp_path)


def test_capstone_foundational_unit_progresses_not_escalates(tmp_path):
    """캡스톤 회귀: 전체-시스템 기준(ac_int=false)이 있어도 기반 유닛은 *자기 기준만*
    통과하면 progress한다 → u1/u2는 done까지 가고, 실패는 *통합* gate에서 난다.
    (구버전: per-unit gate가 전체 spec을 검사해 기반 유닛이 ac_int 때문에 escalate)
    """
    state = run_loop(
        "x", BrainClient(_tagged_spec("false")), executor=None,
        gate=CheckRunner(workdir=tmp_path),
        executor_factory=lambda wt: PassExec(),
        gate_factory=lambda wt: CheckRunner(workdir=wt),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1,
    )
    # 유닛은 *자기 기준* 통과 → done까지 진행(머지됨). escalate는 통합에서.
    assert {p.unit: p.state.value for p in state.plan} == {"u1": "done", "u2": "done"}
    assert state.status is Status.escalated
    # 실패 원인이 *통합* breakage이지, 기반 유닛 자기 기준 실패가 아니다.
    assert any("통합 gate" in str(e) for e in state.pending_escalations), state.pending_escalations
    assert not any("gate" in str(e) and "u1" in str(e) for e in state.pending_escalations)
    assert_clean(tmp_path)


def test_untagged_spec_parallel_backcompat(tmp_path):
    """미태그 spec: per-unit은 trivial pass(자기 기준 0), 통합 gate가 전체를 잡는다.
    전부 통과(true)면 기존 병렬 의미 그대로 최종 done.
    """
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None,  # ac1 미태그(true)
        gate=CheckRunner(workdir=tmp_path),
        executor_factory=lambda wt: PassExec(),
        gate_factory=lambda wt: CheckRunner(workdir=wt),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    # per-unit 이벤트는 미태그라 빈 checks(자기 기준 없음 → executor-ok)
    by_unit = {e.unit: e.checks for e in state.events if e.unit}
    assert all(checks == [] for checks in by_unit.values())
    # 통합이 미태그 ac1을 검사
    integ = [e for e in state.events if e.unit is None][0]
    assert [c.ac_id for c in integ.checks] == ["ac1"]
    assert_clean(tmp_path)


def test_events_sorted_deterministically_by_unit(tmp_path):
    """완료 타이밍과 무관하게 이벤트는 (unit-id) 순으로 정렬돼 저장된다."""
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=PassGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    unit_events = [e.unit for e in state.events if e.unit]
    assert unit_events == ["u1", "u2"]
    # seq는 1..N 연속
    assert [e.seq for e in state.events] == list(range(1, len(state.events) + 1))
    assert_clean(tmp_path)
