import asyncio
from collections.abc import Sequence

import pytest
from sqlalchemy import select

from app import llm
from app.agent import agent
from app.agent.care import PAIN_ASK, REFUSED_SAY, SEVERE_SAY
from app.agent.walk import INSTRUCTION
from app.db import SessionLocal
from app.models import Condition, Observation, SessionStatus, TestSession
from app.twin.monitor import monitor

from .conftest import get_user
from .fakes import GOOD_WALK, FakeDevices, connect_app


class FakeAI:
    """llm.decide 대신 쓴다. 상황 글에 들어 있는 말로 답을 고르고, 없으면 첫 행동과 정해진 말로 답한다."""

    def __init__(self, replies: dict[str, dict[str, str]]) -> None:
        self.replies = replies
        self.situations: list[str] = []

    async def __call__(self, system: str, prompt: str, actions: Sequence[str], *, timeout: float) -> dict[str, str]:
        situation = prompt.split("[상황] ", 1)[1].split("\n[고를 수 있는 행동]", 1)[0]
        self.situations.append(situation)
        for key, reply in self.replies.items():
            if key in situation:
                return {"reason": "테스트", **reply}
        return {"action": actions[0], "say": "AI가 한 말", "reason": "테스트"}


def use_ai(monkeypatch: pytest.MonkeyPatch, replies: dict[str, dict[str, str]]) -> FakeAI:
    fake = FakeAI(replies)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "decide", fake)
    return fake


async def start_first_walk(devices: FakeDevices, tag: str = "RFID-A-0001") -> None:
    await devices.send("pi", "rfid.scanned", {"tag": tag})
    await devices.pi.wait_for("trial.go")


async def latest_session() -> TestSession:
    async with SessionLocal() as db:
        session = await db.scalar(select(TestSession).order_by(TestSession.id.desc()))
    assert session is not None
    return session


