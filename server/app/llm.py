"""Claude 호출을 한곳에 모은다.

키가 없거나 호출이 실패·시간 초과되면 None을 돌려준다. 호출한 쪽은 None이면 기존 규칙 응답을 쓴다.
안전 신호 처리에는 쓰지 않는다(protocol.md: 안전 규칙은 LLM을 거치지 않는다).
"""

import logging
from collections.abc import Sequence
from typing import Literal

from anthropic import APIError, AsyncAnthropic
from anthropic.types import MessageParam

from .config import settings

log = logging.getLogger(__name__)

Turn = tuple[Literal["user", "assistant"], str]

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic | None:
    global _client
    if not settings.anthropic_api_key:
        return None
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_s, max_retries=1)
    return _client


def _alternating(history: Sequence[Turn]) -> list[MessageParam]:
    """API는 user로 시작해 user/assistant가 번갈아 와야 한다. 맞지 않는 턴은 버린다."""
    messages: list[MessageParam] = []
    for role, text in history:
        expected = "user" if not messages or messages[-1]["role"] == "assistant" else "assistant"
        if role == expected == "user":
            messages.append({"role": "user", "content": text})
        elif role == expected == "assistant":
            messages.append({"role": "assistant", "content": text})
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    return messages


async def complete(system: str, prompt: str, *, history: Sequence[Turn] = (),
                   tier: Literal["fast", "smart"] = "fast", max_tokens: int = 512,
                   timeout: float | None = None) -> str | None:
    """fast: 검사 중 빠른 분류. smart: 결과 해설, 보호자 챗봇. history는 이전 대화(오래된 순)."""
    client = _get_client()
    if client is None:
        return None
    model = settings.llm_model_fast if tier == "fast" else settings.llm_model_smart
    messages = [*_alternating(history), {"role": "user", "content": prompt}]
    try:
        message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            timeout=timeout or settings.llm_timeout_s,
        )
    except APIError as e:
        log.warning("LLM call failed (%s), falling back to rules: %s", model, e)
        return None
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    return text or None


async def classify(instruction: str, text: str, labels: Sequence[str], *, timeout: float = 5.0) -> str | None:
    """text를 labels 중 하나로 분류한다. 실패하거나 답에 라벨이 하나로 특정되지 않으면 None."""
    system = f"{instruction}\n반드시 다음 중 하나의 단어만 그대로 답한다: {', '.join(labels)}"
    out = await complete(system, text, tier="fast", max_tokens=10, timeout=timeout)
    if out is None:
        return None
    found = [label for label in labels if label in out.lower()]
    if len(found) != 1:
        log.warning("LLM label not recognized for %r: %r", text, out)
        return None
    return found[0]
