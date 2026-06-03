"""replan 러너 — (spec, state, last_result) → Decision.

director의 두 번째 능력: 게이트 판정이 난 직후 호출되어 다음 단 하나의 결정을 낸다.
replan.md를 시스템 프롬프트로, (spec + state + last_result)를 user로 태운다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from haetae.llm import LLMClient
from haetae.models import Decision, ProjectSpec, State
from haetae.parsing import ParseError, parse_yaml_model

DEFAULT_PROMPT_PATH = "prompts/replan.md"


class ReplanError(ParseError):
    """replan 응답 파싱/검증 실패. raw 응답은 .raw_response에 보존된다."""


def _dump_yaml(model) -> str:
    return yaml.safe_dump(
        model.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )


def _build_user(spec: ProjectSpec, state: State, last_result: str) -> str:
    return (
        "# spec (pinned · north-star)\n"
        f"```yaml\n{_dump_yaml(spec)}```\n\n"
        "# state (지금까지의 진행)\n"
        f"```yaml\n{_dump_yaml(state)}```\n\n"
        "# last_result (방금 executor가 돌려준 결과 + 게이트 판정)\n"
        f"{last_result}"
    )


def replan(
    spec: ProjectSpec,
    state: State,
    last_result: str,
    client: LLMClient,
    prompt_path: str | Path = DEFAULT_PROMPT_PATH,
) -> Decision:
    """spec+state+last_result를 보고 다음 Decision을 합성해 반환한다.

    실패 시(YAML 파싱 불가 / 스키마 검증 불통과) raw 응답을 담은 ReplanError.
    """
    system = Path(prompt_path).read_text(encoding="utf-8")
    user = _build_user(spec, state, last_result)
    raw = client.complete(system, user)
    return parse_yaml_model(raw, Decision, ReplanError)
