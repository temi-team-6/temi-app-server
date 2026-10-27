from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analysis import dual_task_cost, time_of_day
from .models import Condition, TestSession, TrialStatus
from .schemas import DualTaskCost, GameSummary, SessionSummary


async def resident_sessions(db: AsyncSession, resident_id: int) -> list[TestSession]:
    rows = await db.scalars(
        select(TestSession).where(TestSession.resident_id == resident_id).order_by(TestSession.started_at)
    )
    return list(rows)


def session_summary(s: TestSession) -> SessionSummary:
    valid_single = [t.speed_mps for t in s.trials if t.status == TrialStatus.VALID and t.condition == Condition.SINGLE]
    return SessionSummary(
        session_id=s.id,
        started_at=s.started_at,
        time_of_day=time_of_day(s.started_at),
        status=s.status,
        synthetic=s.synthetic,
        single_speed_mps=valid_single[-1] if valid_single else None,
        dual_task_cost=DualTaskCost.model_validate(dual_task_cost(s.trials)),
        invalid_trials=sum(t.status == TrialStatus.INVALID for t in s.trials),
        games=[
            GameSummary(game_type=g.game_type, accuracy=g.accuracy, rt_mean_ms=g.rt_mean_ms, rt_sd_ms=g.rt_sd_ms)
            for g in s.games
            if g.accuracy is not None
        ],
    )
