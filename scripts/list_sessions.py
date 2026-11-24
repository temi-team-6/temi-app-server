"""어르신별 최근 검사 기록을 한국 시간으로 보여 준다(받아 적은 걷기 수 포함). 앱과 DB가 같은지 확인할 때 쓴다.

docker compose run --rm --no-deps -v ./scripts:/scripts api python /scripts/list_sessions.py [--last 8]
"""

import argparse
import asyncio
import sys
from datetime import timedelta, timezone

sys.path.insert(0, "/app")  # api 이미지 안에서 실행

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Resident, TestSession  # noqa: E402

KST = timezone(timedelta(hours=9))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=8, help="어르신마다 보여 줄 최근 검사 수")
    args = ap.parse_args()
    async with SessionLocal() as db:
        for r in (await db.scalars(select(Resident).order_by(Resident.id))).all():
            rows = (
                await db.scalars(
                    select(TestSession).where(TestSession.resident_id == r.id).order_by(TestSession.started_at)
                )
            ).all()
            print(f"어르신 {r.id} {r.name} ({r.rfid_tag}): 검사 {len(rows)}개")
            for s in rows[-args.last :]:
                said = sum(1 for t in s.trials if t.transcript)
                print(
                    f"  #{s.id} {s.started_at.astimezone(KST):%m-%d %H:%M} {s.status.value}"
                    f" 가상={s.synthetic} 받아적은걷기={said}"
                )


if __name__ == "__main__":
    asyncio.run(main())
