"""세션 상태와 모든 흐름이 함께 쓰는 기기·앱 전송, 기록, 요청 생성."""

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select, update

from .. import llm
from ..config import settings
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
from ..twin.monitor import monitor
from ..voice import with_audio
from .ai import SYSTEM, Context, Decision, accept, build_prompt

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
    trial_attempt: int = 0
    call_queue: list[tuple[int, str]] = field(default_factory=list)
    gate: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    said: asyncio.Event = field(default_factory=asyncio.Event)
    awaiting_say: str | None = None
    confirmed: bool = False
    dialog: list[tuple[str, str]] = field(default_factory=list)
    # 그만할지 되묻는 동안 멈춘 측정(조건, 시도). 계속하면 여기서 다시 시작한다.
    paused: tuple[str, int] | None = None
    last_decision: dict[str, Any] | None = None
    # AI가 발화를 판단하는 작업과, 그동안 들어와 다음 판단에 한데 넘길 말 덩어리들(care.py)
    listener: asyncio.Task[None] | None = None
    heard: list[str] = field(default_factory=list)


class AgentCore:
    def __init__(self) -> None:
        self.state: SessionState | None = None
        self._temi_lines: deque[tuple[float, str]] = deque(maxlen=20)
        # trial_id → 서버가 그 걷기의 녹음을 받아 적고 채점을 마쳤음(api.upload_trial_speech가 알린다)
        self._speech_done: dict[int, asyncio.Event] = {}

    def speech_scored(self, trial_id: int) -> None:
        self._speech_done.setdefault(trial_id, asyncio.Event()).set()

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

    def _cancel_listener(self) -> None:
        st = self.state
        if st is None:
            return
        st.heard.clear()
        if st.listener is not None and st.listener is not asyncio.current_task():
            st.listener.cancel()
        st.listener = None

    async def _finish(self, status: SessionStatus, reason: str | None) -> None:
        st = self._require_state()
        self._cancel_gate()
        self._cancel_listener()
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

    async def _decide(self, point: str, situation: str, actions: Mapping[str, str]) -> Decision | None:
        """AI에게 판단을 맡긴다. 꺼져 있거나 실패하거나 가드레일 밖이면 None이고, 호출한 흐름은 규칙대로 한다."""
        st = self.state
        if st is None or not self._ai_on():
            return None
        prompt = build_prompt(situation, actions, await self._ai_context(st))
        decision = accept(await llm.decide(SYSTEM, prompt, list(actions), timeout=settings.agent_ai_timeout_s), actions)
        record = {"point": point, "by": "ai" if decision else "rule", "situation": situation,
                  **(asdict(decision) if decision else {})}
        st.last_decision = record
        log.info("AI decision %s by %s: %s", point, record["by"], {k: v for k, v in record.items() if k != "situation"})
        await monitor.record("note", "ai", make("ai.decision", record, session_id=st.session_id))
        return decision

    def _ai_on(self) -> bool:
        return settings.agent_ai_enabled and llm.available()

    async def _line(self, point: str, situation: str, default: str) -> str:
        """판단 없이 말만 하는 지점. AI가 쓴 말, 없으면 규칙 대사."""
        decision = await self._decide(point, f"{situation} 규칙 대사라면: '{default}'", {"say": "말만 한다"})
        return decision.say if decision else default

    async def _ai_context(self, st: SessionState) -> Context:
        async with SessionLocal() as db:
            notes = list(await db.scalars(
                select(Observation.text).where(Observation.resident_id == st.resident_id)
                .order_by(Observation.id.desc()).limit(5)
            ))
        now = f"단계 {st.mode}"
        if st.trial_condition is not None:
            now += f" · 조건 {st.trial_condition} {st.trial_attempt}회째" + (" · 측정 중" if st.trial_id else "")
        return Context(st.resident_name, now, st.dialog, notes)

    def _require_state(self) -> SessionState:
        if self.state is None:
            raise RuntimeError("no active session")
        return self.state

    async def _temi(self, type_: str, payload: dict[str, Any]) -> str:
        msg = make(type_, with_audio(type_, payload), session_id=self.state.session_id if self.state else None)
        if type_ in ("temi.say", "temi.ask"):
            self._temi_lines.append((time.monotonic(), payload["text"]))
            if self.state is not None:
                self.state.dialog.append(("Temi", payload["text"]))
        await hub.send_device("temi", msg)
        return msg["id"]

    async def _pi(self, type_: str, payload: dict[str, Any]) -> None:
        await hub.send_device("pi", make(type_, payload, session_id=self.state.session_id if self.state else None))



def _log_task_error(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("agent task failed", exc_info=exc)
