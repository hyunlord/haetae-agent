"""LLM 응답 → 검증된 pydantic 모델 공통 파이프라인.

intake(synthesize)와 replan이 똑같이: raw 응답 → 코드펜스 제거 → yaml.safe_load
→ Model.model_validate, 실패 시 raw 응답을 보존한 예외. 그 로직을 DRY로 모은다.
"""

from __future__ import annotations

from typing import Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


class ParseError(Exception):
    """LLM 응답 파싱/검증 실패. 디버깅을 위해 raw 응답을 동봉한다."""

    def __init__(self, message: str, raw_response: str):
        self.raw_response = raw_response
        super().__init__(f"{message}\n--- raw LLM 응답 ---\n{raw_response}")


def strip_code_fence(text: str) -> str:
    """```yaml ... ``` 또는 ``` ... ``` 펜스를 방어적으로 제거한다.

    펜스가 없으면 양끝 공백만 정리해 그대로 반환.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    lines = lines[1:]  # 첫 줄(``` 또는 ```yaml) 제거
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]  # 마지막 펜스 줄 제거
    return "\n".join(lines).strip()


def parse_yaml_model(
    raw: str,
    model_cls: type[M],
    error_cls: type[ParseError] = ParseError,
    normalize: Callable[[dict], dict] | None = None,
) -> M:
    """raw LLM 응답을 model_cls로 파싱·검증해 반환. 실패 시 error_cls(raw 동봉).

    normalize: model_validate 직전, safe_load된 dict를 보정하는 안전망(선택).
               흔한 키/타입 변종을 흡수하되, 못 잡는 변종은 그대로 검증 실패로 둔다.
    """
    body = strip_code_fence(raw)

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        raise error_cls(f"YAML 파싱 실패: {e}", raw) from e

    if not isinstance(data, dict):
        raise error_cls(
            f"응답이 매핑(dict)이 아님: {type(data).__name__}", raw
        )

    if normalize is not None:
        data = normalize(data)

    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        raise error_cls(f"{model_cls.__name__} 검증 실패: {e}", raw) from e
