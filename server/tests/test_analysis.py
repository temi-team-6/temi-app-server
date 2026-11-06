from app.agent.walk import rule_explanation
from app.analysis import usual_time_of_day


def test_usual_time_of_day_is_most_frequent() -> None:
    assert usual_time_of_day(["morning", "afternoon", "morning"]) == "morning"
    assert usual_time_of_day([]) is None


def test_rule_explanation_mentions_time_of_day_only_when_it_differs() -> None:
    result = {
        "exceeded": ["animals"],
        "checks": {"animals": {"value": 32.3, "threshold": 14.1}},
        "time_of_day": "afternoon",
        "usual_time_of_day": "morning",
        "time_of_day_differs": True,
    }

    differs = rule_explanation(result, confirmed=True)
    same = rule_explanation({**result, "time_of_day_differs": False}, confirmed=False)

    assert "32.3%(개인 기준 14.1%)" in differs
    assert "평소(오전)와 다른 시간대(오후)" in differs and "한 번 더 측정해도" in differs
    assert "시간대" not in same and "한 번 더" not in same
