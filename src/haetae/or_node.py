"""OR-node 헬퍼 (WO#41, Phase D = LEAP AND-OR DAG).

gate가 정상 재시도까지 소진하고도 실패하면, 같은 goal에 대한 *근본적으로 다른 접근*을
생성하기 위한 (a) gate 실패 증거 요약과 (b) 대안 생성 지시(피드백)를 만든다.

**bar 불변(anti-erosion) — 이 모듈의 핵심 규율:**
  대안은 *접근(알고리즘/구조)*만 바꾼다. acceptance_criteria/done_when은 절대 건드리지
  않는다. 생성 지시는 gate 실패 *증거*만 피드백하고, **기준 약화/변경을 유도하지 않는다**.
  대안 order는 호출부에서 분해 critic(#40)으로 "진짜 다름"을 검증한다(재진술이면 weak).

대안 *생성*은 기존 replan을 재사용한다(replan의 feedback 채널로 이 지시를 태운다) —
replan/judge/gate의 프롬프트·판정 로직은 건드리지 않는다(독립 gate 불변).
"""

from __future__ import annotations

from typing import Any

# 대안 생성 지시(IP). replan feedback으로 태운다. **criteria 약화 금지**를 못박는다.
_ALTERNATIVE_DIRECTIVE = (
    "직전 접근이 독립 gate 판정을 정상 재시도까지 소진하고도 통과하지 못했다.\n"
    "이번엔 **근본적으로 다른 접근**(다른 알고리즘/자료구조/설계)으로 같은 목표를 노려라.\n"
    "- 직전 접근의 변형(파라미터 튜닝/사소한 수정)이 아니라 *대안*이어야 한다.\n"
    "- **acceptance_criteria / done_when은 절대 바꾸지 마라**(같은 bar에서 경쟁한다). "
    "기준을 낮추거나 재정의하자고 제안하지 마라 — 접근만 바꾼다.\n"
    "- 같은 독립 gate가 이 대안도 판정한다. 기준의 *字面*만이 아니라 *취지*를 충족하라."
)


def summarize_gate_evidence(gr: Any, *, cap: int = 400) -> str | None:
    """GateResult의 *실패* 증거를 한 줄로 요약(대안 생성 피드백용, 읽기만).

    실패 check의 ac_id + run-judge reason(또는 detail)을 모은다. 실패가 없으면 None.
    bar 불변: 이건 *증거*일 뿐 — 기준을 바꾸자는 게 아니다.
    """
    checks = getattr(gr, "checks", None) or []
    failed = [c for c in checks if getattr(c, "status", None) == "fail"]
    if not failed:
        # 실패 check가 없으면 verdict라도 노출(통합 gate가 checks 없이 fail일 수 있음).
        v = getattr(gr, "verdict", None)
        vv = v.value if hasattr(v, "value") else v
        return f"gate verdict={vv}" if vv else None
    parts: list[str] = []
    for c in failed[:3]:
        ct = getattr(c, "check_type", None)
        what = getattr(c, "ac_id", None) or (ct.value if hasattr(ct, "value") else ct) or "check"
        bits = [str(what)]
        ev = getattr(c, "run_evidence", None)
        reason = getattr(ev, "reason", None) if ev is not None else None
        detail = reason or getattr(c, "detail", None)
        if detail:
            bits.append(" ".join(str(detail).split())[:200])
        parts.append(": ".join(bits))
    s = " | ".join(parts)
    return s if len(s) <= cap else s[:cap] + "…"


def build_alternative_feedback(
    approach: str | None, evidence: str | None, *, scope: str = "unit"
) -> str:
    """대안 생성 피드백 텍스트(replan feedback으로 주입). bar 불변 directive 포함.

    approach: 폐기하는 직전 접근 요약(반복 회피용). evidence: gate 실패 증거.
    scope: "unit" | "integration" — 통합이면 cross-unit 통합이 깨졌음을 명시.
    """
    lines = []
    where = "통합(cross-unit)" if scope == "integration" else "이 유닛"
    lines.append(f"[OR 대안 — {where}]")
    if approach:
        lines.append(f"- 폐기하는 직전 접근: {' '.join(str(approach).split())[:200]}")
    if evidence:
        lines.append(f"- 실패 증거(독립 gate): {evidence}")
    lines.append(_ALTERNATIVE_DIRECTIVE)
    return "\n".join(lines)
