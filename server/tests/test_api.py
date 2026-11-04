from httpx import AsyncClient

from .conftest import login
from .fakes import FakeDevices


async def test_dev_session_start_is_staff_only(devices: FakeDevices, client: AsyncClient) -> None:
    body = {"resident_id": 2}

    guardian = await client.post("/api/dev/sessions/start", json=body, headers=await login(client, "guardian_b"))
    assert guardian.status_code == 403
    assert devices.pi.of_type("trial.start") == []

    staff = await client.post("/api/dev/sessions/start", json=body, headers=await login(client, "staff1"))
    assert staff.status_code == 200, staff.text
    await devices.pi.wait_for("trial.start")


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
