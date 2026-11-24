import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent import agent
from app.config import settings
from app.db import SessionLocal
from app.hub import hub
from app.models import AgentRequest, TestSession, Trial, TrialStatus
from app.twin.control import Twin, TwinError
from app.twin.devices import GAME_RESULT
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


async def test_scenarios_drive_only_the_virtual_pi_with_a_real_temi(fresh_state: None) -> None:
    real_temi = FakeDevices(attach=("temi",))
    real_temi.temi.virtual = False  # type: ignore[misc]
    real_temi.game_result = GAME_RESULT  # 실제 Temi에서 사람이 스트룹을 푼다
    twin = Twin(pace=0)

    await run(twin, "pain_mild")
    await run(twin, "refuse")

    assert hub.devices["temi"] is real_temi.temi
    heard = [e["peer"] for e in monitor.log if e["dir"] == "in" and e["type"] == "speech.heard"]
    assert heard and set(heard) == {"pi"}
    assert real_temi.temi.of_type("temi.ask")  # 통증을 묻는 말은 실제 Temi가 했다
    await real_temi.drain()


async def test_fall_button_alerts_and_calls_through_a_real_temi(fresh_state: None) -> None:
    real_temi = FakeDevices(attach=("temi",))
    real_temi.temi.virtual = False  # type: ignore[misc]
    twin = Twin(pace=0)
    await twin.set_device("pi", True)
    assert twin.devices is not None

    await twin.rfid("RFID-B-0002")
    go = await twin.devices.pi.wait_for("trial.go")
    await twin.fall()

    await real_temi.temi.wait_for("temi.video_call")
    abort = await twin.devices.pi.wait_for("trial.abort")
    assert abort["payload"] == {"trial_id": go["payload"]["trial_id"], "reason": "safety:fall"}
    async with SessionLocal() as db:
        alert = await db.scalar(select(AgentRequest).where(AgentRequest.topic == "emergency"))
    assert alert is not None
    await twin.stop()
    await real_temi.drain()


async def test_manual_walk_drives_the_current_trial_with_a_real_temi(fresh_state: None) -> None:
    real_temi = FakeDevices(attach=("temi",))
    real_temi.temi.virtual = False  # type: ignore[misc]
    twin = Twin(pace=0)
    await run(twin, "normal")  # 이 시나리오의 걷기 결과가 남아 있어도 직접 조작에서는 쓰지 않아야 한다
    assert twin.devices is not None
    pi = twin.devices.pi

    with pytest.raises(TwinError, match="출발 신호"):
        await twin.walk("good")
    mark = pi.mark()
    await twin.rfid("RFID-A-0001")
    first = await pi.wait_for("trial.go", after=mark)
    await twin.walk("stopped")
    with pytest.raises(TwinError, match="이미 걷는 중"):
        await twin.walk("good")
    retry = await pi.wait_for("trial.start", lambda m: m["payload"]["attempt"] == 2, after=mark)
    retry_id = retry["payload"]["trial_id"]
    await pi.wait_for("trial.go", lambda m: m["payload"]["trial_id"] == retry_id, after=mark)
    await twin.walk("good")
    await pi.wait_for("trial.start", lambda m: m["payload"]["condition"] == "animals", after=mark)

    async with SessionLocal() as db:
        stopped = await db.get(Trial, first["payload"]["trial_id"])
        valid = await db.get(Trial, retry_id)
    assert stopped is not None and stopped.invalid_reason == "stopped"
    assert valid is not None and valid.status == TrialStatus.VALID
    assert hub.devices["temi"] is real_temi.temi
    await twin.stop()
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
