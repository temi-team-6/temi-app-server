import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import stt
from app.agent import agent
from app.config import settings
from app.db import SessionLocal
from app.models import Condition, SessionStatus, TestSession, Trial, TrialStatus

from .fakes import GOOD_WALK, FakeDevices, Message

PI = {"Authorization": f"Bearer {settings.pi_token}", "Content-Type": "audio/wav"}
# Pi가 단어 수를 세지 않고 녹음만 올리는 걷기(이중과제 trial.completed에 word_count가 없다)
NO_COUNT_WALK: dict[str, Message] = {
    cond: {**{k: v for k, v in GOOD_WALK[cond].items() if k != "word_count"}, "speech_upload": cond != "single"}
    for cond in GOOD_WALK
}


# ---- 채점 ----


def test_animals_counts_distinct_names_and_single_letters_only_alone() -> None:
    assert stt.score(Condition.ANIMALS, "사자, 호랑이, 사자, 북극곰, 펭귄") == (4, None)
    # '말해요'의 '말'은 동물이 아니고, 따로 떨어진 '말'은 동물이다
    assert stt.score(Condition.ANIMALS, "음 뭐라고 말해요, 말, 소리") == (1, None)
    assert stt.score(Condition.ANIMALS, "") == (0, None)


def test_countdown_counts_numbers_and_serial_errors() -> None:
    assert stt.score(Condition.COUNTDOWN, "100, 97, 94, 91, 88") == (5, 0)
    assert stt.score(Condition.COUNTDOWN, "97, 94, 91") == (3, 0)  # 첫 숫자로 97도 맞음
    # 93은 틀림(94여야). 그 뒤 90은 93에서 3을 바르게 뺐으니 맞음, 88은 틀림
    assert stt.score(Condition.COUNTDOWN, "100, 97, 93, 90, 88") == (5, 2)
    assert stt.score(Condition.COUNTDOWN, "백, 구십칠, 구십사") == (3, 0)
    assert stt.score(Condition.COUNTDOWN, "음 잘 모르겠어요") == (0, 0)


def test_audio_mime() -> None:
    assert stt.audio_mime("audio/x-wav") == "audio/wav"
    assert stt.audio_mime("audio/m4a") == "audio/mp4"
    assert stt.audio_mime("audio/mpeg; charset=binary") == "audio/mpeg"
    assert stt.audio_mime("application/octet-stream") == "audio/wav"


# ---- Gemini 호출 ----


def gemini_reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


