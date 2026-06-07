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
