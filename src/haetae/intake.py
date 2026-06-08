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
from haetae.models import ProjectSpec
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
