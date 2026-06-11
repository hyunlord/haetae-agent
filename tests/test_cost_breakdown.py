"""per-unit × tier × source × kind 비용 분해 테스트 (WO#70).

계측된 각 호출(leaf)을 source(orchestration/executor/judge/critic)·tier(#64)·kind
(synth/replan/scaffold/build/retry/OR/integration-OR/judge)·unit으로 귀속하고, 그 합이
budget.spent(권위 total)과 정합하는지(어긋나면 '(미귀속)'으로 정직 노출) 검증한다.

순수 텔레메트리 — gate/judge 판정·ALLOWED_SANDBOXES 불변. mock LLM/executor/gate만.
"""

import json
import threading
from pathlib import Path

from haetae.executors import Tier
from haetae.llm import MockClient
from haetae.loop import MockExecutor, MockGate, run_loop
from haetae.dashboard import INDEX_HTML_PATH, state_to_view
from haetae.metering import Usage, combine_costs, cost_leaves, tag_cost
from haetae.models import (
    Budget,
    Cost,
    Event,
    GateResult,
    PlanItem,
    PlanState,
    State,
    Status,
    Verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "prompts"


# ──────────────────────────── 순수: combine parts / tag_cost ────────────────────────────


def test_combine_costs_populates_flattened_parts():
    """combine_costs 결과는 입력의 leaf를 평탄화해 .parts에 싣는다(중첩 combine은 leaf로 펼침)."""
    a = Cost(tokens=100, source="orchestration", kind="replan")
    b = Cost(tokens=50, source="executor", kind="build")
    inner = combine_costs([a, b])  # parts=[a, b]
    c = Cost(tokens=20, source="judge", kind="judge")
    outer = combine_costs([inner, c])
    # 중첩(inner)은 leaf로 펼쳐진다 → parts=[a, b, c] (inner 자신은 안 들어감)
    assert outer.tokens == 170
    assert outer.source == "mixed"
    kinds = sorted(p.kind for p in outer.parts)
    assert kinds == ["build", "judge", "replan"]
    assert all(not p.parts for p in outer.parts)  # leaf는 더 안 쪼개짐


def test_cost_leaves_single_vs_combined():
    leaf = Cost(tokens=10, source="judge")
    assert cost_leaves(leaf) == [leaf]  # 단일 → 자기
    assert cost_leaves(None) == []
    combined = combine_costs([Cost(tokens=1, source="a"), Cost(tokens=2, source="b")])
    assert len(cost_leaves(combined)) == 2  # combine → parts


def test_tag_cost_fills_leaves_and_top_fill_if_none():
    a = Cost(tokens=100, source="orchestration")
    combined = combine_costs([a])
    tag_cost(combined, kind="replan", unit="u1")
    assert combined.kind == "replan" and combined.unit == "u1"
    assert combined.parts[0].kind == "replan" and combined.parts[0].unit == "u1"
    # fill-if-None: 이미 단 값은 덮지 않는다
    tag_cost(combined, kind="OTHER", unit="u9")
    assert combined.kind == "replan" and combined.unit == "u1"


def test_tag_cost_none_is_noop():
    assert tag_cost(None, kind="x") is None


# ──────────────────────────── 순차 루프: leaf 태그 + 정합 ────────────────────────────


def _spec_yaml() -> str:
    return """\
spec_id: brk-001
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


class _UsageExec:
    """build usage를 노출하는 executor(executor source leaf 생성용)."""

    def __init__(self):
        self.last_usage = None

    def run(self, order):
        self.last_usage = Usage(3000, 1000, "m")
        return "ran"


class _JudgeCostGate:
    """GateResult.judge_cost를 실어 반환하는 gate(judge source leaf 생성용)."""

    def __init__(self, verdict, judge_tokens=300):
        self.verdict = verdict
        self.judge_tokens = judge_tokens

    def judge(self, result, spec, unit=None):
        return GateResult(
            verdict=self.verdict,
            judge_cost=Cost(tokens=self.judge_tokens, input=self.judge_tokens, output=0,
                            usd=None, source="judge"),
        )


def test_sequential_leaves_tagged_unit_source_kind():
    """순차 run: 각 leaf가 (unit, source, kind) 태그를 갖는다 — synth/replan/build/judge."""
    client = MockClient(
        [_spec_yaml(), _next_order("u1")],
        usages=[Usage(1000, 500, "m"), Usage(2000, 800, "m")],  # synth, replan
    )
    state = run_loop(
        "x", client, executor=_UsageExec(), gate=_JudgeCostGate(Verdict.done, 300),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    assert state.status is Status.done
    leaves = state.cost_parts
    assert leaves, "ledger(cost_parts)가 채워져야 한다"
    # source별로 기대 kind/unit이 붙어있다
    by = {}
    for lf in leaves:
        by.setdefault(lf.source, []).append(lf)
    # synth(orchestration, kind=synth, unit None)
    synth = [lf for lf in by.get("orchestration", []) if lf.kind == "synth"]
    assert synth and synth[0].unit is None
    # replan(orchestration, kind=replan, unit=u1)
    replan = [lf for lf in by.get("orchestration", []) if lf.kind == "replan"]
    assert replan and replan[0].unit == "u1"
    # build(executor, kind=build, unit=u1)
    build = by.get("executor", [])
    assert build and build[0].kind == "build" and build[0].unit == "u1"
    assert build[0].tokens == 4000
    # judge(judge, kind=judge, unit=u1)
    judge = by.get("judge", [])
    assert judge and judge[0].kind == "judge" and judge[0].unit == "u1"


def test_sequential_ledger_reconciles_to_budget():
    """Σledger.tokens == budget.spent.tokens (account 단일 길목 → 정합 by construction)."""
    client = MockClient(
        [_spec_yaml(), _next_order("u1")],
        usages=[Usage(1000, 500, "m"), Usage(2000, 800, "m")],
    )
    state = run_loop(
        "x", client, executor=_UsageExec(), gate=_JudgeCostGate(Verdict.done, 300),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    total = state.budget.spent.tokens
    ledger_sum = sum((lf.tokens or 0) for lf in state.cost_parts)
    assert total is not None and ledger_sum == total
    # event.cost.parts도 분해 가능(유닛 event = replan+build+judge leaf)
    ev = state.events[0]
    assert ev.cost.parts
    kinds = sorted(p.kind for p in ev.cost.parts)
    assert kinds == ["build", "judge", "replan"]


def test_event_cost_total_unchanged_no_regress():
    """event.cost의 권위 total(tokens/source)은 종전 그대로 — parts는 추가일 뿐(무회귀)."""
    client = MockClient(
        [_spec_yaml(), _next_order("u1")],
        usages=[Usage(10, 10, "m"), Usage(20, 0, "m")],
    )
    state = run_loop(
        "x", client, executor=MockExecutor("a"), gate=MockGate(Verdict.done),
        prompt_dir=PROMPT_DIR, pricing={"m": (1.0, 1.0)},
    )
    ev = state.events[0]
    assert ev.cost.source == "orchestration"  # replan만(비-LLM executor)
    assert ev.cost.tokens == 20


# ──────────────────────────── 병렬: tier 분해(#64 연계) ────────────────────────────


LADDER3 = [Tier("m0", "medium"), Tier("m1", "high"), Tier("m2", "xhigh")]

_DEC = """\
verdict: pass
action: next_order
rationale: "build"
next_order:
  unit: placeholder
  goal: "구현"
  deliverable: "요약"
"""

_SPEC_SINGLE = """\
spec_id: brk-tier-001
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
non_goals: ["n"]
done_when: "ac1"
decomposition:
  - { unit: u1, desc: a, deps: [] }
open_questions: []
"""


class _BrainClient:
    """call#1=synthesize / 이후=replan(DEC). usage는 미노출(orchestration tokens None)."""

    def __init__(self):
        self.n = 0

    def complete(self, system, user, **opts):
        self.n += 1
        return _SPEC_SINGLE if self.n == 1 else _DEC


class _TierUsageExec:
    """tier-aware executor — 받은 tier의 model을 usage로 노출(tier별 비용 식별용)."""

    def __init__(self, tier):
        self.tier = tier
        self.last_usage = None

    def run(self, order):
        self.last_usage = Usage(100, 0, self.tier.model)
        return "ran"


class _GateFail:
    def judge(self, result, spec, unit=None):
        return GateResult(verdict=Verdict.fail_recoverable)


def test_parallel_tier_split_costs_recorded(tmp_path):
    """같은 유닛이 t0→t1→t2로 escalate하면 tier별 비용이 갈려 ledger에 기록된다(#64)."""
    state = run_loop(
        "x", _BrainClient(), executor=None, gate=_GateFail(),
        executor_factory=lambda wt, tier: _TierUsageExec(tier),
        gate_factory=lambda wt: _GateFail(),
        max_parallel=2, workdir=tmp_path, prompt_dir=PROMPT_DIR,
        unit_retries=2, or_alternatives=0, tier_ladder=LADDER3,
    )
    assert state.status is Status.escalated
    # executor leaf의 tier 라벨이 t0/t1/t2로 갈린다(각 시도 한 칸 상향).
    exec_leaves = [lf for lf in state.cost_parts if lf.source == "executor"]
    tiers = sorted({lf.tier for lf in exec_leaves})
    assert tiers == ["m0/medium", "m1/high", "m2/xhigh"]
    # 각 tier 100 tokens × 3 = 300, kind는 build(첫)·retry(재시도)
    assert sum((lf.tokens or 0) for lf in exec_leaves) == 300
    kinds = {lf.kind for lf in exec_leaves}
    assert kinds == {"build", "retry"}  # 최초 build, 이후 retry

    # 대시보드 by_tier에 tier별 분해가 보인다
    v = state_to_view(state)
    bt = v["cost"]["by_tier"]
    assert bt["m0/medium"]["tokens"] == 100
    assert bt["m1/high"]["tokens"] == 100
    assert bt["m2/xhigh"]["tokens"] == 100


# ──────────────────────────── 대시보드: 분해 집계 정합 + 드릴다운 ────────────────────────────


def _ledger_state() -> State:
    """ledger(cost_parts)와 유닛 event.cost.parts를 가진 state(분해 뷰 검증용)."""
    u6_build = Cost(tokens=3100, source="executor", tier="m2/xhigh", kind="build", unit="u6")
    u6_or = Cost(tokens=2000, source="executor", tier="m1/high", kind="OR", unit="u6")
    u6_judge = Cost(tokens=1300, source="judge", kind="judge", unit="u6")
    u6_event = combine_costs([u6_build, u6_or, u6_judge])  # parts 보존
    synth = Cost(tokens=500, source="orchestration", kind="synth")
    return State(
        spec_ref="brk", spec_version=1, status=Status.running,
        plan=[PlanItem(unit="u6", state=PlanState.done)],
        events=[Event(seq=1, unit="u6", work_order_ref="u6", verdict=Verdict.pass_, cost=u6_event)],
        budget=Budget(spent=Cost(tokens=6900, input=None, output=None, usd=None)),
        cost_parts=[synth, u6_build, u6_or, u6_judge],
    )


def test_view_by_kind_and_by_tier_reconcile():
    v = state_to_view(_ledger_state())
    c = v["cost"]
    # 차원 합 == total(6900)
    for dim in ("by_source", "by_tier", "by_kind"):
        s = sum((b["tokens"] or 0) for b in c[dim].values())
        assert s == 6900, f"{dim} 합이 total과 어긋남: {s}"
    assert c["reconciliation"]["reconciled"] is True
    assert c["reconciliation"]["unattributed_tokens"] == 0
    # kind별: build 3100 / OR 2000 / judge 1300 / synth 500
    assert c["by_kind"]["build"]["tokens"] == 3100
    assert c["by_kind"]["OR"]["tokens"] == 2000
    assert c["by_kind"]["judge"]["tokens"] == 1300
    assert c["by_kind"]["synth"]["tokens"] == 500
    # tier별: xhigh 3100 / high 2000 / (미상=judge+synth) 1800
    assert c["by_tier"]["m2/xhigh"]["tokens"] == 3100
    assert c["by_tier"]["m1/high"]["tokens"] == 2000
    assert c["by_tier"]["(미상)"]["tokens"] == 1300 + 500


def test_view_per_unit_drilldown():
    """유닛 비용이 source/tier/kind로 드릴다운된다(WO#70 헤드라인: u6 = xhigh 빌드·OR·judge)."""
    v = state_to_view(_ledger_state())
    u6 = v["cost"]["by_unit"]["u6"]
    assert u6["tokens"] == 6400  # 유닛 총합(synth는 전역이라 제외)
    assert u6["by_kind"]["build"]["tokens"] == 3100
    assert u6["by_kind"]["OR"]["tokens"] == 2000
    assert u6["by_kind"]["judge"]["tokens"] == 1300
    assert u6["by_tier"]["m2/xhigh"]["tokens"] == 3100
    assert u6["by_source"]["executor"]["tokens"] == 5100  # build+OR
    assert u6["by_source"]["judge"]["tokens"] == 1300


def test_view_unattributed_bucket_when_events_only():
    """ledger 없고 event Σ < total이면 '(미귀속)' 버킷으로 정직 노출(누락 0)."""
    s = State(
        spec_ref="x", spec_version=1, status=Status.running,
        plan=[PlanItem(unit="u1", state=PlanState.done)],
        events=[Event(seq=1, unit="u1", cost=Cost(tokens=1000, source="executor"))],
        budget=Budget(spent=Cost(tokens=1500)),  # 합성 등 500이 event에 없음(gap)
    )
    v = state_to_view(s)
    c = v["cost"]
    assert c["reconciliation"]["reconciled"] is False
    assert c["reconciliation"]["unattributed_tokens"] == 500
    assert c["by_source"]["(미귀속)"]["tokens"] == 500
    assert c["by_source"]["executor"]["tokens"] == 1000
    # 차원 합 == total(1500)
    assert sum((b["tokens"] or 0) for b in c["by_source"].values()) == 1500


def test_view_usd_none_tokens_only():
    """usd 미주입(pricing 없음) → 분해도 tokens-only(usd=None, No Fake Metrics)."""
    v = state_to_view(_ledger_state())
    c = v["cost"]
    assert c["total"]["usd"] is None
    assert c["by_kind"]["build"]["usd"] is None
    assert c["by_unit"]["u6"]["by_tier"]["m2/xhigh"]["usd"] is None


def test_view_breakdown_json_serializable():
    json.dumps(state_to_view(_ledger_state()))


def test_old_state_no_cost_parts_graceful():
    """구버전 state(cost_parts 없음)도 무크래시 — event 폴백으로 분해, 새 키 존재."""
    s = State(spec_ref="x", spec_version=1, status=Status.running)  # 빈 state
    v = state_to_view(s)
    assert v["cost"]["by_tier"] == {}
    assert v["cost"]["by_kind"] == {}
    assert v["cost"]["by_unit"] == {}
    assert v["cost"]["reconciliation"]["reconciled"] is True  # total None → 정합 취급


# ──────────────────────────── 대시보드 HTML 스모크 ────────────────────────────


def test_dashboard_html_has_cost_breakdown_drilldown():
    """분해 패널 렌더 요소(by_kind/by_tier + 유닛 드릴다운 + 정합)가 HTML에 존재."""
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    assert "function renderCost" in html
    assert "function costDimTable" in html
    assert "function toggleCostUnit" in html  # 유닛 클릭 → 분해 펼침
    assert "by_kind" in html and "by_tier" in html
    assert "KIND_KO" in html  # kind 한글 라벨
    assert "reconciliation" in html  # 정합 표기
    assert "cost-drill" in html  # 드릴다운 컨테이너
