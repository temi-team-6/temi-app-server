from httpx import AsyncClient

from .conftest import get_user, login
from .fakes import FakeDevices, connect_app


async def test_dev_session_start_is_staff_only(devices: FakeDevices, client: AsyncClient) -> None:
    body = {"resident_id": 2}

    guardian = await client.post("/api/dev/sessions/start", json=body, headers=await login(client, "guardian_b"))
    assert guardian.status_code == 403
    assert devices.pi.of_type("trial.start") == []

    staff = await client.post("/api/dev/sessions/start", json=body, headers=await login(client, "staff1"))
    assert staff.status_code == 200, staff.text
    await devices.pi.wait_for("trial.start")


async def test_new_memo_is_announced_only_to_apps_that_can_see_the_resident(client: AsyncClient) -> None:
    guardian_a = connect_app(await get_user("guardian_a"))
    guardian_b = connect_app(await get_user("guardian_b"))
    staff2 = connect_app(await get_user("staff2"))

    res = await client.post("/api/residents/1/observations", json={"text": "무릎 통증 호소"},
                            headers=await login(client, "staff1"))

    assert res.status_code == 201
    for socket in (guardian_a, staff2):
        event = await socket.wait_for("observation.created")
        assert event["payload"] == {"resident_id": 1, "observation_id": res.json()["id"]}
    assert guardian_b.of_type("observation.created") == []


async def test_goto_failed_is_accepted_from_temi(devices: FakeDevices) -> None:
    await devices.send("temi", "temi.goto_failed", {"location": "보행_시작", "reason": "obstacle"})

    assert devices.temi.of_type("error") == []


async def test_health_reports_deployed_commit(client: AsyncClient) -> None:
    res = await client.get("/api/health")

    assert res.status_code == 200
    assert res.json()["commit"] == "unknown"


async def test_seed_has_pending_example_requests(client: AsyncClient) -> None:
    for username, expected_kinds in (("guardian_a", {"question"}), ("guardian_b", {"question"}),
                                     ("staff1", {"check", "alert"})):
        res = await client.get("/api/requests?status=pending", headers=await login(client, username))
        assert res.status_code == 200
        assert {r["kind"] for r in res.json()} == expected_kinds, username
