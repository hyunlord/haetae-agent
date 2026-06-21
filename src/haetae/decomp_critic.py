"""분해 critic at replan (WO#40, Phase C = LEAP LLM 리뷰어).

spec critic(WO#20)이 *spec 1회*의 합격선 물렁함을 잡는다면, 이 모듈은 replan이 *매
iteration* 내놓는 work order(분해)의 **진전성**을 적대적으로 판정한다.

LEAP 교훈: 이 리뷰어 ablation → 형식상 멀쩡한데 무진전인 분해를 못 걸러 8 rollout에도
실패. haetae는 캡스톤에서 u1 과부하·헛돎을 봤다 — 그 틈을 메운다.

설계(spec critic과 동형):
  - **적대적 분리**: critic은 *verifier 쪽* 기능. **독립 client(critic-model)**, 빌더 전용
    스킬(WO#32) 주입을 받지 않는다(자기가 짠 걸 자기가 통과시키는 collapse 방지).
  - **best-effort·soft**: critic LLM 실패/파싱 불가 → "progress"로 흡수(루프 안 죽임).
  - 적대 프레이밍은 prompts/decomp_critic.md에 있다(막연한 지적 금지, 구체일 때만 weak).

판정: progress(유닛을 단순화/진전 — dispatch OK) | weak(전체 goal/spec 재진술, 무진전,
직전 실패 접근 반복). bounded 재replan은 호출부(loop.py)가 제어한다 — 여기선 *판정만*.
"""

from __future__ import annotations

import re
from pathlib import Path

from haetae.llm import LLMClient
from haetae.models import DecompCritique, NextOrder, PlanState, ProjectSpec, State
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_DECOMP_CRITIC_PROMPT_PATH = "prompts/decomp_critic.md"


# ──────────────────────────── 입도/책임 수 축 (WO#148) ────────────────────────────
#
# #147 진단: snake 엔진 전체(이동+성장+충돌+먹이+점수+game-over)를 한 유닛에 몰자 약한
# 로컬 빌더가 4 dispatch + OR-alt 내내 미수렴 — gate의 test:engine이 self-test와 *정렬*
# (인공 바 아님)이라 순수 *유닛 용량* 문제. director-side 보정: 과대-다행동 유닛을 결정적
# 으로 탐지해 critic 프롬프트에 *신호*로 주입(codex critic이 최종 판정 — LLM 권위 유지·
# 오버블록 회피). 통합/조립 유닛은 면제(distinct 조립 파일 소유 = disjoint-scope, #51/#123).
# gate/run-judge/judge와 무관(director-side 계획 신호일 뿐).

# 독립 행동을 가르는 리스트 구분자(경로의 '/'는 제외 — 파일경로 false-count 회피).
_BEHAVIOR_SEP = re.compile(r"[,，;；·、]|\+| 및 | and | & |\n")
# 통합/조립 유닛 마커(다수 모듈을 wire — distinct 조립 파일 소유, 과대 아님 → 면제).
_INTEGRATION_MARKERS = (
    "통합", "조립", "조합", "결합", "조율", "오케스트", "wire", "wiring",
    "compose", "composition", "integrat", "assemble", "orchestr",
)
_OVER_LARGE_THRESHOLD = 4  # ≥4개 독립 행동 클로즈를 한 유닛에 묶으면 과대(약한 빌더 기준).


def _behavior_clauses(text: str) -> list[str]:
    """텍스트를 리스트 구분자로 쪼개 *비자명* 행동 클로즈만 반환(길이<2·순수 filler 제외)."""
    parts = [p.strip(" \t·*-—:") for p in _BEHAVIOR_SEP.split(text or "")]
    return [p for p in parts if len(p) >= 2 and p not in ("및", "등", "그리고", "and")]


