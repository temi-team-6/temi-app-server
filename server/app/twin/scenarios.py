"""시연 장면을 가상 기기로 끝까지 돌리는 시나리오. 트윈 화면의 버튼과 테스트가 같은 시나리오를 쓴다."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select

from ..agent import agent
from ..db import SessionLocal
from ..models import AgentRequest, RequestStatus, Role, User
from .devices import GAME_RESULT, GOOD_WALK, SLOW_ANIMALS, SLOW_DUAL_WALK, Message, VirtualDevices
from .monitor import monitor

RFID = {"A": "RFID-A-0001", "B": "RFID-B-0002"}
STEP_TIMEOUT_S = 90.0


class Ctx:
    def __init__(self, devices: VirtualDevices, timeout: float = STEP_TIMEOUT_S) -> None:
        self.devices = devices
        self.timeout = timeout

    async def scan(self, resident: str) -> None:
        await self.devices.send("pi", "rfid.scanned", {"tag": RFID[resident]})

    async def say(self, text: str, reply_to: str | None = None) -> None:
        await self.devices.send("temi", "speech.heard", {"text": text}, reply_to)

    async def until(self, device: str, type_: str, mark: int,
                    where: Callable[[Message], bool] = lambda _: True) -> Message:
        """서버가 기기에 보낸 메시지를 기다린다. 모니터를 보므로 그 기기가 실제여도 된다."""
        return await monitor.wait_for(device, type_, where, after=mark, timeout=self.timeout)

    async def until_idle(self, mark: int) -> None:
        await self.until("temi", "temi.show", mark, lambda m: m["payload"].get("screen") == "idle")

    async def walk_session(self, resident: str) -> None:
        mark = monitor.mark()
        await self.scan(resident)
        await self.until_idle(mark)

    async def interrupt_first_walk(self, resident: str, text: str) -> int:
        mark = monitor.mark()
        await self.scan(resident)
        await self.until("pi", "trial.go", mark)
        after_go = monitor.mark()
        await self.say(text)
        return after_go


async def answer_pending(username: str, answer: str) -> bool:
    """그 보호자에게 온 가장 최근 대기 질문에 보호자 대신 답한다. 질문이 없으면 False."""
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.username == username))
        if user is None or user.role != Role.GUARDIAN:
            return False
        req = await db.scalar(
            select(AgentRequest)
            .where(AgentRequest.target_user_id == user.id, AgentRequest.status == RequestStatus.PENDING)
            .order_by(AgentRequest.created_at.desc())
        )
    if req is None:
        return False
    await agent.answer_request(req.id, answer, user)
    return True


async def normal(ctx: Ctx) -> None:
    ctx.devices.walk = GOOD_WALK
    await ctx.walk_session("B")


async def disturbed(ctx: Ctx) -> None:
    ctx.devices.walk = {
        "single": [{**GOOD_WALK["single"], "stopped": True}, {**GOOD_WALK["single"], "min_confidence": 0.3},
                   GOOD_WALK["single"]],
        "animals": [{**GOOD_WALK["animals"], "max_persons": 2}, GOOD_WALK["animals"]],
        "countdown": [{**GOOD_WALK["countdown"], "word_count": 1}, GOOD_WALK["countdown"]],
    }
    await ctx.walk_session("A")


async def slow_both(ctx: Ctx) -> None:
    ctx.devices.walk = SLOW_DUAL_WALK
    await ctx.walk_session("B")


async def slow_one(ctx: Ctx) -> None:
    ctx.devices.walk = {**GOOD_WALK, "animals": [SLOW_ANIMALS, SLOW_ANIMALS]}
    await ctx.walk_session("B")


async def question(ctx: Ctx) -> None:
    ctx.devices.walk = GOOD_WALK
    mark = monitor.mark()
    await ctx.interrupt_first_walk("A", "아무 동물이나 돼요?")
    await ctx.until_idle(mark)


async def pain_mild(ctx: Ctx) -> None:
    ctx.devices.walk = None
    ctx.devices.game_result = GAME_RESULT
    mark = await ctx.interrupt_first_walk("A", "다리가 좀 아파요")
    ask = await ctx.until("temi", "temi.ask", mark)
    await ctx.say("무릎이 조금 시큰거려요", reply_to=ask["id"])
    await ctx.until_idle(mark)


async def pain_severe(ctx: Ctx) -> None:
    ctx.devices.walk = None
    ctx.devices.call_answers = [False, False]
    mark = await ctx.interrupt_first_walk("A", "아파")
    ask = await ctx.until("temi", "temi.ask", mark)
    await ctx.say("일어설 수가 없어", reply_to=ask["id"])
    await ctx.until_idle(mark)


async def fall(ctx: Ctx) -> None:
    ctx.devices.walk = None
    ctx.devices.call_answers = [True]
    mark = monitor.mark()
    await ctx.scan("B")
    await ctx.until("pi", "trial.go", mark)
    await ctx.devices.send("pi", "safety.alert", {"kind": "fall"})
    await ctx.until_idle(mark)


async def refuse(ctx: Ctx) -> None:
    ctx.devices.walk = None
    mark = await ctx.interrupt_first_walk("B", "오늘은 하기 싫어")
    await ctx.until_idle(mark)


async def guardian_sleep(_: Ctx) -> None:
    if not await answer_pending("guardian_b", "잠을 잘 못 주무셨어요"):
        raise LookupError("guardian_b에게 대기 중인 질문이 없습니다. '기준 초과' 시나리오를 먼저 실행하세요.")


async def guardian_decline(_: Ctx) -> None:
    if not await answer_pending("guardian_b", "요즘 길을 자주 헤매세요"):
        raise LookupError("guardian_b에게 대기 중인 질문이 없습니다. '기준 초과' 시나리오를 먼저 실행하세요.")


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    drives: frozenset[str]
    run: Callable[[Ctx], Awaitable[None]]


PI = frozenset({"pi"})
BOTH = frozenset({"pi", "temi"})
NONE: frozenset[str] = frozenset()

SCENARIOS: dict[str, Scenario] = {s.id: s for s in [
    Scenario("normal", "정상 검사", "B님 세 조건 모두 유효, 기준 범위 안", PI, normal),
    Scenario("disturbed", "검사 방해", "멈춤, 화면 밖, 2명, 말 안 함 → 같은 조건 재측정", PI, disturbed),
    Scenario("slow_both", "기준 초과 → 보호자 질문", "두 이중과제 모두 느림 → 해설과 함께 guardian_b에게 질문", PI,
             slow_both),
    Scenario("slow_one", "한 과제만 느림 → 확인 재측정", "동물 이름만 느림 → 한 번 더 측정 → 질문 안 함", PI, slow_one),
    Scenario("question", "검사 중 질문", "\"아무 동물이나 돼요?\" → 답하고 다시 측정", BOTH, question),
    Scenario("pain_mild", "통증(가벼움)", "보행 중단 → 요양사 확인 요청 → 스트룹 게임", BOTH, pain_mild),
    Scenario("pain_severe", "통증(심함) → 통화 무응답", "긴급 알림 → 요양사 순서대로 통화 → 아무도 안 받음", BOTH,
             pain_severe),
    Scenario("fall", "주저앉음 감지", "안전 신호 → 긴급 알림 → 첫 요양사가 통화 받음", BOTH, fall),
    Scenario("refuse", "참여 거부", "\"오늘은 하기 싫어\" → 검사 종료", BOTH, refuse),
    Scenario("guardian_sleep", "보호자 답: 잠을 못 잠", "guardian_b의 대기 질문에 답 → 일시적 변화로 기록", NONE,
             guardian_sleep),
    Scenario("guardian_decline", "보호자 답: 길을 헤맴", "guardian_b의 대기 질문에 답 → 요양사에게 상담 안내 요청", NONE,
             guardian_decline),
]}
