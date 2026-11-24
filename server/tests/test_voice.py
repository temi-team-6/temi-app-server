import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from app import voice

from .fakes import GOOD_WALK, FakeDevices


@pytest.fixture
def voice_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(voice, "VOICE_DIR", tmp_path)
    voice.manifest.cache_clear()
    yield tmp_path
    voice.manifest.cache_clear()


def write_manifest(folder: Path, entries: dict[str, str], files: list[str]) -> None:
    for name in files:
        (folder / name).write_bytes(b"ID3")
    (folder / "manifest.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def test_known_line_gets_audio_url(voice_dir: Path) -> None:
    write_manifest(voice_dir, {"출발하세요.": "abc.mp3"}, ["abc.mp3"])

    assert voice.with_audio("temi.say", {"text": "출발하세요."}) == {
        "text": "출발하세요.",
        "audio_url": "/voice/abc.mp3",
    }
    assert voice.with_audio("temi.ask", {"text": "출발하세요."})["audio_url"] == "/voice/abc.mp3"


def test_other_lines_and_messages_are_unchanged(voice_dir: Path) -> None:
    write_manifest(voice_dir, {"출발하세요.": "abc.mp3"}, ["abc.mp3"])

    assert voice.with_audio("temi.say", {"text": "처음 듣는 문장"}) == {"text": "처음 듣는 문장"}
    assert voice.with_audio("temi.show", {"screen": "idle", "text": "출발하세요."}) == {
        "screen": "idle",
        "text": "출발하세요.",
    }


def test_missing_or_broken_files_fall_back_to_temi_tts(voice_dir: Path) -> None:
    write_manifest(voice_dir, {"출발하세요.": "gone.mp3"}, [])
    assert voice.audio_url("출발하세요.") is None

    voice.manifest.cache_clear()
    (voice_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    assert voice.manifest() == {}

    voice.manifest.cache_clear()
    (voice_dir / "manifest.json").unlink()
    assert voice.manifest() == {}


def test_bundled_manifest_points_at_real_files() -> None:
    voice.manifest.cache_clear()
    data = json.loads((voice.VOICE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert data, "scripts/gen_temi_voice.py로 만든 음성이 있어야 한다"
    assert all((voice.VOICE_DIR / name).is_file() for name in data.values())


async def test_voice_files_are_served(client: AsyncClient) -> None:
    voice.manifest.cache_clear()
    res = await client.get("/voice/manifest.json")
    assert res.status_code == 200
    name = next(iter(res.json().values()))
    assert (await client.get(f"/voice/{name}")).status_code == 200
    # Temi 앱의 '서버 음성' 모드가 쓰는 예전 주소도 같은 파일
    assert (await client.get("/twin/voice/manifest.json")).json() == res.json()
    assert (await client.get(f"/twin/voice/{name}")).status_code == 200


async def test_walk_sends_audio_for_pre_generated_lines(devices: FakeDevices) -> None:
    voice.manifest.cache_clear()
    devices.walk = GOOD_WALK

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    said = devices.temi.of_type("temi.say")
    go = [m for m in said if m["payload"]["text"] == "출발하세요."]
    assert go and all(m["payload"]["audio_url"].startswith("/voice/") for m in go)
    known = voice.manifest()
    assert all(("audio_url" in m["payload"]) == (m["payload"]["text"] in known) for m in said)
