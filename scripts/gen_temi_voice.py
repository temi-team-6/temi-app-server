"""Temi 대사를 로컬 TTS 서버로 미리 음성 파일로 만든다. 트윈 화면이 Temi 대사에 맞춰 재생한다.

    cd server && uv run python ../scripts/gen_temi_voice.py [--tts http://127.0.0.1:7788] [--names A B]

대사는 에이전트 코드의 고정 문장에서 모은다. 대사를 바꾸면 다시 실행하고 결과를 커밋한다.
"""

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
AGENT = SERVER / "app" / "agent"
OUT = SERVER / "app" / "static" / "twin" / "voice"


def temi_lines(names: list[str]) -> list[str]:
    import sys

    sys.path.insert(0, str(SERVER))
    from app.agent.walk import INSTRUCTION, RETRY_SAY

    lines = set(INSTRUCTION.values()) | {RETRY_SAY["interrupted"]}
    lines |= {f"{text} 다시 해볼게요." for key, text in RETRY_SAY.items() if key != "interrupted"}
    lines |= {f"{name}님, 안녕하세요. 오늘도 걷기 검사를 해볼게요." for name in names}
    for source in AGENT.glob("*.py"):
        lines |= set(re.findall(r'"text": "([^"{}]+)"', source.read_text(encoding="utf-8")))
        lines |= set(re.findall(r'_say_and_wait\("([^"{}]+)"\)', source.read_text(encoding="utf-8")))
    return sorted(lines)


def synthesize(tts: str, text: str) -> bytes:
    body = json.dumps({"model": "supertonic-3", "input": text, "voice": "F1", "response_format": "mp3", "lang": "ko"})
    req = urllib.request.Request(f"{tts}/v1/audio/speech", data=body.encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as res:
        return res.read()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tts", default="http://127.0.0.1:7788")
    parser.add_argument("--names", nargs="+", default=["A", "B", "C"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for text in temi_lines(args.names):
        name = hashlib.sha1(text.encode()).hexdigest()[:12] + ".mp3"
        if not (OUT / name).exists():
            (OUT / name).write_bytes(synthesize(args.tts, text))
        manifest[text] = name
    for stale in set(p.name for p in OUT.glob("*.mp3")) - set(manifest.values()):
        (OUT / stale).unlink()
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(manifest)} lines -> {OUT}")


if __name__ == "__main__":
    main()
