"""기계적 게이트 신호 분류 — 약-judge 런서 '결정적 사실 주력'을 표면화(WO#171-B, read-only).

새 thesis(강 모델 0)에서 judge LLM이 약하므로, gate verdict가 *얼마나 결정적(모델-무관) 사실에
기대는지* 정직하게 보이게 한다. **gate/run_judge 판정 로직은 불변** — 이 모듈은 *이미 산출된*
GateResult.checks를 *읽어 분류만* 한다(verdict 재계산 0·바 불완화 0). 기계적 신호(빌드/테스트 exit
code·트레이스 증거계약·하니스 구조 smoke·런타임-smoke·install)는 subprocess/필드-존재라 모델 강도와
무관하게 결정적이고, aggregate_verdict가 *어떤 기계 fail이든* veto한다(기존 동작) — 즉 '기계 주력,
약 LLM 보조'는 *이미 아키텍처*다. 이 분류는 그 사실을 *측정·표면화*할 뿐 verdict를 만들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haetae.models import CheckType, GateResult

# 결정적·모델-무관 체크 타입(subprocess exit-code 또는 필드-존재 schema 체크). 약 LLM 판정과 무관 —
# 빌드/타입체크·테스트 exit·증거계약(#78)·하니스 구조 smoke(#82-B)·런타임-smoke(#160)·install이 여기.
MECHANICAL_CHECK_TYPES = frozenset(
    {CheckType.build, CheckType.test, CheckType.lint, CheckType.bench, CheckType.schema}
)
# 약 LLM 판정(주관 루브릭)에 기대는 체크 타입. run은 별도(혼합 — 결정적 booted + LLM 행동판정).
LLM_CHECK_TYPES = frozenset({CheckType.judge})


def _bucket(check_type) -> str:
    """check_type을 mechanical|llm|run 버킷으로. run은 별도(혼합), 미상 타입은 보수적으로 mechanical."""
    if check_type in MECHANICAL_CHECK_TYPES:
        return "mechanical"
    if check_type in LLM_CHECK_TYPES:
        return "llm"
    if check_type == CheckType.run:
        return "run"
    return "mechanical"  # 미상 타입 → 보수적으로 기계(결정적)로 분류(누락 방지)


@dataclass
class GateSignalSplit:
    """GateResult.checks를 기계적/LLM/run(혼합)으로 분류한 read-only 요약(verdict 불변).

    mechanical/llm/run:        각 분류의 ac_id 리스트.
    *_fail:                    그 분류에서 status=="fail"인 ac_id.
    fail_locus(property):      fail이 어디서 왔나 — "mechanical"(결정적·모델무관)|"llm"(약 judge)|
                               "mixed"|None(fail 아님). 약-judge 런서 "이 실패는 기계적이라 모델
                               강도와 무관"을 정직히 말하게 한다(run_fail은 보수적으로 soft 취급 —
                               run은 LLM 행동판정일 수 있어 결정적이라 *과대주장 안 함*).
    mechanical_decisive(prop): fail이 기계(결정적·모델무관) 신호를 포함하나 — 그 부분은 약 judge와
                               무관하게 견고하다는 정직 신호.
    """

    mechanical: list[str] = field(default_factory=list)
    llm: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    mechanical_fail: list[str] = field(default_factory=list)
    llm_fail: list[str] = field(default_factory=list)
    run_fail: list[str] = field(default_factory=list)

    @property
    def fail_locus(self) -> str | None:
        mech = bool(self.mechanical_fail)
        # run_fail은 보수적으로 soft(LLM 측)로 — run은 LLM 행동판정일 수 있어 '기계적'으로 과대주장 안 함.
        soft = bool(self.llm_fail or self.run_fail)
        if mech and soft:
            return "mixed"
        if mech:
            return "mechanical"
        if soft:
            return "llm"
        return None

    @property
    def mechanical_decisive(self) -> bool:
        """fail이 기계(결정적·모델무관) 신호를 포함하는가 — 그 부분은 약 judge 강도와 무관하게 견고."""
        return bool(self.mechanical_fail)


def classify_gate_signals(gr: GateResult) -> GateSignalSplit:
    """*이미 산출된* GateResult를 읽어 기계/LLM/run 신호로 분류한다(verdict 재계산 0·판정 로직 불변).

    순수·read-only — GateResult를 변형하지 않고 verdict를 만들지도 않는다(aggregate_verdict 불변).
    약-judge 런서 'verdict가 얼마나 결정적 사실에 기댔나'를 표면화(point B)하고, shadow가 역전 locus
    (LLM 판정 갭 vs 기계 비결정)를 분류하는 데 쓴다.
    """
    split = GateSignalSplit()
    for c in gr.checks or []:
        bucket = _bucket(c.check_type)
        getattr(split, bucket).append(c.ac_id)
        if c.status == "fail":
            getattr(split, f"{bucket}_fail").append(c.ac_id)
    return split
