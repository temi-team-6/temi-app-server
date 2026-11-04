"""보행 검사: 출발 동기화, 측정 유효성 판정과 재측정, 세 조건 완료 후 기준선 비교."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from .. import analysis
from ..config import settings
from ..db import SessionLocal
from ..models import (
    Condition,
    ObservationSource,
    Priority,
    RequestKind,
    Resident,
    Role,
    SessionStatus,
    TestSession,
    Trial,
    TrialStatus,
)
from ..protocol import TrialCompleted
from ..queries import resident_sessions
from .core import AgentCore, SessionState, _log_task_error

log = logging.getLogger(__name__)


WALK_PLAN = (Condition.SINGLE, Condition.ANIMALS, Condition.COUNTDOWN)
MAX_ATTEMPTS = 3
INSTRUCTION = {
    Condition.SINGLE: "시작선에 서 주세요. 신호가 나면 평소 속도로 끝선까지 걸어가세요.",
    Condition.ANIMALS: "이번에는 걸으면서 생각나는 동물 이름을 계속 소리 내어 말해 주세요.",
    Condition.COUNTDOWN: "이번에는 걸으면서 100부터 3씩 거꾸로 세어 주세요.",
}
RETRY_SAY = {
    "stopped": "중간에 멈추셨네요. 끝선까지 멈추지 말고 걸어 주세요.",
    "out_of_frame": "제가 잘 보지 못했어요. 표시된 선 안쪽으로 걸어 주세요.",
    "multiple_persons": "다른 분이 지나가셔서 다시 측정할게요.",
    "task_not_engaged": "말씀이 잘 들리지 않았어요. 걸으면서 소리 내어 계속 말해 주세요.",
    "no_measurement": "측정이 잘 되지 않았어요.",
    "no_person": "시작선 앞에 서 계신 모습이 보이지 않아요.",
    "early_start": "출발 신호를 듣고 걸어 주세요.",
    "interrupted": "다시 처음부터 해볼게요.",
}


class WalkFlow(AgentCore):
    async def start_session(self, resident_id: int) -> int:
        async with SessionLocal() as db:
            resident = await db.get(Resident, resident_id)
            if resident is None:
                raise LookupError(f"resident {resident_id} not found")
            if self.state is not None:
                await self._finish(SessionStatus.ABORTED, "replaced_by_new_session")
            session = TestSession(resident_id=resident.id)
            db.add(session)
            await db.commit()
            self.state = SessionState(session.id, resident.id, resident.name)
        await self._temi("temi.say", {"text": f"{resident.name}님, 안녕하세요. 오늘도 걷기 검사를 해볼게요."})
        await self._start_trial(WALK_PLAN[0], attempt=1)
        return self.state.session_id

    async def _on_rfid(self, tag: str) -> None:
        async with SessionLocal() as db:
            resident = await db.scalar(select(Resident).where(Resident.rfid_tag == tag))
        if resident is None:
            await self._temi("temi.say", {"text": "등록되지 않은 카드예요. 선생님께 확인해 주세요."})
            return
        await self.start_session(resident.id)

    async def _start_trial(self, condition: Condition, attempt: int) -> None:
        st = self._require_state()
        async with SessionLocal() as db:
            trial = Trial(session_id=st.session_id, condition=condition, attempt=attempt)
            db.add(trial)
            await db.commit()
        self._cancel_gate()
        st.mode = "walk"
        st.trial_id = trial.id
        st.ready = asyncio.Event()
        await self._pi("trial.start", {"trial_id": trial.id, "condition": condition.value, "attempt": attempt})
        await self._temi("temi.show", {"screen": "walk", "condition": condition.value, "attempt": attempt})
        st.gate = asyncio.create_task(self._run_gate(trial.id, condition))
        st.gate.add_done_callback(_log_task_error)

    async def _run_gate(self, trial_id: int, condition: Condition) -> None:
        st = self._require_state()
        await self._say_and_wait(INSTRUCTION[condition])
        for prompt in range(settings.ready_prompts + 1):
            try:
                await asyncio.wait_for(st.ready.wait(), settings.ready_timeout_s)
                break
            except TimeoutError:
                if prompt == settings.ready_prompts:
                    await self._fail_trial(trial_id, "no_person")
                    return
                await self._say_and_wait("시작선 앞에 서 주세요.")
        if condition != Condition.SINGLE:
            await self._say_and_wait("지금부터 말하기를 시작하세요.")
            await asyncio.sleep(settings.lead_in_s)
        await self._temi("temi.say", {"text": "출발하세요."})
        await self._pi("trial.go", {"trial_id": trial_id})

    async def _say_and_wait(self, text: str) -> None:
        st = self._require_state()
        st.said = asyncio.Event()
        st.awaiting_say = await self._temi("temi.say", {"text": text})
        try:
            await asyncio.wait_for(st.said.wait(), settings.say_done_timeout_s)
        except TimeoutError:
            log.warning("temi.say_done not received within %ss, continuing", settings.say_done_timeout_s)

    def _on_trial_ready(self, trial_id: int) -> None:
        if self.state is not None and self.state.trial_id == trial_id:
            self.state.ready.set()

    def _on_say_done(self, reply_to: str | None) -> None:
        if self.state is not None and reply_to is not None and reply_to == self.state.awaiting_say:
            self.state.said.set()

    async def _fail_trial(self, trial_id: int, reason: str) -> None:
        st = self._require_state()
        st.trial_id = None
        await self._pi("trial.abort", {"trial_id": trial_id, "reason": reason})
        async with SessionLocal() as db:
            trial = await db.get(Trial, trial_id)
            assert trial is not None
            trial.status = TrialStatus.INVALID
            trial.invalid_reason = reason
            trial.completed_at = datetime.now(UTC)
            await db.commit()
        await self._after_trial(trial.condition, trial.attempt, reason)

    async def _on_trial_completed(self, m: TrialCompleted) -> None:
        st = self.state
        if st is None or st.trial_id != m.trial_id:
            log.warning("ignored trial.completed for inactive trial %s", m.trial_id)
            return
        self._cancel_gate()
        async with SessionLocal() as db:
            trial = await db.get(Trial, m.trial_id)
            assert trial is not None
            reason = analysis.invalid_reason(trial.condition, m)
            for key in ("speed_mps", "duration_s", "stopped", "max_persons", "min_confidence", "word_count",
                        "error_count", "raw"):
                setattr(trial, key, getattr(m, key))
            trial.completed_at = datetime.now(UTC)
            trial.status = TrialStatus.INVALID if reason else TrialStatus.VALID
            trial.invalid_reason = reason
            await db.commit()
        st.trial_id = None
        await self._after_trial(trial.condition, trial.attempt, reason)

    async def _after_trial(self, condition: Condition, attempt: int, reason: str | None) -> None:
        st = self._require_state()
        if reason is None:
            await self._next_condition()
        elif attempt < MAX_ATTEMPTS:
            await self._temi("temi.say", {"text": RETRY_SAY[reason] + " 다시 해볼게요."})
            await self._start_trial(condition, attempt + 1)
        else:
            await self._observe(st.resident_id, ObservationSource.AGENT,
                                f"{condition.value} 조건 측정이 {MAX_ATTEMPTS}회 무효({reason})여서 건너뜀")
            await self._temi("temi.say", {"text": "이 걷기는 오늘 건너뛸게요."})
            await self._next_condition()

    async def _next_condition(self) -> None:
        st = self._require_state()
        st.step += 1
        if st.step < len(WALK_PLAN):
            await self._start_trial(WALK_PLAN[st.step], attempt=1)
        else:
            await self._finish_walk()

    async def _finish_walk(self) -> None:
        st = self._require_state()
        async with SessionLocal() as db:
            sessions = await resident_sessions(db, st.resident_id)
        current = next(s for s in sessions if s.id == st.session_id)
        today = analysis.dual_task_cost(current.trials)
        tod = analysis.time_of_day(current.started_at)
        past = [s for s in sessions if s.id != st.session_id and s.status == SessionStatus.COMPLETED]
        same_tod = [s for s in past if analysis.time_of_day(s.started_at) == tod]
        reference = same_tod if len(same_tod) >= analysis.BASELINE_MIN_N else past
        checks: dict[str, Any] = {}
        for cond, value in today.items():
            if value is None:
                continue
            history = [v for s in reference if (v := analysis.dual_task_cost(s.trials)[cond]) is not None]
            checks[cond] = analysis.baseline_check(history, value)
        exceeded = [c for c, chk in checks.items() if chk["exceeded"]]
        data = {"dual_task_cost": today, "time_of_day": tod, "same_time_of_day": reference is same_tod,
                "checks": checks}
        if exceeded:
            await self._observe(st.resident_id, ObservationSource.AGENT,
                                f"이중과제 비용이 개인 기준보다 높음({', '.join(exceeded)}). 보호자에게 최근 상태를 묻는다.",
                                data=data)
            await self._create_request(
                st.resident_id, Role.GUARDIAN, RequestKind.QUESTION, Priority.NORMAL, "condition_check",
                f"{st.resident_name}님의 오늘 걷기 검사 결과가 평소와 조금 달랐습니다. "
                "이번 주 수면이나 컨디션에 변화가 있었나요?",
                options=["잠을 잘 못 주무셨어요", "평소와 같아요", "요즘 길을 자주 헤매세요"],
            )
        else:
            await self._observe(st.resident_id, ObservationSource.AGENT, "보행 검사 결과가 개인 기준 범위 안", data=data)
        await self._temi("temi.say", {"text": "오늘 걷기 검사를 모두 마쳤어요. 수고하셨어요."})
        await self._finish(SessionStatus.COMPLETED, None)

    async def _abort_trial(self, reason: str) -> tuple[Condition, int]:
        st = self._require_state()
        self._cancel_gate()
        trial_id, st.trial_id = st.trial_id, None
        if trial_id is None:
            return WALK_PLAN[min(st.step, len(WALK_PLAN) - 1)], 0
        await self._pi("trial.abort", {"trial_id": trial_id, "reason": reason})
        async with SessionLocal() as db:
            trial = await db.get(Trial, trial_id)
            assert trial is not None
            trial.status = TrialStatus.ABORTED
            trial.invalid_reason = reason[:50]
            trial.completed_at = datetime.now(UTC)
            await db.commit()
        return trial.condition, trial.attempt
