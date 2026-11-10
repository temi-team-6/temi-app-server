import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .agent import agent
from .api import router
from .auth import device_from_token, user_id_from_token
from .config import settings
from .db import SessionLocal, engine
from .hub import AppClient, hub
from .models import Role, User
from .protocol import Envelope, make
from .seed import seed_if_empty
from .twin.api import router as twin_router
from .twin.monitor import monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("temi")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await seed_if_empty()
    # 재시작 전에 진행 중이던 검사는 이어 갈 수 없으므로 중단으로 닫는다.
    await agent.close_orphaned_sessions("server_restarted")
    yield
    await engine.dispose()


app = FastAPI(title="Temi 이중과제 보행 검사 서버", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(twin_router)
app.mount("/twin", StaticFiles(directory=Path(__file__).parent / "static" / "twin", html=True), name="twin")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/sim/", include_in_schema=False)
async def old_simulator() -> RedirectResponse:
    return RedirectResponse("/twin/")


@app.websocket("/ws/device")
async def device_ws(ws: WebSocket, token: str) -> None:
    device = device_from_token(token)
    if device is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    previous = hub.devices.get(device)
    if previous is not None:
        await previous.close(code=status.WS_1012_SERVICE_RESTART)
    hub.devices[device] = ws
    log.info("device connected: %s", device)
    await monitor.broadcast()
    await ws.send_json(make("hello", {"device": device}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                env = Envelope.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as e:
                await ws.send_json(make("error", {"message": f"bad envelope: {e}"}))
                continue
            log.info("<- %s %s %s", device, env.type, env.payload)
            await agent.handle(device, env)
    except WebSocketDisconnect:
        pass
    finally:
        if hub.devices.get(device) is ws:
            del hub.devices[device]
        log.info("device disconnected: %s", device)
        await monitor.broadcast()


@app.websocket("/ws/app")
async def app_ws(ws: WebSocket, token: str) -> None:
    try:
        user_id = user_id_from_token(token)
    except HTTPException:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
    if user is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    client = AppClient(ws, user.id, user.role)
    hub.apps.add(client)
    await ws.send_json(make("hello", {"user_id": user.id, "role": user.role.value}))
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json(make("pong", reply_to=msg.get("id")))
    except WebSocketDisconnect:
        pass
    finally:
        hub.apps.discard(client)


@app.websocket("/ws/monitor")
async def monitor_ws(ws: WebSocket, token: str) -> None:
    """트윈 상태판용(요양사 계정). 모든 기기·앱 메시지와 검사 상태를 보낸다. 보기 전용이다."""
    try:
        user_id = user_id_from_token(token)
    except HTTPException:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
    if user is None or user.role != Role.STAFF:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    await ws.send_json({"kind": "hello", "log": list(monitor.log), **monitor.snapshot()})
    monitor.clients.add(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"kind": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        monitor.clients.discard(ws)
