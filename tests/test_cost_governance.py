"""WO#68 — 비용 거버넌스 테스트 (mock, codex/네트워크 없음).

A. usage-limit/크레딧 graceful stop (분류 + 전 경로 catch → stopped_credit, traceback 없음).
B. --max-tokens 전역 cap (외부 컷오프 전 clean stop).
C. 유닛 누적 수렴 ceiling → 사람 escalate (다음 OR로 안 던짐, 바 자동 미완화).
재개 정합(#58): seal 상태가 완료 유닛 보존(미수렴 유닛 not-done).
"""

import subprocess
import threading
from pathlib import Path

import pytest

from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import Cost, GateResult, Status, Verdict
from haetae.providers.codex import (
    CodexError,
    CodexUsageLimitError,
    _looks_like_usage_limit,
    exec_codex_with_usage,
)
from haetae.worktree import ROOT_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


# ──────────────────────────── 공통 mock/spec ────────────────────────────


def _spec(decomp: str) -> str:
    return f"""\
spec_id: cg-001
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


SPEC_SINGLE = _spec("  - { unit: u1, desc: a, deps: [] }")
SPEC_TWO_INDEP = _spec("  - { unit: u1, desc: a, deps: [] }\n  - { unit: u2, desc: b, deps: [] }")
SPEC_TWO_LINEAR = _spec("  - { unit: u1, desc: a, deps: [] }\n  - { unit: u2, desc: b, deps: [u1] }")

NEXT = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: u1
  goal: "구현"
  deliverable: "요약"
"""
STOP = "verdict: done\naction: stop\nrationale: \"done\"\n"


class BrainClient:
    """call#1=synthesize(spec) / 이후=replan(DEC). 재dispatch로 호출이 늘어도 안전."""

    DEC = """\
verdict: pass
action: next_order
rationale: "build unit"
next_order:
  unit: placeholder
  goal: "unit 구현"
  deliverable: "요약"
"""

    def __init__(self, spec_yaml: str):
        self.spec = spec_yaml
        self.n = 0

    def complete(self, system: str, user: str, **opts) -> str:
        self.n += 1
        return self.spec if self.n == 1 else self.DEC


class PassExec:
    def run(self, order):
        return f"{order.unit} done"


class PassGate:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.pass_)


class FailGate:
    """항상 fail_recoverable — 유닛이 절대 수렴 안 함(ceiling 검증용)."""

    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.fail_recoverable)


class UnitSelectiveGate:
    """result 문자열에 fail_unit이 있으면 fail, 아니면 pass(유닛별 선택 실패)."""

    def __init__(self, fail_unit: str):
        self.fail_unit = fail_unit

    def judge(self, result, spec, unit=None):
        v = Verdict.fail_recoverable if self.fail_unit in (result or "") else Verdict.pass_
        return GateResult(verdict=v)


class CostGate:
    """매 judge가 judge_cost(tokens)를 실어 budget이 쌓이게 한다(전역 cap 검증용)."""

    def __init__(self, verdict: Verdict = Verdict.pass_, tokens: int = 1000):
        self.verdict = verdict
        self.tokens = tokens

    def judge(self, result, spec, unit=None):
        return GateResult(verdict=self.verdict, judge_cost=Cost(tokens=self.tokens))


def _assert_clean(workdir):
    wl = subprocess.run(["git", "worktree", "list"], cwd=workdir, capture_output=True, text=True)
    assert len([ln for ln in wl.stdout.splitlines() if ln.strip()]) == 1
    assert not (Path(workdir) / ROOT_NAME).exists()


# ════════════════════ A. usage-limit/크레딧 graceful stop ════════════════════


def test_looks_like_usage_limit_markers():
    assert _looks_like_usage_limit("You've hit your usage limit, try later", "")
    assert _looks_like_usage_limit("", "insufficient credit balance")
    assert _looks_like_usage_limit("error: quota exceeded", "")
    # 일반 버그 메시지는 크레딧 아님
    assert not _looks_like_usage_limit("SyntaxError: invalid token", "Traceback...")


class _FakeProc:
    def __init__(self, rc, out, err):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_exec_classifies_usage_limit(monkeypatch):
    """exit 1 + usage-limit 메시지 → CodexUsageLimitError(타입드 분류)."""
    import haetae.providers.codex as cm
    monkeypatch.setattr(
        cm.subprocess, "run",
        lambda *a, **k: _FakeProc(1, "You've hit your usage limit...", ""),
    )
    with pytest.raises(CodexUsageLimitError):
        exec_codex_with_usage("p", sandbox="read-only", cwd=None)


def test_exec_normal_error_is_plain_codexerror_not_credit(monkeypatch):
    """일반 비정상 종료(진짜 버그)는 CodexError지 CodexUsageLimitError 아님(무회귀)."""
    import haetae.providers.codex as cm
    monkeypatch.setattr(
        cm.subprocess, "run",
        lambda *a, **k: _FakeProc(1, "SyntaxError: bad", "Traceback"),
    )
    with pytest.raises(CodexError) as ei:
        exec_codex_with_usage("p", sandbox="read-only", cwd=None)
    assert not isinstance(ei.value, CodexUsageLimitError)


class _CreditExec:
    def run(self, order):
        raise CodexUsageLimitError("codex 크레딧 소진", "usage limit", "")


class _CreditOnSynth:
    def complete(self, system, user, **opts):
        raise CodexUsageLimitError("codex 크레딧 소진", "usage limit", "")


