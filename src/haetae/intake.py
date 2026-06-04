"""intake 러너 — 주문(order) → ProjectSpec.

director의 첫 능력: synthesizer.md를 시스템 프롬프트로 LLM에 태우고,
응답(YAML/JSON)을 ProjectSpec으로 검증해 돌려준다.

model_validate 직전에 _normalize_spec_dict로 흔한 출력 변종을 흡수한다(보조 안전망).
정규화는 만능이 아니다 — 못 잡는 변종은 그대로 SynthesisError(raw 보존).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from haetae.llm import LLMClient
from haetae.models import ProjectSpec
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_PROMPT_PATH = "prompts/synthesizer.md"


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


def synthesize(
    order: str,
    client: LLMClient,
    context: str | None = None,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
) -> ProjectSpec:
    """주문을 합성기에 태워 검증된 ProjectSpec을 반환한다.

    실패 시(YAML 파싱 불가 / 스키마 검증 불통과) raw 응답을 담은 SynthesisError.
    """
    system = Path(prompt_path).read_text(encoding="utf-8")

    user = f"# 주문(order)\n{order}"
    if context:
        user += f"\n\n# 프로젝트 컨텍스트(project_context)\n{context}"

    raw = client.complete(system, user)
    return parse_yaml_model(
        raw, ProjectSpec, SynthesisError, normalize=_normalize_spec_dict
    )
