import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from . import analysis
from .config import settings
from .db import SessionLocal
from .hub import hub
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
    RequestStatus,
    Resident,
    Role,
    SessionStatus,
    TestSession,
    Trial,
    TrialStatus,
    User,
)
from .protocol import (
    EVENT_PAYLOADS,
    Arrived,
    Envelope,
    GameCompleted,
    RfidScanned,
    SafetyAlert,
    SayDone,
    SpeechHeard,
    TrialCompleted,
    TrialReady,
    VideoCallResult,
    make,
)
from .queries import resident_sessions
from .schemas import request_out

log = logging.getLogger(__name__)

WALK_PLAN = (Condition.SINGLE, Condition.ANIMALS, Condition.COUNTDOWN)
MAX_ATTEMPTS = 3

INSTRUCTION = {
    Condition.SINGLE: "시작선에 서 주세요. 신호가 나면 평소 속도로 끝선까지 걸어가세요.",
    Condition.ANIMALS: "이번에는 걸으면서 생각나는 동물 이름을 계속 소리 내어 말해 주세요.",
    Condition.COUNTDOWN: "이번에는 걸으면서 100부터 3씩 거꾸로 세어 주세요.",
}
RETRY_SAY = {
    "stopped": "중간에 멈추셨네요. 끝선까지 멈추지 말고 걸어 주세요.",
    "out_of_frame": "제가 잘 보지 못했어요. 표시된 선 안쪽으로 걸어 주세요.",
    "multiple_persons": "다른 분이 지나가셔서 다시 측정할게요.",
    "task_not_engaged": "말씀이 잘 들리지 않았어요. 걸으면서 소리 내어 계속 말해 주세요.",
    "no_measurement": "측정이 잘 되지 않았어요.",
    "no_person": "시작선 앞에 서 계신 모습이 보이지 않아요.",
    "early_start": "출발 신호를 듣고 걸어 주세요.",
    "interrupted": "다시 처음부터 해볼게요.",
}
PAIN_WORDS = ("아파", "아퍼", "아프", "통증")
SEVERE_WORDS = ("못 일어", "일어설 수가 없", "일어날 수가 없", "못 움직", "많이 아파")
REFUSE_WORDS = ("하기 싫", "안 할래", "그만할래", "그만 할래")
SLEEP_WORDS = ("잠", "피곤", "컨디션")
DECLINE_WORDS = ("헤매", "깜빡", "반복", "잊어")


@dataclass
class SessionState:
    session_id: int
    resident_id: int
    resident_name: str
    step: int = 0
    mode: str = "walk"
    trial_id: int | None = None
    call_queue: list[tuple[int, str]] = field(default_factory=list)
    gate: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    said: asyncio.Event = field(default_factory=asyncio.Event)
    awaiting_say: str | None = None


