"""검사 진행 에이전트.

흐름별로 파일을 나눴다. 기능 담당자는 자기 흐름 파일만 고치면 된다.
- core.py: 세션 상태, 기기·앱 전송, 관찰 기록·요청 생성 등 공통 도구
- walk.py: 보행 검사 진행과 기준선 비교
- care.py: 검사 중 발화, 안전 신호, 영상통화, 게임
- guardian.py: 앱 답변 처리와 보호자 답변 판단
"""

from typing import Any

from pydantic import BaseModel, ValidationError

from ..hub import hub
from ..models import SessionStatus
from ..protocol import (
    EVENT_PAYLOADS,
    Arrived,
    Envelope,
    GameCompleted,
    GotoFailed,
    RfidScanned,
    SafetyAlert,
    SayDone,
    SpeechHeard,
    TrialCompleted,
    TrialProgress,
    TrialReady,
    VideoCallResult,
    make,
)
from ..twin.monitor import monitor
from .core import log
from .guardian import GuardianFlow


class Agent(GuardianFlow):
    """규칙 기반 임시 에이전트. 이벤트 입출력은 실제 LLM 에이전트와 같고, 판단만 고정 규칙이다."""

    def snapshot(self) -> dict[str, Any]:
        """트윈 상태판에 보여 줄 진행 중인 검사의 단계."""
        st = self.state
        if st is None:
            return {"active": False}
        if st.awaiting_say is not None and not st.said.is_set():
            waiting = "say_done"
        elif st.trial_id is not None and not st.ready.is_set():
            waiting = "trial.ready"
        else:
            waiting = None
        return {
            "active": True,
            "session_id": st.session_id,
            "resident_id": st.resident_id,
            "resident_name": st.resident_name,
            "mode": st.mode,
            "step": st.step,
            "trial_id": st.trial_id,
            "condition": st.trial_condition,
            "confirmed": st.confirmed,
            "waiting": waiting,
            "call_queue": [name for _, name in st.call_queue],
        }

    async def abort_session(self, reason: str) -> None:
        if self.state is None:
            return
        await self._abort_trial(reason)
        await self._finish(SessionStatus.ABORTED, reason)

    async def handle(self, device: str, env: Envelope) -> None:
        await monitor.record("in", device, env.model_dump(mode="json"))
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
            case SpeechHeard(text=text) if device == "pi" and self._is_temi_echo(text):
                log.info("ignored Temi's own voice heard by the pi mic: %s", text)
            case SpeechHeard(text=text):
                await self._on_speech(text)
            case SafetyAlert():
                await self._on_safety(payload)
            case VideoCallResult():
                await self._on_video_call_result(payload)
            case Arrived():
                log.info("temi arrived at %s", payload.location)
            case GotoFailed():
                log.warning("temi failed to reach %s: %s", payload.location, payload.reason)
            case TrialProgress():
                pass
        await monitor.broadcast()


agent = Agent()
monitor.state_provider = agent.snapshot
