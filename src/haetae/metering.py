"""토큰/코스트 계측 (WO#33 Part A).

LLM 호출의 token usage를 캡처해 Cost로 변환(가격표로 usd 계산)하고,
budget.spent에 누적하거나 event.cost로 귀속한다.

핵심 원칙:
  - best-effort: 계측 실패는 흡수(None). 계측이 run을 죽이면 안 된다.
  - 정직: 못 잡는 비용(미상 모델/usage 미노출)은 usd=None / tokens=None으로 둔다.
    가짜 숫자 금지.

가격표(PRICING)는 model → (input_per_mtok, output_per_mtok) USD. 미등록 모델은
usd 계산 불가(None)다. 여기 적힌 숫자는 *추정치*이며, 호출부에서 override 가능하다
(codex 기본 모델은 우리가 이름을 모르므로 model=None → usd=None가 정상 동작).
"""

from __future__ import annotations

from dataclasses import dataclass

from haetae.models import Cost

# model → (input $/Mtok, output $/Mtok). 추정치(override 가능). 미등록 → usd=None.
# codex 기본 모델은 이름 미상(model=None)이라 보통 여기 안 걸려 usd=None가 정상이다.
PRICING: dict[str, tuple[float, float]] = {}


@dataclass(frozen=True)
class Usage:
    """LLM 한 호출의 토큰 usage(+모델). 캡처 실패 필드는 None.

    클라이언트가 usage를 노출하면 complete 직후 self.last_usage에 이걸 싣고,
    MeteredClient가 읽어 Cost로 변환한다.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


def _total(inp: int | None, out: int | None) -> int | None:
    if inp is None and out is None:
        return None
    return (inp or 0) + (out or 0)


def cost_from_usage(
    usage: Usage | None,
    *,
    source: str,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> Cost | None:
    """Usage → Cost. usd는 모델이 가격표에 있고 input/output 둘 다 있을 때만 계산.

    usage가 None이면 None(무크래시). 모델 미상/미등록이면 usd=None·tokens만(날조 금지).
    """
    if usage is None:
        return None
    table = PRICING if pricing is None else pricing
    inp, out = usage.input_tokens, usage.output_tokens
    usd: float | None = None
    rate = table.get(usage.model) if usage.model else None
    if rate is not None and inp is not None and out is not None:
        usd = inp / 1_000_000 * rate[0] + out / 1_000_000 * rate[1]
    return Cost(
        tokens=_total(inp, out),
        usd=usd,
        input=inp,
        output=out,
        source=source,
    )


def _sum_opt(values: list[int | float]) -> int | float | None:
    """None을 무시하고 합산. 알려진 값이 없으면 None(미상 보존)."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def combine_costs(costs: list[Cost | None]) -> Cost | None:
    """여러 Cost를 하나로 합산(이벤트 귀속용). 모두 None/빈 리스트면 None.

    source가 유일하면 그대로, 둘 이상이면 'mixed'. note는 이어붙인다.
    각 필드는 알려진 값만 합산(미상은 무시 → 정직).
    """
    real = [c for c in costs if c is not None]
    if not real:
        return None
    sources = {c.source for c in real if c.source}
    source = sources.pop() if len(sources) == 1 else ("mixed" if sources else None)
    notes = [c.note for c in real if c.note]
    return Cost(
        tokens=_sum_opt([c.tokens for c in real]),
        usd=_sum_opt([c.usd for c in real]),
        input=_sum_opt([c.input for c in real]),
        output=_sum_opt([c.output for c in real]),
        source=source,
        note="; ".join(notes) if notes else None,
    )


def accumulate(spent: Cost, cost: Cost | None) -> None:
    """budget.spent에 cost를 in-place 누적. cost=None이면 no-op.

    각 수치 필드(tokens/usd/input/output)는 알려진 값만 더한다(미상은 건너뜀).
    """
    if cost is None:
        return
    for attr in ("tokens", "usd", "input", "output"):
        v = getattr(cost, attr)
        if v is not None:
            cur = getattr(spent, attr)
            setattr(spent, attr, (cur or 0) + v)


class MeteredClient:
    """LLMClient 래퍼 — complete 호출마다 inner.last_usage를 읽어 Cost로 적립한다.

    inner의 complete 반환(str)은 그대로 통과시켜 호출부는 불변. 적립된 Cost는
    drain()으로 꺼낸다(루프가 이벤트/예산에 귀속). usage 캡처는 전부 best-effort —
    inner가 last_usage를 안 주거나 변환이 실패해도 흡수(기록 0)하고 텍스트는 반환한다.

    source: 이 클라이언트 호출을 무엇으로 귀속할지("orchestration" 등).
    """

    def __init__(
        self,
        inner,
        *,
        source: str,
        pricing: dict[str, tuple[float, float]] | None = None,
    ):
        self.inner = inner
        self.source = source
        self.pricing = pricing
        self.records: list[Cost] = []

    def complete(self, system: str, user: str, **opts) -> str:
        text = self.inner.complete(system, user, **opts)
        # 계측은 절대 run을 죽이지 않는다 — 캡처 경로 전체를 흡수.
        try:
            usage = getattr(self.inner, "last_usage", None)
            cost = cost_from_usage(usage, source=self.source, pricing=self.pricing)
            if cost is not None:
                self.records.append(cost)
        except Exception:  # noqa: BLE001 — best-effort 계측
            pass
        return text

    def drain(self) -> list[Cost]:
        """적립된 Cost를 꺼내고 버퍼를 비운다."""
        out = self.records
        self.records = []
        return out
