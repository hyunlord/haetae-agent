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
# WO#155: 트레이스-하니스 KIND 마커(전체 행동 사슬 구동 + evidence emit). 통합 유닛이 조립에
# *더해* 이 KIND까지 한 유닛에 묶으면(엔진-파사드 + 트레이스-재구성) = 서로 다른 *종류*라 in-place
# 축소로 부족(#153) → 구조적 재분해(전용 트레이스-하니스 유닛 분리) 대상. 순수 조립 유닛(이 마커
# 없음)은 면제 유지. distinct 트레이스/하니스/evidence 어휘만 — 모듈명 우연매칭 회피.
_TRACE_HARNESS_MARKERS = (
    "트레이스", "하니스", "헤드리스", "trace", "harness", "headless",
    "evidence", "증거", "행동 구동", "사슬 구동", "구동해", "playthrough", "플레이스루",
)
_OVER_LARGE_THRESHOLD = 4  # ≥4개 독립 행동 클로즈를 한 유닛에 묶으면 과대(약한 빌더 기준).

# WO#152: distinct-KIND 책임 분류 — *서로 다른 종류*의 검증가능 로직(판정 vs 상태전이 vs
# 렌더 vs 입력)을 한 유닛에 묶으면 행동 수가 임계(≥4) 미만이라도 분할(예: #151 u3 =
# 충돌 *판정*(detection) + game-over *상태고정*(state) = 2 종류 → escalate). 같은-종류
# 하위측면(벽충돌+자기충돌 = 둘 다 detection)은 한 KIND으로 묶여 과분할 안 함(가드 유지).
_RESPONSIBILITY_KINDS: dict[str, tuple[str, ...]] = {
    "판정(detection)": (
        "충돌", "판정", "감지", "검출", "겹침", "경계 검사", "경계검사",
        "collision", "collide", "detect", "overlap", "hit-test", "hittest", "boundary check",
    ),
    "상태전이(state)": (
        "게임오버", "game over", "gameover", "game-over", "상태 고정", "상태고정",
        "상태 전이", "상태전이", "상태 관리", "상태관리", "생명주기", "재시작", "일시정지",
        "freeze", "transition", "state machine", "lifecycle", "restart", "pause", "reset",
    ),
    "렌더(render)": (
        "렌더", "그리기", "그린다", "canvas", "캔버스", "화면 표시", "화면에 표시",
        "render", "draw", "display", "paint", "repaint",
    ),
    "입력(input)": (
        "키보드 입력", "키 입력", "입력 처리", "키 이벤트", "화살표 키", "조작 처리",
        "keyboard", "keypress", "keydown", "arrow key", "input handl", "event handler",
    ),
}


def _behavior_clauses(text: str) -> list[str]:
    """텍스트를 리스트 구분자로 쪼개 *비자명* 행동 클로즈만 반환(길이<2·순수 filler 제외)."""
    parts = [p.strip(" \t·*-—:") for p in _BEHAVIOR_SEP.split(text or "")]
    return [p for p in parts if len(p) >= 2 and p not in ("및", "등", "그리고", "and")]


def _distinct_kinds(text: str) -> list[str]:
    """blob이 건드리는 *서로 다른 종류*의 책임 라벨 목록(WO#152). 같은 종류 하위측면은
    한 라벨로 접힌다(벽충돌+자기충돌 = '판정' 1개) → 과분할 가드. 키워드는 구체적이라
    우연 매칭이 적다. distinct 종류가 ≥2면 단일-책임 위반(판정 vs 상태전이 등) 신호."""
    low = (text or "").lower()
    found: list[str] = []
    for label, kws in _RESPONSIBILITY_KINDS.items():
        if any(kw.lower() in low for kw in kws):
            found.append(label)
    return found


