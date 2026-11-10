"""Claude 호출을 한곳에 모은다.

키가 없거나 호출이 실패·시간 초과되면 None을 돌려준다. 호출한 쪽은 None이면 기존 규칙 응답을 쓴다.
안전 신호 처리에는 쓰지 않는다(protocol.md: 안전 규칙은 LLM을 거치지 않는다).
"""

import logging
import re
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
    if message.stop_reason == "max_tokens":
        # 토큰 한도에서 잘린 답은 문장 중간에서 끝난다. 마지막 완결 문장까지만 쓴다.
        log.warning("LLM reply hit max_tokens (%s, %d), trimming to the last full sentence", model, max_tokens)
        text = trim_to_sentence(text)
    return text or None


# 문장 끝 기호 뒤에 공백이나 글 끝이 와야 문장 끝으로 본다("1.3 m/s"의 점은 제외).
SENTENCE_END = re.compile(r"[.?!…。](?=\s|$)")


def trim_to_sentence(text: str) -> str:
    """마지막 완결 문장 뒤를 버린다. 완결된 문장이 없으면 빈 문자열."""
    ends = [m.end() for m in SENTENCE_END.finditer(text)]
    return text[:ends[-1]].strip() if ends else ""


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
