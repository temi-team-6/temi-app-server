"""음성 받아 적기(Google Gemini)와 이중과제 채점.

Pi는 걷는 동안 녹음만 하고(걸음 측정에 부담을 주지 않도록), 걷기가 끝나면 녹음을 서버에 올린다.
서버가 Gemini로 들리는 그대로 받아 적고, 받아 적은 글로 단어 수·오류 수를 센다.
녹음 원본은 저장하지 않는다. 받아 적은 글만 남긴다.

엔진은 PoC(poc_voice)에서 걸으며 말한 녹음으로 Whisper와 비교해 골랐다.
"""

import asyncio
import base64
import logging
import re
import time

import httpx

from .config import settings
from .models import Condition

log = logging.getLogger(__name__)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
FALLBACK_MODEL = "gemini-flash-latest"  # 설정한 모델이 사라졌을 때(404)만 쓴다

# 검사에서는 틀린 숫자도 중요한 정보다. 정답 순서를 주거나 고쳐 적게 하면 오류가 사라진다.
RULE = (
    "이 녹음을 들리는 그대로 한국어로 받아 적으세요. 말한 사람이 틀린 숫자나 말을 했어도 고치지 마세요. "
    "숫자는 아라비아 숫자로, 단어나 숫자 사이는 쉼표로 구분하고, 받아 적은 내용만 출력하세요. "
    "말소리가 없으면 아무것도 출력하지 마세요."
)
CONTEXT = {
    Condition.ANIMALS: " 참고: 동물 이름을 말하는 과제입니다.",
    Condition.COUNTDOWN: " 참고: 숫자를 거꾸로 세는 과제입니다.",
}
MIME = {"m4a": "audio/mp4", "mp4": "audio/mp4", "mpeg": "audio/mpeg", "mp3": "audio/mpeg", "x-wav": "audio/wav"}


class SttError(Exception):
    """받아 적기 실패(키 없음, 사용량 제한이 풀리지 않음, 응답 이상)."""


def enabled() -> bool:
    return bool(settings.gemini_api_key.strip())


def audio_mime(content_type: str | None) -> str:
    """요청의 Content-Type을 Gemini가 받는 형식으로 맞춘다. 모르면 wav로 본다."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if not mime.startswith("audio/"):
        return "audio/wav"
    return MIME.get(mime.removeprefix("audio/"), mime)


async def transcribe(audio: bytes, mime: str, condition: Condition | None = None) -> tuple[str, str]:
    """녹음을 받아 적는다. (받아 적은 글, 쓴 모델)을 돌려준다.

    사용량 제한(429)·혼잡(503)이면 구글이 알려 준 시간만큼 쉬고 다시 시도하되,
    전체 시간이 settings.stt_timeout_s를 넘으면 포기한다.
    """
    if not enabled():
        raise SttError("GEMINI_API_KEY가 없음")
    prompt = RULE + CONTEXT.get(condition, "") if condition else RULE
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    headers = {"x-goog-api-key": settings.gemini_api_key.strip()}
    deadline = time.monotonic() + settings.stt_timeout_s
    model = settings.gemini_stt_model
    async with httpx.AsyncClient(timeout=settings.stt_timeout_s) as client:
        while True:
            try:
                res = await client.post(f"{GEMINI_API}/{model}:generateContent", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise SttError(f"연결 실패: {type(e).__name__}") from e
            if res.status_code == 404 and model != FALLBACK_MODEL:
                log.warning("STT model %s unavailable, falling back to %s", model, FALLBACK_MODEL)
                model = FALLBACK_MODEL
                continue
            if res.status_code in (429, 503):
                delay = _retry_delay(res.text)
                if time.monotonic() + delay > deadline:
                    raise SttError(f"사용량 제한({res.status_code})이 제때 풀리지 않음")
                log.warning("STT %s, retrying in %ss", res.status_code, delay)
                await asyncio.sleep(delay)
                continue
            if res.status_code != 200:
                raise SttError(f"{res.status_code} {res.text[:200]}")
            parts = (res.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            return " ".join(p.get("text", "") for p in parts).strip(), model


def _retry_delay(body: str) -> float:
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
    return float(match.group(1)) + 1 if match else 5.0


# ---- 채점: 받아 적은 글 → 단어 수·오류 수 (규칙은 PoC와 같다) ----

ANIMALS = sorted(
    set(
        """강아지 개 고양이 사자 호랑이 곰 여우 늑대 토끼 다람쥐 쥐 햄스터 소 말 돼지 양 염소 닭 오리 거위 칠면조
