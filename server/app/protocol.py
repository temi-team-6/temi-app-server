from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class Envelope(BaseModel):
    type: str
    id: str = Field(default_factory=lambda: uuid4().hex)
    ts: datetime | None = None
    session_id: int | None = None
    reply_to: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def make(
    type_: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: int | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": type_,
        "id": uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }
    if session_id is not None:
        msg["session_id"] = session_id
    if reply_to is not None:
        msg["reply_to"] = reply_to
    return msg


class RfidScanned(BaseModel):
    tag: str


class TrialCompleted(BaseModel):
    trial_id: int
    speed_mps: float | None = Field(None, description="시작선~끝선 평균 속도(m/s). 측정 실패면 null")
    duration_s: float | None = Field(None, description="시작선~끝선 통과 시간(초)")
    stopped: bool = Field(False, description="구간 안에서 멈춤 감지")
    max_persons: int = Field(1, description="측정 중 화면 내 최대 인원")
    min_confidence: float | None = Field(None, description="측정 중 관절점 신뢰도 최솟값(0~1)")
    word_count: int | None = Field(None, description="이중과제 발화 단어 수. 출처 미정")
    error_count: int | None = Field(None, description="숫자 세기 오류 수")
    early_start: bool = Field(False, description="trial.go 전에 시작선을 넘음")
    raw: dict[str, Any] = Field(default_factory=dict, description="디버깅용 추가 데이터")


class TrialReady(BaseModel):
    trial_id: int


class SayDone(BaseModel):
    pass


class GameCompleted(BaseModel):
    game_id: int
    rt_ms: list[float] = Field(description="자극별 반응시간(ms)")
    correct: list[bool] = Field(description="자극별 정답 여부")
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _same_length(self) -> "GameCompleted":
        if not self.rt_ms or len(self.rt_ms) != len(self.correct):
            raise ValueError("rt_ms and correct must be non-empty and the same length")
        return self


class SpeechHeard(BaseModel):
    text: str


class SafetyAlert(BaseModel):
    kind: str = Field(description="fall 등")
    detail: str | None = None


class Arrived(BaseModel):
    location: str


class VideoCallResult(BaseModel):
    staff_id: int
    answered: bool


EVENT_PAYLOADS: dict[str, type[BaseModel]] = {
    "rfid.scanned": RfidScanned,
    "trial.ready": TrialReady,
    "trial.completed": TrialCompleted,
    "temi.say_done": SayDone,
    "game.completed": GameCompleted,
    "speech.heard": SpeechHeard,
    "safety.alert": SafetyAlert,
    "temi.arrived": Arrived,
    "video_call.result": VideoCallResult,
}
