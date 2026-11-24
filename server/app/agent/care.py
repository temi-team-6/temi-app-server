"""검사 중 발화(통증·거부·질문), 안전 신호, 영상통화, 게임 전환."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from .. import analysis, llm
from ..db import SessionLocal
from ..models import (
    Condition,
    GameResult,
    GameStatus,
    GameType,
    ObservationSource,
    Priority,
    RequestKind,
    Role,
    SessionStatus,
    User,
)
from ..protocol import (
    GameCompleted,
    SafetyAlert,
    VideoCallResult,
)
from .core import SessionState, _log_task_error
from .walk import RETRY_SAY, WALK_PLAN, WalkFlow

log = logging.getLogger(__name__)


PAIN_WORDS = ("아파", "아퍼", "아프", "통증")
SEVERE_WORDS = ("못 일어", "일어설 수가 없", "일어날 수가 없", "못 움직", "많이 아파")
REFUSE_WORDS = ("하기 싫", "안 할래", "그만할래", "그만 할래")
STOP_YES_WORDS = ("그만", "싫", "안 할", "응", "네", "예", "그래")
REFUSED_SAY = "알겠어요. 오늘은 쉬세요. 선생님께 말씀드릴게요."
PAIN_ASK = "어디가 얼마나 아프세요?"
STOP_ASK = "오늘 걷기 검사는 그만할까요?"
QUESTION_SAY = "네, 생각나는 대로 아무거나 말씀하시면 돼요."
SEVERE_SAY = "알겠어요. 바로 선생님과 연결할게요. 제가 옆에 있을게요."
MILD_SAY = "그럼 걷기는 쉬고, 앉아서 하는 게임으로 바꿀게요."
SPEECH_LABELS = ("pain", "refuse", "question", "other")
SPEECH_INSTRUCTION = (
    "로봇이 진행하는 보행 검사 중에 참가자가 한 말이다. 뜻을 분류한다.\n"
    "pain: 몸이 아프거나 불편하다는 호소(어지러움, 저림, 시큼거림 포함).\n"
    "refuse: 검사를 그만하거나 하기 싫다는 뜻.\n"
    "question: 검사 방법에 대한 질문.\n"
    "other: 그 밖의 말."
)
SEVERITY_LABELS = ("severe", "mild")
SEVERITY_INSTRUCTION = (
    "로봇이 '어디가 얼마나 아프세요?'라고 물었고 참가자가 답했다. 상태를 분류한다.\n"
    "severe: 일어서거나 움직이기 어렵다, 통증이 심하다, 어지럽다, 숨이 차다 등 바로 사람이 와야 하는 상태.\n"
    "mild: 그 밖의 가벼운 통증이나 불편."
)


def speech_intent_by_keywords(text: str) -> str | None:
    if any(w in text for w in PAIN_WORDS):
        return "pain"
    if any(w in text for w in REFUSE_WORDS):
        return "refuse"
    if "?" in text or text.rstrip().endswith(("돼요", "되나요", "해요")):
        return "question"
    return None


class CareFlow(WalkFlow):
    async def _is_severe(self, text: str) -> bool:
        """안전 쪽으로만 기운다: 키워드나 LLM 중 하나라도 심하다고 보면 심한 것으로 처리한다."""
        if any(w in text for w in SEVERE_WORDS):
            return True
        return await llm.classify(SEVERITY_INSTRUCTION, text, SEVERITY_LABELS) == "severe"

    async def _on_speech(self, text: str) -> None:
        """참가자 발화. 어떻게 받을지는 AI가 지금 단계와 대화를 보고 정한다. AI 판단이 없으면 키워드·분류 규칙대로 한다.

        AI 판단은 1초 안팎 걸린다. 기기 연결은 메시지를 하나씩 처리하므로, 그동안 같은 Pi의 안전 신호·측정 결과가
        밀리지 않게 판단은 따로 돌린다. Pi 마이크는 말을 덩어리로 끊어 보내므로, 판단하는 동안 온 말은 모아 다음 판단에 한데 넘긴다.
        영상통화 중에는 발화를 처리하지 않는다(안전 흐름).
        """
        st = self.state
        if st is None or st.mode == "video_call":
            return
        if not self._ai_on():
            await self._handle_speech(st, text)
            return
        st.heard.append(text)
        if st.listener is None or st.listener.done():
            st.listener = asyncio.create_task(self._listen(st))
            st.listener.add_done_callback(_log_task_error)

    async def _listen(self, st: SessionState) -> None:
        while st.heard and self.state is st and st.mode != "video_call":
            text = " ".join(st.heard)
            st.heard.clear()
            await self._handle_speech(st, text)

    def _superseded(self, st: SessionState, mode: str) -> bool:
        """판단을 기다리는 동안 검사가 끝나거나 단계가 바뀌었으면(안전 신호 등) 그 판단은 버린다."""
        return self.state is not st or st.mode != mode

    async def _handle_speech(self, st: SessionState, text: str) -> None:
        st.dialog.append(("어르신", text))
        if st.mode == "pain_check":
            await self._on_pain_answer(text)
            return
        if st.mode == "confirm_stop":
            await self._on_stop_answer(text)
            return
        keyword = speech_intent_by_keywords(text)
        measuring_dual_task = st.trial_id is not None and st.trial_condition != "single"
        decision = None
        if not measuring_dual_task:
            mode = st.mode
            actions = {
                "ignore": "어르신이 로봇에게 한 말이 아니거나(다른 사람 목소리, 혼잣말, 잡음) 할 일이 없다. 아무것도 하지 않는다(say는 빈 문자열)",
                "reply": "대답만 하고 하던 것을 계속한다(측정 중이면 측정은 그대로)",
                "ask_pain": "몸이 불편하다는 뜻이면 측정을 멈추고 어디가 얼마나 아픈지 묻는다",
                "stop": "그만하겠다는 뜻이 분명하면 오늘 검사를 끝낸다(요양사에게 자동으로 알린다)",
            }
            if st.mode == "walk":
                actions["confirm_stop"] = "그만하고 싶다는 뜻 같으면 측정을 멈추고 오늘 검사를 그만할지 되묻는다"
                if st.trial_id is not None:
                    actions["restart_trial"] = "질문에 답하고 지금 측정을 처음부터 다시 한다"
            decision = await self._decide("speech", f"어르신이 '{text}'라고 말했다.", actions)
            if self._superseded(st, mode):
                return
            if decision is not None and keyword == "pain" and decision.action != "ask_pain":
                log.warning("AI chose %s for pain words %r, following the pain rule instead", decision.action, text)
                decision = None
        if decision is not None:
            await self._act_on_speech(decision.action, text, decision.say)
            return
        # 규칙: 키워드가 먼저고, 키워드가 못 잡은 말만 분류한다(이중과제 측정 중에는 분류하지 않는다).
        intent = keyword or ("other" if measuring_dual_task
                             else await llm.classify(SPEECH_INSTRUCTION, text, SPEECH_LABELS) or "other")
        rule_action = {"pain": "ask_pain", "refuse": "stop"}.get(intent)
        if intent == "question" and st.mode == "walk":
            rule_action = "restart_trial"
        if rule_action is not None:
            await self._act_on_speech(rule_action, text, None)

    async def _act_on_speech(self, action: str, text: str, say: str | None) -> None:
        """say가 None이면 규칙 대사를 쓴다."""
        st = self._require_state()
        if action == "reply":
            await self._temi("temi.say", {"text": say or QUESTION_SAY})
        elif action == "restart_trial":
            await self._temi("temi.say", {"text": say or QUESTION_SAY})
            if st.trial_id is not None:
                condition, attempt = await self._abort_trial("interrupted")
                if say is None:
                    await self._temi("temi.say", {"text": RETRY_SAY["interrupted"]})
                await self._start_trial(condition, attempt + 1)
        elif action == "ask_pain":
            await self._abort_trial("pain")
            st.mode = "pain_check"
            await self._temi("temi.ask", {"text": say or PAIN_ASK})
        elif action == "confirm_stop":
            condition, attempt = await self._abort_trial("interrupted")
            st.paused = (condition.value, attempt)
            st.mode = "confirm_stop"
            await self._temi("temi.ask", {"text": say or STOP_ASK})
        elif action == "stop":
            await self._end_refused(text, say)

    async def _on_stop_answer(self, text: str) -> None:
        st = self._require_state()
        decision = await self._decide(
            "stop_answer", f"'{STOP_ASK}'라고 물었고 어르신이 '{text}'라고 답했다.",
            {"stop": "오늘 검사를 끝낸다(요양사에게 자동으로 알린다)", "resume": "검사를 계속한다(멈춘 걷기를 다시 잰다)"},
        )
        if self._superseded(st, "confirm_stop"):
            return
        if decision is not None:
            stop = decision.action == "stop"
        else:
            stop = "아니" not in text and any(w in text for w in STOP_YES_WORDS)
        if stop:
            await self._end_refused(text, decision.say if decision else None)
            return
        condition, attempt = st.paused or (WALK_PLAN[min(st.step, len(WALK_PLAN) - 1)].value, 0)
        st.paused, st.mode = None, "walk"
        await self._temi("temi.say", {"text": decision.say if decision else "네, 그럼 계속해 볼게요."})
        await self._start_trial(Condition(condition), attempt + 1)

    async def _end_refused(self, text: str, say: str | None) -> None:
        """거부로 끝낼 때는 항상 요양사에게 알린다(가드레일). 다시 권하는 것은 사람이 맡는다."""
        st = self._require_state()
        await self._abort_trial("refused")
        await self._observe(st.resident_id, ObservationSource.AGENT, f"참여 거부: '{text}'")
        await self._temi("temi.say", {"text": say or REFUSED_SAY})
        await self._create_request(st.resident_id, Role.STAFF, RequestKind.CHECK, Priority.NORMAL, "refused",
                                   f"{st.resident_name}님이 오늘 보행 검사를 그만하셨습니다('{text}'). "
                                   "컨디션을 살펴 주시고, 괜찮으시면 다시 권해 주세요.")
        await self._finish(SessionStatus.ABORTED, "refused")

    async def _on_pain_answer(self, text: str) -> None:
        """심한지는 AI가 판단하되, 심한 통증 키워드가 있으면 AI가 가볍다고 해도 심한 것으로 처리한다."""
        st = self._require_state()
        decision = await self._decide(
            "pain_answer", f"'{PAIN_ASK}'라고 물었고 어르신이 '{text}'라고 답했다.",
            {"severe": "일어서기·움직이기 어렵거나 통증이 심하거나 어지럽다: 바로 긴급 알림과 요양사 영상통화",
             "mild": "가벼운 불편: 걷기를 멈추고 앉아서 하는 게임으로 바꾸고 요양사에게 확인 요청"},
        )
        if self._superseded(st, "pain_check"):
            return
        keyword_severe = any(w in text for w in SEVERE_WORDS)
        if decision is not None:
            severe = decision.action == "severe" or keyword_severe
        else:
            severe = await self._is_severe(text)
        say = decision.say if decision is not None and (decision.action == "severe") == severe else None
        if severe:
            await self._temi("temi.say", {"text": say or SEVERE_SAY})
            await self._create_request(st.resident_id, Role.STAFF, RequestKind.ALERT, Priority.URGENT, "emergency",
                                       f"{st.resident_name}님이 '{text}'라고 하십니다. 영상통화를 연결합니다.")
            await self._start_video_call()
        else:
            await self._temi("temi.say", {"text": say or MILD_SAY})
            await self._create_request(st.resident_id, Role.STAFF, RequestKind.CHECK, Priority.NORMAL, "pain_check",
                                       f"{st.resident_name}님이 통증('{text}')을 호소해 보행 검사를 중단했습니다. 확인 부탁드립니다.")
            await self._start_game(GameType.STROOP, level=1)

    async def _on_safety(self, m: SafetyAlert) -> None:
        st = self.state
        if st is None:
            return
        self._cancel_listener()
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
        if game_type == GameType.WHACK:
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
            await self._temi("temi.say", {"text": await self._line(
                "game_done", "앉아서 하는 게임을 마쳤다. 수고를 칭찬하고 오늘 검사를 마친다. 점수는 말하지 않는다.",
                "게임을 마쳤어요. 수고하셨어요.")})
            await self._finish(SessionStatus.COMPLETED, None)
