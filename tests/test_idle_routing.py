"""WO#54 — codex 멈춤(CodexStalled) 라우팅 테스트.

필수(빌드·replan·합성) = 멈춤 지속 시 escalate(가짜 진행 금지).
best-effort(critic·judge) = degrade — critic→진행, judge→skipped(ambiguous, 절대 가짜 pass 아님).
모두 mock으로 — 실제 codex 안 부름.
"""

from pathlib import Path

import pytest

from haetae.decomp_critic import critique_decomposition
from haetae.gate import aggregate_verdict
from haetae.judge import LLMJudge
from haetae.llm import CodexStalled, MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.models import (
    AcceptanceCriterion,
    CheckType,
    NextOrder,
    ProjectSpec,
    State,
    Status,
    Verdict,
)
from haetae.spec_critic import critique_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"

# 합성/replan 스크립트(test_loop와 동형 — 교차 import 회피로 로컬 정의).
SPEC_YAML = """\
spec_id: idle-001
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
    check: { type: test, cmd: "t" }
assumptions: []
non_goals:
  - "a"
  - "b"
done_when: "ac1 통과"
decomposition:
  - { unit: u1, desc: "스켈레톤", deps: [] }
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
  local_checks: [{{ type: test, cmd: "t {unit}" }}]
  executor: codex
  deliverable: "요약"
"""


# ──────────────────────────── 멈춤을 던지는 클라이언트 ────────────────────────────


class StallingClient:
    """N번째 complete 호출부터 CodexStalled를 던지는 mock(멈춤 라우팅 검증용).

    responses: 멈춤 전까지 돌려줄 정상 응답들(소진하면 그 다음 호출부터 stall).
    """

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self._i = 0
        self.calls = 0
        self.last_usage = None

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls += 1
        if self._i < len(self._responses):
            r = self._responses[self._i]
            self._i += 1
            return r
        raise CodexStalled("codex 무진행(idle) — 테스트 stall")


class AlwaysStallClient:
    """모든 complete가 CodexStalled를 던지는 mock."""

    def __init__(self):
        self.calls = 0
        self.last_usage = None

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls += 1
        raise CodexStalled("codex 무진행(idle) — 테스트 stall")


class StallingExecutor:
    """run()이 CodexStalled를 던지는 executor(빌드 멈춤 시뮬레이션)."""

    def __init__(self):
        self.calls = 0
        self.last_usage = None

    def run(self, order: NextOrder) -> str:
        self.calls += 1
        raise CodexStalled("codex 무진행(idle) — 빌드 stall")


# ──────────────────────────── 필수: 합성/replan/빌드 멈춤 → escalate ────────────────────────────


def test_synthesis_stall_escalates_not_crash():
    """합성 단계 codex 멈춤 → run_loop이 crash 대신 escalate(정직)."""
    client = AlwaysStallClient()  # 합성 첫 complete부터 stall
    state = run_loop(
        order="x", client=client,
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    assert any(
        "무진행" in str(e.get("reason", "")) or "stalled" in str(e.get("reason", ""))
        for e in state.pending_escalations
    )


def test_replan_stall_escalates_not_crash():
    """합성은 성공하고 replan codex가 멈추면 → escalate(crash 없음)."""
    client = StallingClient([SPEC_YAML])  # SPEC 합성 후, replan 호출부터 stall
    state = run_loop(
        order="x", client=client,
        executor=MockExecutor("noop"), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    assert state.pending_escalations  # 사유 첨부


def test_build_stall_escalates_not_crash():
    """빌드(executor) codex 멈춤 → escalate(순차 경로). 무한 hang 없음."""
    client = MockClient([SPEC_YAML, _next_order("u1")])
    state = run_loop(
        order="x", client=client,
        executor=StallingExecutor(), gate=MockGate(Verdict.pass_),
        prompt_dir=PROMPT_DIR,
    )
    assert state.status is Status.escalated
    assert state.pending_escalations


# ──────────────────────────── best-effort: critic 멈춤 → 진행 ────────────────────────────


def _min_spec() -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "s1", "version": 1, "order_raw": "x", "goal": "g",
            "task_type": "feature_impl", "verifiability": "objective", "mode": "normal",
            "acceptance_criteria": [{"id": "ac1", "desc": "d", "check": {"type": "test", "cmd": "t"}}],
            "non_goals": ["a", "b"], "done_when": "ac1",
            "decomposition": [{"unit": "u1", "desc": "x", "deps": []}],
        }
    )


