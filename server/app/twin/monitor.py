"""트윈 상태판의 관찰자. 기기·앱과 오간 메시지를 기록하고, 그 흐름만으로 2D 지도 상태를 만든다.

실제 기기와 가상 기기를 구분하지 않으므로, 리허설 때 실제 기기를 붙여도 같은 화면을 쓴다.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)

COURSE_M = 4.0
WAIT_POSITION_M = -0.6
SILENT_TYPES = {"ping", "pong"}


class JsonSocket(Protocol):
    async def send_json(self, data: Any) -> None: ...


def _empty_world() -> dict[str, Any]:
    return {
        "temi": {"location": "보행_측정", "target": None, "screen": "idle", "saying": None, "heard": None, "call": None},
        "session_id": None,
        "person": None,
        "game": None,
        "last_trial": None,
    }


class Monitor:
    def __init__(self) -> None:
        self.log: deque[dict[str, Any]] = deque(maxlen=400)
        self.clients: set[JsonSocket] = set()
        self.state_provider: Callable[[], dict[str, Any]] = dict
        self.devices_provider: Callable[[], dict[str, Any]] = dict
        self.world = _empty_world()
        self.seq = 0
        self._cond = asyncio.Condition()

    def reset(self) -> None:
        self.log.clear()
        self.world = _empty_world()

    def snapshot(self) -> dict[str, Any]:
        return {"agent": self.state_provider(), "devices": self.devices_provider(), "world": self.world}

    async def record(self, direction: str, peer: str, msg: dict[str, Any], *, dropped: bool = False) -> None:
        if msg.get("type") in SILENT_TYPES:
            return
        type_: str | None = msg.get("type")
        payload: dict[str, Any] = msg.get("payload") or {}
        self.seq += 1
        entry = {"seq": self.seq, "t": datetime.now(UTC).isoformat(), "dir": direction, "peer": peer,
                 "type": type_, "id": msg.get("id"), "payload": payload, "dropped": dropped}
        self.log.append(entry)
        session_id = msg.get("session_id")
        if session_id is not None and session_id != self.world["session_id"]:
            self.world.update(session_id=session_id, last_trial=None, game=None)
        self._apply(direction, type_, payload)
        async with self._cond:
            self._cond.notify_all()
        await self.broadcast({"kind": "event", "entry": entry})

    def mark(self) -> int:
        return self.seq

    async def wait_for(self, peer: str, type_: str, where: Callable[[dict[str, Any]], bool] = lambda _: True, *,
                       after: int, timeout: float) -> dict[str, Any]:
        """after 이후에 기록된 메시지 중 조건에 맞는 첫 메시지를 기다린다. 실제 기기든 가상 기기든 같다."""
        def find() -> dict[str, Any] | None:
            return next((e for e in self.log if e["seq"] > after and e["peer"] == peer and e["type"] == type_
                         and where(e)), None)

        async with self._cond:
            await asyncio.wait_for(self._cond.wait_for(lambda: find() is not None), timeout)
        found = find()
        assert found is not None
        return found

    async def broadcast(self, data: dict[str, Any] | None = None) -> None:
        message = {**(data or {"kind": "state"}), **self.snapshot()}
        for client in list(self.clients):
            try:
                await client.send_json(message)
            except Exception as e:  # noqa: BLE001 - 끊긴 상태판 연결은 어떤 오류든 정리만 한다
                log.info("monitor client dropped: %s", e)
                self.clients.discard(client)

    def _apply(self, direction: str, type_: str | None, p: dict[str, Any]) -> None:
        temi, person = self.world["temi"], self.world["person"]
        match direction, type_:
            case "out", "temi.goto":
                temi["target"] = p.get("location")
            case "in", "temi.arrived":
                temi.update(location=p.get("location"), target=None)
            case "in", "temi.goto_failed":
                temi["target"] = None
            case "out", "temi.show":
                temi["screen"] = p.get("screen")
                if p.get("screen") == "game":
                    self.world["game"] = {k: p.get(k) for k in ("game_id", "game_type", "level")} | {"state": "running"}
                if p.get("screen") == "idle":
                    self.world.update(person=None, game=None)
                    temi.update(saying=None, call=None)
            case "out", "temi.say" | "temi.ask":
                temi["saying"] = p.get("text")
            case "in", "speech.heard":
                temi["heard"] = p.get("text")
            case "out", "temi.video_call":
                temi["call"] = {"name": p.get("name"), "state": "ringing"}
            case "in", "video_call.result":
                temi["call"] = {**(temi["call"] or {}), "state": "answered" if p.get("answered") else "no_answer"}
            case "out", "trial.start":
                self.world["person"] = {"trial_id": p.get("trial_id"), "condition": p.get("condition"),
                                        "attempt": p.get("attempt"), "phase": "approaching", "position_m": None}
            case "in", "trial.ready" if person:
                person.update(phase="ready", position_m=WAIT_POSITION_M)
            case "out", "trial.go" if person:
                person.update(phase="walking", position_m=person["position_m"] or WAIT_POSITION_M)
            case "in", "trial.progress" if person and person["trial_id"] == p.get("trial_id"):
                person["position_m"] = p.get("position_m")
            case "in", "trial.completed":
                if person and person["trial_id"] == p.get("trial_id"):
                    walked = p.get("speed_mps") is not None and not p.get("stopped")
                    person.update(phase="done", position_m=COURSE_M if walked else person["position_m"])
                self.world["last_trial"] = {k: p.get(k) for k in
                                            ("trial_id", "speed_mps", "duration_s", "stopped", "max_persons",
                                             "min_confidence", "word_count")}
            case "out", "trial.abort" if person:
                person.update(phase="aborted", reason=p.get("reason"))
            case "in", "game.completed" if self.world["game"]:
                self.world["game"]["state"] = "done"
            case _:
                pass


monitor = Monitor()