def granularity_signal(order: NextOrder) -> str | None:
    """과대-다행동 유닛이면 *분할 권고 신호*(한 줄)를, 아니면 None을 반환(결정적·director-side).

    WO#148: 한 유닛 goal/scope/deliverable에 독립 행동이 임계(≥4) 이상 묶이면 약한 로컬
    빌더가 단일 유닛으로 신뢰성있게 수렴하기 과대 → 단일-책임 disjoint-scope 유닛들로 쪼갠다.
    통합/조립 유닛(모듈 wire)은 면제(distinct 조립 파일 소유). **판정 아님** — critic(codex)에
    주입되는 *신호*다(LLM이 최종 판정·오버블록 회피). gate/run-judge와 무관(director-side).
    """
    blob = " ".join(p for p in (order.goal, order.scope, order.deliverable) if p)
    if any(m in blob.lower() for m in _INTEGRATION_MARKERS):
        return None  # 통합/조립 유닛 — 모듈 wire(disjoint-scope 소유), 과대 아님
    clauses = _behavior_clauses(blob)
    if len(clauses) < _OVER_LARGE_THRESHOLD:
        return None
    head = ", ".join(clauses[:6])
    return (
        f"이 유닛이 독립 행동을 약 {len(clauses)}개({head}…) 한 덩어리로 묶음. 약한 로컬 "
        "빌더가 단일 유닛으로 신뢰성있게 수렴하기엔 과대 — 각 독립 행동을 *distinct 모듈 "
        "파일을 소유하는 단일-책임 disjoint-scope 유닛*으로 쪼개고(파일 겹침 0), 별도 통합/"
        "조립 유닛이 wire하도록 분할 권고."
    )


