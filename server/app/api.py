from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent import agent
from .auth import current_user, issue_token, verify_password
from .db import get_db
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

router = APIRouter(prefix="/api")

CONSULT_GUIDE = "치매상담콜센터(1899-9988)나 가까운 치매안심센터에서 상담을 받으실 수 있습니다."


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


@router.get("/health")
async def health() -> HealthOut:
    return HealthOut(ok=True, devices=sorted(hub.devices), app_clients=len(hub.apps))


@router.post("/auth/login")
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)) -> LoginOut:
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    return LoginOut(access_token=issue_token(user), user=_user_out(user))


@router.get("/me")
async def me(user: User = Depends(current_user)) -> UserOut:
    return _user_out(user)


@router.get("/residents")
async def list_residents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[ResidentOut]:
    query = select(Resident).order_by(Resident.id)
    if user.role == Role.GUARDIAN:
        query = query.where(Resident.guardian_id == user.id)
    return [_resident_out(r) for r in await db.scalars(query)]


@router.get("/residents/{resident_id}")
async def get_resident(resident_id: int, user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)) -> ResidentOut:
    return _resident_out(await _visible_resident(db, resident_id, user))


@router.get("/residents/{resident_id}/trend")
async def trend(resident_id: int, user: User = Depends(current_user),
                db: AsyncSession = Depends(get_db)) -> list[SessionSummary]:
    await _visible_resident(db, resident_id, user)
    return [session_summary(s) for s in await resident_sessions(db, resident_id)]


@router.get("/residents/{resident_id}/sessions/{session_id}")
async def session_detail(resident_id: int, session_id: int, user: User = Depends(current_user),
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


@router.get("/residents/{resident_id}/observations")
async def list_observations(resident_id: int, user: User = Depends(current_user),
                            db: AsyncSession = Depends(get_db)) -> list[ObservationOut]:
    await _visible_resident(db, resident_id, user)
    rows = await db.scalars(
        select(Observation).where(Observation.resident_id == resident_id).order_by(Observation.created_at.desc())
    )
    return [ObservationOut(id=o.id, source=o.source, text=o.text, data=o.data, created_at=o.created_at) for o in rows]


@router.post("/residents/{resident_id}/observations", status_code=status.HTTP_201_CREATED)
async def add_observation(resident_id: int, body: ObservationIn, user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_db)) -> CreatedOut:
    await _visible_resident(db, resident_id, user)
    obs = Observation(resident_id=resident_id, source=ObservationSource(user.role.value), author_id=user.id,
                      text=body.text)
    db.add(obs)
    await db.commit()
    return CreatedOut(id=obs.id)


@router.get("/requests")
async def list_requests(status_: RequestStatus | None = Query(None, alias="status"), user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_db)) -> list[RequestOut]:
    query = select(AgentRequest).where(AgentRequest.target_role == user.role).order_by(AgentRequest.created_at.desc())
    if user.role == Role.GUARDIAN:
        query = query.where(AgentRequest.target_user_id == user.id)
    if status_ is not None:
        query = query.where(AgentRequest.status == status_)
    return [request_out(r) for r in await db.scalars(query)]


@router.post("/requests/{request_id}/answer")
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


@router.post("/chat")
async def chat(body: ChatIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> ChatOut:
    resident = await _visible_resident(db, body.resident_id, user)
    db.add(Observation(resident_id=resident.id, source=ObservationSource(user.role.value), author_id=user.id,
                       text=f"챗봇 질문: {body.message}"))
    await db.commit()
    recent = [s for s in await resident_sessions(db, resident.id) if s.trials][-4:]
    costs = [session_summary(s).dual_task_cost.animals for s in recent]
    trend_text = ", ".join(f"{c}%" for c in costs if c is not None) or "아직 기록이 부족합니다"
    reply = (
        f"말씀하신 내용은 관찰 기록으로 남겨 다음 검사 계획에 반영하겠습니다. "
        f"이 서비스는 진단을 하지 않습니다. 최근 걷기 검사의 이중과제 비용은 {trend_text}입니다. "
        f"{CONSULT_GUIDE} 언제부터, 얼마나 자주 그런 모습이 보이셨나요?"
    )
    return ChatOut(reply=reply, generated_at=datetime.now().astimezone(), mock=True)


@router.post("/dev/sessions/start")
async def dev_start_session(body: StartSessionIn, _: User = Depends(current_user)) -> StartSessionOut:
    try:
        return StartSessionOut(session_id=await agent.start_session(body.resident_id))
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
