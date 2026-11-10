from sqlalchemy import select

from app.agent import agent
from app.db import SessionLocal
from app.models import Condition, SessionStatus, TestSession, Trial, TrialStatus

from .fakes import GOOD_WALK, FakeDevices


async def _active_session(resident_id: int) -> tuple[int, int]:
    """서버가 재시작되어 에이전트가 잊어버린, DB에만 '진행 중'으로 남은 검사를 만든다."""
    async with SessionLocal() as db:
        session = TestSession(resident_id=resident_id)
        db.add(session)
        await db.flush()
        trial = Trial(session_id=session.id, condition=Condition.SINGLE, attempt=1)
        db.add(trial)
        await db.commit()
        return session.id, trial.id


async def test_orphaned_session_is_closed_as_aborted(fresh_state: None) -> None:
    session_id, trial_id = await _active_session(1)

    assert await agent.close_orphaned_sessions("server_restarted") == 1
    assert await agent.close_orphaned_sessions("server_restarted") == 0

    async with SessionLocal() as db:
        session = await db.get(TestSession, session_id)
        trial = await db.get(Trial, trial_id)
    assert session is not None and session.status == SessionStatus.ABORTED
    assert session.end_reason == "server_restarted" and session.ended_at is not None
    assert trial is not None and trial.status == TrialStatus.ABORTED and trial.completed_at is not None


async def test_new_session_closes_orphans_but_keeps_itself(devices: FakeDevices) -> None:
    orphan_id, _ = await _active_session(1)
    devices.walk = GOOD_WALK

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    async with SessionLocal() as db:
        sessions = {s.id: s for s in await db.scalars(select(TestSession))}
    assert sessions[orphan_id].status == SessionStatus.ABORTED
    assert sessions[orphan_id].end_reason == "orphaned"
    newest = max(sessions)
    assert newest != orphan_id and sessions[newest].status == SessionStatus.COMPLETED
