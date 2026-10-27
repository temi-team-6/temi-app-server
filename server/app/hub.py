import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from .models import Role

log = logging.getLogger(__name__)


@dataclass(eq=False)
class AppClient:
    ws: WebSocket
    user_id: int
    role: Role


class Hub:
    def __init__(self) -> None:
        self.devices: dict[str, WebSocket] = {}
        self.apps: set[AppClient] = set()

    async def send_device(self, device: str, msg: dict[str, Any]) -> bool:
        ws = self.devices.get(device)
        if ws is None:
            log.warning("device %s offline, dropped %s", device, msg["type"])
            return False
        await ws.send_json(msg)
        log.info("-> %s %s %s", device, msg["type"], msg.get("payload"))
        return True

    async def send_apps(self, msg: dict[str, Any], *, role: Role, user_id: int | None = None) -> None:
        for client in list(self.apps):
            if client.role != role or (user_id is not None and client.user_id != user_id):
                continue
            try:
                await client.ws.send_json(msg)
            except RuntimeError:
                self.apps.discard(client)


hub = Hub()
