import statistics
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .config import settings
from .models import Condition, Trial, TrialStatus
from .protocol import TrialCompleted

MIN_CONFIDENCE = 0.5
MIN_WORDS = 3
DUAL_CONDITIONS = (Condition.ANIMALS, Condition.COUNTDOWN)
BASELINE_MIN_N = 3
TimeOfDay = Literal["morning", "afternoon", "evening"]
BASELINE_SD_FLOOR = 1.0  # 이중과제 비용 % 단위. 기록이 너무 고르면 임계값이 지나치게 좁아지는 것을 막는다


def invalid_reason(condition: Condition, m: TrialCompleted) -> str | None:
    if m.early_start:
        return "early_start"
    if m.speed_mps is None:
        return "no_measurement"
    if m.max_persons >= 2:
        return "multiple_persons"
    if m.min_confidence is not None and m.min_confidence < MIN_CONFIDENCE:
        return "out_of_frame"
    if m.stopped:
        return "stopped"
    if condition != Condition.SINGLE and m.word_count is not None and m.word_count < MIN_WORDS:
        return "task_not_engaged"
    return None


def dual_task_cost(trials: Iterable[Trial]) -> dict[str, float | None]:
    speeds: dict[Condition, float] = {}
    for t in sorted(trials, key=lambda t: t.id):
        if t.status == TrialStatus.VALID and t.speed_mps:
            speeds[t.condition] = t.speed_mps
    single = speeds.get(Condition.SINGLE)
    return {
        c.value: round((single - speeds[c]) / single * 100, 1) if single and c in speeds else None
        for c in DUAL_CONDITIONS
    }


def baseline_check(history: list[float], value: float) -> dict[str, Any]:
    n = len(history)
    if n < BASELINE_MIN_N:
        return {"n": n, "value": value, "exceeded": False, "note": "insufficient_history"}
    mean = statistics.fmean(history)
    sd = max(statistics.stdev(history), BASELINE_SD_FLOOR)
    threshold = mean + 2 * sd
    return {
        "n": n,
        "value": value,
        "mean": round(mean, 1),
        "sd": round(sd, 1),
        "threshold": round(threshold, 1),
        "exceeded": value > threshold,
    }


def rt_stats(rt_ms: list[float], correct: list[bool]) -> dict[str, float | None]:
    hits = [rt for rt, ok in zip(rt_ms, correct, strict=True) if ok]
    return {
        "accuracy": round(len(hits) / len(rt_ms), 3),
        "rt_mean_ms": round(statistics.fmean(hits), 1) if hits else None,
        "rt_sd_ms": round(statistics.stdev(hits), 1) if len(hits) >= 2 else None,
    }


def time_of_day(dt: datetime) -> TimeOfDay:
    hour = dt.astimezone(ZoneInfo(settings.timezone)).hour
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"