class Agent:
    """규칙 기반 임시 에이전트. 이벤트 입출력은 실제 LLM 에이전트와 같고, 판단만 고정 규칙이다."""

    def __init__(self) -> None:
        self.state: SessionState | None = None

    async def handle(self, device: str, env: Envelope) -> None:
        if env.type == "ping":
            await hub.send_device(device, make("pong", reply_to=env.id))
            return
        model = EVENT_PAYLOADS.get(env.type)
        if model is None:
            await hub.send_device(device, make("error", {"message": f"unknown type {env.type}"}, reply_to=env.id))
            return
        try:
            payload: BaseModel = model.model_validate(env.payload)
        except ValidationError as e:
            await hub.send_device(device, make("error", {"message": str(e)}, reply_to=env.id))
            return
        match payload:
            case RfidScanned(tag=tag):
                await self._on_rfid(tag)
            case TrialReady(trial_id=trial_id):
                self._on_trial_ready(trial_id)
            case SayDone():
                self._on_say_done(env.reply_to)
            case TrialCompleted():
                await self._on_trial_completed(payload)
            case GameCompleted():
                await self._on_game_completed(payload)
            case SpeechHeard(text=text):
                await self._on_speech(text)
            case SafetyAlert():
                await self._on_safety(payload)
            case VideoCallResult():
                await self._on_video_call_result(payload)
            case Arrived():
                log.info("temi arrived at %s", payload.location)

    async def start_session(self, resident_id: int) -> int:
        async with SessionLocal() as db:
            resident = await db.get(Resident, resident_id)
            if resident is None:
                raise LookupError(f"resident {resident_id} not found")
            if self.state is not None:
                await self._finish(SessionStatus.ABORTED, "replaced_by_new_session")
            session = TestSession(resident_id=resident.id)
            db.add(session)
            await db.commit()
            self.state = SessionState(session.id, resident.id, resident.name)
        await self._temi("temi.say", {"text": f"{resident.name}님, 안녕하세요. 오늘도 걷기 검사를 해볼게요."})
        await self._start_trial(WALK_PLAN[0], attempt=1)
        return self.state.session_id

    async def answer_request(self, request_id: int, answer: str, user: User) -> AgentRequest:
        async with SessionLocal() as db:
            req = await db.get(AgentRequest, request_id)
            if req is None:
                raise LookupError(f"request {request_id} not found")
            req.answer = answer
            req.answered_by = user.id
            req.answered_at = datetime.now(UTC)
            req.status = RequestStatus.ANSWERED
            await db.commit()
        await hub.send_apps(make("request.updated", request_out(req).model_dump(mode="json")), role=req.target_role)
        if req.resident_id is not None and req.topic == "condition_check":
            await self._judge_guardian_answer(req.resident_id, answer)
        elif req.resident_id is not None:
            await self._observe(req.resident_id, ObservationSource(user.role.value), answer, author_id=user.id)
        return req

    async def _on_rfid(self, tag: str) -> None:
        async with SessionLocal() as db:
            resident = await db.scalar(select(Resident).where(Resident.rfid_tag == tag))
        if resident is None:
            await self._temi("temi.say", {"text": "등록되지 않은 카드예요. 선생님께 확인해 주세요."})
            return
        await self.start_session(resident.id)

    async def _start_trial(self, condition: Condition, attempt: int) -> None:
        st = self._require_state()
        async with SessionLocal() as db:
            trial = Trial(session_id=st.session_id, condition=condition, attempt=attempt)
            db.add(trial)
            await db.commit()
        self._cancel_gate()
        st.mode = "walk"
        st.trial_id = trial.id
        st.ready = asyncio.Event()
        await self._pi("trial.start", {"trial_id": trial.id, "condition": condition.value, "attempt": attempt})
        await self._temi("temi.show", {"screen": "walk", "condition": condition.value, "attempt": attempt})
        st.gate = asyncio.create_task(self._run_gate(trial.id, condition))
        st.gate.add_done_callback(_log_task_error)

    async def _run_gate(self, trial_id: int, condition: Condition) -> None:
        st = self._require_state()
        await self._say_and_wait(INSTRUCTION[condition])
        for prompt in range(settings.ready_prompts + 1):
            try:
                await asyncio.wait_for(st.ready.wait(), settings.ready_timeout_s)
                break
            except TimeoutError:
                if prompt == settings.ready_prompts:
                    await self._fail_trial(trial_id, "no_person")
                    return
                await self._say_and_wait("시작선 앞에 서 주세요.")
        if condition != Condition.SINGLE:
            await self._say_and_wait("지금부터 말하기를 시작하세요.")
            await asyncio.sleep(settings.lead_in_s)
        await self._temi("temi.say", {"text": "출발하세요."})
        await self._pi("trial.go", {"trial_id": trial_id})

    async def _say_and_wait(self, text: str) -> None:
        st = self._require_state()
        st.said = asyncio.Event()
        st.awaiting_say = await self._temi("temi.say", {"text": text})
        try:
            await asyncio.wait_for(st.said.wait(), settings.say_done_timeout_s)
        except TimeoutError:
            log.warning("temi.say_done not received within %ss, continuing", settings.say_done_timeout_s)

    def _cancel_gate(self) -> None:
        st = self.state
        if st is None or st.gate is None:
            return
        if st.gate is not asyncio.current_task():
            st.gate.cancel()
        st.gate = None

    def _on_trial_ready(self, trial_id: int) -> None:
        if self.state is not None and self.state.trial_id == trial_id:
            self.state.ready.set()

    def _on_say_done(self, reply_to: str | None) -> None:
        if self.state is not None and reply_to is not None and reply_to == self.state.awaiting_say:
            self.state.said.set()

    async def _fail_trial(self, trial_id: int, reason: str) -> None:
        st = self._require_state()
        st.trial_id = None
        await self._pi("trial.abort", {"trial_id": trial_id, "reason": reason})
        async with SessionLocal() as db:
            trial = await db.get(Trial, trial_id)
            assert trial is not None
            trial.status = TrialStatus.INVALID
            trial.invalid_reason = reason
            trial.completed_at = datetime.now(UTC)
            await db.commit()
        await self._after_trial(trial.condition, trial.attempt, reason)

    async def _on_trial_completed(self, m: TrialCompleted) -> None:
        st = self.state
        if st is None or st.trial_id != m.trial_id:
            log.warning("ignored trial.completed for inactive trial %s", m.trial_id)
            return
        self._cancel_gate()
        async with SessionLocal() as db:
            trial = await db.get(Trial, m.trial_id)
            assert trial is not None
            reason = analysis.invalid_reason(trial.condition, m)
            for key in ("speed_mps", "duration_s", "stopped", "max_persons", "min_confidence", "word_count",
                        "error_count", "raw"):
                setattr(trial, key, getattr(m, key))
            trial.completed_at = datetime.now(UTC)
            trial.status = TrialStatus.INVALID if reason else TrialStatus.VALID
            trial.invalid_reason = reason
            await db.commit()
        st.trial_id = None
        await self._after_trial(trial.condition, trial.attempt, reason)

    async def _after_trial(self, condition: Condition, attempt: int, reason: str | None) -> None:
        st = self._require_state()
        if reason is None:
            await self._next_condition()
        elif attempt < MAX_ATTEMPTS:
            await self._temi("temi.say", {"text": RETRY_SAY[reason] + " 다시 해볼게요."})
            await self._start_trial(condition, attempt + 1)
        else:
            await self._observe(st.resident_id, ObservationSource.AGENT,
                                f"{condition.value} 조건 측정이 {MAX_ATTEMPTS}회 무효({reason})여서 건너뜀")
            await self._temi("temi.say", {"text": "이 걷기는 오늘 건너뛸게요."})
            await self._next_condition()

    async def _next_condition(self) -> None:
        st = self._require_state()
        st.step += 1
        if st.step < len(WALK_PLAN):
            await self._start_trial(WALK_PLAN[st.step], attempt=1)
        else:
            await self._finish_walk()

    async def _finish_walk(self) -> None:
        st = self._require_state()
        async with SessionLocal() as db:
            sessions = await resident_sessions(db, st.resident_id)
        current = next(s for s in sessions if s.id == st.session_id)
        today = analysis.dual_task_cost(current.trials)
        tod = analysis.time_of_day(current.started_at)
        past = [s for s in sessions if s.id != st.session_id and s.status == SessionStatus.COMPLETED]
        same_tod = [s for s in past if analysis.time_of_day(s.started_at) == tod]
        reference = same_tod if len(same_tod) >= analysis.BASELINE_MIN_N else past
        checks: dict[str, Any] = {}
        for cond, value in today.items():
            if value is None:
                continue
            history = [v for s in reference if (v := analysis.dual_task_cost(s.trials)[cond]) is not None]
            checks[cond] = analysis.baseline_check(history, value)
        exceeded = [c for c, chk in checks.items() if chk["exceeded"]]
        data = {"dual_task_cost": today, "time_of_day": tod, "same_time_of_day": reference is same_tod,
                "checks": checks}
        if exceeded:
            await self._observe(st.resident_id, ObservationSource.AGENT,
                                f"이중과제 비용이 개인 기준보다 높음({', '.join(exceeded)}). 보호자에게 최근 상태를 묻는다.",
                                data=data)
            await self._create_request(
                st.resident_id, Role.GUARDIAN, RequestKind.QUESTION, Priority.NORMAL, "condition_check",
                f"{st.resident_name}님의 오늘 걷기 검사 결과가 평소와 조금 달랐습니다. "
                "이번 주 수면이나 컨디션에 변화가 있었나요?",
                options=["잠을 잘 못 주무셨어요", "평소와 같아요", "요즘 길을 자주 헤매세요"],
            )
        else:
            await self._observe(st.resident_id, ObservationSource.AGENT, "보행 검사 결과가 개인 기준 범위 안", data=data)
        await self._temi("temi.say", {"text": "오늘 걷기 검사를 모두 마쳤어요. 수고하셨어요."})
        await self._finish(SessionStatus.COMPLETED, None)

    async def _judge_guardian_answer(self, resident_id: int, answer: str) -> None:
        await self._observe(resident_id, ObservationSource.GUARDIAN, answer)
        if any(w in answer for w in DECLINE_WORDS):
            await self._observe(resident_id, ObservationSource.AGENT, "보호자 답변상 일상 변화가 보여 관찰을 이어감. 요양사에게 상담 안내 요청.")
            await self._create_request(resident_id, Role.STAFF, RequestKind.CHECK, Priority.NORMAL, "consult",
                                       f"보호자가 '{answer}'라고 답했습니다. 상담 기관 안내를 부탁드립니다.")
        elif any(w in answer for w in SLEEP_WORDS):
            await self._observe(resident_id, ObservationSource.AGENT, "수면·컨디션 영향 가능성이 있어 일시적 변화로 보고 다음 주 재측정.")
        else:
            await self._observe(resident_id, ObservationSource.AGENT, "특이 답변 없음. 다음 정기 검사에서 다시 비교.")

    async def _on_speech(self, text: str) -> None:
        st = self.state
        if st is None:
            return
        if st.mode == "pain_check":
            if any(w in text for w in SEVERE_WORDS):
                await self._temi("temi.say", {"text": "알겠어요. 바로 선생님과 연결할게요. 제가 옆에 있을게요."})
                await self._create_request(st.resident_id, Role.STAFF, RequestKind.ALERT, Priority.URGENT, "emergency",
                                           f"{st.resident_name}님이 '{text}'라고 하십니다. 영상통화를 연결합니다.")
                await self._start_video_call()
            else:
                await self._temi("temi.say", {"text": "그럼 걷기는 쉬고, 앉아서 하는 게임으로 바꿀게요."})
                await self._create_request(st.resident_id, Role.STAFF, RequestKind.CHECK, Priority.NORMAL, "pain_check",
                                           f"{st.resident_name}님이 통증('{text}')을 호소해 보행 검사를 중단했습니다. 확인 부탁드립니다.")
                await self._start_game(GameType.STROOP, level=1)
            return
        if any(w in text for w in PAIN_WORDS):
            await self._abort_trial("pain")
            st.mode = "pain_check"
            await self._temi("temi.ask", {"text": "어디가 얼마나 아프세요?"})
        elif any(w in text for w in REFUSE_WORDS):
            await self._abort_trial("refused")
            await self._observe(st.resident_id, ObservationSource.AGENT, f"참여 거부: '{text}'")
            await self._temi("temi.say", {"text": "알겠어요. 오늘은 쉬시고 다음에 다시 올게요."})
            await self._finish(SessionStatus.ABORTED, "refused")
        elif st.mode == "walk" and ("?" in text or text.rstrip().endswith(("돼요", "되나요", "해요"))):
            await self._temi("temi.say", {"text": "네, 생각나는 대로 아무거나 말씀하시면 돼요."})
            if st.trial_id is not None:
                condition, attempt = await self._abort_trial("interrupted")
                await self._temi("temi.say", {"text": RETRY_SAY["interrupted"]})
                await self._start_trial(condition, attempt + 1)

    async def _on_safety(self, m: SafetyAlert) -> None:
        st = self.state
        if st is None:
            return
        await self._abort_trial(f"safety:{m.kind}")
        await self._pi("game.abort", {})
        await self._temi("temi.say", {"text": "괜찮으세요? 바로 선생님을 불러드릴게요."})
        await self._create_request(st.resident_id, Role.STAFF, RequestKind.ALERT, Priority.URGENT, "emergency",
                                   f"{st.resident_name}님 안전 신호({m.kind}) 감지. 검사를 중단하고 영상통화를 연결합니다.")
        await self._start_video_call()

    async def _start_video_call(self) -> None:
        st = self._require_state()
        st.mode = "video_call"
        async with SessionLocal() as db:
            staff = await db.scalars(
                select(User).where(User.role == Role.STAFF, User.call_priority.is_not(None)).order_by(User.call_priority)
            )
            st.call_queue = [(u.id, u.name) for u in staff]
        await self._call_next()

    async def _call_next(self) -> None:
        st = self._require_state()
        if not st.call_queue:
            await self._temi("temi.say", {"text": "선생님 연결이 늦어지고 있어요. 곧 오실 거예요. 제가 옆에 있을게요."})
            await self._create_request(st.resident_id, Role.STAFF, RequestKind.ALERT, Priority.URGENT, "emergency",
                                       f"{st.resident_name}님 영상통화에 아무도 응답하지 않았습니다. 바로 방문해 주세요.")
            await self._finish(SessionStatus.ABORTED, "emergency_unanswered")
            return
        staff_id, name = st.call_queue.pop(0)
        await self._temi("temi.video_call", {"staff_id": staff_id, "name": name})

    async def _on_video_call_result(self, m: VideoCallResult) -> None:
        st = self.state
        if st is None or st.mode != "video_call":
            return
        if m.answered:
            await self._observe(st.resident_id, ObservationSource.SYSTEM, f"요양사(id={m.staff_id}) 영상통화 연결됨")
            await self._finish(SessionStatus.ABORTED, "emergency_call_connected")
        else:
            await self._call_next()

    async def _start_game(self, game_type: GameType, level: int) -> None:
        st = self._require_state()
        async with SessionLocal() as db:
            game = GameResult(session_id=st.session_id, game_type=game_type, level=level)
            db.add(game)
            await db.commit()
        st.mode = "game"
        params = {"game_id": game.id, "game_type": game_type.value, "level": level}
        await self._temi("temi.show", {"screen": "game", **params})
        if game_type in (GameType.STROOP, GameType.WHACK):
            await self._pi("game.start", params)

    async def _on_game_completed(self, m: GameCompleted) -> None:
        async with SessionLocal() as db:
            game = await db.get(GameResult, m.game_id)
            if game is None or game.status != GameStatus.RUNNING:
                log.warning("ignored game.completed for game %s", m.game_id)
                return
            stats = analysis.rt_stats(m.rt_ms, m.correct)
            game.rt_ms, game.correct, game.raw = m.rt_ms, m.correct, m.raw
            game.accuracy, game.rt_mean_ms, game.rt_sd_ms = stats["accuracy"], stats["rt_mean_ms"], stats["rt_sd_ms"]
            game.status = GameStatus.COMPLETED
            game.completed_at = datetime.now(UTC)
            await db.commit()
        if self.state is not None and self.state.mode == "game":
            await self._temi("temi.say", {"text": "게임을 마쳤어요. 수고하셨어요."})
            await self._finish(SessionStatus.COMPLETED, None)

    async def _abort_trial(self, reason: str) -> tuple[Condition, int]:
        st = self._require_state()
        self._cancel_gate()
        trial_id, st.trial_id = st.trial_id, None
        if trial_id is None:
            return WALK_PLAN[min(st.step, len(WALK_PLAN) - 1)], 0
        await self._pi("trial.abort", {"trial_id": trial_id, "reason": reason})
        async with SessionLocal() as db:
            trial = await db.get(Trial, trial_id)
            assert trial is not None
            trial.status = TrialStatus.ABORTED
            trial.invalid_reason = reason[:50]
            trial.completed_at = datetime.now(UTC)
            await db.commit()
        return trial.condition, trial.attempt

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

    async def _observe(self, resident_id: int, source: ObservationSource, text: str, *,
                       author_id: int | None = None, data: dict[str, Any] | None = None) -> None:
        async with SessionLocal() as db:
            db.add(Observation(resident_id=resident_id, source=source, author_id=author_id, text=text, data=data or {}))
            await db.commit()

    async def _create_request(self, resident_id: int, role: Role, kind: RequestKind, priority: Priority, topic: str,
                              text: str, *, options: list[str] | None = None) -> None:
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


agent = Agent()
