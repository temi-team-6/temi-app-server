from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .analysis import TimeOfDay
from .models import (
    AgentRequest,
    Condition,
    GameType,
    ObservationSource,
    Priority,
    RequestKind,
    RequestStatus,
    Role,
    SessionStatus,
    TrialStatus,
)


class UserOut(BaseModel):
    id: int
    username: str
    name: str
    role: Role


class LoginOut(BaseModel):
    access_token: str
    user: UserOut


class ResidentOut(BaseModel):
    id: int
    name: str
    rfid_tag: str
    birth_year: int | None
    guardian_id: int | None


class DualTaskCost(BaseModel):
    animals: float | None
    countdown: float | None


class GameSummary(BaseModel):
    game_type: GameType
    accuracy: float | None
    rt_mean_ms: float | None
    rt_sd_ms: float | None


class SessionSummary(BaseModel):
    session_id: int
    started_at: datetime
    time_of_day: TimeOfDay
    status: SessionStatus
    synthetic: bool
    single_speed_mps: float | None
    dual_task_cost: DualTaskCost
    invalid_trials: int
    games: list[GameSummary]


class TrialOut(BaseModel):
    id: int
    condition: Condition
    attempt: int
    status: TrialStatus
    invalid_reason: str | None
    speed_mps: float | None
    word_count: int | None


class SessionDetail(SessionSummary):
    end_reason: str | None
    trials: list[TrialOut]


class ObservationOut(BaseModel):
    id: int
    source: ObservationSource
    text: str
    data: dict[str, Any]
    created_at: datetime


class CreatedOut(BaseModel):
    id: int


class RequestOut(BaseModel):
    id: int
    resident_id: int | None
    session_id: int | None
    target_role: Role
    kind: RequestKind
    priority: Priority
    topic: str
    text: str
    options: list[str]
    status: RequestStatus
    answer: str | None
    created_at: datetime
    answered_at: datetime | None


class ChatOut(BaseModel):
    reply: str
    generated_at: datetime
    mock: bool


class StartSessionOut(BaseModel):
    session_id: int


class HealthOut(BaseModel):
    ok: bool
    devices: list[str]
    app_clients: int


def request_out(r: AgentRequest) -> RequestOut:
    return RequestOut(
        id=r.id,
        resident_id=r.resident_id,
        session_id=r.session_id,
        target_role=r.target_role,
        kind=r.kind,
        priority=r.priority,
        topic=r.topic,
        text=r.text,
        options=r.options,
        status=r.status,
        answer=r.answer,
        created_at=r.created_at,
        answered_at=r.answered_at,
    )
