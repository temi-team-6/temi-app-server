from app import llm


async def test_complete_returns_none_without_api_key() -> None:
    assert await llm.complete("system", "prompt") is None
