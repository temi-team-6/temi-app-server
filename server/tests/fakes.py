import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from app.agent import agent
from app.hub import AppClient, hub
from app.models import Role, User
from app.protocol import Envelope

Message = dict[str, Any]

GOOD_WALK: dict[str, Message] = {
    "single": {"speed_mps": 1.30, "duration_s": 3.1, "max_persons": 1, "min_confidence": 0.9},
    "animals": {"speed_mps": 1.20, "duration_s": 3.3, "max_persons": 1, "min_confidence": 0.9, "word_count": 8},
    "countdown": {"speed_mps": 1.15, "duration_s": 3.5, "max_persons": 1, "min_confidence": 0.9, "word_count": 7},
}
SLOW_DUAL_WALK: dict[str, Message] = {
    **GOOD_WALK,
    "animals": {**GOOD_WALK["animals"], "speed_mps": 0.88},
    "countdown": {**GOOD_WALK["countdown"], "speed_mps": 0.88},
}


class FakeSocket:
    """서버가 보낸 프레임을 기록한다. 기기·앱 WebSocket 자리에 끼운다."""

    def __init__(self, on_send: Callable[[Message], None] | None = None) -> None:
        self.sent: list[Message] = []
        self._cond = asyncio.Condition()
        self._on_send = on_send

    async def send_json(self, msg: Message) -> None:
        async with self._cond:
            self.sent.append(msg)
            self._cond.notify_all()
        if self._on_send is not None:
            self._on_send(msg)

    def of_type(self, type_: str) -> list[Message]:
        return [m for m in self.sent if m["type"] == type_]

    async def wait_for(self, type_: str, where: Callable[[Message], bool] = lambda _: True,
                       timeout: float = 5.0) -> Message:
        def find() -> Message | None:
            return next((m for m in self.sent if m["type"] == type_ and where(m)), None)

        async with self._cond:
            await asyncio.wait_for(self._cond.wait_for(lambda: find() is not None), timeout)
        found = find()
        assert found is not None
        return found


class FakeDevices:
    """Temi와 Pi를 흉내 낸다. Temi는 말을 다 하면 say_done을, Pi는 trial.start에 ready를 바로 보낸다.

    walk를 주면 trial.go를 받았을 때 그 조건의 결과로 trial.completed를 보낸다.
    결과가 목록이면 측정할 때마다 앞에서부터 하나씩 쓰고, 마지막 것을 계속 쓴다.
    None이면 측정 결과를 보내지 않으므로 테스트가 직접 이벤트를 보낸다.
    """

    def __init__(self) -> None:
        self.walk: Mapping[str, Message | list[Message]] | None = None
        self.temi = FakeSocket(self._temi_frame)
        self.pi = FakeSocket(self._pi_frame)
        self._conditions: dict[int, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        hub.devices["temi"] = self.temi  # type: ignore[assignment]
        hub.devices["pi"] = self.pi  # type: ignore[assignment]

    async def send(self, device: str, type_: str, payload: Message | None = None,
                   reply_to: str | None = None) -> None:
        await agent.handle(device, Envelope(type=type_, payload=payload or {}, reply_to=reply_to))

    def _emit(self, device: str, type_: str, payload: Message | None = None, reply_to: str | None = None) -> None:
        task = asyncio.create_task(self.send(device, type_, payload, reply_to))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _temi_frame(self, msg: Message) -> None:
        if msg["type"] == "temi.say":
            self._emit("temi", "temi.say_done", reply_to=msg["id"])

    def _pi_frame(self, msg: Message) -> None:
        payload = msg["payload"]
        if msg["type"] == "trial.start":
            self._conditions[payload["trial_id"]] = payload["condition"]
            self._emit("pi", "trial.ready", {"trial_id": payload["trial_id"]})
        elif msg["type"] == "trial.go" and self.walk is not None:
            result = self.walk[self._conditions[payload["trial_id"]]]
            if isinstance(result, list):
                result = result.pop(0) if len(result) > 1 else result[0]
            self._emit("pi", "trial.completed", {"trial_id": payload["trial_id"], **result})

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks)


def connect_app(user: User) -> FakeSocket:
    socket = FakeSocket()
    hub.apps.add(AppClient(socket, user.id, Role(user.role)))  # type: ignore[arg-type]
    return socket
