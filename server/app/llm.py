"""Claude 호출을 한곳에 모은다.

키가 없거나 호출이 실패·시간 초과되면 None을 돌려준다. 호출한 쪽은 None이면 기존 규칙 응답을 쓴다.
안전 신호 처리에는 쓰지 않는다(protocol.md: 안전 규칙은 LLM을 거치지 않는다).
"""

import logging
from typing import Literal

from anthropic import APIError, AsyncAnthropic

from .config import settings

log = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic | None:
    global _client
    if not settings.anthropic_api_key:
        return None
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_s, max_retries=1)
    return _client


async def complete(system: str, prompt: str, *, tier: Literal["fast", "smart"] = "fast",
                   max_tokens: int = 512) -> str | None:
    """fast: 검사 중 빠른 판단(유효성, 발화 대응). smart: 결과 해설, 보호자 챗봇."""
    client = _get_client()
    if client is None:
        return None
    model = settings.llm_model_fast if tier == "fast" else settings.llm_model_smart
    try:
        message = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except APIError as e:
        log.warning("LLM call failed (%s), falling back to rules: %s", model, e)
        return None
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    return text or None
