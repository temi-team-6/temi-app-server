from typing import Any

from .db import SessionLocal
from .hub import hub
from .models import Resident, Role
from .protocol import make


async def notify_resident(type_: str, payload: dict[str, Any], resident_id: int) -> None:
    """어르신 기록이 바뀌었음을 그 어르신을 볼 수 있는 앱 연결(요양사 전체, 연결된 보호자)에 알린다."""
    async with SessionLocal() as db:
        resident = await db.get(Resident, resident_id)
    msg = make(type_, {"resident_id": resident_id, **payload})
    await hub.send_apps(msg, role=Role.STAFF)
    if resident is not None and resident.guardian_id is not None:
        await hub.send_apps(msg, role=Role.GUARDIAN, user_id=resident.guardian_id)
