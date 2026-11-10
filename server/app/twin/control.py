import asyncio
import logging
from typing import Any

from ..agent import agent
from ..hub import hub
from .devices import GAME_RESULT, TEMI_GAMES, VirtualDevices
from .monitor import monitor
from .scenarios import SCENARIOS, Ctx

log = logging.getLogger(__name__)

DEVICES = ("temi", "pi")


class TwinError(Exception):
    pass


class Twin:
    """트윈의 가상 기기와 시나리오 실행을 관리한다. 시나리오는 한 번에 하나만 돈다."""

    def __init__(self, pace: float = 1.0) -> None:
        self.pace = pace
        self.devices: VirtualDevices | None = None
        self.task: asyncio.Task[None] | None = None
        self.running: str | None = None
        self.last_error: str | None = None

    def _devices(self) -> VirtualDevices:
        if self.devices is None:
            self.devices = VirtualDevices(pace=self.pace, attach=())
        return self.devices

    def status(self) -> dict[str, Any]:
        return {
            "devices": hub.device_modes(),
            "running": self.running,
            "last_error": self.last_error,
            "scenarios": [{"id": s.id, "title": s.title, "description": s.description, "drives": sorted(s.drives)}
                          for s in SCENARIOS.values()],
        }

    async def set_device(self, device: str, on: bool) -> None:
        if device not in DEVICES:
            raise TwinError(f"모르는 기기: {device}")
        devices = self._devices()
        if on:
            if device in hub.devices and not devices.attached(device):
                raise TwinError(f"실제 {device}가 연결돼 있어 가상 {device}를 붙일 수 없습니다.")
            devices.attach(device)
        else:
            devices.detach(device)
        await monitor.broadcast()

    async def run(self, scenario_id: str) -> asyncio.Task[None]:
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            raise TwinError(f"모르는 시나리오: {scenario_id}")
        if self.running is not None:
            raise TwinError(f"'{SCENARIOS[self.running].title}' 시나리오가 진행 중입니다.")
        if scenario.drives and agent.state is not None:
            raise TwinError("진행 중인 검사가 있습니다. 먼저 중지하세요.")
        devices = self._devices()
        for device in DEVICES:
            if device in hub.devices and not devices.attached(device):
                if device in scenario.drives:
                    raise TwinError(f"이 시나리오는 가상 {device}가 필요한데, 실제 {device}가 연결돼 있습니다.")
                continue
            devices.attach(device)
        devices.walk, devices.call_answers, devices.game_result = None, [], None
        self.running, self.last_error = scenario_id, None
        self.task = asyncio.create_task(self._run(scenario_id, Ctx(devices)))
        await monitor.broadcast()
        return self.task

    async def _run(self, scenario_id: str, ctx: Ctx) -> None:
        try:
            await SCENARIOS[scenario_id].run(ctx)
        except asyncio.CancelledError:
            self.last_error = "중지됨"
            raise
        except (LookupError, TimeoutError) as e:
            self.last_error = str(e) or "시간 안에 다음 단계가 오지 않았습니다."
            log.warning("twin scenario %s failed: %s", scenario_id, self.last_error)
        finally:
            self.running = None
            await monitor.broadcast()

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.devices is not None:
            self.devices.cancel()
        await agent.abort_session("twin_stopped")
        await monitor.broadcast()

    async def speak(self, text: str, reply_to: str | None = None) -> None:
        await self._require("temi").send("temi", "speech.heard", {"text": text}, reply_to)

    async def rfid(self, tag: str) -> None:
        await self._require("pi").send("pi", "rfid.scanned", {"tag": tag})

    async def answer_call(self, answered: bool) -> None:
        devices = self._require("temi")
        call = next((m for m in reversed(devices.temi.sent) if m["type"] == "temi.video_call"), None)
        if call is None:
            raise TwinError("걸려 온 영상통화가 없습니다.")
        await devices.send("temi", "video_call.result", {"staff_id": call["payload"]["staff_id"], "answered": answered})

    async def finish_game(self) -> None:
        game = monitor.world["game"]
        if not game or game.get("state") != "running":
            raise TwinError("진행 중인 게임이 없습니다.")
        device = "temi" if game.get("game_type") in TEMI_GAMES else "pi"
        await self._require(device).send(device, "game.completed", {"game_id": game["game_id"], **GAME_RESULT})

    def _require(self, device: str) -> VirtualDevices:
        devices = self._devices()
        if not devices.attached(device):
            raise TwinError(f"가상 {device}가 연결돼 있지 않습니다.")
        return devices


twin = Twin()
