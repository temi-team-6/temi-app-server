from typing import Any

import pytest
from httpx import AsyncClient

from app import chat, llm

from .conftest import login


class FakeLLM:
    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, system: str, prompt: str, **kwargs: Any) -> str | None:
        self.calls.append({"system": system, "prompt": prompt, **kwargs})
        return self.reply


async def resident_id(client: AsyncClient, headers: dict[str, str]) -> int:
    res = await client.get("/api/residents", headers=headers)
    return res.json()[0]["id"]


async def test_chat_uses_llm_with_trend_and_keeps_history(client: AsyncClient,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLLM("최근 3주 기록은 시연용 가상 기록이며, 이중과제 비용은 큰 변화가 없습니다.")
    monkeypatch.setattr(llm, "complete", fake)
    headers = await login(client, "guardian_b")
    rid = await resident_id(client, headers)

    first = await client.post("/api/chat", json={"resident_id": rid, "message": "같은 말을 반복하세요"}, headers=headers)
    second = await client.post("/api/chat", json={"resident_id": rid, "message": "2주 전부터요"}, headers=headers)

    assert first.status_code == 200 and first.json()["mock"] is False
    assert first.json()["reply"] == fake.reply
    assert "B님" in fake.calls[0]["system"] and "[가상]" in fake.calls[0]["system"]
    assert "이중과제 비용 동물 이름" in fake.calls[0]["system"]
    assert fake.calls[0]["history"] == []
    assert fake.calls[1]["history"] == [("user", "같은 말을 반복하세요"), ("assistant", fake.reply)]
    assert second.status_code == 200

    history = (await client.get(f"/api/residents/{rid}/chat", headers=headers)).json()
    assert [(m["role"], m["text"]) for m in history] == [
        ("user", "같은 말을 반복하세요"), ("assistant", fake.reply), ("user", "2주 전부터요"), ("assistant", fake.reply),
    ]
    observations = (await client.get(f"/api/residents/{rid}/observations", headers=headers)).json()
    assert any(o["text"] == "챗봇 질문: 2주 전부터요" for o in observations)


async def test_chat_falls_back_to_fixed_reply_when_llm_unavailable(client: AsyncClient) -> None:
    headers = await login(client, "guardian_a")
    rid = await resident_id(client, headers)

    res = await client.post("/api/chat", json={"resident_id": rid, "message": "길을 자주 헤매세요"}, headers=headers)

    assert res.status_code == 200
    assert res.json()["mock"] is True
    assert chat.CONSULT_GUIDE in res.json()["reply"]
    history = (await client.get(f"/api/residents/{rid}/chat", headers=headers)).json()
    assert [m["mock"] for m in history] == [False, True]


async def test_chat_history_is_private_to_each_account(client: AsyncClient) -> None:
    guardian_b = await login(client, "guardian_b")
    rid_b = await resident_id(client, guardian_b)
    await client.post("/api/chat", json={"resident_id": rid_b, "message": "질문"}, headers=guardian_b)

    other_guardian = await client.get(f"/api/residents/{rid_b}/chat", headers=await login(client, "guardian_a"))
    staff = await client.get(f"/api/residents/{rid_b}/chat", headers=await login(client, "staff1"))

    assert other_guardian.status_code == 404
    assert staff.status_code == 200 and staff.json() == []
