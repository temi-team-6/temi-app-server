"""Temi 대사를 미리 음성 파일로 만든다. 서버가 temi.say·temi.ask에 audio_url로 붙이고, Temi 앱과 트윈 화면이 재생한다.

기본은 Google Gemini TTS의 Leda 목소리(친근한 말투)다. 서버 .env에 GEMINI_API_KEY가 있어야 한다.

    docker compose run --rm --no-deps -e TEMI_SERVER_DIR=/app -v ./scripts:/scripts -v ./server/app/static/voice:/app/app/static/voice api sh -c "uv pip install -q --python /app/.venv/bin/python lameenc && python /scripts/gen_temi_voice.py"

관우님 로컬 Supertonic 서버로 만들 때:

    cd server && uv run python ../scripts/gen_temi_voice.py --engine supertonic --tts http://127.0.0.1:7788

대사는 에이전트 코드의 고정 문장에서 모은다. 대사를 바꾸면 다시 실행하고 결과를 커밋한다.
이미 만든 문장은 건너뛴다. 무료 사용량 제한(429)으로 일부가 빠지면 나중에 다시 실행해 채운다.
빠진 문장은 manifest에 없으므로 Temi가 내장 TTS로 말한다.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

SERVER = Path(os.environ.get("TEMI_SERVER_DIR") or Path(__file__).resolve().parent.parent / "server")
AGENT = SERVER / "app" / "agent"
OUT = SERVER / "app" / "static" / "voice"

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_VOICE = "Leda"
# 말투 지시는 대사와 섞지 않고 systemInstruction으로 따로 준다. 대사 앞에 붙이면 모델이 지시문까지
# 소리 내어 읽는다(실제로 긴 대사 앞에 "다정하고 밝게…"가 읽혔다). 받아 주지 않는 모델이면 지시 없이 만든다.
GEMINI_STYLE = "너는 요양원 어르신을 돕는 다정한 로봇이야. 받은 글을 한 글자도 더하거나 빼지 말고 그대로, 밝고 따뜻한 목소리로 또박또박 천천히 읽어."
GEMINI_FALLBACK_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
RATE = 24000


class QuotaExhausted(RuntimeError):
    """모든 모델이 사용량 제한에 걸림. 오늘 무료 사용량을 다 썼을 가능성이 높다."""  # Gemini TTS는 24kHz 16비트 모노 PCM을 준다


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


def supertonic(tts: str, text: str) -> tuple[bytes, str]:
    body = json.dumps({"model": "supertonic-3", "input": text, "voice": "F1", "response_format": "mp3", "lang": "ko"})
    req = urllib.request.Request(
        f"{tts}/v1/audio/speech", data=body.encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        return res.read(), ".mp3"


def _gemini_get(url: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.loads(res.read())


def gemini_models(key: str) -> list[str]:
    """이 키로 쓸 수 있는 TTS 모델. 최신 flash부터, lite는 마지막 예비. pro는 무료 키로 막혀 뺀다."""
    try:
        names = [m["name"].split("/", 1)[1] for m in _gemini_get(f"{GEMINI_API}?pageSize=200", key).get("models", [])]
    except (OSError, ValueError) as e:
        print("모델 목록을 못 받아 기본 목록을 씀:", e)
        return GEMINI_FALLBACK_MODELS
    tts = [n for n in names if "tts" in n and "pro" not in n]
    picked = sorted((n for n in tts if "lite" not in n), reverse=True) + sorted(
        (n for n in tts if "lite" in n), reverse=True
    )
    return picked or GEMINI_FALLBACK_MODELS


def clean_pcm(pcm: bytes) -> bytes:
    """Gemini 음성 끝에 가끔 붙는 0.1~0.2초짜리 잡음(최대 음량으로 '지직')을 잘라 내고,
    끝의 긴 무음을 줄인 뒤 짧게 페이드 아웃한다. 16비트 모노 PCM."""
    import array

    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    n, win = len(a), RATE // 100  # 10ms 단위로 본다

    def loud(i: int) -> int:
        return max((abs(x) for x in a[i : i + win]), default=0)

    tail_from = max(0, n - int(RATE * 0.5))
    clipped = next((i for i in range(tail_from, n) if abs(a[i]) >= 32000), None)
    if clipped is not None:  # 잡음이 시작된 곳까지 거슬러 올라가 거기서 자른다
        cut = clipped - clipped % win
        while cut - win >= 0 and loud(cut - win) > 300:
            cut -= win
        del a[cut:]
    end = len(a)
    while end - win > 0 and loud(end - win) <= 300:  # 끝 무음
        end -= win
    del a[min(len(a), end + int(RATE * 0.15)) :]  # 무음은 0.15초만 남긴다
    fade = min(len(a), int(RATE * 0.02))
    for k in range(fade):
        a[len(a) - fade + k] = int(a[len(a) - fade + k] * (fade - k) / fade)
    return a.tobytes()


def to_audio(pcm: bytes) -> tuple[bytes, str]:
    """lameenc가 있으면 mp3(작음), 없으면 wav로 저장한다."""
    try:
        import lameenc  # type: ignore[import-not-found]
    except ImportError:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        return buf.getvalue(), ".wav"
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    return enc.encode(pcm) + enc.flush(), ".mp3"


def max_seconds(text: str) -> float:
    """대사 길이로 본 최대 재생 시간. 천천히 말해도 한 글자에 0.3초 안쪽이다(지시문을 읽으면 이를 넘는다)."""
    return 1.5 + 0.3 * len(text)


NO_STYLE: set[str] = set()  # 말투 지시(systemInstruction)를 받지 않는 모델


def gemini(key: str, model: str, text: str) -> tuple[bytes, str]:
    """한 모델로만 만든다. 분당 제한이면 구글이 알려 준 시간만큼 쉬고 다시, 하루 제한이면 멈춘다."""
    base: dict = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}},
        },
    }
    waits = too_long = 0
    while True:
        body = base if model in NO_STYLE else {**base, "systemInstruction": {"parts": [{"text": GEMINI_STYLE}]}}
        try:
            res = _gemini_get(f"{GEMINI_API}/{model}:generateContent", key, body)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if e.code == 400 and "systemInstruction" in body:  # 말투 지시를 못 받는 모델이면 지시 없이
                NO_STYLE.add(model)
                print(f"  {model}: 말투 지시를 받지 않는 모델이라 지시 없이 만들어요")
                continue
            if e.code in (429, 503):
                quota = (re.search(r'"quotaId":\s*"([^"]+)"', msg) or [None, ""])[1]
                if "PerDay" in quota:
                    raise QuotaExhausted(f"하루 사용량 초과({quota})") from None
                waits += 1
                if waits > 6:
                    raise QuotaExhausted(f"사용량 제한이 풀리지 않음({quota or e.code})") from None
                delay = int((re.search(r'"retryDelay":\s*"(\d+)', msg) or [None, "30"])[1]) + 3
                print(f"  {e.code} 사용량 제한{f'({quota})' if quota else ''}: {delay}초 쉬고 다시")
                time.sleep(delay)
                continue
            raise RuntimeError(f"{model} {e.code} {msg[:200]}") from None
        pcm = clean_pcm(base64.b64decode(res["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]))
        seconds = len(pcm) / (RATE * 2)
        if seconds > max_seconds(text):  # 지시문까지 읽었거나 말을 지어냄: 한 번 더
            too_long += 1
            print(f"  {seconds:.0f}초로 너무 길어 버림")
            if too_long >= 2:
                raise RuntimeError("두 번 모두 너무 긴 음성")
            continue
        print(f"  {seconds:.1f}초")
        return to_audio(pcm)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["gemini", "supertonic"], default="gemini")
    parser.add_argument("--tts", default="http://127.0.0.1:7788", help="Supertonic 서버 주소")
    parser.add_argument("--names", nargs="+", default=["A", "B", "C"])
    parser.add_argument(
        "--model", help="Gemini TTS 모델 고정(기본: 쓸 수 있는 최신 flash). 모든 대사를 한 모델로 만든다"
    )
    parser.add_argument(
        "--try",
        dest="try_n",
        type=int,
        default=0,
        help="대사 N개만 static/voice/try/에 만들어 들어 본다(목록은 안 바꿈)",
    )
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    if args.engine == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise SystemExit("GEMINI_API_KEY가 없어요. 서버 .env에 넣어 주세요.")
        # 모델마다 목소리 결이 달라서(가벼운 모델은 아이 목소리처럼 들림) 모든 대사를 한 모델로 만든다.
        model = args.model or gemini_models(key)[0]
        print(f"TTS 모델: {model} (목소리 {GEMINI_VOICE})")
        voice_id = f"{model}-{GEMINI_VOICE}"  # 모델이 바뀌면 파일 이름도 바뀌어 전부 새로 만든다

        def synth(text: str) -> tuple[bytes, str]:
            time.sleep(4)  # 분당 제한을 피한다
            return gemini(key, model, text)
    else:
        voice_id = "supertonic-F1"

        def synth(text: str) -> tuple[bytes, str]:
            return supertonic(args.tts, text)

    lines = temi_lines(args.names)
    if args.try_n:  # 짧은 것·이름 인사·긴 것을 골고루 몇 개만 만들어 본다
        by_len = sorted(lines, key=len)
        picks = list(dict.fromkeys([by_len[0], next(t for t in lines if "님, 안녕하세요" in t), by_len[-1], *by_len]))
        folder = OUT / "try"
        folder.mkdir(exist_ok=True)
        for i, text in enumerate(picks[: args.try_n], 1):
            data, ext = synth(text)
            (folder / f"{i}{ext}").write_bytes(data)
            print(f"try/{i}{ext}: {text}")
        print(f"들어 보기: {folder}")
        return

    manifest: dict[str, str] = {}
    failed: list[str] = []
    for i, text in enumerate(lines, 1):
        stem = hashlib.sha1(f"{voice_id}|{text}".encode()).hexdigest()[:12]
        existing = [p for p in (OUT / f"{stem}.mp3", OUT / f"{stem}.wav") if p.exists()]
        if existing:
            manifest[text] = existing[0].name
            continue
        try:
            data, ext = synth(text)
        except QuotaExhausted as e:
            failed += [t for t in lines[i - 1 :] if t not in manifest]
            print(f"[{i}/{len(lines)}] 사용량 제한으로 멈춰요({e}). 나중에 다시 실행하면 이어서 만들어요.")
            break
        except Exception as e:  # 한 문장이 실패해도 나머지는 만든다
            print(f"[{i}/{len(lines)}] 실패: {text} ({e})")
            failed.append(text)
            continue
        (OUT / f"{stem}{ext}").write_bytes(data)
        manifest[text] = f"{stem}{ext}"
        print(f"[{i}/{len(lines)}] {text}")
    if failed:  # 못 만든 문장은 예전 목소리 파일이 있으면 그대로 쓴다
        old_path = OUT / "manifest.json"
        old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else {}
        manifest |= {t: old[t] for t in failed if t in old and (OUT / old[t]).exists()}
    else:  # 다 만들었을 때만 예전 목소리 파일을 지운다
        for stale in {p.name for p in OUT.glob("*.mp3")} | {p.name for p in OUT.glob("*.wav")}:
            if stale not in manifest.values():
                (OUT / stale).unlink()
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    print(f"{len(manifest)}/{len(lines)} lines -> {OUT}")
    if failed:
        print(
            f"{len(failed)}개는 못 만들었어요(예전 음성 파일이 있으면 그걸, 없으면 Temi 내장 TTS로 말함). 나중에 다시 실행하면 채워요."
        )


if __name__ == "__main__":
    main()
