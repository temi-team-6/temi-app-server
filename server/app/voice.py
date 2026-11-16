"""Temi 대사 음성 파일. scripts/gen_temi_voice.py가 미리 만든 파일을 temi.say·temi.ask에 붙인다.

미리 만든 문장이면 payload에 audio_url이 붙고, Temi 앱은 그 파일을 재생한다(없으면 Temi 내장 TTS).
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VOICE_DIR = Path(__file__).parent / "static" / "voice"
URL_PREFIX = "/voice"
SPOKEN = ("temi.say", "temi.ask")


@lru_cache(maxsize=1)
def manifest() -> dict[str, str]:
    """문장 → 파일 이름. 파일이 없거나 깨졌으면 빈 목록(모든 문장을 Temi 내장 TTS로)."""
    try:
        data = json.loads((VOICE_DIR / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("voice manifest unreadable, falling back to Temi TTS", exc_info=True)
        return {}
    return {str(k): str(v) for k, v in data.items() if (VOICE_DIR / str(v)).is_file()}


def audio_url(text: str) -> str | None:
    name = manifest().get(text)
    return f"{URL_PREFIX}/{name}" if name else None


def with_audio(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
    """말하는 명령이고 미리 만든 음성이 있으면 audio_url을 붙인 새 payload를 돌려준다."""
    if type_ not in SPOKEN or "audio_url" in payload:
        return payload
    url = audio_url(str(payload.get("text", "")))
    return {**payload, "audio_url": url} if url else payload
