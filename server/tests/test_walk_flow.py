from httpx import AsyncClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgentRequest, Observation, ObservationSource, SessionStatus, TestSession

from .conftest import get_user, login
from .fakes import GOOD_WALK, SLOW_DUAL_WALK, FakeDevices, Message, connect_app

SLOW_ANIMALS: Message = {**GOOD_WALK["animals"], "speed_mps": 0.88}


def is_idle(msg: dict) -> bool:
    return msg["payload"].get("screen") == "idle"


async def test_normal_walk_completes_without_guardian_question(devices: FakeDevices) -> None:
    guardian_b = connect_app(await get_user("guardian_b"))
    devices.walk = GOOD_WALK

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await devices.temi.wait_for("temi.show", is_idle)

    conditions = [m["payload"]["condition"] for m in devices.pi.of_type("trial.start")]
    assert conditions == ["single", "animals", "countdown"]
    assert len(devices.pi.of_type("trial.go")) == 3
    assert guardian_b.of_type("request.created") == []
    async with SessionLocal() as db:
        session = await db.scalar(select(TestSession).order_by(TestSession.id.desc()))
    assert session is not None and session.status == SessionStatus.COMPLETED
    ended = await guardian_b.wait_for("session.ended")
    assert ended["payload"]["session_id"] == session.id and ended["payload"]["status"] == "completed"


async def test_invalid_trial_is_retried(devices: FakeDevices) -> None:
    devices.walk = {**GOOD_WALK, "single": {**GOOD_WALK["single"], "stopped": True}}

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    await devices.temi.wait_for("temi.show", is_idle)

    attempts = [m["payload"]["attempt"] for m in devices.pi.of_type("trial.start")
                if m["payload"]["condition"] == "single"]
    assert attempts == [1, 2, 3]


async def test_slow_dual_task_asks_only_that_guardian(devices: FakeDevices, client: AsyncClient) -> None:
    guardian_a = connect_app(await get_user("guardian_a"))
    guardian_b = connect_app(await get_user("guardian_b"))
    staff = connect_app(await get_user("staff1"))
    devices.walk = SLOW_DUAL_WALK

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    created = await guardian_b.wait_for("request.created")
    await devices.temi.wait_for("temi.show", is_idle)

    assert created["payload"]["topic"] == "condition_check"
    assert "두 가지 과제" in created["payload"]["explanation"]
    assert len(devices.pi.of_type("trial.start")) == 3
    assert guardian_a.of_type("request.created") == []
    assert staff.of_type("request.created") == []

    headers = await login(client, "guardian_b")
    res = await client.post(f"/api/requests/{created['payload']['id']}/answer",
                            json={"answer": "평소와 같아요"}, headers=headers)
    assert res.status_code == 200, res.text

    updated = await guardian_b.wait_for("request.updated")
    assert updated["payload"]["status"] == "answered"
    assert guardian_a.of_type("request.updated") == []


async def last_agent_observation() -> str:
    async with SessionLocal() as db:
        obs = await db.scalar(select(Observation).where(Observation.source == ObservationSource.AGENT)
                              .order_by(Observation.id.desc()))
    assert obs is not None
    return obs.text


async def test_single_task_signal_is_retested_and_not_sent_when_inconsistent(devices: FakeDevices) -> None:
    guardian_b = connect_app(await get_user("guardian_b"))
    devices.walk = {**GOOD_WALK, "animals": [SLOW_ANIMALS, SLOW_ANIMALS]}

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await devices.temi.wait_for("temi.show", is_idle)

    starts = [(m["payload"]["condition"], m["payload"]["attempt"]) for m in devices.pi.of_type("trial.start")]
    assert starts == [("single", 1), ("animals", 1), ("countdown", 1), ("animals", 2)]
    assert guardian_b.of_type("request.created") == []
    assert "한 과제에서만" in await last_agent_observation()


async def test_single_task_signal_that_recovers_on_retest_is_temporary(devices: FakeDevices) -> None:
    guardian_b = connect_app(await get_user("guardian_b"))
    devices.walk = {**GOOD_WALK, "animals": [SLOW_ANIMALS, GOOD_WALK["animals"]]}

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await devices.temi.wait_for("temi.show", is_idle)

    assert len(devices.pi.of_type("trial.start")) == 4
    assert guardian_b.of_type("request.created") == []
    assert "일시적" in await last_agent_observation()


async def test_guardian_decline_answer_asks_staff_for_consult(devices: FakeDevices, client: AsyncClient) -> None:
    staff = connect_app(await get_user("staff1"))
    devices.walk = SLOW_DUAL_WALK
    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await devices.temi.wait_for("temi.show", is_idle)
    async with SessionLocal() as db:
        question = await db.scalar(select(AgentRequest).where(AgentRequest.session_id.is_not(None)))
    assert question is not None

    headers = await login(client, "guardian_b")
    res = await client.post(f"/api/requests/{question.id}/answer",
                            json={"answer": "요즘 길을 자주 헤매세요"}, headers=headers)
    assert res.status_code == 200, res.text

    consult = await staff.wait_for("request.created", lambda m: m["payload"]["topic"] == "consult")
    assert consult["payload"]["kind"] == "check"
