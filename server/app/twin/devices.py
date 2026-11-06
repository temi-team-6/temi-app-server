"""가상 Temi·Pi. 실제 기기와 같은 메시지를 hub에 주고받는다.

pace=0이면 지연 없이 바로 응답하고(테스트용), pace=1이면 실제와 비슷한 속도로 움직인다(트윈용).
walk에 조건별 측정 결과를 주면 trial.go를 받았을 때 그 결과를 보낸다. 결과가 목록이면 측정할 때마다
앞에서부터 하나씩 쓰고 마지막 것을 계속 쓴다. walk가 None이면 측정 결과는 직접 보내야 한다.
"""

import asyncio
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..agent import agent
from ..hub import hub
from ..protocol import Envelope

Message = dict[str, Any]
COURSE_M = 4.0

GOOD_WALK: dict[str, Message] = {
    "single": {"speed_mps": 1.30, "duration_s": 3.1, "max_persons": 1, "min_confidence": 0.9},
    "animals": {"speed_mps": 1.20, "duration_s": 3.3, "max_persons": 1, "min_confidence": 0.9, "word_count": 8},
    "countdown": {"speed_mps": 1.15, "duration_s": 3.5, "max_persons": 1, "min_confidence": 0.9, "word_count": 7},
}
SLOW_ANIMALS: Message = {**GOOD_WALK["animals"], "speed_mps": 0.88, "duration_s": 4.5}
SLOW_COUNTDOWN: Message = {**GOOD_WALK["countdown"], "speed_mps": 0.88, "duration_s": 4.5}
SLOW_DUAL_WALK: dict[str, Message] = {**GOOD_WALK, "animals": SLOW_ANIMALS, "countdown": SLOW_COUNTDOWN}
GAME_RESULT: Message = {"rt_ms": [352, 410, 388, 365, 420, 398], "correct": [True, True, False, True, True, True]}


class VirtualSocket:
    """서버가 기기에 보낸 프레임을 기록한다. hub.devices에 실제 WebSocket 대신 들어간다."""

    virtual = True

    def __init__(self, on_send: Callable[[Message], None] | None = None) -> None:
        self.sent: list[Message] = []
        self.closed = False
        self._cond = asyncio.Condition()
        self._on_send = on_send

    async def send_json(self, data: Message) -> None:
        async with self._cond:
            self.sent.append(data)
            self._cond.notify_all()
        if self._on_send is not None:
            self._on_send(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def mark(self) -> int:
        return len(self.sent)

    def of_type(self, type_: str) -> list[Message]:
        return [m for m in self.sent if m["type"] == type_]

    async def wait_for(self, type_: str, where: Callable[[Message], bool] = lambda _: True,
                       timeout: float = 5.0, after: int = 0) -> Message:
        def find() -> Message | None:
            return next((m for m in self.sent[after:] if m["type"] == type_ and where(m)), None)

        async with self._cond:
            await asyncio.wait_for(self._cond.wait_for(lambda: find() is not None), timeout)
        found = find()
        assert found is not None
        return found


class VirtualDevices:
    def __init__(self, pace: float = 0.0, attach: Iterable[str] = ("temi", "pi")) -> None:
        self.pace = pace
        self.walk: Mapping[str, Message | list[Message]] | None = None
        self.call_answers: list[bool] = []
        self.game_result: Message | None = None
        self.temi = VirtualSocket(self._temi_frame)
        self.pi = VirtualSocket(self._pi_frame)
        self._conditions: dict[int, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        for device in attach:
            self.attach(device)

    def socket(self, device: str) -> VirtualSocket:
        return self.temi if device == "temi" else self.pi

    def attach(self, device: str) -> None:
        socket = self.socket(device)
        socket.closed = False
        hub.devices[device] = socket

    def detach(self, device: str) -> None:
        if hub.devices.get(device) is self.socket(device):
            del hub.devices[device]

    def attached(self, device: str) -> bool:
        return hub.devices.get(device) is self.socket(device)

    async def send(self, device: str, type_: str, payload: Message | None = None, reply_to: str | None = None) -> None:
        await agent.handle(device, Envelope(type=type_, payload=payload or {}, reply_to=reply_to))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pause(self, seconds: float) -> None:
        if self.pace > 0:
            await asyncio.sleep(seconds * self.pace)

    async def _later(self, seconds: float, device: str, type_: str, payload: Message | None = None,
                     reply_to: str | None = None) -> None:
        await self._pause(seconds)
        if self.attached(device):
            await self.send(device, type_, payload, reply_to)

    def _emit(self, seconds: float, device: str, type_: str, payload: Message | None = None,
              reply_to: str | None = None) -> None:
        self._spawn(self._later(seconds, device, type_, payload, reply_to))

    def _temi_frame(self, msg: Message) -> None:
        payload = msg["payload"]
        match msg["type"]:
            case "temi.say":
                self._emit(0.6 + 0.09 * len(payload.get("text", "")), "temi", "temi.say_done", reply_to=msg["id"])
            case "temi.goto":
                self._emit(2.5, "temi", "temi.arrived", {"location": payload["location"]})
            case "temi.video_call" if self.call_answers:
                answered = self.call_answers.pop(0)
                self._emit(3.0, "temi", "video_call.result", {"staff_id": payload["staff_id"], "answered": answered})

    def _pi_frame(self, msg: Message) -> None:
        payload = msg["payload"]
        match msg["type"]:
            case "trial.start":
                self._conditions[payload["trial_id"]] = payload["condition"]
                self._emit(1.0, "pi", "trial.ready", {"trial_id": payload["trial_id"]})
            case "trial.go" if self.walk is not None:
                result = self.walk[self._conditions[payload["trial_id"]]]
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                self._spawn(self._walk(payload["trial_id"], result))
            case "game.start" if self.game_result is not None:
                self._emit(4.0, "pi", "game.completed", {"game_id": payload["game_id"], **self.game_result})

    async def _walk(self, trial_id: int, result: Message) -> None:
        if self.pace > 0:
            duration = result.get("duration_s") or 3.5
            ticks = max(int(duration / 0.25), 1)
            for i in range(1, ticks + 1):
                await self._pause(duration / ticks)
                if not self.attached("pi") or i == ticks:
                    break
                position = COURSE_M * i / ticks
                if result.get("stopped") and position > COURSE_M / 2:
                    position = COURSE_M / 2
                await self.send("pi", "trial.progress", {"trial_id": trial_id, "position_m": round(position, 2)})
        if self.attached("pi"):
            await self.send("pi", "trial.completed", {"trial_id": trial_id, **result})

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks)

    def cancel(self) -> None:
        for task in list(self._tasks):
            task.cancel()
