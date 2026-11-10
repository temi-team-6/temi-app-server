"""세션 상태와 모든 흐름이 함께 쓰는 기기·앱 전송, 기록, 요청 생성."""

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select, update

from ..db import SessionLocal
from ..events import notify_resident
from ..hub import hub
from ..models import (
    AgentRequest,
    GameResult,
    GameStatus,
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
)
from ..protocol import make
from ..schemas import request_out

log = logging.getLogger(__name__)

# Pi 마이크는 Temi 위에 있어 Temi가 한 말도 받아 적는다. 받아 적기는 업로드·인식 때문에 말이 끝난 뒤에 도착하므로,
# 시각이 아니라 글자로 거른다: 최근 Temi 문장과 거의 같으면 참가자 발화가 아니다.
ECHO_WINDOW_S = 15.0
ECHO_SIMILARITY = 0.6


def _letters(text: str) -> str:
    return re.sub(r"[\W_]+", "", text)




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
        self._temi_lines: deque[tuple[float, str]] = deque(maxlen=20)

    def _is_temi_echo(self, heard: str) -> bool:
        heard_letters = _letters(heard)
        if len(heard_letters) < 2:
            return False
        since = time.monotonic() - ECHO_WINDOW_S
        for said_at, line in self._temi_lines:
            said = _letters(line)
            if said_at < since or not said:
                continue
            if len(heard_letters) >= 4 and heard_letters in said:
                return True
            if SequenceMatcher(None, heard_letters, said).ratio() >= ECHO_SIMILARITY:
                return True
        return False

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

    async def close_orphaned_sessions(self, reason: str) -> int:
        """에이전트가 진행하지 않는데 DB에 '진행 중'으로 남은 검사를 중단으로 닫는다.

        진행 상태는 메모리에만 있어 서버가 재시작되면 이어 갈 수 없다. 그대로 두면 앱 달력·리포트에
        끝나지 않는 검사로 남으므로, 시작할 때와 새 검사를 열기 전에 정리한다. 닫은 검사 수를 돌려준다.
        """
        current = self.state.session_id if self.state else None
        now = datetime.now(UTC)
        async with SessionLocal() as db:
            ids = list(await db.scalars(
                select(TestSession.id).where(TestSession.status == SessionStatus.ACTIVE, TestSession.id != current)
            ))
            if not ids:
                return 0
            await db.execute(update(Trial).where(Trial.session_id.in_(ids), Trial.status == TrialStatus.RUNNING)
                             .values(status=TrialStatus.ABORTED, invalid_reason=reason[:50], completed_at=now))
            await db.execute(update(GameResult).where(GameResult.session_id.in_(ids),
                                                      GameResult.status == GameStatus.RUNNING)
                             .values(status=GameStatus.ABORTED, completed_at=now))
            await db.execute(update(TestSession).where(TestSession.id.in_(ids))
                             .values(status=SessionStatus.ABORTED, end_reason=reason, ended_at=now))
            await db.commit()
        log.warning("closed %d orphaned session(s) as %s: %s", len(ids), reason, ids)
        return len(ids)

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
        if type_ in ("temi.say", "temi.ask"):
            self._temi_lines.append((time.monotonic(), payload["text"]))
        await hub.send_device("temi", msg)
        return msg["id"]

    async def _pi(self, type_: str, payload: dict[str, Any]) -> None:
        await hub.send_device("pi", make(type_, payload, session_id=self.state.session_id if self.state else None))



def _log_task_error(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("start gate failed", exc_info=exc)
