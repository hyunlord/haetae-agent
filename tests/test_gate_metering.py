"""gate judge/run-judge 비용 노출 테스트 (WO#34).

CompositeGate가 자기 judge MeteredClient에 쌓인 비용을 GateResult.judge_cost로
노출한다(읽기전용/행동중립). passthrough라 verdict/checks는 메터링 유무로 불변.
"""

from pathlib import Path

from haetae.gate import CompositeGate
from haetae.llm import MockClient
from haetae.metering import MeteredClient, Usage
from haetae.models import ProjectSpec, Verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_PROMPT = REPO_ROOT / "prompts" / "judge.md"
RUN_JUDGE_PROMPT = REPO_ROOT / "prompts" / "run_judge.md"


def _spec(acs: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_id": "gate-meter-001",
            "version": 1,
            "order_raw": "x",
            "goal": "g",
            "task_type": "feature_impl",
            "verifiability": "objective",
            "mode": "normal",
            "acceptance_criteria": acs,
            "non_goals": ["a", "b"],
            "done_when": "전부 통과",
        }
    )


_JUDGE_PASS = "verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: 좋음\n"


# ──────────────────────────── judge 비용 노출 ────────────────────────────


def test_composite_gate_exposes_judge_cost_from_metered_client(tmp_path):
    """metered judge client 사용 시 GateResult.judge_cost에 비용이 실린다(source=judge)."""
    inner = MockClient(_JUDGE_PASS, usages=[Usage(1000, 200, "m")])
    metered = MeteredClient(inner, source="judge", pricing={"m": (1.0, 1.0)})
    spec = _spec([{"id": "ac1", "desc": "품질", "check": {"type": "judge"}}])
    gr = CompositeGate(
        workdir=tmp_path, judge_client=metered, judge_prompt_path=JUDGE_PROMPT
    ).judge("결과", spec)
    assert gr.verdict is Verdict.pass_
    assert gr.judge_cost is not None
    assert gr.judge_cost.tokens == 1200
    assert gr.judge_cost.source == "judge"
    assert abs(gr.judge_cost.usd - 1200 / 1_000_000) < 1e-12


def test_run_judge_cost_captured(tmp_path):
    """run 타입(run-judge 경로)도 metered judge client면 judge_cost가 잡힌다."""
    run_pass = "verdicts:\n  - ac_id: ac1\n    status: pass\n    reason: 부팅\n"
    inner = MockClient(run_pass, usages=[Usage(500, 100, "m")])
    metered = MeteredClient(inner, source="judge", pricing={"m": (1.0, 1.0)})
    spec = _spec([{"id": "ac1", "desc": "부팅", "check": {"type": "run", "cmd": "true"}}])
    gr = CompositeGate(
        workdir=tmp_path, judge_client=metered,
        judge_prompt_path=JUDGE_PROMPT, run_judge_prompt_path=RUN_JUDGE_PROMPT,
        run_timeout=10,
    ).judge("결과", spec)
    assert gr.judge_cost is not None
    assert gr.judge_cost.tokens == 600


def test_judge_cost_none_without_metering(tmp_path):
    """plain client(메터링 없음)면 judge_cost=None — 행동 불변, 비용만 미상."""
    inner = MockClient(_JUDGE_PASS, usages=[Usage(1000, 200, "m")])
    spec = _spec([{"id": "ac1", "desc": "품질", "check": {"type": "judge"}}])
    gr = CompositeGate(
        workdir=tmp_path, judge_client=inner, judge_prompt_path=JUDGE_PROMPT
    ).judge("결과", spec)
    assert gr.verdict is Verdict.pass_
    assert gr.judge_cost is None


def test_judge_absent_degrade_cost_none(tmp_path):
    """judge_client 없음(run degrade) → LLM 호출 0 → judge_cost None(무크래시)."""
    spec = _spec([{"id": "ac1", "desc": "부팅", "check": {"type": "run", "cmd": "true"}}])
    gr = CompositeGate(workdir=tmp_path, judge_client=None, run_timeout=10).judge("결과", spec)
    assert gr.judge_cost is None


# ──────────────────────────── passthrough 가드(행동 불변) ────────────────────────────


def test_metering_does_not_change_verdict_or_checks(tmp_path):
    """메터링 래핑 유무로 verdict·checks가 동일 — gate 검증 행동 불변 증명."""
    spec = _spec(
        [
            {"id": "ac0", "desc": "기계", "check": {"type": "test", "cmd": "true"}},
            {"id": "ac1", "desc": "품질", "check": {"type": "judge"}},
        ]
    )

    def run(client):
        return CompositeGate(
            workdir=tmp_path, judge_client=client, judge_prompt_path=JUDGE_PROMPT
        ).judge("결과", spec)

    raw = run(MockClient(_JUDGE_PASS, usages=[Usage(1000, 200, "m")]))
    metered = run(
        MeteredClient(
            MockClient(_JUDGE_PASS, usages=[Usage(1000, 200, "m")]),
            source="judge", pricing={"m": (1.0, 1.0)},
        )
    )
    # 행동(verdict) 동일
    assert raw.verdict == metered.verdict
    # 근거(checks) 동일 — ac_id/status/check_type 모두
    assert [(c.ac_id, c.status, c.check_type) for c in raw.checks] == [
        (c.ac_id, c.status, c.check_type) for c in metered.checks
    ]
    # 차이는 비용 노출뿐: raw는 None, metered는 채워짐
    assert raw.judge_cost is None
    assert metered.judge_cost is not None


# ──────────────────────────── best-effort(드레인 예외 흡수) ────────────────────────────


def test_drain_exception_absorbed(tmp_path):
    """judge client.drain()이 던져도 gate는 verdict를 정상 반환(judge_cost=None)."""

    class _BoomDrain:
        def __init__(self):
            self.last_usage = None

        def complete(self, system, user, **opts):
            return _JUDGE_PASS

        def drain(self):
            raise RuntimeError("drain boom")

    spec = _spec([{"id": "ac1", "desc": "품질", "check": {"type": "judge"}}])
    gr = CompositeGate(
        workdir=tmp_path, judge_client=_BoomDrain(), judge_prompt_path=JUDGE_PROMPT
    ).judge("결과", spec)
    assert gr.verdict is Verdict.pass_  # run 안 죽음
    assert gr.judge_cost is None
