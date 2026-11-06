import pytest

from app import llm


async def test_complete_returns_none_without_api_key() -> None:
    assert await llm.complete("system", "prompt") is None


@pytest.mark.parametrize(("reply", "expected"), [
    ("refuse", "refuse"),
    ("Refuse.", "refuse"),
    ("label: pain", "pain"),
    ("pain 또는 refuse", None),
    ("모르겠습니다", None),
    (None, None),
])
async def test_classify_accepts_only_one_known_label(monkeypatch: pytest.MonkeyPatch, reply: str | None,
                                                    expected: str | None) -> None:
    async def fake_complete(*_: object, **__: object) -> str | None:
        return reply

    monkeypatch.setattr(llm, "complete", fake_complete)

    assert await llm.classify("분류", "문장", ("pain", "refuse", "other")) == expected
