from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import current_user
from ..config import settings
from ..models import Role, User
from .control import TwinError, twin


def staff_with_twin(user: User = Depends(current_user)) -> User:
    if user.role != Role.STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "staff only")
    if not settings.twin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "twin disabled (TWIN_ENABLED=false)")
    return user


router = APIRouter(prefix="/api/twin", tags=["twin"], dependencies=[Depends(staff_with_twin)],
                   generate_unique_id_function=lambda route: f"twin_{route.name}")


class DeviceIn(BaseModel):
    virtual: bool


class SpeechIn(BaseModel):
    text: str
    reply_to: str | None = None


class RfidIn(BaseModel):
    tag: str


class CallIn(BaseModel):
    answered: bool


async def _guard(action: Any) -> dict[str, Any]:
    try:
        await action
    except TwinError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return twin.status()


@router.get("/status")
async def status_() -> dict[str, Any]:
    return twin.status()


@router.post("/devices/{device}")
async def set_device(device: str, body: DeviceIn) -> dict[str, Any]:
    return await _guard(twin.set_device(device, body.virtual))


@router.post("/scenarios/{scenario_id}")
async def run_scenario(scenario_id: str) -> dict[str, Any]:
    return await _guard(twin.run(scenario_id))


@router.post("/stop")
async def stop() -> dict[str, Any]:
    return await _guard(twin.stop())


@router.post("/temi/speech")
async def speech(body: SpeechIn) -> dict[str, Any]:
    return await _guard(twin.speak(body.text, body.reply_to))


@router.post("/temi/call")
async def call(body: CallIn) -> dict[str, Any]:
    return await _guard(twin.answer_call(body.answered))


@router.post("/pi/rfid")
async def rfid(body: RfidIn) -> dict[str, Any]:
    return await _guard(twin.rfid(body.tag))


@router.post("/pi/game")
async def game() -> dict[str, Any]:
    return await _guard(twin.finish_game())
