"""LLM 클라이언트 추상화.

이번 단계는 mock만. 실제 provider(Anthropic/OpenAI/Gemini/Ollama)는 다음 WO에서
이 인터페이스를 구현해 끼운다. intake/replan 같은 상위 로직은 구체 provider를
몰라야 한다 → Protocol로 의존성을 끊는다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """동기 텍스트 완성 인터페이스.

    system: 시스템 프롬프트 (예: synthesizer.md)
    user:   유저 메시지 (예: order + context)
    반환:   모델의 raw 텍스트 응답
    """

    def complete(self, system: str, user: str, **opts) -> str: ...


class MockClient:
    """테스트용. 미리 주입한 응답을 순서대로 반환한다.

    responses: 단일 문자열이면 매 호출 같은 값을 반환,
               리스트면 호출마다 다음 항목을 반환(소진 시 예외).
    호출 인자(system/user/opts)는 `calls`에 기록되어 검증에 쓸 수 있다.

    usages(WO#33 계측 테스트용): 호출마다 노출할 token usage 리스트(선택). 주어지면
      complete 호출 후 self.last_usage에 (호출 순서대로, 소진 시 마지막을 반복) 싣는다.
      기본 None → last_usage는 계속 None(usage 미노출 — 기존 동작 불변).
    """

    def __init__(self, responses: list[str] | str, usages: list | None = None):
        if isinstance(responses, str):
            self._responses = [responses]
            self._cycle_single = True
        else:
            self._responses = list(responses)
            self._cycle_single = False
        self._index = 0
        self.calls: list[dict] = []
        self._usages = list(usages) if usages is not None else None
        # 직전 호출의 token usage(WO#33). usages 미주입이면 항상 None.
        self.last_usage = None

    def complete(self, system: str, user: str, **opts) -> str:
        self.calls.append({"system": system, "user": user, "opts": opts})
        if self._usages is not None:
            idx = min(len(self.calls) - 1, len(self._usages) - 1)
            self.last_usage = self._usages[idx]
        if self._cycle_single:
            return self._responses[0]
        if self._index >= len(self._responses):
            raise IndexError(
                f"MockClient: 주입된 응답 {len(self._responses)}개를 모두 소진함 "
                f"(호출 {self._index + 1}번째)"
            )
        resp = self._responses[self._index]
        self._index += 1
        return resp


# 실제 provider 재노출 (codex.py는 llm.py를 import하지 않아 순환 없음)
from haetae.providers.codex import CodexClient, CodexError, CodexStalled  # noqa: E402

__all__ = ["LLMClient", "MockClient", "CodexClient", "CodexError", "CodexStalled"]
