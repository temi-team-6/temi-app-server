from collections.abc import Sequence

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import llm
from app.agent import agent
from app.db import SessionLocal
from app.models import AgentRequest

from .conftest import get_user, login
from .fakes import GOOD_WALK, FakeDevices, connect_app


class FakeClassifier:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, instruction: str, text: str, labels: Sequence[str], **_: object) -> str | None:
        self.calls.append(text)
        return self.answers.get(text)


async def test_llm_catches_pain_the_keywords_miss(devices: FakeDevices, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "classify", FakeClassifier({"무릎이 시큰거려요": "pain"}))
    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    await devices.pi.wait_for("trial.go")

    await devices.send("temi", "speech.heard", {"text": "무릎이 시큰거려요"})

    assert devices.pi.of_type("trial.abort")
    assert devices.temi.of_type("temi.ask")


async def test_llm_is_not_asked_about_speech_during_dual_task(devices: FakeDevices,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = FakeClassifier({})
    monkeypatch.setattr(llm, "classify", classifier)
    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    single = await devices.pi.wait_for("trial.go")
    await devices.send("pi", "trial.completed", {"trial_id": single["payload"]["trial_id"], **GOOD_WALK["single"]})
    await devices.pi.wait_for("trial.go", lambda m: m["payload"]["trial_id"] != single["payload"]["trial_id"])

    await devices.send("temi", "speech.heard", {"text": "고양이 강아지 호랑이"})

    assert classifier.calls == []
    assert devices.pi.of_type("trial.abort") == []


async def test_llm_can_escalate_pain_to_severe(devices: FakeDevices, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "classify", FakeClassifier({"다리에 힘이 하나도 없어요": "severe"}))
    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    await devices.pi.wait_for("trial.go")
    await devices.send("temi", "speech.heard", {"text": "아파요"})

    await devices.send("temi", "speech.heard", {"text": "다리에 힘이 하나도 없어요"})

    assert devices.temi.of_type("temi.video_call")


async def test_llm_cannot_downgrade_severe_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = FakeClassifier({"못 일어나겠어": "mild"})
    monkeypatch.setattr(llm, "classify", classifier)

    assert await agent._is_severe("못 일어나겠어") is True
    assert classifier.calls == []


async def test_llm_reads_guardian_answer_without_keywords(devices: FakeDevices, client: AsyncClient,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    answer = "요즘 가스불을 켜 둔 채로 나가세요"
    monkeypatch.setattr(llm, "classify", FakeClassifier({answer: "daily_change"}))
    staff = connect_app(await get_user("staff1"))
    async with SessionLocal() as db:
        question = await db.scalar(select(AgentRequest).where(AgentRequest.topic == "condition_check",
                                                              AgentRequest.target_user_id == 2))
    assert question is not None

    res = await client.post(f"/api/requests/{question.id}/answer", json={"answer": answer},
                            headers=await login(client, "guardian_b"))

    assert res.status_code == 200, res.text
    consult = await staff.wait_for("request.created", lambda m: m["payload"]["topic"] == "consult")
    assert answer in consult["payload"]["text"]
