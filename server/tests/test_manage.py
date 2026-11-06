import pytest
from httpx import AsyncClient

from app import manage
from app.models import Role


async def test_operator_creates_linked_guardian_who_can_log_in(client: AsyncClient) -> None:
    resident = await manage.create_resident("C", "RFID-C-0003", birth_year=1950)
    await manage.create_user("guardian_c", "C 보호자", Role.GUARDIAN, "c-password-1", resident_id=resident.id)

    login = await client.post("/api/auth/login", json={"username": "guardian_c", "password": "c-password-1"})
    residents = await client.get("/api/residents",
                                 headers={"Authorization": f"Bearer {login.json()['access_token']}"})

    assert login.status_code == 200
    assert [r["name"] for r in residents.json()] == ["C"]


async def test_manage_rejects_bad_input(client: AsyncClient) -> None:
    with pytest.raises(manage.ManageError, match="이미 있습니다"):
        await manage.create_user("guardian_a", "중복", Role.GUARDIAN, "long-enough-1")
    with pytest.raises(manage.ManageError, match="8자 이상"):
        await manage.create_user("guardian_x", "짧음", Role.GUARDIAN, "short")
    with pytest.raises(manage.ManageError, match="보호자 계정"):
        await manage.link_guardian(1, "staff1")
    with pytest.raises(manage.ManageError, match="이미 쓰고"):
        await manage.create_resident("중복 카드", "RFID-A-0001")
