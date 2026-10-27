import asyncio
import random
import statistics
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import settings
from .db import Base, SessionLocal, engine
from .auth import hash_password
from .models import (
    Condition,
    GameResult,
    GameStatus,
    GameType,
    Observation,
    ObservationSource,
    Resident,
    Role,
    SessionStatus,
    TestSession,
    Trial,
    TrialStatus,
    User,
)

DEV_PASSWORD = "temi1234"
WEEKS = 3
SESSION_WEEKDAYS = (0, 2, 4)

PROFILES = {
    "A": {"single": 1.30, "animals_cost": 12.0, "countdown_cost": 18.0, "rt": 340.0},
    "B": {"single": 1.22, "animals_cost": 10.0, "countdown_cost": 15.0, "rt": 360.0},
}


async def seed() -> None:
    rng = random.Random(42)
    tz = ZoneInfo(settings.timezone)
    async with SessionLocal() as db:
        users = {
            "guardian_a": User(username="guardian_a", name="A 보호자", role=Role.GUARDIAN),
            "guardian_b": User(username="guardian_b", name="B 보호자", role=Role.GUARDIAN),
            "staff1": User(username="staff1", name="요양사 김", role=Role.STAFF, call_priority=1),
            "staff2": User(username="staff2", name="요양사 이", role=Role.STAFF, call_priority=2),
        }
        for u in users.values():
            u.password_hash = hash_password(DEV_PASSWORD)
        db.add_all(users.values())
        await db.flush()

        residents = {
            "A": Resident(name="A", rfid_tag="RFID-A-0001", birth_year=1948, guardian_id=users["guardian_a"].id),
            "B": Resident(name="B", rfid_tag="RFID-B-0002", birth_year=1945, guardian_id=users["guardian_b"].id),
        }
        db.add_all(residents.values())
        await db.flush()

        today = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
        start = today - timedelta(weeks=WEEKS)
        days = [start + timedelta(days=d) for d in range(WEEKS * 7) if (start + timedelta(days=d)).weekday() in SESSION_WEEKDAYS]
        for key, resident in residents.items():
            p = PROFILES[key]
            for i, day in enumerate(days):
                practice = 0.3 * i
                at = day + timedelta(minutes=rng.randint(-20, 20))
                session = TestSession(resident_id=resident.id, started_at=at, ended_at=at + timedelta(minutes=8),
                                      status=SessionStatus.COMPLETED, synthetic=True)
                db.add(session)
                await db.flush()
                single = p["single"] + rng.gauss(0, 0.03)
                speeds = {
                    Condition.SINGLE: single,
                    Condition.ANIMALS: single * (1 - (p["animals_cost"] - practice + rng.gauss(0, 1.5)) / 100),
                    Condition.COUNTDOWN: single * (1 - (p["countdown_cost"] - practice + rng.gauss(0, 1.5)) / 100),
                }
                for n, (cond, speed) in enumerate(speeds.items()):
                    db.add(Trial(session_id=session.id, condition=cond, status=TrialStatus.VALID,
                                 started_at=at + timedelta(minutes=n), completed_at=at + timedelta(minutes=n, seconds=4),
                                 speed_mps=round(speed, 3), duration_s=round(4 / speed, 2), stopped=False,
                                 max_persons=1, min_confidence=0.9,
                                 word_count=None if cond == Condition.SINGLE else rng.randint(6, 10)))
                rts = [max(180.0, rng.gauss(p["rt"] - 2 * i, 40)) for _ in range(20)]
                correct = [rng.random() > 0.05 for _ in rts]
                hits = [rt for rt, ok in zip(rts, correct, strict=True) if ok]
                db.add(GameResult(session_id=session.id, game_type=GameType.WHACK, status=GameStatus.COMPLETED,
                                  started_at=at + timedelta(minutes=4), completed_at=at + timedelta(minutes=6),
                                  rt_ms=[round(r, 1) for r in rts], correct=correct,
                                  accuracy=round(len(hits) / len(rts), 3), rt_mean_ms=round(statistics.fmean(hits), 1),
                                  rt_sd_ms=round(statistics.stdev(hits), 1)))
            db.add(Observation(resident_id=resident.id, source=ObservationSource.SYSTEM,
                               text=f"시연용 가상 기록 {len(days)}회 생성", data={"synthetic": True}))
        await db.commit()


async def seed_if_empty() -> None:
    async with SessionLocal() as db:
        if await db.scalar(select(func.count()).select_from(User)):
            return
    await seed()


async def reset() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await seed()
    await engine.dispose()


if __name__ == "__main__":
    if sys.argv[1:] != ["--reset"]:
        sys.exit("usage: python -m app.seed --reset")
    asyncio.run(reset())
