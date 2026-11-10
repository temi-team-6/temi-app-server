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


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Message:
    def __init__(self, text: str, stop_reason: str) -> None:
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, message: _Message) -> None:
        self._message = message

    async def create(self, **_: object) -> _Message:
        return self._message


class _Client:
    def __init__(self, message: _Message) -> None:
        self.messages = _Messages(message)


@pytest.mark.parametrize(("text", "stop_reason", "expected"), [
    ("검사를 마쳤습니다. 속도는 1.3 m/s입니다.", "end_turn", "검사를 마쳤습니다. 속도는 1.3 m/s입니다."),
    ("검사를 마쳤습니다. 정답률 83", "max_tokens", "검사를 마쳤습니다."),
    ("문장이 끝나지 않은 답", "max_tokens", None),
    ("속도는 1.3 m/s입니다. 정답률 8.3", "max_tokens", "속도는 1.3 m/s입니다."),
])
async def test_complete_drops_a_sentence_cut_by_max_tokens(monkeypatch: pytest.MonkeyPatch, text: str,
                                                           stop_reason: str, expected: str | None) -> None:
    monkeypatch.setattr(llm, "_get_client", lambda: _Client(_Message(text, stop_reason)))

    assert await llm.complete("system", "prompt", tier="smart") == expected
