"""shadow 비교 게이트 — 약 judge(적용)와 강 codex judge(기록만)를 나란히(WO#171, 적용 0).

`--shadow-judge codex`(opt-in)일 때만 쓰인다. 약 judge가 verdict 권위로 *적용*되고, codex가 *같은
workdir/result/spec*을 shadow 판정해 **나란히 기록만** 된다(적용 0 — 반환은 언제나 약 judge의
GateResult). 검증역전(약=pass인데 강=fail)을 누적해 *약 self-judge가 어디서 봐주는지* 데이터를 만든다
→ 새 thesis 무결성을 주장 아닌 *측정*으로. shadow OFF(기본)면 이 래퍼를 안 쓴다 = 100% 로컬·codex
흔적 0(thesis 순수).

**gate/run_judge 판정 로직 불변**: 이 래퍼는 두 *기존* Gate를 호출하고 약 결과를 그대로 반환할 뿐 —
verdict 계산을 안 한다(aggregate_verdict 불변). 약 verdict가 *유일한 권위*고 shadow는 관측이다.
shadow gate는 같은 workdir를 *재판정*하므로 기계 체크가 재실행될 수 있다(결정적이라 보통 일치 —
역전은 LLM 쪽). 이건 opt-in 측정의 알려진 비용이다(shadow OFF면 0).
"""

from __future__ import annotations

import threading

from haetae.gate_signals import classify_gate_signals
from haetae.models import GateResult, ProjectSpec, ShadowComparison, Verdict

# 강 shadow가 fail로 본 verdict들(약=pass와 합쳐 검증역전 판정).
_FAIL_VERDICTS = frozenset({Verdict.fail_recoverable, Verdict.fail_replan})


class ShadowSink:
    """스레드 안전 ShadowComparison 누적기 — 병렬 per-worktree 게이트가 *하나*에 모은다.

    병렬 모드에서 gate_factory가 워크트리별 ShadowComparingGate를 만들어도 모두 같은 sink에 append해,
    루프가 한 번에 state.shadow_comparisons로 드레인한다(스레드 안전). snapshot은 방어적 복사.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items: list[ShadowComparison] = []

    def add(self, cmp: ShadowComparison) -> None:
        with self._lock:
            self._items.append(cmp)

    def snapshot(self) -> list[ShadowComparison]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def compare_verdicts(
    primary: GateResult, shadow: GateResult, unit: str | None
) -> ShadowComparison:
    """약(primary, 적용) vs 강(shadow, 기록만) verdict 비교 → ShadowComparison(검증역전 포함). 순수 함수.

    inverted = 약=pass & 강=fail (약 judge가 봐준 지점 = 검증역전). locus는 *강이 fail*한 체크의 분류
    (llm/mechanical/mixed)로 — 역전이 약 LLM 판정 갭인지(흥미로운 신호) 기계 차이(드문 비결정)인지
    구분한다(#113 기계 신호는 결정적이라 보통 일치 → 역전 locus는 보통 llm). verdict를 만들지 않는다
    (적용 0 — 비교만).
    """
    pv = primary.verdict
    sv = shadow.verdict
    inverted = (pv == Verdict.pass_) and (sv in _FAIL_VERDICTS)
    locus: str | None = None
    detail: str | None = None
    if pv != sv:
        split = classify_gate_signals(shadow)
        locus = split.fail_locus  # 강이 *어디서* fail했나(llm/mechanical/mixed)
        fails = split.mechanical_fail + split.llm_fail + split.run_fail
        if fails:
            detail = "shadow fail @ " + ", ".join(fails[:6])
    return ShadowComparison(
        unit=unit,
        primary_verdict=pv.value,
        shadow_verdict=sv.value,
        inverted=inverted,
        locus=locus,
        detail=detail,
    )


class ShadowComparingGate:
    """약 judge(적용) + 강 shadow judge(기록만)를 나란히 돌리는 Gate 래퍼(WO#171, 적용 0).

    judge()는 (1) primary(약/로컬) gate로 판정해 그 GateResult를 *적용*(반환), (2) shadow(codex) gate로
    같은 result/spec/unit을 판정, (3) 비교를 sink에 *기록만* → primary를 *그대로 반환*(verdict 권위
    불변). shadow 판정/비교 중 *어떤 예외도* 흡수한다 — 관측이 적용 verdict를 절대 못 바꾼다(best-effort,
    적용 0). primary 판정 자체의 예외는 그대로 전파(관측 래퍼가 정상 실패 경로를 안 가린다).
    """

    def __init__(self, primary, shadow, sink: ShadowSink):
        self.primary = primary
        self.shadow = shadow
        self.sink = sink

    def judge(self, result: str, spec: ProjectSpec, unit: str | None = None) -> GateResult:
        primary_result = self.primary.judge(result, spec, unit=unit)
        try:
            shadow_result = self.shadow.judge(result, spec, unit=unit)
            self.sink.add(compare_verdicts(primary_result, shadow_result, unit))
        except Exception:  # noqa: BLE001 — shadow=관측(적용 0): 어떤 실패도 적용 verdict를 안 바꾼다
            pass
        return primary_result  # 적용 권위 = 언제나 약 judge(shadow는 기록만)
