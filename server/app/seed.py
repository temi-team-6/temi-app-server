import asyncio
import random
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from .auth import hash_password
from .config import settings
from .db import SessionLocal, engine
from .models import (
    AgentRequest,
    Condition,
    GameResult,
    GameStatus,
    GameType,
    Observation,
    ObservationSource,
    Priority,
    RequestKind,
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
        db.add_all(_example_requests(residents, users))
        await db.commit()


def _example_requests(residents: dict[str, Resident], users: dict[str, User]) -> list[AgentRequest]:
    """앱 화면을 로봇 검사 없이 개발할 수 있도록 대기 중인 요청을 종류별로 하나씩 넣는다."""
    questions = [
        AgentRequest(
            resident_id=residents[key].id, target_role=Role.GUARDIAN, target_user_id=users[guardian].id,
            kind=RequestKind.QUESTION, priority=Priority.NORMAL, topic="condition_check",
            text=f"[시연 예시] {key}님의 오늘 걷기 검사 결과가 평소와 조금 달랐습니다. "
                 "이번 주 수면이나 컨디션에 변화가 있었나요?",
            options=["잠을 잘 못 주무셨어요", "평소와 같아요", "요즘 길을 자주 헤매세요"],
        )
        for key, guardian in (("A", "guardian_a"), ("B", "guardian_b"))
    ]
    return [
        *questions,
        AgentRequest(
            resident_id=residents["A"].id, target_role=Role.STAFF, kind=RequestKind.CHECK,
            priority=Priority.NORMAL, topic="pain_check",
            text="[시연 예시] A님이 통증('다리가 조금 아파요')을 호소해 보행 검사를 중단했습니다. 확인 부탁드립니다.",
        ),
        AgentRequest(
            resident_id=residents["B"].id, target_role=Role.STAFF, kind=RequestKind.ALERT,
            priority=Priority.URGENT, topic="emergency",
            text="[시연 예시] B님 안전 신호(fall) 감지. 검사를 중단하고 영상통화를 연결합니다.",
        ),
    ]


async def seed_if_empty() -> None:
    async with SessionLocal() as db:
        if await db.scalar(select(func.count()).select_from(User)):
            return
    await seed()


async def _seed_and_dispose() -> None:
    await seed()
    await engine.dispose()


def reset() -> None:
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    asyncio.run(_seed_and_dispose())


if __name__ == "__main__":
    if sys.argv[1:] != ["--reset"]:
        sys.exit("usage: python -m app.seed --reset")
    reset()