코끼리 기린 하마 코뿔소 얼룩말 원숭이 고릴라 침팬지 캥거루 코알라 판다 낙타 사슴 노루 고라니 너구리 오소리 수달 해달
박쥐 고슴도치 두더지 족제비 스컹크 치타 표범 재규어 퓨마 하이에나 펭귄 독수리 매 부엉이 올빼미 참새 비둘기 까치 까마귀
제비 앵무새 갈매기 학 두루미 백조 공작 타조 플라밍고 황새 기러기 꿩 메추리 뱀 악어 거북이 거북 도마뱀 카멜레온 개구리
두꺼비 고래 돌고래 상어 물개 바다표범 바다사자 문어 오징어 낙지 게 새우 가재 조개 붕어 잉어 고등어 연어 참치 금붕어
나비 벌 꿀벌 개미 거미 잠자리 메뚜기 귀뚜라미 매미 모기 파리 무당벌레 사마귀 지렁이 달팽이 소라 해파리 불가사리 미어캣 알파카
북극곰 반달곰 불곰 팬더 라마 나무늘보 아르마딜로 바다거북 망아지 송아지 병아리 생쥐 들쥐 청설모 담비 물소 들소 순록 무스
엘크 버팔로""".split()
    ),
    key=len,
    reverse=True,  # 긴 이름 먼저 찾아야 '바다거북'이 '거북'으로 잘리지 않는다
)
DIGIT = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
COUNTDOWN_START = 100
COUNTDOWN_STEP = 3


def korean_number(word: str) -> int | None:
    """'구십칠' → 97, '백' → 100. 한자어 수사 0~199만 다룬다."""
    if not word or any(c not in "일이삼사오육칠팔구십백" for c in word):
        return None
    total, rest = 0, word
    if "백" in rest:
        head, rest = rest.split("백", 1)
        total += (DIGIT.get(head, 1) if head else 1) * 100
    if "십" in rest:
        head, rest = rest.split("십", 1)
        total += (DIGIT.get(head, 1) if head else 1) * 10
    if rest:
        if rest not in DIGIT:
            return None
        total += DIGIT[rest]
    return total


def numbers(text: str) -> list[int]:
    out = []
    for token in re.findall(r"\d+|[일이삼사오육칠팔구십백]+", text):
        n = int(token) if token.isdigit() else korean_number(token)
        if n is not None and 0 <= n <= COUNTDOWN_START:
            out.append(n)
    return out


def animals(text: str) -> list[str]:
    """두 글자 이상 이름은 붙어 있어도 찾고, '개·소·말' 같은 한 글자는 따로 떨어진 단어일 때만 센다
    ('말해요'의 '말', '소리'의 '소'를 동물로 세지 않으려고)."""
    tokens = re.findall(r"[가-힣]+", text)
    found = [t for t in tokens if len(t) == 1 and t in ANIMALS]
    joined = "|".join(t for t in tokens if len(t) > 1)
    for name in (a for a in ANIMALS if len(a) > 1):
        while name in joined:
            found.append(name)
            joined = joined.replace(name, "|", 1)
    return found


def score(condition: Condition, text: str) -> tuple[int, int | None]:
    """(단어 수, 오류 수). 동물은 서로 다른 동물 수(반복은 한 번), 오류 수는 없다.
    숫자 세기는 말한 숫자 개수와 오류 수. 연속 빼기 검사의 보통 채점처럼 각 숫자를 바로 앞에 말한 숫자에서
    3을 뺀 값과 비교한다(앞에서 틀렸어도 거기서 3을 바르게 빼면 맞음). 첫 숫자는 100이나 97이면 맞음."""
    if condition == Condition.ANIMALS:
        return len(set(animals(text))), None
    said = numbers(text)
    if not said:
        return 0, 0
    errors = int(said[0] not in (COUNTDOWN_START, COUNTDOWN_START - COUNTDOWN_STEP))
    errors += sum(1 for prev, cur in zip(said, said[1:], strict=False) if cur != prev - COUNTDOWN_STEP)
    return len(said), errors