@pytest.fixture
def gemini(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], list[str]]:
    """Gemini 대신 handler가 답하게 한다. 부른 모델 이름 목록을 돌려준다."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    real_client = httpx.AsyncClient
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(stt.asyncio, "sleep", no_sleep)

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> list[str]:
        models: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            models.append(request.url.path.rsplit("/", 1)[1].split(":")[0])
            return handler(request)

        monkeypatch.setattr(
            stt.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(record), **kw)
        )
        return models

    return install


async def test_transcribe_sends_rule_and_audio(gemini: Callable[..., list[str]]) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return gemini_reply("100, 97, 94")

    models = gemini(handler)
    text, model = await stt.transcribe(b"RIFF", "audio/wav", Condition.COUNTDOWN)

    assert (text, model) == ("100, 97, 94", settings.gemini_stt_model) and models == [model]
    prompt, audio = bodies[0]["contents"][0]["parts"]
    assert "고치지 마세요" in prompt["text"] and "숫자를 거꾸로" in prompt["text"]
    assert audio["inlineData"]["mimeType"] == "audio/wav"


async def test_transcribe_waits_out_rate_limit_and_falls_back_on_404(gemini: Callable[..., list[str]]) -> None:
    replies = [
        httpx.Response(404, json={}),
        httpx.Response(429, text='{"error": {"details": [{"retryDelay": "3s"}]}}'),
        gemini_reply("사자"),
    ]
    models = gemini(lambda _: replies.pop(0))

    text, model = await stt.transcribe(b"x", "audio/wav")

    assert text == "사자" and model == stt.FALLBACK_MODEL
    assert models == [settings.gemini_stt_model, stt.FALLBACK_MODEL, stt.FALLBACK_MODEL]


async def test_transcribe_gives_up_when_limit_outlasts_timeout(
    gemini: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "stt_timeout_s", 5.0)
    gemini(lambda _: httpx.Response(429, text='{"retryDelay": "60s"}'))

    with pytest.raises(stt.SttError, match="사용량 제한"):
        await stt.transcribe(b"x", "audio/wav")


async def test_transcribe_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with pytest.raises(stt.SttError):
        await stt.transcribe(b"x", "audio/wav")


# ---- 업로드 API와 보행 흐름 ----


@pytest.fixture
def fake_transcribe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """받아 적기 결과를 순서대로 돌려준다. 키는 있는 것으로 둔다."""
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "stt_wait_s", 2.0)
    texts: list[str] = []

    async def transcribe(audio: bytes, mime: str, condition: Condition | None = None) -> tuple[str, str]:
        assert audio and mime == "audio/wav"
        return texts.pop(0), "fake-model"

    monkeypatch.setattr(stt, "transcribe", transcribe)
    return texts


async def trial_of(devices: FakeDevices, condition: str, attempt: int = 1) -> int:
    msg = await devices.pi.wait_for(
        "trial.start", lambda m: m["payload"]["condition"] == condition and m["payload"]["attempt"] == attempt
    )
    return msg["payload"]["trial_id"]


async def test_walk_waits_for_server_transcript_and_saves_it(
    devices: FakeDevices, client: AsyncClient, fake_transcribe: list[str]
) -> None:
    fake_transcribe += ["사자, 호랑이, 뱀, 말, 곰", "100, 97, 94, 91, 88, 85"]
    devices.walk = NO_COUNT_WALK

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    for condition in ("animals", "countdown"):
        trial_id = await trial_of(devices, condition)
        res = await client.post(f"/api/trials/{trial_id}/speech", content=b"RIFF....", headers=PI)
        assert res.status_code == 200, res.text
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    async with SessionLocal() as db:
        session = await db.scalar(select(TestSession).order_by(TestSession.id.desc()))
        assert session is not None
        trials = (await db.scalars(select(Trial).where(Trial.session_id == session.id))).all()
    assert session.status == SessionStatus.COMPLETED
    dual = {t.condition: t for t in trials if t.condition != Condition.SINGLE}
    assert (dual[Condition.ANIMALS].word_count, dual[Condition.ANIMALS].transcript) == (5, "사자, 호랑이, 뱀, 말, 곰")
    assert (dual[Condition.COUNTDOWN].word_count, dual[Condition.COUNTDOWN].error_count) == (6, 0)
    assert all(t.status == TrialStatus.VALID for t in trials)


async def test_too_few_words_heard_retries_the_walk(
    devices: FakeDevices, client: AsyncClient, fake_transcribe: list[str]
) -> None:
    fake_transcribe += ["사자", "사자, 호랑이, 뱀, 여우"]
    devices.walk = {**GOOD_WALK, "animals": NO_COUNT_WALK["animals"]}

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    first = await trial_of(devices, "animals")
    await client.post(f"/api/trials/{first}/speech", content=b"RIFF", headers=PI)
    second = await trial_of(devices, "animals", attempt=2)
    await client.post(f"/api/trials/{second}/speech", content=b"RIFF", headers=PI)
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    async with SessionLocal() as db:
        first_trial, second_trial = await db.get(Trial, first), await db.get(Trial, second)
    assert first_trial and first_trial.status == TrialStatus.INVALID
    assert first_trial.invalid_reason == "task_not_engaged" and first_trial.word_count == 1
    assert second_trial and second_trial.status == TrialStatus.VALID and second_trial.word_count == 4


async def test_walk_goes_on_without_transcript_after_waiting(
    devices: FakeDevices, fake_transcribe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "stt_wait_s", 0.05)
    devices.walk = NO_COUNT_WALK

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    async with SessionLocal() as db:
        session = await db.scalar(select(TestSession).order_by(TestSession.id.desc()))
        assert session is not None
        trials = (await db.scalars(select(Trial).where(Trial.session_id == session.id))).all()
    assert len(trials) == 3 and all(t.status == TrialStatus.VALID and t.word_count is None for t in trials)


async def test_pi_that_does_not_upload_is_not_waited_for(
    devices: FakeDevices, fake_transcribe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 녹음을 올리지 않는 예전 Pi(speech_upload 없음, word_count도 없음)는 키가 있어도 기다리지 않는다
    monkeypatch.setattr(settings, "stt_wait_s", 30.0)
    devices.walk = {cond: {k: v for k, v in msg.items() if k != "speech_upload"} for cond, msg in NO_COUNT_WALK.items()}

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-B-0002"})
    await asyncio.wait_for(devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle"), 5)


async def test_speech_upload_rejects_wrong_device_or_trial(
    devices: FakeDevices, client: AsyncClient, fake_transcribe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    devices.walk = None
    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    single = await trial_of(devices, "single")

    async def post(trial_id: int, headers: dict[str, str]) -> int:
        return (await client.post(f"/api/trials/{trial_id}/speech", content=b"RIFF", headers=headers)).status_code

    assert await post(single, {**PI, "Authorization": "Bearer nope"}) == 401
    assert await post(single, {**PI, "Authorization": f"Bearer {settings.temi_token}"}) == 403
    assert await post(9999, PI) == 404
    assert await post(single, PI) == 400
    async with SessionLocal() as db:
        dual = Trial(session_id=(await db.get(Trial, single)).session_id, condition=Condition.ANIMALS)  # type: ignore[union-attr]
        db.add(dual)
        await db.commit()
    monkeypatch.setattr(settings, "gemini_api_key", "")
    assert await post(dual.id, PI) == 503
    assert (await client.post("/api/speech", content=b"RIFF", headers=PI)).status_code == 503


async def test_short_speech_is_handled_like_speech_heard(
    client: AsyncClient, fake_transcribe: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    heard: list[str] = []

    async def on_speech(text: str) -> None:
        heard.append(text)

    monkeypatch.setattr(agent, "_on_speech", on_speech)
    await agent._temi("temi.ask", {"text": "어디가 얼마나 아프세요?"})
    fake_transcribe += ["무릎이 좀 아파요", "어디가 얼마나 아프세요", ""]

    for _ in range(3):
        res = await client.post("/api/speech", content=b"RIFF", headers=PI)
        assert res.status_code == 200

    assert heard == ["무릎이 좀 아파요"]  # Temi 목소리와 빈 녹음은 넘기지 않는다