def granularity_signal(order: NextOrder) -> str | None:
    """과대-다행동 유닛이면 *분할 권고 신호*(한 줄)를, 아니면 None을 반환(결정적·director-side).

    WO#148: 한 유닛 goal/scope/deliverable에 독립 행동이 임계(≥4) 이상 묶이면 약한 로컬
    빌더가 단일 유닛으로 신뢰성있게 수렴하기 과대 → 단일-책임 disjoint-scope 유닛들로 쪼갠다.
    통합/조립 유닛(모듈 wire)은 면제(distinct 조립 파일 소유). **판정 아님** — critic(codex)에
    주입되는 *신호*다(LLM이 최종 판정·오버블록 회피). gate/run-judge와 무관(director-side).
    """
    blob = " ".join(p for p in (order.goal, order.scope, order.deliverable) if p)
    low = blob.lower()
    if any(m in low for m in _INTEGRATION_MARKERS):
        # WO#155: 순수 조립(모듈 wire = 1 KIND)은 면제(#51/#123). 그러나 통합 유닛이 조립에 *더해*
        # 트레이스-하니스 KIND(전체 행동 사슬 구동 + evidence emit)까지 묶으면 = 서로 다른 *종류*의
        # 책임(조립 vs 검증-하니스)이라 in-place 축소로 부족 → *구조적 재분해*(유닛 추가) 권고.
        if any(m in low for m in _TRACE_HARNESS_MARKERS):
            return (
                "이 통합 유닛이 모듈 조립(wire)에 *더해* 트레이스-하니스(전체 행동 사슬 구동 + "
                "evidence emit)까지 한 유닛에 묶음 — 조립 vs 검증-하니스는 서로 다른 *종류*의 책임이라 "
                "in-place 축소로는 부족하다(#153: 엔진-파사드 + 브라우저-어댑터 + 트레이스-재구성 3종, "
                "OR-alt 축소 불충분). *구조적 재분해* 권고 — in-place 축소가 아니라 *유닛을 추가*해 "
                "(1) wire/파사드 유닛(모듈 조립)과 (2) *전용 트레이스-하니스 유닛*(wire에 deps, 풀-행동 "
                "사슬을 구동해 evidence emit)으로 분리하라. 트레이스가 자체 유닛이라 빌더가 집중하고 "
                "gate가 별도 검증한다. (단 *순수 조립*만 하는 통합 유닛은 면제 — 트레이스-하니스를 "
                "겸할 때만 분리.)"
            )
        return None  # 순수 조립/wire 유닛 — disjoint-scope 소유, 과대 아님(트레이스-하니스 미겸)
    # WO#157: *순수* 검증 트레이스-하니스 유닛(트레이스-하니스 마커 O·통합 마커 X)은 본질적으로
    # end-to-end다 — 한 플레이스루가 *통합 게임 전체*(이동·먹이·성장·점수·충돌·game-over…)를
    # 입증하는 게 목적이라 여러 행동/종류를 담는 게 *정상*(과대 아님). 행동별로 쪼개면 #113 풀-사슬
    # 바가 도로 확장돼 붕괴한다(#156 u8: 이동만으로 좁힘 → 풀-사슬 재확장 → 약빌더 미수렴·escalate).
    # 근본: *빌드 분해 ≠ 검증 분해* — 검증 트레이스는 한 end-to-end 유닛으로 유지하고(#148 행동수·
    # #152 종류수 split 면제), 대신 #27 스캐폴드(scaffold.trace_harness_skeleton)로 tractable화한다.
    # 빌드 모듈(트레이스 마커 없음)은 아래 #148/#152 split 그대로(무회귀). **바 완화 아님** —
    # run-judge의 #113 풀-사슬 판정은 불변(부분 트레이스는 여전히 fail).
    if any(m in low for m in _TRACE_HARNESS_MARKERS):
        return None
    clauses = _behavior_clauses(blob)
    kinds = _distinct_kinds(blob)  # WO#152: 서로 다른 *종류*의 책임(판정/상태전이/렌더/입력)
    over_count = len(clauses) >= _OVER_LARGE_THRESHOLD     # 행동 수 과대(#148)
    distinct_kind = len(kinds) >= 2                        # distinct 종류 묶음(#152 — #151 u3)
    if not (over_count or distinct_kind):
        return None
    if distinct_kind:
        # #152: 행동 수가 임계 미만이어도 서로 다른 종류의 책임이면 분할(같은-종류는 1라벨로 접힘).
        why = (
            f"이 유닛이 서로 다른 *종류*의 책임 {len(kinds)}개({', '.join(kinds)})를 묶음 — "
            "판정·상태전이·렌더·입력처럼 독립적으로 검증되는 distinct 책임은 별도 유닛이어야 "
            "한다(#151 u3: 충돌 판정 + game-over 상태고정이 한 유닛이라 미수렴·escalate)"
        )
    else:
        head = ", ".join(clauses[:6])
        why = f"이 유닛이 독립 행동을 약 {len(clauses)}개({head}…) 한 덩어리로 묶음"
    return (
        f"{why}. 약한 로컬 빌더가 단일 유닛으로 신뢰성있게 수렴하기엔 과대 — 각 (종류별) "
        "책임을 *distinct 모듈 파일을 소유하는 단일-책임 disjoint-scope 유닛*으로 쪼개고"
        "(파일 겹침 0), 별도 통합/조립 유닛이 wire하도록 분할 권고. (단 같은 종류 하위측면은 "
        "묶음 유지 — 과분할 금지.)"
    )


# ──────────────────── 파일-소유권 disjoint 축 (WO#165, replan-time) ────────────────────
#
# 분해된 *병렬 형제* 유닛이 *같은 파일을 소유*(scope 겹침)하면 동시 빌드 worktree 머지 충돌 →
# 직렬화(#21) → 통합 escalate. intake(#59)는 *synthesis-time* 겹침을 1회 bounded 재합성으로 잡지만
# decomp-critic엔 의도적으로 안 넣었다(progress-only). 이 축은 그 *replan-time* 갭을 메운다 —
# replan이 유닛을 정련/도입하며 만든 형제 scope 겹침을 결정적으로 탐지해 critic 프롬프트에 *신호*로
# 주입한다(codex가 최종 판정 — #148/#152 입도/KIND 축과 동형, 오버블록 회피). **판정 아님·gate 무관**
# (director-side 분해 입력). intake/scheduler를 import하지 않는다(둘 다 무변경·디커플) — 같은 보수
# 기준(병렬 형제·양쪽 scope 선언·정확-문자열 겹침; intake #59 scope-겹침 탐지와 동형)을 로컬 재구현한다.


