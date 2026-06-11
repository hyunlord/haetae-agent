"""intake 러너 — 주문(order) → ProjectSpec.

director의 첫 능력: synthesizer.md를 시스템 프롬프트로 LLM에 태우고,
응답(YAML/JSON)을 ProjectSpec으로 검증해 돌려준다.

model_validate 직전에 _normalize_spec_dict로 흔한 출력 변종을 흡수한다(보조 안전망).
정규화는 만능이 아니다 — 못 잡는 변종은 그대로 SynthesisError(raw 보존).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from haetae.llm import LLMClient
from haetae.models import ProjectSpec, State
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_PROMPT_PATH = "prompts/synthesizer.md"

# 합성 출력 YAML 파싱이 실패하면 에러를 모델에 되먹여 *몇 번까지* 다시 시킬지.
# 기본 2 → 첫 시도 + 재시도 2 = 총 3시도. (LEAP식 컴파일러-피드백을 합성기에 적용:
# transient malformed YAML — 콜론 포함 문자열 미인용 등 — 을 폐기 대신 회복.)
DEFAULT_SYNTH_RETRIES = 2


class SynthesisError(ParseError):
    """합성 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


def _to_str(item: Any) -> str:
    """문자열 항목으로 정규화. 객체면 desc/text/name/value 순으로 추출."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("desc", "text", "name", "value"):
            if isinstance(item.get(key), str):
                return item[key]
    return str(item)


def _normalize_spec_dict(data: dict) -> dict:
    """synthesizer 출력의 흔한 변종을 ProjectSpec 스키마 모양으로 보정한다.

    흡수 대상(WO#10에서 관측):
      - constraints / non_goals 항목이 객체({id, desc})면 → 문자열로
      - decomposition[] 항목이 unit 없이 id만 있으면 → id를 unit으로
      - acceptance_criteria[].check.command → cmd
    """
    if not isinstance(data, dict):
        return data
    d = dict(data)

    # constraints / non_goals: 객체 리스트 → 문자열 리스트
    for key in ("constraints", "non_goals"):
        v = d.get(key)
        if isinstance(v, list):
            d[key] = [_to_str(item) for item in v]

    # decomposition[].id → unit (unit 없고 id만 있을 때)
    dec = d.get("decomposition")
    if isinstance(dec, list):
        new_dec = []
        for item in dec:
            if isinstance(item, dict) and "unit" not in item and "id" in item:
                item = {**item, "unit": item["id"]}
            new_dec.append(item)
        d["decomposition"] = new_dec

    # acceptance_criteria[].check.command → cmd
    acs = d.get("acceptance_criteria")
    if isinstance(acs, list):
        new_acs = []
        for ac in acs:
            if isinstance(ac, dict) and isinstance(ac.get("check"), dict):
                chk = dict(ac["check"])
                if "cmd" not in chk and "command" in chk:
                    chk["cmd"] = chk.pop("command")
                ac = {**ac, "check": chk}
            new_acs.append(ac)
        d["acceptance_criteria"] = new_acs

    return d


def _yaml_repair_feedback(err: SynthesisError) -> str:
    """직전 출력의 YAML 파싱 에러를 모델에 되먹일 교정 지시문을 만든다.

    파서 오류(라인/컬럼 포함)를 그대로 넣어 무엇이 깨졌는지 보이고, 가장 흔한
    원인(콜론·특수문자를 포함한 문자열 값을 미인용)을 콕 집어 따옴표 인용을 요구한다.
    """
    return (
        "\n\n# ⚠️ 직전 출력이 YAML 파싱에 실패했다 — 같은 spec을 *유효한 YAML로* 다시 내라\n"
        f"파서 오류: {err.__cause__}\n"
        "가장 흔한 원인: 콜론(:)·특수문자를 포함한 문자열 값(특히 order_raw/goal/desc처럼 "
        "주문 원문을 그대로 옮긴 필드)을 따옴표로 감싸지 않음 — unquoted 콜론은 YAML이 "
        "mapping으로 오해한다. **모든 문자열 값을 큰따옴표로 감싸** 유효한 YAML로 "
        "다시(그리고 그것만) 출력하라. 키·구조·내용은 그대로 두고 인용만 고쳐라."
    )


def synthesize(
    order: str,
    client: LLMClient,
    context: str | None = None,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    *,
    feedback: str | None = None,
    synth_retries: int = DEFAULT_SYNTH_RETRIES,
) -> ProjectSpec:
    """주문을 합성기에 태워 검증된 ProjectSpec을 반환한다.

    feedback: 직전 합성된 spec에 대한 적대적 비평. 주어지면 user 메시지에 얹어
              모델이 *기준을 강화한* spec을 다시 내도록 유도한다(critic 재합성 경로).

    synth_retries: 출력 YAML이 *파싱 실패*할 때 에러를 되먹여 다시 시킬 횟수
                   (기본 2 → 총 3시도). 파싱 실패에 한정 — 비-매핑/스키마 검증 실패는
                   재시도하지 않고 즉시 SynthesisError(기존 동작 그대로).

    실패 시(YAML 파싱 불가 / 스키마 검증 불통과) raw 응답을 담은 SynthesisError.
    """
    system = Path(prompt_path).read_text(encoding="utf-8")

    base_user = f"# 주문(order)\n{order}"
    if context:
        base_user += f"\n\n# 프로젝트 컨텍스트(project_context)\n{context}"
    if feedback:
        base_user += (
            "\n\n# ⚠️ 직전 합성된 spec이 적대적 비평에서 '물렁하다'고 지적됨 — "
            "아래 지적을 반영해 acceptance_criteria/done_when을 *더 엄격하게* 강화한 "
            "ProjectSpec을 다시(그리고 그것만) 출력하라\n"
            f"{feedback}"
        )

    repair_note = ""
    last_err: SynthesisError | None = None
    for _ in range(synth_retries + 1):
        # client.complete 예외(CodexError 등)는 의도적으로 잡지 않는다 — 기존처럼 전파.
        # 재시도는 *파싱 실패*에만 적용한다(아래 except).
        raw = client.complete(system, base_user + repair_note)
        try:
            return parse_yaml_model(
                raw, ProjectSpec, SynthesisError, normalize=_normalize_spec_dict
            )
        except SynthesisError as e:
            last_err = e
            # YAML 파싱 자체의 실패만 재시도 대상(원인이 yaml.YAMLError일 때).
            # 비-매핑/스키마 검증 실패는 '깨진 YAML'이 아니므로 즉시 전파(기존 동작).
            if not isinstance(e.__cause__, yaml.YAMLError):
                raise
            repair_note = _yaml_repair_feedback(e)

    # 재시도 소진 — 마지막 파싱 실패를 그대로 전파(첫 합성→escalate 경로,
    # 재합성→호출부 synthesize_with_critique가 원본 fallback).
    assert last_err is not None
    raise last_err


# ──────────────────── WO#51: 통합 유닛 deps 추론 넛지 ────────────────────
#
# 머지 충돌 근본 차단: 통합 성격 유닛(대시보드·진입점·e2e·트레이스 — 다른 유닛 산출물을
# import/wire)이 *자기가 엮는 유닛들에 의존(deps)* 하면 DAG가 자연히 그 유닛을 맨 뒤,
# 의존 머지 후 빌드한다 → 같은 파일 동시 수정으로 인한 충돌이 애초에 안 난다.
# 스케줄러는 deps를 이미 존중하므로 *deps만 정확*해지면 별도 스케줄러 변경 없이 직렬화된다.
#
# 설계:
#   - 휴리스틱은 **보수적** — desc 키워드로 통합 유닛을 탐지하고, 그 유닛이 비통합 빌더
#     유닛 다수(≥2)를 deps에서 빠뜨릴 때만 넛지(애매하면 안 함 → 과직렬화·오탐 회피).
#   - 넛지는 **새 LLM critic이 아니라** 기존 #31 재합성-피드백 채널 재사용(synthesize feedback).
#   - **bounded**: 정확히 1회 재합성. 실패/여전히 과소면 *진행*(데드락 금지).
#   - **criteria/done_when 불변**: 재합성에서 *deps만* 채택(splice). criteria·done_when·goal 등
#     protected 필드는 원본 유지 — governance(plan은 mutable, 기준은 protected) 그대로.

# 통합(다른 유닛 산출물을 엮는) 성격 유닛을 알리는 보수적 키워드 집합. 영어는 소문자 매칭,
# 한국어는 부분일치. 애매한 단어는 넣지 않는다(오탐→과직렬화). 탐지만 보수적이면,
# 실제 어느 유닛을 엮는지는 재합성 LLM이 판단한다(우린 deps만 채택).
_INTEGRATION_KEYWORDS = (
    "dashboard", "대시보드",
    "integration", "integrate", "통합",
    "entrypoint", "entry point", "entry-point", "진입점",
    "e2e", "end-to-end", "end to end",
    "sim:trace", "trace 진입점", "헤드리스 트레이스", "트레이스 진입점",
    "wire", "wiring", "연결", "엮",
    "실제 엔진", "import",
)


def _is_integration_unit(desc: str | None) -> bool:
    """desc 키워드로 통합 성격 유닛인지 보수적으로 판정(영어 소문자·한국어 부분일치)."""
    low = (desc or "").lower()
    return any(k in low for k in _INTEGRATION_KEYWORDS)


def _integration_dep_gaps(spec: ProjectSpec) -> dict[str, list[str]]:
    """통합 유닛별로 *빠뜨린 비통합 빌더 유닛* 목록을 반환(과소 의존 탐지, 보수적).

    빌더(비통합) 유닛이 2개 미만이면 엮을 게 없어 {}. 통합 유닛이 없어도 {}. 통합 유닛이
    비통합 빌더를 **2개 이상** 빠뜨릴 때만 gap으로 잡는다(단일 누락은 의도일 수 있음 →
    오탐 회피). 비통합 유닛끼리는 절대 의존을 만들지 않는다(병렬성 보존 = 과직렬화 금지).
    """
    units = spec.decomposition
    if len(units) < 2:
        return {}
    integ = {u.unit for u in units if _is_integration_unit(u.desc)}
    if not integ:
        return {}
    gaps: dict[str, list[str]] = {}
    for u in units:
        if u.unit not in integ:
            continue  # 비통합 빌더엔 넛지 안 함(과직렬화 금지)
        deps = set(u.deps or [])
        # 후보 = 이 통합 유닛이 엮을 법한 *비통합 빌더* 유닛(자기 자신·다른 통합 유닛 제외).
        builders = [o.unit for o in units if o.unit != u.unit and o.unit not in integ]
        missing = [b for b in builders if b not in deps]
        if len(missing) >= 2:  # 보수적 임계 — 다수를 빠뜨릴 때만
            gaps[u.unit] = missing
    return gaps


def integration_dep_feedback(spec: ProjectSpec) -> str | None:
    """과소 의존 통합 유닛이 있으면 #31 재합성 채널에 줄 넛지 피드백 텍스트, 없으면 None."""
    gaps = _integration_dep_gaps(spec)
    if not gaps:
        return None
    lines = [
        "통합 유닛의 의존(deps)이 과소 지정됨 — 통합 유닛은 *자기가 엮는 유닛들에 의존*하게 하라.",
        "통합 성격 유닛(대시보드·진입점·e2e·트레이스 등)은 다른 유닛의 산출물을 import/연결한다.",
        "그 유닛이 엮는 빌더 유닛들을 deps에 추가해 *마지막에, 의존이 머지된 뒤* 빌드되게 하라 "
        "— 그래야 같은 파일을 동시에 건드려 머지 충돌나지 않는다.",
        "단 *자기가 실제로 엮는 유닛*에만 의존을 더하라(전부 직렬화 금지 — 비통합 유닛 병렬성 보존).",
        "criteria/done_when/goal은 그대로 두고 decomposition의 deps만 고쳐라.",
    ]
    for unit, missing in gaps.items():
        lines.append(
            f"- 통합 유닛 [{unit}]: 현재 deps가 빌더 유닛 {missing}을(를) 빠뜨림 — 엮는 것들을 deps에 추가."
        )
    return "\n".join(lines)


def _adopt_deps_only(original: ProjectSpec, restructured: ProjectSpec) -> ProjectSpec:
    """재합성 결과에서 **deps만** 채택 — protected 필드(criteria/done_when/goal 등)는 원본 유지.

    governance: plan(분해/deps)은 mutable, 기준은 protected. 통합 deps 교정이 criteria를
    바꾸지 못하게 못박는다. unit 집합이 다르면(재합성이 분해 구조를 갈아엎음) deps만 떼올 수
    없으므로 보수적으로 원본 그대로(데드락/구조 변형 금지).
    """
    orig_units = {u.unit for u in original.decomposition}
    new_deps = {u.unit: list(u.deps or []) for u in restructured.decomposition}
    if orig_units != set(new_deps):
        return original  # 분해 구조가 바뀜 → deps-only splice 불가, 보수적으로 원본
    merged = original.model_copy(deep=True)
    for u in merged.decomposition:
        u.deps = new_deps.get(u.unit, u.deps)
    return merged


def nudge_integration_deps(
    order: str,
    spec: ProjectSpec,
    client: LLMClient,
    *,
    context: str | None = None,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    synth_retries: int = DEFAULT_SYNTH_RETRIES,
) -> ProjectSpec:
    """통합 유닛 deps가 과소면 #31 채널로 *바운드 1회* 재합성해 deps만 교정한 spec 반환.

    deps가 충분하면 no-op으로 원본 그대로(기존 동작·비용 불변). 넛지가 필요해도:
      - 정확히 1회 재합성(bounded, 무한·데드락 금지).
      - 재합성 실패(SynthesisError/클라이언트 예외)는 흡수 → 원본 진행(advisory).
      - 성공해도 *deps만* 채택(criteria/done_when 불변 가드).
    """
    feedback = integration_dep_feedback(spec)
    if feedback is None:
        return spec  # 통합 의존 충분 — no-op(추가 LLM 호출 없음)
    try:
        restructured = synthesize(
            order, client, context=context, prompt_path=prompt_path,
            feedback=feedback, synth_retries=synth_retries,
        )
    except Exception:  # noqa: BLE001 — 넛지는 advisory: 어떤 실패도 run을 막지 않는다(원본 진행)
        return spec
    return _adopt_deps_only(spec, restructured)


# ──────────────────── WO#58: 이어가기(②a) 증분 합성 context 빌더 ────────────────────
#
# 완료된 부모 run 위에서 *이어서* 새 run을 돌릴 때, 부모의 코드는 이미 workdir에
# 시딩돼 있다. 합성기에게 "처음부터 다시 짓지 말고 delta만 계획하라"를 알리는 context를
# 만든다. **이 context는 합성기/빌더에만 간다 — judge·run-judge·critic엔 절대 안 간다**
# (적대적 분리: 부모 done을 맹신 않음, 최종 통합 gate가 합쳐진 결과를 그대로 판정).
#
# **anti-erosion**: 문구는 "확장하라"지 "완화하라"가 아니다. 부모 done_when/criteria를
# 약화하지 말라고 *명시*한다. 새 spec은 기존 spec critic(#20)이 그대로 검증한다.


def build_continuation_context(
    parent_spec: ProjectSpec | None,
    parent_state: State | None = None,
    *,
    parent_order: str | None = None,
) -> str:
    """부모 spec/완료상태 → 증분 합성 context 문자열(합성기 전용, judge 무주입).

    parent_spec이 있으면 goal/constraints/done_when/decomposition 요약을 싣고, 없으면
    (사이드카 없는 옛 부모 run) parent_order로 degrade한다. parent_state가 있으면 유닛별
    완료상태와 종료 status를 덧붙인다. anti-erosion: 기준 *확장*만 허용을 명시한다.
    """
    lines: list[str] = [
        "# 프로젝트 이어가기 (증분 — greenfield 아님)",
        "이 프로젝트는 이미 작업 디렉터리에 존재한다(이전 run의 최종 결과가 시딩됨).",
        "아래 새 주문이 요구하는 *delta(추가/변경)*만 계획·빌드하라 — 이미 만들어진 것을 "
        "처음부터 다시 짓지 마라. 기존 파일을 헐지 말고 그 위에 기능을 더하라.",
        "",
        "## 이전(부모) 프로젝트 요약",
    ]
    if parent_spec is not None:
        lines.append(f"- goal: {parent_spec.goal}")
        lines.append(f"- done_when: {parent_spec.done_when}")
        if parent_spec.constraints:
            lines.append(f"- constraints: {'; '.join(parent_spec.constraints)}")
        if parent_spec.non_goals:
            lines.append(f"- non_goals: {'; '.join(parent_spec.non_goals)}")
        if parent_spec.decomposition:
            lines.append("- 이미 구현(또는 계획)된 유닛:")
            for u in parent_spec.decomposition:
                status = ""
                if parent_state is not None:
                    pi = next((p for p in parent_state.plan if p.unit == u.unit), None)
                    if pi is not None:
                        status = f" [{pi.state.value}]"
                desc = (u.desc or "").strip()
                lines.append(f"  - {u.unit}{(': ' + desc) if desc else ''}{status}")
    elif parent_order:
        # 사이드카(spec.yaml) 없는 옛 부모 — 원 주문으로 degrade(여전히 증분 신호 제공).
        lines.append(f"- 이전 주문(원문): {parent_order}")
    else:
        lines.append("- (부모 spec 요약 없음 — 시딩된 코드가 현재 상태의 단일 근거다)")

    if parent_state is not None:
        lines.append(f"- 이전 run 종료 상태: {parent_state.status.value}")

    lines += [
        "",
        "## 증분 규칙 (중요 · 위반 금지)",
        "- 새 acceptance_criteria/done_when은 부모 기준을 *확장*하라 — **절대 약화/완화하지 마라**.",
        "  (부모가 충족한 기준은 새 run에서도 계속 충족돼야 한다. 통합 gate가 합쳐진 결과를 판정한다.)",
        "- 새 주문의 기능을 *추가*하는 방향으로만 분해하라(기존 동작 회귀 금지).",
        "",
        "## 검증된 유닛 재사용 (reuse_of — 선택, 토큰 절약)",
        "- 이번 delta로 **바뀌지 않는** 부모 유닛(같은 acceptance_criteria·scope로 이미 검증됨)을",
        "  새 분해에 그대로 다시 둘 때는 그 유닛에 `reuse_of: <부모 unit-id>`를 달아라.",
        "  → 루프가 부모와 기준 동등성을 *대조*해 맞으면 재빌드를 생략한다(코드는 이미 시딩됨).",
        "- **바를 바꿨거나(기준/scope 변경) 새로 만드는 유닛에는 절대 `reuse_of`를 달지 마라** —",
        "  그러면 다른 바이므로 정상 빌드+gate가 돼야 한다(재사용은 검증 우회가 아니다).",
        "  부모 유닛 id가 헷갈리면 생략하라(생략 시 정상 빌드). 라벨이 틀려도 루프 가드가 막는다.",
        "",
        "# 새 주문(delta) — 아래가 이번에 추가/변경할 것이다",
    ]
    return "\n".join(lines)


def unit_bar_signature(spec: ProjectSpec, unit_id: str) -> dict:
    """유닛의 '바' 지문 — 그 유닛에 태그된 acceptance_criteria + scope (WO#71 재사용 대조용).

    재사용(continue-from)은 *바가 불변*일 때만 허용된다(anti-erosion). 부모/새 spec에서 같은
    함수로 지문을 뽑아 **직접 대조**해 동등성을 판정한다(합성 라벨만 신뢰하지 않음 = 라벨+가드 이중).
    criteria: 정렬된 (id, desc, check.type, check.cmd, check.pass) 튜플 리스트. scope: 정렬 경로.
    유닛이 없으면 빈 지문. 순수 함수(LLM/IO 없음) — 직렬화·동등비교 가능한 dict.
    """
    criteria: list[tuple] = []
    for ac in spec.acceptance_criteria:
        if ac.unit == unit_id:
            chk = ac.check
            ctype = chk.type.value if hasattr(chk.type, "value") else chk.type
            criteria.append((ac.id, ac.desc, ctype, chk.cmd, chk.pass_))
    criteria.sort()
    unit = next((u for u in spec.decomposition if u.unit == unit_id), None)
    scope = sorted(unit.scope) if unit and unit.scope else []
    return {"criteria": criteria, "scope": scope}


# ──────────────────── WO#59: disjoint-scope 분해 유도 (#51의 형제) ────────────────────
#
# "쪼개기"는 *유닛별로 다른 파일/모듈을 소유*할 때만 이득이다(병렬 worktree 머지가 깨끗).
# 형제 유닛이 같은 파일을 건드리면 통합 벽(머지 충돌)을 더 세게 친다. #51이 *통합 유닛*의
# 루트 충돌을 예방한다면, ③는 그 **형제 버전** — *병렬 형제 유닛*이 disjoint scope를 갖게 유도.
#
# 설계(#51 nudge_integration_deps와 동형):
#   - **선제적 합성 경로에만**: 프롬프트 유도 + 결정적 탐지 → bounded 1회 재합성. decomp
#     critic(#40)은 *무변경*(progress-only 유지 — scope-overlap을 weak 트리거로 넣지 않음).
#   - **anti-erosion(bar 불변)**: 재구성이 bar(goal/done_when/acceptance_criteria/constraints/
#     non_goals)를 하나라도 바꾸면 reject·원본 유지. scope/deps(=decomposition)만 채택.
#   - **advisory·bounded**: 정확히 1회. 실패/예외 흡수→원본. scope 미선언/겹침 없음 → no-op(호출 0).


def _transitive_deps(spec: ProjectSpec) -> dict[str, set[str]]:
    """유닛별 *전이적* 의존 집합. 사이클은 visited 가드로 방어(무한루프 금지)."""
    direct = {u.unit: set(u.deps or []) for u in spec.decomposition}
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


def _are_parallel(a: str, b: str, tdeps: dict[str, set[str]]) -> bool:
    """a·b가 *병렬 형제*인가 — 서로 전이 의존으로 안 엮였으면 True(동시 빌드 → 머지 동시).

    dep 경로로 엮였으면(직렬) 같은 파일을 건드려도 순차 머지라 충돌 위험 작음 → 형제 아님.
    """
    return b not in tdeps.get(a, set()) and a not in tdeps.get(b, set())


def _scope_overlaps(spec: ProjectSpec) -> list[tuple[str, str, list[str]]]:
    """병렬 형제 + 양쪽 scope 선언 + scope 겹침인 쌍을 결정적으로 탐지(보수적).

    반환: [(unitA, unitB, 겹친_경로들)] (unit-id 정렬). 겹침은 *정확 문자열 일치*(퍼지 매칭
    없음 → 오탐 회피, #51 보수성과 동형). 한쪽이라도 scope 미선언이면 그 쌍은 스킵(no-op).
    """
    units = spec.decomposition
    if len(units) < 2:
        return []
    tdeps = _transitive_deps(spec)
    scope_of = {u.unit: set(u.scope or []) for u in units}
    overlaps: list[tuple[str, str, list[str]]] = []
    ids = [u.unit for u in units]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            sa, sb = scope_of.get(a) or set(), scope_of.get(b) or set()
            if not sa or not sb:
                continue  # 한쪽만/미선언 → no-op(보수적)
            if not _are_parallel(a, b, tdeps):
                continue  # 직렬(dep로 엮임) → 순차 머지, 충돌 위험 작음 → 스킵
            shared = sa & sb
            if shared:
                lo, hi = sorted((a, b))
                overlaps.append((lo, hi, sorted(shared)))
    overlaps.sort(key=lambda x: (x[0], x[1]))
    return overlaps


def disjoint_scope_feedback(spec: ProjectSpec) -> str | None:
    """병렬 형제 scope가 겹치면 #31 재합성 채널에 줄 disjoint 유도 피드백, 없으면 None."""
    overlaps = _scope_overlaps(spec)
    if not overlaps:
        return None
    lines = [
        "병렬 형제 유닛(서로 dep로 안 엮인 유닛)이 *같은 파일/모듈 scope*를 공유한다 — "
        "동시 빌드 시 같은 파일을 건드려 worktree 머지 충돌을 일으킨다.",
        "각 파일/모듈은 *한 유닛만 소유*하게 분해를 재배치하라:",
        "  · 한 유닛이 그 파일을 소유하고 나머지는 그 산출물에 의존(deps)하거나,",
        "  · 엮기(여러 산출물 연결)는 *통합 유닛*으로 미뤄라(#51).",
        "겹친 부분(고칠 대상):",
    ]
    for a, b, shared in overlaps:
        lines.append(f"- [{a}] ↔ [{b}] 공유 scope: {', '.join(shared)}")
    lines.append(
        "**중요(불변)**: goal·done_when·acceptance_criteria·constraints·non_goals(성공 기준=bar)는 "
        "한 글자도 바꾸지 마라. **decomposition의 scope/deps만** disjoint하게 고쳐라(기준 변경 금지)."
    )
    return "\n".join(lines)


# bar(성공 기준) — 재구성이 이걸 하나라도 바꾸면 reject(anti-erosion). decomposition은 mutable.
_BAR_FIELDS = ("goal", "done_when", "acceptance_criteria", "constraints", "non_goals")


def _adopt_decomposition_only(
    original: ProjectSpec, restructured: ProjectSpec
) -> ProjectSpec:
    """재구성에서 *decomposition(units/deps/scope)만* 채택 — bar가 byte-동일일 때만(anti-erosion).

    bar(goal/done_when/acceptance_criteria/constraints/non_goals) 중 하나라도 바뀌면
    disjoint nudge가 criteria 변경으로 둔갑한 것 → **reject·원본 유지**. 또한 재구성된
    acceptance_criteria의 unit 태그가 새 unit 집합에서 dangling이면(bar 동일이어도) reject.
    """
    od = original.model_dump(by_alias=True, mode="json")
    rd = restructured.model_dump(by_alias=True, mode="json")
    if any(od.get(k) != rd.get(k) for k in _BAR_FIELDS):
        return original  # bar 변경 → reject(anti-erosion)
    # dangling 가드: ac.unit 태그가 새 decomposition에 없으면 reject(검증 불가 구조).
    new_units = {u.unit for u in restructured.decomposition}
    for ac in restructured.acceptance_criteria:
        tag = ac.unit
        if tag is not None and tag != "integration" and tag not in new_units:
            return original
    merged = original.model_copy(deep=True)
    merged.decomposition = [d.model_copy(deep=True) for d in restructured.decomposition]
    return merged


def nudge_disjoint_scope(
    order: str,
    spec: ProjectSpec,
    client: LLMClient,
    *,
    context: str | None = None,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    synth_retries: int = DEFAULT_SYNTH_RETRIES,
) -> ProjectSpec:
    """병렬 형제 scope가 겹치면 #31 채널로 *바운드 1회* 재합성해 disjoint하게 재배치한 spec 반환.

    겹침 없음/미선언이면 no-op으로 원본(추가 LLM 호출 0·비용 불변). 넛지가 필요해도:
      - 정확히 1회 재합성(bounded).
      - 실패(SynthesisError/예외) 흡수 → 원본 진행(advisory).
      - 성공해도 *decomposition만* 채택, bar 변경이면 reject·원본 유지(anti-erosion 가드).
    적대 분리: feedback/context는 *합성기에만* — critic/judge 미수신(#58 패턴 유지).
    """
    feedback = disjoint_scope_feedback(spec)
    if feedback is None:
        return spec  # 형제 scope 겹침 없음 — no-op
    try:
        restructured = synthesize(
            order, client, context=context, prompt_path=prompt_path,
            feedback=feedback, synth_retries=synth_retries,
        )
    except Exception:  # noqa: BLE001 — 넛지는 advisory: 어떤 실패도 run을 막지 않는다(원본 진행)
        return spec
    return _adopt_decomposition_only(spec, restructured)
