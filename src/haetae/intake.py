"""intake 러너 — 주문(order) → ProjectSpec.

director의 첫 능력: synthesizer.md를 시스템 프롬프트로 LLM에 태우고,
응답(YAML)을 ProjectSpec으로 검증해 돌려준다.
"""

from __future__ import annotations

from pathlib import Path

from haetae.llm import LLMClient
from haetae.models import ProjectSpec
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_PROMPT_PATH = "prompts/synthesizer.md"


class SynthesisError(ParseError):
    """합성 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


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
    return parse_yaml_model(raw, ProjectSpec, SynthesisError)