def _scope_transitive_deps(units: list) -> dict[str, set[str]]:
    """유닛별 *전이적* 의존 집합(사이클 visited 가드). intake._transitive_deps와 동형(로컬 재구현)."""
    direct = {u.unit: set(u.deps or []) for u in units}
    out: dict[str, set[str]] = {}
    for unit in direct:
        seen: set[str] = set()
        stack = list(direct.get(unit, ()))
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            stack.extend(direct.get(d, ()))
        out[unit] = seen
    return out


def scope_overlap_signal(order: NextOrder, spec: ProjectSpec) -> str | None:
    """order 유닛의 소유 scope가 *병렬 형제* 유닛과 겹치면 disjoint-위반 신호(한 줄), 아니면 None.

    WO#165 replan-time 파일-소유권 축. 보수적(intake #59 scope-겹침 탐지와 동형):
      - order 유닛이 spec.decomposition에 있고 scope 선언 + 상대도 선언 + *정확-문자열 겹침*일 때만.
      - 직렬(전이 dep로 엮임)·미선언·order 유닛 미상·겹침 없음 → None(no-op — 과개입 0·기존 동작).
    **판정 아님** — critic(codex)에 주입되는 *참고 신호*다(최종 판정은 codex). gate/run-judge 무관·
    director-side. 같은 입력(scope) 다른 시점: synthesis-time 겹침은 intake #59가 담당, 여긴 replan-time만.
    """
    units = spec.decomposition
    own = next((list(u.scope or []) for u in units if u.unit == order.unit), [])
    if not own:
        return None  # order 유닛 미상 / scope 미선언 → 보수적 no-op
    tdeps = _scope_transitive_deps(units)
    own_set = set(own)
    collisions: list[tuple[str, list[str]]] = []
    for u in units:
        if u.unit == order.unit:
            continue
        other = set(u.scope or [])
        if not other:
            continue  # 상대 미선언 → 스킵(보수적)
        # 병렬 형제만(직렬 dep로 엮이면 순차 머지 → 충돌 위험 작음, #59와 동형)
        if order.unit in tdeps.get(u.unit, set()) or u.unit in tdeps.get(order.unit, set()):
            continue
        shared = own_set & other
        if shared:
            collisions.append((u.unit, sorted(shared)))
    if not collisions:
        return None
    collisions.sort(key=lambda x: x[0])
    pairs = "; ".join(f"[{order.unit}]↔[{u}] 공유: {', '.join(s)}" for u, s in collisions)
    return (
        f"이 유닛이 병렬 형제와 *같은 파일을 소유*(scope 겹침)한다 — {pairs}. 형제 간 owned-scope "
        "교집합은 ∅이어야 한다(disjoint 불변식) — 동시 빌드 시 같은 파일을 건드려 worktree 머지 "
        "충돌→직렬화(#21)→통합 벽. 공유 파일을 *별도 유닛으로 추출*(한 유닛이 소유·나머지는 deps)하거나 "
        "경계를 재조정해 각 파일을 *한 유닛만* 소유하게 하고, 닿아야 하면 *파일 공유가 아니라* facade "
        "계약(#160 export/import)으로 결합하도록 재분해를 권고하라."
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
    # WO#165: replan-time 파일-소유권 겹침 신호(형제 owned-scope 교집합 ≠ ∅) — *참고*(판정은 codex).
    osig = scope_overlap_signal(order, spec)
    if osig:
        base += (
            "\n\n# 소유권 신호 (파일-소유권 겹침 — replan-time 자동 탐지, WO#165)\n"
            f"{osig}\n"
            "(이 신호는 *판정이 아니라 참고*다. 형제 유닛이 실제로 같은 파일을 소유해 머지 충돌 "
            "리스크가 있으면 weak로 disjoint 재분해(공유 파일 추출/경계 재조정·facade 계약 결합)를 "
            "권고하고, 실은 disjoint거나 직렬(dep)로 엮였으면 무시하라.)"
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
        "별도 통합/조립 유닛이 그 모듈들을 wire하게 하라. "
        "형제 유닛이 *같은 파일을 소유*(scope 겹침)하면(#165) 그 공유 파일을 *별도 유닛으로 추출*하거나 "
        "경계를 재조정해 각 파일을 *한 유닛만* 소유하게 하고(owned-scope 교집합 = ∅), 닿아야 하면 "
        "*파일 공유가 아니라* facade 계약(#160 export/import)으로 결합하라. "
        "통합 유닛이 조립에 *더해* 트레이스-하니스(전체 행동 사슬 구동 + evidence emit)까지 겸하면, "
        "in-place로 줄이지 말고 wire/파사드 유닛과 *전용 트레이스-하니스 유닛*(wire에 deps)으로 "
        "*유닛을 추가*해 구조적으로 분리하라(#155)."
    )