class DecompCriticError(ParseError):
    """분해 critic 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


def _norm_verdict(v: str | None) -> str:
    """verdict 변종을 progress|weak로 정규화. 'weak'만 weak, 그 외 전부 progress(보수적).

    미지값·평가불가는 progress로 흡수 → 진행을 막지 않는다(데드락/오버블록 금지).
    """
    return "weak" if (v or "").strip().lower() == "weak" else "progress"


def _normalize_critique_dict(data: dict) -> dict:
    """critic 출력의 흔한 변종을 DecompCritique 스키마 모양으로 보정(보조 안전망).

    - reason 키 변종(rationale/why/note/explanation) 흡수.
    - verdict가 'no-progress'/'restated' 등 변종이면 weak로, 'ok'/'good' 등은 progress로.
    """
    if not isinstance(data, dict):
        return data
    d = dict(data)
    if "reason" not in d:
        for k in ("rationale", "why", "note", "explanation", "reasoning"):
            if k in d:
                d["reason"] = d.pop(k)
                break
    v = str(d.get("verdict", "")).strip().lower()
    weak_aliases = {"weak", "no-progress", "no_progress", "noprogress", "restated", "restate", "stuck"}
    if v in weak_aliases:
        d["verdict"] = "weak"
    return d


def is_weak(crit: DecompCritique) -> bool:
    """이 분해가 무진전(weak)으로 판정됐는지 — 호출부의 reject/재replan 트리거."""
    return _norm_verdict(crit.verdict) == "weak"


def _plan_progress(state: State) -> str:
    """현재 plan 상태를 critic용 한 단락으로: done/in_progress/pending/blocked 유닛."""
    by_state: dict[str, list[str]] = {}
    done_units = {p.unit for p in state.plan if p.state == PlanState.done}
    for p in state.plan:
        by_state.setdefault(p.state.value, []).append(p.unit)
    lines = []
    for st in ("done", "in_progress", "pending", "failed"):
        units = by_state.get(st)
        if units:
            lines.append(f"- {st}: {', '.join(units)}")
    # blocking: 미완 유닛의 완료 안 된 의존(무엇이 진전을 막나).
    blocked = []
    for p in state.plan:
        if p.state == PlanState.done:
            continue
        bys = [d for d in (p.deps or []) if d not in done_units]
        if bys:
            blocked.append(f"{p.unit}←{','.join(bys)}")
    if blocked:
        lines.append(f"- blocked(미완 deps): {'; '.join(blocked)}")
    return "\n".join(lines) or "(plan 비어있음 또는 전부 pending)"


def _build_user(
    order: NextOrder, spec: ProjectSpec, state: State, last_result: str | None
) -> str:
    checks = ", ".join(
        f"[{c.type.value}] {c.cmd or '(cmd 없음)'}" for c in order.local_checks
    ) or "(없음)"
    base = (
        f"# work_order (replan이 낸 다음 분해 — 판정 대상)\n"
        f"- unit: {order.unit}\n"
        f"- goal: {order.goal}\n"
        f"- scope: {order.scope or '(없음)'}\n"
        f"- deliverable: {order.deliverable or '(없음)'}\n"
        f"- local_checks: {checks}\n\n"
        f"# spec (전체 목표)\n"
        f"- goal: {spec.goal}\n"
        f"- done_when: {spec.done_when}\n\n"
        f"# progress (현재 plan 상태)\n{_plan_progress(state)}\n\n"
        f"# 직전 진행(last_result)\n{last_result or '(아직 없음)'}"
    )
    # WO#148: 과대-다행동 유닛이면 입도 신호를 *참고*로 주입(판정은 너의 몫 — 오버블록 회피).
    sig = granularity_signal(order)
    if sig:
        base += (
            "\n\n# 입도 신호 (자동 탐지 — 입도/책임 수 축, WO#148)\n"
            f"{sig}\n"
            "(이 신호는 *판정이 아니라 참고*다. 행동들이 실제로 독립이고 한 유닛이 과대하면 "
            "weak로 단일-책임 disjoint-scope 분할을 권고하고, 한 책임의 하위측면일 뿐이면 무시하라.)"
        )
    return base


def critique_decomposition(
    order: NextOrder,
    spec: ProjectSpec,
    state: State,
    client: LLMClient,
    *,
    last_result: str | None = None,
    prompt_path: str | Path = DEFAULT_DECOMP_CRITIC_PROMPT_PATH,
) -> DecompCritique:
    """replan이 낸 work order(분해)를 적대적으로 판정해 DecompCritique를 반환한다.

    완전 best-effort: critic은 *보조* 단계라 그 *어떤* 실패도 본 run을 막아선 안 된다.
    critic 작업 전체(client.complete + 파싱/검증)를 감싸 **절대 raise하지 않는다** —
    깨진 출력·클라이언트 예외·기타 예외 모두 verdict="progress"(평가 불가, 진행 막지
    않음)로 흡수하되 사유를 reason에 남긴다.

    독립성(적대적 분리): client는 호출부가 주입하는 *독립 critic-model* 클라이언트다.
    빌더 전용 스킬(WO#32)이 주입되지 않은 *원본* work order를 받는다(호출부가 보장).
    """
    system = Path(prompt_path).read_text(encoding="utf-8")
    user = _build_user(order, spec, state, last_result)

    try:
        raw = client.complete(system, user)
        crit = parse_yaml_model(
            raw, DecompCritique, DecompCriticError, normalize=_normalize_critique_dict
        )
    except ParseError as e:
        return DecompCritique(
            verdict="progress",
            unit=order.unit,
            reason=f"평가 불가: 분해 critic 출력 파싱/검증 실패 ({e.message})",
        )
    except Exception as e:  # noqa: BLE001 — critic은 advisory: 어떤 실패도 run을 죽이면 안 된다
        return DecompCritique(
            verdict="progress",
            unit=order.unit,
            reason=f"분해 critic 실행 실패: {e} (평가 불가)",
        )

    crit.verdict = _norm_verdict(crit.verdict)
    if crit.unit is None:
        crit.unit = order.unit
    return crit


def build_decomp_feedback(crit: DecompCritique) -> str:
    """weak 판정을 재replan 피드백 텍스트로 직렬화(brain이 더 작은 진전 스텝을 내게)."""
    reason = crit.reason or "직전 분해가 무진전/전체 goal 재진술로 판정됨."
    return (
        "적대적 분해 critic이 직전 work order를 *무진전(weak)*으로 판정했다:\n"
        f"- 사유: {reason}\n"
        "→ 전체 goal을 재진술하지 말고, 남은 거리를 *의미있게 줄이는 더 작은 한 조각*으로 "
        "분해하라. 이번 유닛이 완료되면 무엇이 명확히 진전되는지 goal에 드러나게 하라. "
        "직전에 실패/거부된 접근을 그대로 반복하지 마라. "
        "유닛에 독립 행동이 여럿이면(이동/충돌/먹이/game-over 등) 각각을 *distinct 모듈 파일을 "
        "소유하는 단일-책임 disjoint-scope 유닛*으로 쪼개고(파일 겹침 0 → 통합 벽 악화 방지), "
        "별도 통합/조립 유닛이 그 모듈들을 wire하게 하라."
    )