def test_spec_critic_stall_proceeds_as_adequate():
    """spec critic 멈춤 → adequate(평가 불가)로 흡수 — 합성을 막지 않는다(진행)."""
    crit = critique_spec("order", _min_spec(), AlwaysStallClient(), prompt_path=PROMPT_DIR / "spec_critic.md")
    assert crit.verdict == "adequate"
    assert crit.note and ("실행 실패" in crit.note or "평가 불가" in crit.note)


def test_decomp_critic_stall_proceeds_as_progress():
    """분해 critic 멈춤 → progress(평가 불가)로 흡수 — replan을 막지 않는다(진행)."""
    no = NextOrder(unit="u1", goal="g", deliverable="d")
    spec = _min_spec()
    state = State(spec_ref="s1", spec_version=1, status=Status.running)
    crit = critique_decomposition(
        no, spec, state, AlwaysStallClient(),
        prompt_path=PROMPT_DIR / "decomp_critic.md",
    )
    assert crit.verdict == "progress"  # 진행 막지 않음


# ──────────────────────────── best-effort: judge 멈춤 → degrade(가짜 pass 금지) ────────────────────────────


def _judge_spec(acs: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "j1", "version": 1, "order_raw": "x", "goal": "g",
            "task_type": "feature_impl", "verifiability": "judge", "mode": "normal",
            "acceptance_criteria": acs, "non_goals": ["a", "b"], "done_when": "전부 통과",
        }
    )


def test_judge_stall_degrades_to_skipped_not_pass(tmp_path):
    """judge codex 멈춤 → 모든 기준 skipped(가짜 pass 절대 아님) → aggregate=ambiguous."""
    (tmp_path / "out.txt").write_text("산출물", encoding="utf-8")
    acs = [
        AcceptanceCriterion(id="ac1", desc="읽기 쉬운가", check={"type": "judge"}),
        AcceptanceCriterion(id="ac2", desc="일관적인가", check={"type": "judge"}),
    ]
    spec = _judge_spec([{"id": "ac1", "desc": "x", "check": {"type": "judge"}}])
    judge = LLMJudge(AlwaysStallClient(), workdir=tmp_path, prompt_path=JUDGE_PROMPT)

    reports = judge.judge_criteria(acs, "result", spec)
    assert len(reports) == 2
    assert all(r.status == "skipped" for r in reports)  # 가짜 pass 없음
    assert all("idle" in (r.detail or "") or "무진행" in (r.detail or "") for r in reports)
    # 집계: skipped만 있으면 ambiguous(=사람/judge tier), 절대 pass 아님.
    assert aggregate_verdict(reports) is Verdict.ambiguous


def test_judge_stall_never_yields_pass_even_with_no_fail(tmp_path):
    """단언 강화: 멈춤은 fail이 아니라 skipped → aggregate는 절대 pass/done이 아니다."""
    acs = [AcceptanceCriterion(id="ac1", desc="d", check={"type": "judge"})]
    spec = _judge_spec([{"id": "ac1", "desc": "x", "check": {"type": "judge"}}])
    reports = LLMJudge(AlwaysStallClient(), workdir=tmp_path, prompt_path=JUDGE_PROMPT).judge_criteria(
        acs, "result", spec
    )
    assert aggregate_verdict(reports) not in (Verdict.pass_, Verdict.done)
