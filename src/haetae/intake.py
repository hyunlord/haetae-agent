"""intake 러너 — 주문(order) → ProjectSpec.

director의 첫 능력: synthesizer.md를 시스템 프롬프트로 LLM에 태우고,
응답(YAML)을 ProjectSpec으로 검증해 돌려준다.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from haetae.llm import LLMClient
from haetae.models import ProjectSpec

DEFAULT_PROMPT_PATH = "prompts/synthesizer.md"


class SynthesisError(Exception):
    """합성 응답 파싱/검증 실패. 디버깅을 위해 raw 응답을 동봉한다."""

    def __init__(self, message: str, raw_response: str):
        self.raw_response = raw_response
        super().__init__(f"{message}\n--- raw LLM 응답 ---\n{raw_response}")


def _strip_code_fence(text: str) -> str:
    """```yaml ... ``` 또는 ``` ... ``` 펜스를 방어적으로 제거한다.

    펜스가 없으면 원본을 그대로(양끝 공백만 정리) 반환.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    # 첫 줄: ``` 또는 ```yaml — 제거
    lines = lines[1:]
    # 마지막 펜스 줄 제거
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


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
    body = _strip_code_fence(raw)

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        raise SynthesisError(f"YAML 파싱 실패: {e}", raw) from e

    if not isinstance(data, dict):
        raise SynthesisError(
            f"합성 응답이 매핑(dict)이 아님: {type(data).__name__}", raw
        )

    try:
        return ProjectSpec.model_validate(data)
    except ValidationError as e:
        raise SynthesisError(f"ProjectSpec 검증 실패: {e}", raw) from e
