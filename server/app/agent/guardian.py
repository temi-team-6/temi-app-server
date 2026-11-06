"""보호자·요양사 답변 처리와 보호자 답변에 따른 판단."""

import logging
from datetime import UTC, datetime

from .. import llm
from ..db import SessionLocal
from ..hub import hub
from ..models import (
    AgentRequest,
    ObservationSource,
    Priority,
    RequestKind,
    RequestStatus,
    Role,
    User,
)
from ..protocol import make
from ..schemas import request_out
from .care import CareFlow

log = logging.getLogger(__name__)


SLEEP_WORDS = ("잠", "피곤", "컨디션")
DECLINE_WORDS = ("헤매", "깜빡", "반복", "잊어")
ANSWER_LABELS = ("temporary", "daily_change", "no_change")
ANSWER_INSTRUCTION = (
    "보호자가 '이번 주 수면이나 컨디션에 변화가 있었나요?'라는 질문에 답했다. 답의 뜻을 분류한다.\n"
    "temporary: 수면 부족, 피곤, 감기, 통증 같은 일시적인 컨디션 문제.\n"
    "daily_change: 길을 헤매, 같은 말을 반복, 물건·약·불을 잊음 같은 일상생활의 변화.\n"
    "no_change: 특별한 변화가 없거나 판단할 내용이 없음."
)


def answer_kind_by_keywords(answer: str) -> str | None:
    if any(w in answer for w in DECLINE_WORDS):
        return "daily_change"
    if any(w in answer for w in SLEEP_WORDS):
        return "temporary"
    return None


class GuardianFlow(CareFlow):
    async def answer_request(self, request_id: int, answer: str, user: User) -> AgentRequest:
        async with SessionLocal() as db:
            req = await db.get(AgentRequest, request_id)
            if req is None:
                raise LookupError(f"request {request_id} not found")
            req.answer = answer
            req.answered_by = user.id
            req.answered_at = datetime.now(UTC)
            req.status = RequestStatus.ANSWERED
            await db.commit()
        await hub.send_apps(make("request.updated", request_out(req).model_dump(mode="json")), role=req.target_role,
                            user_id=req.target_user_id if req.target_role == Role.GUARDIAN else None)
        if req.resident_id is not None and req.topic == "condition_check":
            await self._judge_guardian_answer(req.resident_id, answer)
        elif req.resident_id is not None:
            await self._observe(req.resident_id, ObservationSource(user.role.value), answer, author_id=user.id)
        return req

    async def _judge_guardian_answer(self, resident_id: int, answer: str) -> None:
        await self._observe(resident_id, ObservationSource.GUARDIAN, answer)
        kind = (answer_kind_by_keywords(answer)
                or await llm.classify(ANSWER_INSTRUCTION, answer, ANSWER_LABELS)
                or "no_change")
        if kind == "daily_change":
            await self._observe(resident_id, ObservationSource.AGENT, "보호자 답변상 일상 변화가 보여 관찰을 이어감. 요양사에게 상담 안내 요청.")
            await self._create_request(resident_id, Role.STAFF, RequestKind.CHECK, Priority.NORMAL, "consult",
                                       f"보호자가 '{answer}'라고 답했습니다. 상담 기관 안내를 부탁드립니다.")
        elif kind == "temporary":
            await self._observe(resident_id, ObservationSource.AGENT, "수면·컨디션 영향 가능성이 있어 일시적 변화로 보고 다음 주 재측정.")
        else:
            await self._observe(resident_id, ObservationSource.AGENT, "특이 답변 없음. 다음 정기 검사에서 다시 비교.")
