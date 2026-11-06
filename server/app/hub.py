import logging
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import WebSocket

from .models import Role
from .twin.monitor import monitor

log = logging.getLogger(__name__)


class DeviceSocket(Protocol):
    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


@dataclass(eq=False)
class AppClient:
    ws: WebSocket
    user_id: int
    role: Role


class Hub:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceSocket] = {}
        self.apps: set[AppClient] = set()

    def device_modes(self) -> dict[str, Any]:
        modes = {name: ("virtual" if getattr(self.devices.get(name), "virtual", False) else "real")
                 if name in self.devices else "offline" for name in ("temi", "pi")}
        return {**modes, "apps": len(self.apps)}

    async def send_device(self, device: str, msg: dict[str, Any]) -> bool:
        ws = self.devices.get(device)
        await monitor.record("out", device, msg, dropped=ws is None)
        if ws is None:
            log.warning("device %s offline, dropped %s", device, msg["type"])
            return False
        await ws.send_json(msg)
        log.info("-> %s %s %s", device, msg["type"], msg.get("payload"))
        return True

    async def send_apps(self, msg: dict[str, Any], *, role: Role, user_id: int | None = None) -> None:
        await monitor.record("out", f"app:{role.value}" + (f":{user_id}" if user_id is not None else ""), msg)
        for client in list(self.apps):
            if client.role != role or (user_id is not None and client.user_id != user_id):
                continue
            try:
                await client.ws.send_json(msg)
            except RuntimeError:
                self.apps.discard(client)


hub = Hub()
monitor.devices_provider = hub.device_modes
