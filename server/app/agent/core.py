"""세션 상태와 모든 흐름이 함께 쓰는 기기·앱 전송, 기록, 요청 생성."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..db import SessionLocal
from ..events import notify_resident
from ..hub import hub
from ..models import (
    AgentRequest,
    Observation,
    ObservationSource,
    Priority,
    RequestKind,
    Resident,
    Role,
    SessionStatus,
    TestSession,
)
from ..protocol import make
from ..schemas import request_out

log = logging.getLogger(__name__)




@dataclass
class SessionState:
    session_id: int
    resident_id: int
    resident_name: str
    step: int = 0
    mode: str = "walk"
    trial_id: int | None = None
    trial_condition: str | None = None
    call_queue: list[tuple[int, str]] = field(default_factory=list)
    gate: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    said: asyncio.Event = field(default_factory=asyncio.Event)
    awaiting_say: str | None = None
    confirmed: bool = False


class AgentCore:
    def __init__(self) -> None:
        self.state: SessionState | None = None

    def _cancel_gate(self) -> None:
        st = self.state
        if st is None or st.gate is None:
            return
        if st.gate is not asyncio.current_task():
            st.gate.cancel()
        st.gate = None

    async def _finish(self, status: SessionStatus, reason: str | None) -> None:
        st = self._require_state()
        self._cancel_gate()
        async with SessionLocal() as db:
            session = await db.get(TestSession, st.session_id)
            assert session is not None
            session.status = status
            session.end_reason = reason
            session.ended_at = datetime.now(UTC)
            await db.commit()
        self.state = None
        await self._temi("temi.show", {"screen": "idle"})
        await notify_resident("session.ended", {"session_id": st.session_id, "status": status.value,
                                                "end_reason": reason}, st.resident_id)

    async def _observe(self, resident_id: int, source: ObservationSource, text: str, *,
                       author_id: int | None = None, data: dict[str, Any] | None = None) -> None:
        async with SessionLocal() as db:
            obs = Observation(resident_id=resident_id, source=source, author_id=author_id, text=text, data=data or {})
            db.add(obs)
            await db.commit()
        await notify_resident("observation.created", {"observation_id": obs.id}, resident_id)

    async def _create_request(self, resident_id: int, role: Role, kind: RequestKind, priority: Priority, topic: str,
                              text: str, *, options: list[str] | None = None, explanation: str | None = None) -> None:
        async with SessionLocal() as db:
            target_user_id = None
            if role == Role.GUARDIAN:
                resident = await db.get(Resident, resident_id)
                target_user_id = resident.guardian_id if resident else None
            req = AgentRequest(
                resident_id=resident_id,
                session_id=self.state.session_id if self.state else None,
                target_role=role,
                target_user_id=target_user_id,
                kind=kind,
                priority=priority,
                topic=topic,
                text=text,
                explanation=explanation,
                options=options or [],
            )
            db.add(req)
            await db.commit()
        await hub.send_apps(make("request.created", request_out(req).model_dump(mode="json")), role=role, user_id=target_user_id)

    def _require_state(self) -> SessionState:
        if self.state is None:
            raise RuntimeError("no active session")
        return self.state

    async def _temi(self, type_: str, payload: dict[str, Any]) -> str:
        msg = make(type_, payload, session_id=self.state.session_id if self.state else None)
        await hub.send_device("temi", msg)
        return msg["id"]

    async def _pi(self, type_: str, payload: dict[str, Any]) -> None:
        await hub.send_device("pi", make(type_, payload, session_id=self.state.session_id if self.state else None))



def _log_task_error(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("start gate failed", exc_info=exc)