async def test_ai_speaks_but_measurement_instructions_stay_fixed(devices: FakeDevices,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    use_ai(monkeypatch, {"인사하고": {"action": "say", "say": "A님 반가워요. 오늘도 함께 걸어 봐요."}})

    await start_first_walk(devices)

    said = [m["payload"]["text"] for m in devices.temi.of_type("temi.say")]
    assert said[0] == "A님 반가워요. 오늘도 함께 걸어 봐요."
    assert INSTRUCTION[Condition.SINGLE] in said
    greet = next(e for e in monitor.log if e["type"] == "ai.decision" and e["payload"]["point"] == "greet")
    assert greet["payload"]["by"] == "ai" and greet["payload"]["reason"] == "테스트"


async def test_ai_asks_before_stopping_and_staff_is_told(devices: FakeDevices,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    use_ai(monkeypatch, {
        "'싫어'라고 말했다": {"action": "confirm_stop", "say": "오늘은 여기까지 할까요?"},
        "그만할래": {"action": "stop", "say": "네, 오늘은 쉬세요. 선생님께 말씀드릴게요."},
    })
    staff = connect_app(await get_user("staff1"))
    await start_first_walk(devices)

    await devices.send("pi", "speech.heard", {"text": "싫어"})
    ask = await devices.temi.wait_for("temi.ask")
    abort = await devices.pi.wait_for("trial.abort")
    await devices.send("pi", "speech.heard", {"text": "응 그만할래"})
    request = await staff.wait_for("request.created")
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    assert ask["payload"]["text"] == "오늘은 여기까지 할까요?"
    assert abort["payload"]["reason"] == "interrupted"
    assert request["payload"]["topic"] == "refused"
    session = await latest_session()
    assert (session.status, session.end_reason) == (SessionStatus.ABORTED, "refused")


async def test_ai_resumes_the_paused_walk_when_asked_to_continue(devices: FakeDevices,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    use_ai(monkeypatch, {
        "'싫어'라고 말했다": {"action": "confirm_stop", "say": "오늘은 여기까지 할까요?"},
        "계속할게": {"action": "resume", "say": "좋아요, 다시 해 봐요."},
    })
    await start_first_walk(devices)

    await devices.send("pi", "speech.heard", {"text": "싫어"})
    await devices.temi.wait_for("temi.ask")
    await devices.send("pi", "speech.heard", {"text": "아니 계속할게"})
    restart = await devices.pi.wait_for("trial.start", lambda m: m["payload"]["attempt"] == 2)

    assert restart["payload"]["condition"] == "single"
    assert agent.state is not None and agent.state.mode == "walk"


async def test_rules_take_over_when_the_ai_breaks_a_guardrail(devices: FakeDevices,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    use_ai(monkeypatch, {"하기 싫어": {"action": "stop", "say": "네, 다음에 다시 올게요."}})
    await start_first_walk(devices)

    await devices.send("pi", "speech.heard", {"text": "오늘은 하기 싫어"})
    await devices.temi.wait_for("temi.show", lambda m: m["payload"].get("screen") == "idle")

    assert devices.temi.of_type("temi.say")[-1]["payload"]["text"] == REFUSED_SAY
    decision = next(e for e in monitor.log if e["type"] == "ai.decision" and e["payload"]["point"] == "speech")
    assert decision["payload"]["by"] == "rule"


async def test_ai_cannot_wave_off_pain_or_downgrade_severe_pain(devices: FakeDevices,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    use_ai(monkeypatch, {
        "'다리가 아파요'라고 말했다": {"action": "reply", "say": "조금만 더 걸어 볼까요?"},
        "일어설 수가 없어": {"action": "mild", "say": "앉아서 하는 게임으로 바꿀게요."},
    })
    await start_first_walk(devices)

    await devices.send("pi", "speech.heard", {"text": "다리가 아파요"})
    ask = await devices.temi.wait_for("temi.ask")
    abort = await devices.pi.wait_for("trial.abort")
    await devices.send("pi", "speech.heard", {"text": "일어설 수가 없어"})
    call = await devices.temi.wait_for("temi.video_call")

    assert ask["payload"]["text"] == PAIN_ASK
    assert abort["payload"]["reason"] == "pain"
    assert call["payload"]["staff_id"]
    assert SEVERE_SAY in [m["payload"]["text"] for m in devices.temi.of_type("temi.say")]


async def test_ai_can_skip_an_invalid_walk(devices: FakeDevices, monkeypatch: pytest.MonkeyPatch) -> None:
    use_ai(monkeypatch, {"무효다": {"action": "skip", "say": "이 걷기는 오늘 넘어갈게요."}})
    devices.walk = {**GOOD_WALK, "single": {**GOOD_WALK["single"], "stopped": True}}

    await devices.send("pi", "rfid.scanned", {"tag": "RFID-A-0001"})
    nxt = await devices.pi.wait_for("trial.start", lambda m: m["payload"]["condition"] == "animals")

    assert nxt["payload"]["attempt"] == 1
    async with SessionLocal() as db:
        notes = list(await db.scalars(select(Observation.text)))
    assert any("건너뜀" in n and "AI 판단" in n for n in notes)


async def test_slow_ai_does_not_hold_back_a_safety_alert(devices: FakeDevices,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    fake = use_ai(monkeypatch, {})
    release = asyncio.Event()

    async def slow_speech(system: str, prompt: str, actions: Sequence[str], *, timeout: float) -> dict[str, str]:
        if "말했다" in prompt:
            await release.wait()
            return {"action": "reply", "say": "늦은 대답", "reason": "테스트"}
        return await fake(system, prompt, actions, timeout=timeout)

    monkeypatch.setattr(llm, "decide", slow_speech)
    await start_first_walk(devices)

    await asyncio.wait_for(devices.send("pi", "speech.heard", {"text": "저기요"}), timeout=1)
    await devices.send("pi", "safety.alert", {"kind": "fall"})
    await devices.temi.wait_for("temi.video_call")
    release.set()

    assert agent.state is not None and agent.state.listener is None
    assert "늦은 대답" not in [m["payload"]["text"] for m in devices.temi.of_type("temi.say")]
