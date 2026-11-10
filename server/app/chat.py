"""보호자 질문 챗봇. 검사 추세와 관찰 기록을 근거로 Claude가 답하고, 실패하면 고정 응답을 쓴다."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import llm
from .models import ChatMessage, Observation, Resident, TestSession
from .queries import resident_sessions, session_summary

CONSULT_GUIDE = "치매상담콜센터(1899-9988)나 가까운 치매안심센터에서 상담을 받으실 수 있습니다."
HISTORY_TURNS = 10
TREND_SESSIONS = 6
CONTEXT_OBSERVATIONS = 8

SYSTEM = """너는 'Temi 보행 검사' 서비스의 보호자 상담 도우미다. 보호자가 {name}님에 대해 묻는다.

지켜야 할 것:
- 진단하지 않는다. 치매, 경도인지장애 같은 병명이나 "가능성이 높다" 같은 판단을 말하지 않는다.
- 아래 검사 기록의 추세만 사실대로 설명한다. 기록에 없는 수치를 만들지 않는다.
- 기록에 [가상]이 붙어 있으면 시연용 가상 기록이라고 밝힌다.
- 이중과제 비용은 그냥 걸을 때보다 말하면서 걸을 때 얼마나 느려지는지(%)다. 높을수록 더 많이 느려진 것이다.
- 걱정되는 증상을 말하면 언제부터, 얼마나 자주인지 한 가지만 되묻는다.
- 상담이 필요해 보이면 이렇게 안내한다: {consult}
- 쓰러짐, 의식 저하, 심한 통증 같은 응급 상황이면 먼저 바로 119에 연락하라고 말한다.
- 보호자가 말한 내용은 관찰 기록으로 남아 다음 검사 계획에 반영된다.
- 한국어 존댓말로 3~5문장, 목록 없이 짧게 답한다.

검사 기록(최근 순):
{trend}

최근 관찰 기록(최근 순):
{observations}"""

TOD_LABEL = {"morning": "오전", "afternoon": "오후", "evening": "저녁"}
GAME_LABEL = {"whack": "두더지 잡기", "stroop": "스트룹", "memory": "그림카드"}


def _fmt(value: float | None, unit: str) -> str:
    return "없음" if value is None else f"{value:g}{unit}"


def trend_lines(sessions: Sequence[TestSession]) -> list[str]:
    lines = []
    for s in sorted(sessions, key=lambda s: s.started_at, reverse=True)[:TREND_SESSIONS]:
        summary = session_summary(s)
        parts = [
            f"{summary.started_at.astimezone().strftime('%Y-%m-%d')} {TOD_LABEL[summary.time_of_day]}",
            f"그냥 걷기 {_fmt(summary.single_speed_mps, ' m/s')}",
            f"이중과제 비용 동물 이름 {_fmt(summary.dual_task_cost.animals, '%')}"
            f" / 숫자 거꾸로 세기 {_fmt(summary.dual_task_cost.countdown, '%')}",
        ]
        parts += [
            f"{GAME_LABEL.get(g.game_type.value, g.game_type.value)} 반응시간 {_fmt(g.rt_mean_ms, 'ms')}"
            f"(변동 {_fmt(g.rt_sd_ms, 'ms')}), 정답률 {_fmt(None if g.accuracy is None else round(g.accuracy * 100), '%')}"
            for g in summary.games
        ]
        lines.append("- " + ", ".join(parts) + (" [가상]" if summary.synthetic else ""))
    return lines or ["- 아직 검사 기록이 없습니다."]


def fallback_reply(sessions: Sequence[TestSession]) -> str:
    recent = [s for s in sessions if s.trials][-4:]
    costs = [session_summary(s).dual_task_cost.animals for s in recent]
    trend_text = ", ".join(f"{c}%" for c in costs if c is not None) or "아직 기록이 부족합니다"
    return (
        "말씀하신 내용은 관찰 기록으로 남겨 다음 검사 계획에 반영하겠습니다. "
        f"이 서비스는 진단을 하지 않습니다. 최근 걷기 검사의 이중과제 비용은 {trend_text}입니다. "
        f"{CONSULT_GUIDE} 언제부터, 얼마나 자주 그런 모습이 보이셨나요?"
    )


async def history(db: AsyncSession, resident_id: int, user_id: int) -> list[ChatMessage]:
    rows = await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.resident_id == resident_id, ChatMessage.user_id == user_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    return list(rows)


async def answer(db: AsyncSession, resident: Resident, user_id: int, message: str) -> ChatMessage:
    previous = await history(db, resident.id, user_id)
    sessions = await resident_sessions(db, resident.id)
    observations = await db.scalars(
        select(Observation).where(Observation.resident_id == resident.id)
        .order_by(Observation.created_at.desc()).limit(CONTEXT_OBSERVATIONS)
    )
    system = SYSTEM.format(
        name=resident.name,
        consult=CONSULT_GUIDE,
        trend="\n".join(trend_lines(sessions)),
        observations="\n".join(f"- [{o.source.value}] {o.text}" for o in observations) or "- 없음",
    )
    turns: list[llm.Turn] = [("user" if m.role == "user" else "assistant", m.text)
                             for m in previous[-HISTORY_TURNS:]]
    reply = await llm.complete(system, message, history=turns, tier="smart", max_tokens=2000)
    db.add(ChatMessage(resident_id=resident.id, user_id=user_id, role="user", text=message))
    out = ChatMessage(resident_id=resident.id, user_id=user_id, role="assistant",
                      text=reply or fallback_reply(sessions), mock=reply is None)
    db.add(out)
    await db.commit()
    return out
