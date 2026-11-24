"""검사 진행 AI. 판단 지점마다 Claude가 허용된 행동 하나와 Temi가 할 말을 고른다(llm.decide).

서버가 지키는 선(가드레일). 이 선을 넘는 판단은 버리고 그 지점은 규칙대로 한다:
- 허용 목록 밖의 행동, 너무 긴 말, 금지 표현(진단·병명, 지킬 수 없는 약속)이 들어간 말.
- 안전 신호·영상통화, 측정 유효성·기준선 비교, 보호자에게 물을지는 AI에게 묻지 않는다(규칙).
- 측정 지시와 출발 신호처럼 측정 조건이 되는 대사는 고정 문장이다(검사끼리 비교할 수 있어야 한다).
각 흐름은 AI 판단 위에 안전 쪽 규칙을 한 번 더 건다(통증 키워드는 넘기지 못하고, 심한 통증 키워드는 낮추지 못한다).
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

SYSTEM = """너는 요양시설에서 어르신의 보행 검사를 진행하는 로봇 Temi다.
- 한국어 존댓말로 1~2문장, 짧고 쉬운 말로 말한다.
- 할 말에는 고른 행동으로 바로 일어날 일을 어르신이 알아듣게 담는다(예: 앉아서 하는 게임으로 바꾼다). 되물을 때는 질문 한 문장으로 바로 묻는다.
- 진단, 병명, 위험도, 점수 해석을 말하지 않는다.
- 지킬 수 없는 약속(나중에 다시 오겠다, 결과를 알려 드리겠다 등)을 하지 않는다. 할 수 있는 일은 지금 말하기, 측정 다시 하기, 검사 끝내기, 선생님(요양사)께 알리기뿐이다.
- 측정 방법 안내와 출발 신호는 시스템이 따로 말하므로 되풀이하지 않는다.
- 뜻이 애매하면 되묻는 쪽을 고른다. 아프다는 말은 가볍게 넘기지 않는다.
- 마이크는 주변 소리를 계속 듣고 말을 덩어리로 끊어 받아 적는다. 다른 사람 목소리, 혼잣말, 잡음, 잘린 말이 섞일 수 있다. 어르신이 로봇에게 한 말이 아니거나 할 일이 없으면 ignore를 고른다.
상황과 고를 수 있는 행동을 보고 act 도구로 답한다."""

MAX_SAY = 90
BANNED = ("치매", "파킨슨", "뇌졸중", "진단", "질환", "다시 올게", "다시 찾아", "다음에 다시", "결과를 알려")
DIALOG_TURNS = 12
SILENT_ACTIONS = frozenset({"ignore"})


@dataclass(frozen=True)
class Decision:
    action: str
    say: str
    reason: str


@dataclass(frozen=True)
class Context:
    resident_name: str
    now: str
    dialog: Sequence[tuple[str, str]]
    notes: Sequence[str]


def build_prompt(situation: str, actions: Mapping[str, str], ctx: Context) -> str:
    dialog = "\n".join(f"{who}: {text}" for who, text in ctx.dialog[-DIALOG_TURNS:]) or "(아직 없음)"
    notes = "\n".join(f"- {n}" for n in ctx.notes) or "(없음)"
    choices = "\n".join(f"- {name}: {desc}" for name, desc in actions.items())
    return (f"[어르신] {ctx.resident_name}님\n[지금] {ctx.now}\n[오늘 대화(오래된 순)]\n{dialog}\n"
            f"[최근 관찰 기록]\n{notes}\n[상황] {situation}\n[고를 수 있는 행동]\n{choices}")


def accept(raw: Mapping[str, str] | None, actions: Mapping[str, str]) -> Decision | None:
    """llm.decide 결과가 가드레일 안이면 Decision, 아니면 None(규칙대로 한다)."""
    if raw is None:
        return None
    decision = Decision(raw["action"], raw["say"], raw["reason"])
    problem = None
    if decision.action not in actions:
        problem = "허용되지 않은 행동"
    elif (decision.action not in SILENT_ACTIONS and not decision.say) or len(decision.say) > MAX_SAY:
        problem = "말이 비었거나 너무 길다"
    elif any(word in decision.say for word in BANNED):
        problem = "금지 표현"
    if problem is not None:
        log.warning("AI decision rejected (%s): %s", problem, asdict(decision))
        return None
    return decision
