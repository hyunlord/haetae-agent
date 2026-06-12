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


# 통합 적응 재빌드 지시(IP, WO#48). 머지 충돌 시 replan feedback으로 태운다.
# stale 베이스에서 같은 변경을 재생성하지 말고 *현재 머지된 main* 위에 통합되게 적응시킨다.
# **criteria 약화 금지** — 통합되게 *적응*할 뿐, 기준 재정의가 아니다(anti-erosion).
_INTEGRATION_DIRECTIVE = (
    "직전 결과가 통합(main) 머지에서 **충돌**했다 — 병렬 형제 유닛이 같은 파일을 건드렸다.\n"
    "워크트리는 이미 **최신 main**(머지된 형제 반영)에서 다시 분기됐다. 그 위에서:\n"
    "- **처음부터 다시 만들지 마라.** 현재 통합 상태를 *읽고*, 그 위에 깔끔히 통합되게 변경을 적응하라.\n"
    "- 겹치는 파일은 **기존 내용을 존중·확장**한다(덮어쓰기·재작성 금지). 너의 책임 부분만 통합한다.\n"
    "- 같은 충돌을 재생성하지 마라 — 형제의 기여를 보존하면서 네 기여를 합쳐라.\n"
    "- **acceptance_criteria / done_when은 절대 바꾸지 마라**(같은 독립 gate가 판정한다). "
    "기준을 낮추거나 재정의하지 마라 — *통합되게 적응*할 뿐이다."
)


def build_integration_feedback(
    unit: str,
    conflict_files: list[str] | None,
    merged_siblings: list[str] | None,
) -> str:
    """머지 충돌 → 통합 적응 재빌드 피드백(replan feedback으로 주입, WO#48).

    unit: 충돌한 유닛. conflict_files: 겹친(충돌) 파일 목록. merged_siblings: 이미
    main에 머지된 형제 유닛들. bar 불변 directive 포함 — 기준 약화 금지, *통합 적응*만.
    """
    lines = [f"[통합 적응 재빌드 — 유닛 {unit}]"]
    if merged_siblings:
        lines.append(
            "- main에 이미 머지된 형제: " + ", ".join(str(s) for s in merged_siblings)
        )
    if conflict_files:
        lines.append("- 충돌(겹친) 파일: " + ", ".join(str(f) for f in conflict_files))
    lines.append(_INTEGRATION_DIRECTIVE)
    return "\n".join(lines)


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


# ──────────────── WO#79: proactive anti-fixation (CRDAL co-regulation) ────────────────
#
# 현 메커니즘은 다 *사후*다: #41 OR은 재시도 소진 *後* 대안 분해, #68C는 누적 ceiling *後* escalate.
# 둘 다 *고착을 다 태운 뒤* 작동. CRDAL(2603.24768): 에이전트 *자기* 모니터(self-regulation)는
# 개선 없음, *별도* 감지(co-regulation)는 개선. → 빌더 자기평가가 아니라 *분리된 신호*(gate fail
# 사유 = judge 산출 + 시도 이력)로 고착을 **조기** 감지해 #41 OR / #68C ceiling *前*에 대안 nudge.
#
# **bar 불변**: nudge는 *접근*만 바꾸지 기준을 안 낮춘다(_ALTERNATIVE_DIRECTIVE와 동일 규율).
# **빌더 전용**: replan feedback 채널로만 주입(judge/run-judge 무수신, #41과 동일 분리).
# **advisory·bounded**: 불확실(실패 체크 0)하면 감지 안 함(no-op). 못 깨면 #41 OR / #68C가 받음.

_ANTI_FIXATION_DIRECTIVE = (
    "직전 시도들이 **같은 사유로 반복 실패**하고 있다(진전 없음 — 고착 신호).\n"
    "같은 접근의 변형(파라미터 튜닝·사소한 수정)을 또 반복하지 마라. 이번엔 **구조적으로 다른 접근**"
    "(다른 알고리즘/자료구조/설계)으로 같은 목표를 재구현하라.\n"
    "- **acceptance_criteria / done_when은 절대 바꾸지 마라**(같은 bar에서 경쟁한다 — 기준 약화 금지).\n"
    "- 같은 독립 gate가 이 접근도 판정한다. 기준의 字面만이 아니라 *취지*를 충족하라."
)


def fixation_fail_digest(gr: Any) -> tuple | None:
    """gate 실패의 *결정적* 사유 지문 — 실패한 (ac_id, check_type) 정렬 튜플 (분리 신호).

    빌더 자기보고가 아니라 *독립 gate가 산출한* 실패 사유로 만든 지문(CRDAL co-regulation).
    같은 지문이 연속 재발 = 같은 문제로 진전 없음 = 고착. 실패 check가 0이면 None(정보 부족 →
    감지 보류, advisory). 순수 함수(LLM/IO 없음) — 결정적·저비용.
    """
    checks = getattr(gr, "checks", None) or []
    fails = []
    for c in checks:
        if getattr(c, "status", None) != "fail":
            continue
        ct = getattr(c, "check_type", None)
        ctv = ct.value if hasattr(ct, "value") else (str(ct) if ct is not None else "")
        fails.append((str(getattr(c, "ac_id", "") or ""), ctv))
    if not fails:
        return None  # 실패 사유 불명 → 고착 판정 보류(advisory no-op)
    return tuple(sorted(fails))


def is_fixated(history: list, threshold: int = 2) -> bool:
    """같은 fail 지문이 threshold회 *연속* 재발하면 고착(결정적). threshold<2면 비활성(off).

    history: 시간순 fail 지문 목록(None=정보부족 항목은 고착 판정을 깬다). 마지막 threshold개가
    전부 동일(그리고 None 아님)이면 True. advisory — 불확실/진전이면 False(nudge 안 함).
    """
    if threshold < 2 or len(history) < threshold:
        return False
    window = history[-threshold:]
    first = window[0]
    return first is not None and all(d == first for d in window)


def build_anti_fixation_feedback(
    approach: str | None, reason: str | None, *, recurrences: int = 2
) -> str:
    """고착 조기 감지 → 구조적 대안 nudge 텍스트(replan feedback으로 주입, 빌더 전용).

    approach: 폐기할 직전 접근 요약. reason: 반복된 실패 사유 요약(분리 신호). bar 불변 directive 포함
    — *접근*만 바꾸고 기준은 안 낮춘다. #41 build_alternative_feedback의 *조기* 버전(OR 소비 없음).
    """
    lines = ["[고착 조기 감지 — 구조적 대안 nudge (재시도 소진 前)]"]
    if reason:
        lines.append(f"- 직전 {recurrences}회 연속 *같은 사유*로 실패: {reason}")
    else:
        lines.append(f"- 직전 {recurrences}회 연속 같은 방식으로 실패(진전 없음)")
    if approach:
        lines.append(f"- 폐기할 직전 접근: {' '.join(str(approach).split())[:200]}")
    lines.append(_ANTI_FIXATION_DIRECTIVE)
    return "\n".join(lines)
