import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent import agent
from app.config import settings
from app.db import SessionLocal
from app.hub import hub
from app.models import AgentRequest, TestSession
from app.twin.control import Twin, TwinError
from app.twin.monitor import monitor
from app.twin.scenarios import SCENARIOS

from .conftest import login
from .fakes import FakeDevices, FakeSocket


async def run(twin: Twin, scenario_id: str) -> None:
    task = await twin.run(scenario_id)
    await asyncio.wait_for(task, timeout=10)
    assert twin.last_error is None, twin.last_error


@pytest.mark.parametrize("scenario_id", [s.id for s in SCENARIOS.values() if s.drives])
async def test_device_scenarios_run_to_the_end_as_virtual_records(fresh_state: None, scenario_id: str) -> None:
    twin = Twin(pace=0)

    await run(twin, scenario_id)

    assert agent.state is None
    async with SessionLocal() as db:
        session = await db.scalar(select(TestSession).order_by(TestSession.id.desc()))
    assert session is not None and session.synthetic and session.ended_at is not None
    assert monitor.world["person"] is None and monitor.world["temi"]["screen"] == "idle"


async def test_guardian_answer_scenarios_follow_the_signal(fresh_state: None) -> None:
    twin = Twin(pace=0)
    await run(twin, "slow_both")
    async with SessionLocal() as db:
        question = await db.scalar(select(AgentRequest).where(AgentRequest.session_id.is_not(None)))
    assert question is not None and question.explanation

    await run(twin, "guardian_decline")

    async with SessionLocal() as db:
        consult = await db.scalar(select(AgentRequest).where(AgentRequest.topic == "consult"))
    assert consult is not None


async def test_scenario_uses_a_real_temi_but_will_not_drive_it(fresh_state: None) -> None:
    real_temi = FakeDevices(attach=("temi",))
    real_temi.temi.virtual = False  # type: ignore[misc]
    twin = Twin(pace=0)

    with pytest.raises(TwinError, match="실제 temi"):
        await twin.run("pain_mild")
    await run(twin, "normal")

    assert hub.devices["temi"] is real_temi.temi
    await real_temi.drain()


async def test_monitor_tracks_walk_position_and_broadcasts(devices: FakeDevices) -> None:
    watcher = FakeSocket()
    monitor.clients.add(watcher)  # type: ignore[arg-type]
    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    go = await devices.pi.wait_for("trial.go")

    await devices.send("pi", "trial.progress", {"trial_id": go["payload"]["trial_id"], "position_m": 1.5})

    assert monitor.world["person"]["phase"] == "walking"
    assert monitor.world["person"]["position_m"] == 1.5
    last = watcher.sent[-1]
    assert last["kind"] == "state" and last["agent"]["condition"] == "single"
    assert any(m.get("entry", {}).get("type") == "trial.progress" for m in watcher.sent)
    monitor.clients.discard(watcher)  # type: ignore[arg-type]


async def test_twin_api_is_staff_only_and_off_by_default(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    staff = await login(client, "staff1")
    assert (await client.get("/api/twin/status", headers=staff)).status_code == 403

    monkeypatch.setattr(settings, "twin_enabled", True)
    guardian = await client.get("/api/twin/status", headers=await login(client, "guardian_a"))
    status = await client.get("/api/twin/status", headers=staff)

    assert guardian.status_code == 403
    assert status.status_code == 200
    assert {s["id"] for s in status.json()["scenarios"]} == set(SCENARIOS)


async def test_twin_api_will_not_replace_a_real_device(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "twin_enabled", True)
    hub.devices["temi"] = FakeSocket()
    hub.devices["temi"].virtual = False  # type: ignore[attr-defined]

    res = await client.post("/api/twin/devices/temi", json={"virtual": True}, headers=await login(client, "staff1"))

    assert res.status_code == 409
    assert "실제 temi" in res.json()["detail"]
