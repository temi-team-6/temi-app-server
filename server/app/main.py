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
from .db import Base, SessionLocal, engine
from .hub import AppClient, hub
from .models import User
from .protocol import Envelope, make
from .seed import seed_if_empty

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("temi")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_if_empty()
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
app.mount("/sim", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="sim")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


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
