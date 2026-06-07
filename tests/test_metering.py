"""토큰/코스트 계측 레이어 테스트 (WO#33 Part A).

usage 캡처 → Cost 변환(가격표로 usd) → 누적 → MeteredClient 래퍼.
순수 데이터/로직. 네트워크/시크릿 없음.
"""

from haetae.metering import (
    PRICING,
    MeteredClient,
    Usage,
    accumulate,
    combine_costs,
    cost_from_usage,
)
from haetae.models import Cost


# ──────────────────────────── cost_from_usage ────────────────────────────


def test_cost_from_usage_known_model_computes_usd():
    """알려진 모델 → 가격표로 usd 계산. tokens=input+output, input/output 보존."""
    pricing = {"test-model": (1.0, 2.0)}  # $/Mtok (input, output)
    u = Usage(input_tokens=1_000_000, output_tokens=500_000, model="test-model")
    c = cost_from_usage(u, source="orchestration", pricing=pricing)
    assert c.input == 1_000_000
    assert c.output == 500_000
    assert c.tokens == 1_500_000
    # 1M*$1 + 0.5M*$2 = 1.0 + 1.0 = 2.0
    assert c.usd == 2.0
    assert c.source == "orchestration"


def test_cost_from_usage_unknown_model_usd_none_tokens_only():
    """미상 모델 → usd=None, tokens만 채운다(날조 금지)."""
    u = Usage(input_tokens=100, output_tokens=50, model="who-knows")
    c = cost_from_usage(u, source="executor", pricing={"test-model": (1.0, 2.0)})
    assert c.tokens == 150
    assert c.input == 100
    assert c.output == 50
    assert c.usd is None
    assert c.source == "executor"


def test_cost_from_usage_model_none_usd_none():
    """모델 미지정(codex 기본) → usd=None, tokens만."""
    u = Usage(input_tokens=10, output_tokens=20, model=None)
    c = cost_from_usage(u, source="orchestration")
    assert c.tokens == 30
    assert c.usd is None


def test_cost_from_usage_none_returns_none():
    """usage 부재 → None(무크래시)."""
    assert cost_from_usage(None, source="orchestration") is None


# ──────────────────────────── combine_costs ────────────────────────────


def test_combine_costs_sums_and_marks_mixed_source():
    """여러 cost 합산: tokens/usd/input/output 합, 서로 다른 source면 'mixed'."""
    a = Cost(tokens=100, usd=0.01, input=60, output=40, source="orchestration")
    b = Cost(tokens=200, usd=0.02, input=150, output=50, source="executor")
    c = combine_costs([a, b])
    assert c.tokens == 300
    assert abs(c.usd - 0.03) < 1e-9
    assert c.input == 210
    assert c.output == 90
    assert c.source == "mixed"


def test_combine_costs_single_source_preserved():
    a = Cost(tokens=100, usd=0.01, source="orchestration")
    b = Cost(tokens=50, source="orchestration")  # usd None
    c = combine_costs([a, b])
    assert c.tokens == 150
    assert c.usd == 0.01  # 알려진 것만 합산(미상은 무시)
    assert c.source == "orchestration"


def test_combine_costs_empty_returns_none():
    assert combine_costs([]) is None
    assert combine_costs([None, None]) is None


# ──────────────────────────── accumulate ────────────────────────────


def test_accumulate_adds_into_budget_spent():
    spent = Cost()
    accumulate(spent, Cost(tokens=100, usd=0.01, input=60, output=40))
    accumulate(spent, Cost(tokens=50, usd=0.02, input=30, output=20))
    assert spent.tokens == 150
    assert abs(spent.usd - 0.03) < 1e-9
    assert spent.input == 90
    assert spent.output == 60


def test_accumulate_none_is_noop():
    spent = Cost(tokens=10)
    accumulate(spent, None)
    assert spent.tokens == 10


# ──────────────────────────── MeteredClient ────────────────────────────


class _StubClient:
    """complete 호출마다 주입된 usage를 last_usage로 노출하는 스텁."""

    def __init__(self, usages):
        self._usages = list(usages)
        self._i = 0
        self.last_usage = None
        self.calls = []

    def complete(self, system, user, **opts):
        self.calls.append((system, user))
        self.last_usage = self._usages[min(self._i, len(self._usages) - 1)]
        self._i += 1
        return "resp"


def test_metered_client_records_cost_per_call_and_drains():
    inner = _StubClient([Usage(input_tokens=100, output_tokens=50, model="m")])
    mc = MeteredClient(inner, source="orchestration", pricing={"m": (1.0, 1.0)})
    assert mc.complete("s", "u") == "resp"
    records = mc.drain()
    assert len(records) == 1
    assert records[0].tokens == 150
    assert records[0].source == "orchestration"
    # drain은 비운다
    assert mc.drain() == []


def test_metered_client_no_usage_records_nothing():
    """inner가 usage 안 주면(last_usage=None) 기록 0(날조 금지)."""
    inner = _StubClient([None])
    mc = MeteredClient(inner, source="orchestration")
    mc.complete("s", "u")
    assert mc.drain() == []


def test_metered_client_passes_through_text_and_is_llmclient():
    from haetae.llm import LLMClient

    inner = _StubClient([None])
    mc = MeteredClient(inner, source="orchestration")
    assert isinstance(mc, LLMClient)  # Protocol 충족(complete 있음)


def test_pricing_table_exists_and_unknown_returns_none():
    """기본 가격표는 dict이고, 미등록 모델은 usd 계산 불가(None)."""
    assert isinstance(PRICING, dict)
    u = Usage(input_tokens=1, output_tokens=1, model="definitely-not-in-table-xyz")
    assert cost_from_usage(u, source="x").usd is None