def test_sequential_build_credit_graceful_stop():
    """빌드 경로 크레딧 소진 → stopped_credit(크래시 아님)."""
    client = MockClient([SPEC_SINGLE, NEXT, STOP])
    state = run_loop(order="x", client=client, executor=_CreditExec(),
                     gate=MockGate(Verdict.pass_), prompt_dir=PROMPT_DIR)
    assert state.status is Status.stopped_credit
    assert any("크레딧" in str(e.get("reason", "")) for e in state.pending_escalations)


def test_sequential_synthesis_credit_graceful_stop():
    """합성 경로 크레딧 소진 → stopped_credit(전 경로 graceful 단언)."""
    state = run_loop(order="x", client=_CreditOnSynth(), executor=MockExecutor("ok"),
                     gate=MockGate(Verdict.pass_), prompt_dir=PROMPT_DIR)
    assert state.status is Status.stopped_credit


def test_parallel_build_credit_graceful_stop(tmp_path):
    """병렬 빌드 worker 크레딧 소진 → stopped_credit + worktree 정리(finally 보장)."""
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=PassGate(),
        executor_factory=lambda wt: _CreditExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.stopped_credit
    _assert_clean(tmp_path)


# ════════════════════ B. --max-tokens 전역 cap ════════════════════


def test_sequential_max_tokens_clean_stop():
    """누적 토큰이 --max-tokens 초과 → 다음 호출 전 stopped_budget(폭주 없음)."""
    client = MockClient([SPEC_TWO_LINEAR, NEXT, NEXT, STOP])
    state = run_loop(order="x", client=client, executor=MockExecutor("ok"),
                     gate=CostGate(Verdict.pass_, tokens=1000), prompt_dir=PROMPT_DIR,
                     max_tokens=500)
    assert state.status is Status.stopped_budget
    assert (state.budget.spent.tokens or 0) >= 500
    assert any("예산" in str(e.get("reason", "")) for e in state.pending_escalations)


def test_max_tokens_unset_is_unlimited_no_regression():
    """미지정이면 무제한 — 정상 done(기존 동작 불변)."""
    client = MockClient([SPEC_SINGLE, NEXT, STOP])
    state = run_loop(order="x", client=client, executor=MockExecutor("ok"),
                     gate=MockGate([Verdict.pass_, Verdict.done]), prompt_dir=PROMPT_DIR)
    assert state.status is Status.done


# ════════════════════ C. 유닛 누적 수렴 ceiling → 사람 escalate ════════════════════


def test_parallel_unit_attempt_budget_escalates_to_human(tmp_path):
    """유닛이 누적 시도(재시도+OR 층 합산) ceiling까지 미통과 → 사람 escalate(다음 OR로 안 감)."""
    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=FailGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: FailGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=2, unit_attempt_budget=3,
    )
    assert state.status is Status.escalated
    esc = [e for e in state.pending_escalations if e.get("unit") == "u1"]
    assert esc, "u1 미수렴 escalation이 있어야"
    last = esc[-1]
    assert "미수렴" in last["reason"]
    assert last["attempts"] >= 3                     # 누적 시도(층 합산)
    # anti-erosion: 사유에 '자동 완화 없음' 명시(바 자동 미완화).
    assert "자동 완화 없음" in last["reason"]
    _assert_clean(tmp_path)


def test_unit_budget_bar_not_auto_lowered(tmp_path):
    """ceiling escalate는 criteria/done_when을 자동 완화하지 않는다(spec 불변, governed만)."""
    brain = BrainClient(SPEC_SINGLE)
    state = run_loop(
        "x", brain, executor=None, gate=FailGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: FailGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, or_alternatives=1, unit_attempt_budget=2,
    )
    assert state.status is Status.escalated
    # spec-change(자동 완화)가 일어나지 않았음: spec_changes 비어있고, 적용 이벤트 없음.
    assert state.spec_changes == []
    assert not any("spec-change applied" in (ev.result or "") for ev in state.events)


def test_passing_unit_not_escalated_by_ceiling(tmp_path):
    """통과하는 유닛은 ceiling이 낮아도 escalate 안 됨(정상 done)."""
    state = run_loop(
        "x", BrainClient(SPEC_SINGLE), executor=None, gate=PassGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_attempt_budget=1,
    )
    assert state.status is Status.done


def test_unit_budget_unset_no_regression(tmp_path):
    """미지정이면 기존 층별 bound만(누적 ceiling off) — 통과 유닛 정상 done."""
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=PassGate(),
        executor_factory=lambda wt: PassExec(), gate_factory=lambda wt: PassGate(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.done
    _assert_clean(tmp_path)


# ════════════════════ 재개 정합(#58): 완료 유닛 보존 ════════════════════


def test_sealed_state_preserves_done_units(tmp_path):
    """ceiling escalate로 seal된 state: 완료 유닛 done 유지, 미수렴 유닛 not-done(#58 재개 원천)."""
    state = run_loop(
        "x", BrainClient(SPEC_TWO_INDEP), executor=None, gate=UnitSelectiveGate("u2"),
        executor_factory=lambda wt: PassExec(),
        gate_factory=lambda wt: UnitSelectiveGate("u2"),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=1, or_alternatives=1, unit_attempt_budget=2,
    )
    assert state.status is Status.escalated
    plan = {p.unit: p.state.value for p in state.plan}
    assert plan["u1"] == "done"                      # 완료 유닛 보존
    assert plan["u2"] in ("failed", "pending")       # 미수렴 유닛 not-done(재개 시 복귀)
    _assert_clean(tmp_path)
