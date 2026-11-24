"""시연용: 녹음 파일을 어르신의 가장 최근 '검사 완료' 기록에 받아 적어 넣는다.

마이크·Pi 업로드가 붙기 전에 '녹음 → Gemini 받아 적기(app/stt.py) → 채점 → 저장 → 앱 검사 상세 표시'를
미리 보려고 쓴다. 서버의 받아 적기·채점 코드를 그대로 쓰고, 녹음은 저장하지 않고 받아 적은 글만 남긴다.
서버 .env에 GEMINI_API_KEY가 있어야 한다(Gemini 호출 2번).

    docker compose run --rm --no-deps -v ./scripts:/scripts -v <녹음 폴더>:/rec api \\
        python /scripts/demo_speech.py --animals /rec/animals.m4a --countdown /rec/countdown.m4a [--tag RFID-A-0001]

녹음은 이중과제 걷기 한 번 분량(동물 이름 말하기, 100부터 3씩 거꾸로 세기). 형식은 m4a·wav·mp3.
"""

import argparse
import asyncio
import sys
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/app")  # api 이미지 안에서 실행

from sqlalchemy import select  # noqa: E402

from app import stt  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Condition, Resident, SessionStatus, TestSession, TrialStatus  # noqa: E402

KST = timezone(timedelta(hours=9))
MIME = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="RFID-A-0001", help="어르신 RFID 태그(이름은 겹칠 수 있어 태그로 고른다)")
    ap.add_argument("--animals", type=Path, required=True, help="동물 이름 말하며 걷기 녹음")
    ap.add_argument("--countdown", type=Path, required=True, help="숫자 거꾸로 세며 걷기 녹음")
    args = ap.parse_args()
    files = {Condition.ANIMALS: args.animals, Condition.COUNTDOWN: args.countdown}

    async with SessionLocal() as db:
        resident = await db.scalar(select(Resident).where(Resident.rfid_tag == args.tag))
        if resident is None:
            raise SystemExit(f"태그 {args.tag}인 어르신이 없어요.")
        session = await db.scalar(
            select(TestSession)
            .where(TestSession.resident_id == resident.id, TestSession.status == SessionStatus.COMPLETED)
            .order_by(TestSession.started_at.desc())
        )
        if session is None:
            raise SystemExit(f"{resident.name}님의 '검사 완료' 기록이 없어요. 트윈에서 정상 검사를 한 번 돌려 주세요.")
        print(f"{resident.name}님 검사 {session.id} (한국 시간 {session.started_at.astimezone(KST):%Y-%m-%d %H:%M})")
        for trial in session.trials:
            path = files.get(trial.condition)
            if path is None or trial.status != TrialStatus.VALID:
                continue
            text, model = await stt.transcribe(
                path.read_bytes(), MIME.get(path.suffix.lower(), "audio/wav"), trial.condition
            )
            words, errors = stt.score(trial.condition, text)
            trial.transcript, trial.word_count, trial.error_count = text, words, errors
            trial.raw = {**(trial.raw or {}), "stt_model": model, "demo_recording": path.name}
            print(f"  {trial.condition.value}: {text} -> 단어 {words}, 오류 {errors}")
        await db.commit()
    print("앱에서 그 어르신 → 최근 검사 기록(또는 달력의 그 날짜) → '자세히 >'로 확인하세요.")


if __name__ == "__main__":
    asyncio.run(main())
