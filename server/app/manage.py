"""운영자용 계정·어르신 관리 명령. 공개 회원가입 대신 운영자가 서버에서 직접 만든다.

    docker compose exec api python -m app.manage list
    docker compose exec api python -m app.manage create-user --username guardian_c --name "C 보호자" \\
        --role guardian --password <8자 이상> [--resident-id 3]
    docker compose exec api python -m app.manage create-resident --name C --rfid RFID-C-0003 \\
        [--birth-year 1950] [--guardian guardian_c]
    docker compose exec api python -m app.manage link-guardian --resident-id 3 --guardian guardian_c

어르신 한 명에 보호자는 한 명이다. 이미 연결된 어르신에 연결하면 보호자가 바뀐다.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import hash_password
from .db import SessionLocal, engine
from .models import Resident, Role, User

MIN_PASSWORD_LENGTH = 8


class ManageError(Exception):
    pass


async def _guardian(db: AsyncSession, username: str) -> User:
    user = await db.scalar(select(User).where(User.username == username))
    if user is None or user.role != Role.GUARDIAN:
        raise ManageError(f"보호자 계정 '{username}'이 없습니다.")
    return user


async def _resident(db: AsyncSession, resident_id: int) -> Resident:
    resident = await db.get(Resident, resident_id)
    if resident is None:
        raise ManageError(f"어르신 id={resident_id}이 없습니다.")
    return resident


async def create_user(username: str, name: str, role: Role, password: str, *,
                      call_priority: int | None = None, resident_id: int | None = None) -> User:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ManageError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    if resident_id is not None and role != Role.GUARDIAN:
        raise ManageError("어르신 연결은 보호자 계정만 할 수 있습니다.")
    async with SessionLocal() as db:
        if await db.scalar(select(User).where(User.username == username)) is not None:
            raise ManageError(f"아이디 '{username}'은 이미 있습니다.")
        user = User(username=username, name=name, role=role, call_priority=call_priority,
                    password_hash=hash_password(password))
        db.add(user)
        await db.flush()
        if resident_id is not None:
            (await _resident(db, resident_id)).guardian_id = user.id
        await db.commit()
    return user


async def create_resident(name: str, rfid_tag: str, *, birth_year: int | None = None,
                          guardian_username: str | None = None) -> Resident:
    async with SessionLocal() as db:
        if await db.scalar(select(Resident).where(Resident.rfid_tag == rfid_tag)) is not None:
            raise ManageError(f"RFID 태그 '{rfid_tag}'는 이미 쓰고 있습니다.")
        guardian = await _guardian(db, guardian_username) if guardian_username else None
        resident = Resident(name=name, rfid_tag=rfid_tag, birth_year=birth_year,
                            guardian_id=guardian.id if guardian else None)
        db.add(resident)
        await db.commit()
    return resident


async def link_guardian(resident_id: int, guardian_username: str) -> Resident:
    async with SessionLocal() as db:
        resident = await _resident(db, resident_id)
        resident.guardian_id = (await _guardian(db, guardian_username)).id
        await db.commit()
    return resident


async def describe() -> str:
    async with SessionLocal() as db:
        users = list(await db.scalars(select(User).order_by(User.id)))
        residents = list(await db.scalars(select(Resident).order_by(Resident.id)))
    names = {u.id: u.username for u in users}
    lines = ["계정:"] + [f"  {u.id} {u.username} ({u.role.value}) {u.name}"
                        + (f" 통화순서={u.call_priority}" if u.call_priority else "") for u in users]
    lines += ["어르신:"] + [f"  {r.id} {r.name} {r.rfid_tag} 보호자={names.get(r.guardian_id or 0, '없음')}"
                         for r in residents]
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    user = sub.add_parser("create-user")
    user.add_argument("--username", required=True)
    user.add_argument("--name", required=True)
    user.add_argument("--role", required=True, choices=[r.value for r in Role])
    user.add_argument("--password", required=True)
    user.add_argument("--call-priority", type=int)
    user.add_argument("--resident-id", type=int)
    resident = sub.add_parser("create-resident")
    resident.add_argument("--name", required=True)
    resident.add_argument("--rfid", required=True)
    resident.add_argument("--birth-year", type=int)
    resident.add_argument("--guardian")
    link = sub.add_parser("link-guardian")
    link.add_argument("--resident-id", type=int, required=True)
    link.add_argument("--guardian", required=True)
    return parser


async def _run(args: argparse.Namespace) -> str:
    try:
        match args.command:
            case "create-user":
                u = await create_user(args.username, args.name, Role(args.role), args.password,
                                      call_priority=args.call_priority, resident_id=args.resident_id)
                return f"계정을 만들었습니다: id={u.id} {u.username}"
            case "create-resident":
                r = await create_resident(args.name, args.rfid, birth_year=args.birth_year,
                                          guardian_username=args.guardian)
                return f"어르신을 등록했습니다: id={r.id} {r.name}"
            case "link-guardian":
                r = await link_guardian(args.resident_id, args.guardian)
                return f"{r.name}님에게 보호자 {args.guardian}을 연결했습니다."
            case _:
                return await describe()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        print(asyncio.run(_run(_parser().parse_args(argv))))
    except ManageError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
