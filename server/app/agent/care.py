"""검사 중 발화(통증·거부·질문), 안전 신호, 영상통화, 게임 전환."""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from .. import analysis
from ..db import SessionLocal
from ..models import (
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
from .walk import RETRY_SAY, WalkFlow

log = logging.getLogger(__name__)


PAIN_WORDS = ("아파", "아퍼", "아프", "통증")
SEVERE_WORDS = ("못 일어", "일어설 수가 없", "일어날 수가 없", "못 움직", "많이 아파")
REFUSE_WORDS = ("하기 싫", "안 할래", "그만할래", "그만 할래")


class CareFlow(WalkFlow):
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
