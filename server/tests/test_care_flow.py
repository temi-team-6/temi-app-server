from sqlalchemy import select

from app.db import SessionLocal
from app.models import SessionStatus, TestSession

from .conftest import get_user
from .fakes import FakeDevices, connect_app


async def start_first_walk(devices: FakeDevices, tag: str = "RFID-A-0001") -> None:
    await devices.send("pi", "rfid.scanned", {"tag": tag})
    await devices.pi.wait_for("trial.go")


async def current_session() -> TestSession:
    async with SessionLocal() as db:
        session = await db.scalar(select(TestSession).order_by(TestSession.id.desc()))
    assert session is not None
    return session


async def test_mild_pain_switches_to_seated_game(devices: FakeDevices) -> None:
    staff = connect_app(await get_user("staff1"))
    guardian = connect_app(await get_user("guardian_a"))
    await start_first_walk(devices)

    await devices.send("temi", "speech.heard", {"text": "다리가 아파"})
    assert devices.pi.of_type("trial.abort")
    await devices.temi.wait_for("temi.ask")

    await devices.send("temi", "speech.heard", {"text": "조금 아파요"})
    check = await staff.wait_for("request.created")
    game = await devices.pi.wait_for("game.start")

    assert check["payload"]["topic"] == "pain_check"
    assert game["payload"]["game_type"] == "stroop"
    assert guardian.of_type("request.created") == []

    await devices.send("pi", "game.completed",
                       {"game_id": game["payload"]["game_id"], "rt_ms": [350, 420, 380], "correct": [True, True, False]})
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")
    assert (await current_session()).status == SessionStatus.COMPLETED


async def test_severe_pain_calls_staff_in_order_then_alerts(devices: FakeDevices) -> None:
    staff = connect_app(await get_user("staff1"))
    await start_first_walk(devices)
    await devices.send("temi", "speech.heard", {"text": "아파"})
    await devices.temi.wait_for("temi.ask")

    await devices.send("temi", "speech.heard", {"text": "일어설 수가 없어"})
    first = await devices.temi.wait_for("temi.video_call")
    await devices.send("temi", "video_call.result", {"staff_id": first["payload"]["staff_id"], "answered": False})
    second = await devices.temi.wait_for("temi.video_call", lambda m: m["id"] != first["id"])
    await devices.send("temi", "video_call.result", {"staff_id": second["payload"]["staff_id"], "answered": False})
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    assert [first["payload"]["name"], second["payload"]["name"]] == ["요양사 김", "요양사 이"]
    alerts = [m for m in staff.of_type("request.created") if m["payload"]["kind"] == "alert"]
    assert len(alerts) == 2 and all(a["payload"]["priority"] == "urgent" for a in alerts)
    session = await current_session()
    assert (session.status, session.end_reason) == (SessionStatus.ABORTED, "emergency_unanswered")


async def test_refusal_ends_session(devices: FakeDevices) -> None:
    await start_first_walk(devices)

    await devices.send("temi", "speech.heard", {"text": "오늘은 하기 싫어"})

    session = await current_session()
    assert (session.status, session.end_reason) == (SessionStatus.ABORTED, "refused")
