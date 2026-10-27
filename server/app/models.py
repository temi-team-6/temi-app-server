from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _enum(e: type[StrEnum]) -> Enum:
    # DB에는 소문자 문자열로 저장한다 (PG enum 타입 마이그레이션 부담을 피한다)
    return Enum(e, native_enum=False, length=30, values_callable=lambda x: [m.value for m in x])


def _now() -> datetime:
    return datetime.now(UTC)


class Role(StrEnum):
    GUARDIAN = "guardian"
    STAFF = "staff"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABORTED = "aborted"


class Condition(StrEnum):
    SINGLE = "single"
    ANIMALS = "animals"
    COUNTDOWN = "countdown"


class TrialStatus(StrEnum):
    RUNNING = "running"
    VALID = "valid"
    INVALID = "invalid"
    ABORTED = "aborted"


class GameType(StrEnum):
    MEMORY = "memory"
    STROOP = "stroop"
    WHACK = "whack"


class GameStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


class RequestKind(StrEnum):
    QUESTION = "question"
    CHECK = "check"
    ALERT = "alert"


class Priority(StrEnum):
    URGENT = "urgent"
    NORMAL = "normal"
    DIGEST = "digest"


class RequestStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"


class ObservationSource(StrEnum):
    AGENT = "agent"
    GUARDIAN = "guardian"
    STAFF = "staff"
    SYSTEM = "system"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(50))
    role: Mapped[Role] = mapped_column(_enum(Role))
    call_priority: Mapped[int | None] = mapped_column()


class Resident(Base):
    __tablename__ = "residents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    rfid_tag: Mapped[str] = mapped_column(String(64), unique=True)
    birth_year: Mapped[int | None] = mapped_column()
    guardian_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class TestSession(Base):
    __tablename__ = "test_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[SessionStatus] = mapped_column(_enum(SessionStatus), default=SessionStatus.ACTIVE)
    end_reason: Mapped[str | None] = mapped_column(String(200))
    synthetic: Mapped[bool] = mapped_column(default=False)

    trials: Mapped[list["Trial"]] = relationship(lazy="selectin", order_by="Trial.id")
    games: Mapped[list["GameResult"]] = relationship(lazy="selectin", order_by="GameResult.id")


class Trial(Base):
    __tablename__ = "trials"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("test_sessions.id"), index=True)
    condition: Mapped[Condition] = mapped_column(_enum(Condition))
    attempt: Mapped[int] = mapped_column(default=1)
    status: Mapped[TrialStatus] = mapped_column(_enum(TrialStatus), default=TrialStatus.RUNNING)
    invalid_reason: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    speed_mps: Mapped[float | None] = mapped_column()
    duration_s: Mapped[float | None] = mapped_column()
    stopped: Mapped[bool | None] = mapped_column()
    max_persons: Mapped[int | None] = mapped_column()
    min_confidence: Mapped[float | None] = mapped_column()
    word_count: Mapped[int | None] = mapped_column()
    error_count: Mapped[int | None] = mapped_column()
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class GameResult(Base):
    __tablename__ = "game_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("test_sessions.id"), index=True)
    game_type: Mapped[GameType] = mapped_column(_enum(GameType))
    level: Mapped[int] = mapped_column(default=1)
    status: Mapped[GameStatus] = mapped_column(_enum(GameStatus), default=GameStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rt_ms: Mapped[list[float]] = mapped_column(JSON, default=list)
    correct: Mapped[list[bool]] = mapped_column(JSON, default=list)
    accuracy: Mapped[float | None] = mapped_column()
    rt_mean_ms: Mapped[float | None] = mapped_column()
    rt_sd_ms: Mapped[float | None] = mapped_column()
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentRequest(Base):
    __tablename__ = "agent_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int | None] = mapped_column(ForeignKey("residents.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("test_sessions.id"))
    target_role: Mapped[Role] = mapped_column(_enum(Role))
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[RequestKind] = mapped_column(_enum(RequestKind))
    priority: Mapped[Priority] = mapped_column(_enum(Priority), default=Priority.NORMAL)
    topic: Mapped[str] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[RequestStatus] = mapped_column(_enum(RequestStatus), default=RequestStatus.PENDING)
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    resident_id: Mapped[int] = mapped_column(ForeignKey("residents.id"), index=True)
    source: Mapped[ObservationSource] = mapped_column(_enum(ObservationSource))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
