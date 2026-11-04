import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

# app을 import하기 전에 설정해야 한다(설정과 DB 엔진이 import 시점에 만들어진다).
# 테스트는 테이블을 비우므로, 개발자가 export해 둔 개발·운영 DATABASE_URL을 쓰지 않도록 항상 덮어쓴다.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://temi:temi@localhost:55432/temi_test"
)
os.environ["LEAD_IN_S"] = "0"
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-bytes"
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.agent import agent  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.hub import hub  # noqa: E402
from app.main import app  # noqa: E402
from app.models import TestSession, User  # noqa: E402
from app.seed import DEV_PASSWORD, seed  # noqa: E402

from .fakes import FakeDevices  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
TestSession.__test__ = False  # type: ignore[attr-defined]  # 모델 이름이 Test로 시작해 pytest가 테스트 클래스로 오인한다


def run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVER_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated_schema() -> None:
    for args in (("downgrade", "base"), ("upgrade", "head")):
        result = run_alembic(*args)
        assert result.returncode == 0, result.stderr


@pytest.fixture
async def fresh_state(migrated_schema: None) -> AsyncIterator[None]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await seed()
    hub.devices.clear()
    hub.apps.clear()
    agent.state = None
    yield
    if agent.state is not None and agent.state.gate is not None:
        agent.state.gate.cancel()
    agent.state = None
    hub.devices.clear()
    hub.apps.clear()


@pytest.fixture
async def devices(fresh_state: None) -> AsyncIterator[FakeDevices]:
    fake = FakeDevices()
    yield fake
    await fake.drain()


@pytest.fixture
async def client(fresh_state: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def get_user(username: str) -> User:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.username == username))
    assert user is not None
    return user


async def login(client: AsyncClient, username: str) -> dict[str, str]:
    res = await client.post("/api/auth/login", json={"username": username, "password": DEV_PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}
