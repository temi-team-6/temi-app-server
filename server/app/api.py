from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import chat as chatbot
from .agent import agent
from .auth import current_user, issue_token, verify_password
from .config import settings
from .db import get_db
from .events import notify_resident
from .hub import hub
from .models import (
    AgentRequest,
    Observation,
    ObservationSource,
    RequestStatus,
    Resident,
    Role,
    User,
)
from .queries import resident_sessions, session_summary
from .schemas import (
    ChatMessageOut,
    ChatOut,
    CreatedOut,
    HealthOut,
    LoginOut,
    ObservationOut,
    RequestOut,
    ResidentOut,
    SessionDetail,
    SessionSummary,
    StartSessionOut,
    TrialOut,
    UserOut,
    request_out,
)

router = APIRouter(prefix="/api", generate_unique_id_function=lambda route: route.name)

class LoginIn(BaseModel):
    username: str
    password: str


class AnswerIn(BaseModel):
    answer: str


class ObservationIn(BaseModel):
    text: str


class ChatIn(BaseModel):
    resident_id: int
    message: str


class StartSessionIn(BaseModel):
    resident_id: int


def _user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, name=u.name, role=u.role)


def _resident_out(r: Resident) -> ResidentOut:
    return ResidentOut(id=r.id, name=r.name, rfid_tag=r.rfid_tag, birth_year=r.birth_year, guardian_id=r.guardian_id)


async def _visible_resident(db: AsyncSession, resident_id: int, user: User) -> Resident:
    resident = await db.get(Resident, resident_id)
    if resident is None or (user.role == Role.GUARDIAN and resident.guardian_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "resident not found")
    return resident


@router.get("/health", tags=["system"])
async def health() -> HealthOut:
    return HealthOut(ok=True, devices=sorted(hub.devices), app_clients=len(hub.apps), commit=settings.git_commit)


@router.post("/auth/login", tags=["auth"])
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)) -> LoginOut:
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    return LoginOut(access_token=issue_token(user), user=_user_out(user))


@router.get("/me", tags=["auth"])
async def get_me(user: User = Depends(current_user)) -> UserOut:
    return _user_out(user)


@router.get("/residents", tags=["residents"])
async def list_residents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[ResidentOut]:
    query = select(Resident).order_by(Resident.id)
    if user.role == Role.GUARDIAN:
        query = query.where(Resident.guardian_id == user.id)
    return [_resident_out(r) for r in await db.scalars(query)]


@router.get("/residents/{resident_id}", tags=["residents"])
async def get_resident(resident_id: int, user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)) -> ResidentOut:
    return _resident_out(await _visible_resident(db, resident_id, user))


@router.get("/residents/{resident_id}/trend", tags=["residents"])
async def get_trend(resident_id: int, user: User = Depends(current_user),
                db: AsyncSession = Depends(get_db)) -> list[SessionSummary]:
    await _visible_resident(db, resident_id, user)
    return [session_summary(s) for s in await resident_sessions(db, resident_id)]


@router.get("/residents/{resident_id}/sessions/{session_id}", tags=["residents"])
async def get_session(resident_id: int, session_id: int, user: User = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> SessionDetail:
    await _visible_resident(db, resident_id, user)
    session = next((s for s in await resident_sessions(db, resident_id) if s.id == session_id), None)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return SessionDetail(
        **session_summary(session).model_dump(),
        end_reason=session.end_reason,
        trials=[
            TrialOut(
                id=t.id,
                condition=t.condition,
                attempt=t.attempt,
                status=t.status,
                invalid_reason=t.invalid_reason,
                speed_mps=t.speed_mps,
                word_count=t.word_count,
            )
            for t in session.trials
        ],
    )


@router.get("/residents/{resident_id}/observations", tags=["residents"])
async def list_observations(resident_id: int, user: User = Depends(current_user),
                            db: AsyncSession = Depends(get_db)) -> list[ObservationOut]:
    await _visible_resident(db, resident_id, user)
    rows = await db.scalars(
        select(Observation).where(Observation.resident_id == resident_id).order_by(Observation.created_at.desc())
    )
    return [ObservationOut(id=o.id, source=o.source, text=o.text, data=o.data, created_at=o.created_at) for o in rows]


@router.post("/residents/{resident_id}/observations", status_code=status.HTTP_201_CREATED, tags=["residents"])
async def add_observation(resident_id: int, body: ObservationIn, user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_db)) -> CreatedOut:
    await _visible_resident(db, resident_id, user)
    obs = Observation(resident_id=resident_id, source=ObservationSource(user.role.value), author_id=user.id,
                      text=body.text)
    db.add(obs)
    await db.commit()
    await notify_resident("observation.created", {"observation_id": obs.id}, resident_id)
    return CreatedOut(id=obs.id)


@router.get("/requests", tags=["requests"])
async def list_requests(status_: RequestStatus | None = Query(None, alias="status"), user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> list[RequestOut]:
    query = select(AgentRequest).where(AgentRequest.target_role == user.role).order_by(AgentRequest.created_at.desc())
    if user.role == Role.GUARDIAN:
        query = query.where(AgentRequest.target_user_id == user.id)
    if status_ is not None:
        query = query.where(AgentRequest.status == status_)
    return [request_out(r) for r in await db.scalars(query)]


@router.post("/requests/{request_id}/answer", tags=["requests"])
async def answer_request(request_id: int, body: AnswerIn, user: User = Depends(current_user),
                         db: AsyncSession = Depends(get_db)) -> RequestOut:
    req = await db.get(AgentRequest, request_id)
    if req is None or req.target_role != user.role or (
        user.role == Role.GUARDIAN and req.target_user_id != user.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")
    if req.status != RequestStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "request already closed")
    return request_out(await agent.answer_request(request_id, body.answer, user))


@router.post("/chat", tags=["chat"])
async def chat(body: ChatIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> ChatOut:
    resident = await _visible_resident(db, body.resident_id, user)
    db.add(Observation(resident_id=resident.id, source=ObservationSource(user.role.value), author_id=user.id,
                       text=f"챗봇 질문: {body.message}"))
    await db.commit()
    reply = await chatbot.answer(db, resident, user.id, body.message)
    return ChatOut(reply=reply.text, generated_at=reply.created_at, mock=reply.mock)


@router.get("/residents/{resident_id}/chat", tags=["chat"])
async def list_chat_messages(resident_id: int, user: User = Depends(current_user),
                             db: AsyncSession = Depends(get_db)) -> list[ChatMessageOut]:
    """로그인한 계정이 이 어르신에 대해 나눈 대화(오래된 순). 다른 계정의 대화는 보이지 않는다."""
    await _visible_resident(db, resident_id, user)
    return [ChatMessageOut(id=m.id, role=m.role, text=m.text, mock=m.mock, created_at=m.created_at)
            for m in await chatbot.history(db, resident_id, user.id)]


@router.post("/dev/sessions/start", tags=["dev"])
async def dev_start_session(body: StartSessionIn, user: User = Depends(current_user)) -> StartSessionOut:
    if user.role != Role.STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "staff only")
    try:
        return StartSessionOut(session_id=await agent.start_session(body.resident_id))
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
