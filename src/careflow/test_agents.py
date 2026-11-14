from datetime import date

import pytest

from careflow.agents.intake import rule_based_intake
from careflow.agents.planner import make_plan
from careflow.agents.safety import screen
from careflow.models import Intake, RequestType, TimeOfDay

TODAY = date(2026, 11, 16)  # Monday


@pytest.mark.parametrize(
    "message",
    [
        "I have chest pain",
        "my chest tightness won't go away",
        "I can't breathe well",
        "shortness of breath since last night",
        "I want to kill myself",
        "I think I took too many pills",
        "her face is drooping",
        "I passed out at work",
    ],
)
def test_red_flags_are_caught(message: str) -> None:
    assert screen(message)


@pytest.mark.parametrize(
    "message",
    ["can I get my chest x-ray results?", "refill my albuterol please", "I'm breathing easier now"],
)
def test_red_flag_screen_does_not_fire_on_ordinary_messages(message: str) -> None:
    assert screen(message) == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Can we reschedule to Thursday?", RequestType.RESCHEDULE),
        ("I'm running low on my metformin", RequestType.REFILL),
        ("I need more sertraline", RequestType.REFILL),
        ("can you send more of the inhaler, almost out", RequestType.REFILL),
        ("What is this charge on my bill?", RequestType.BILLING),
        ("My email has changed, it's [EMAIL_1]", RequestType.CONTACT),
        ("Where do I park?", RequestType.OTHER),
    ],
)
def test_intake_classification(message: str, expected: RequestType) -> None:
    assert rule_based_intake(message, TODAY).request_type == expected


def test_intake_extracts_dates_and_time_of_day() -> None:
    result = rule_based_intake("Could I move it to Friday afternoon?", TODAY)
    assert result.entities.preferred_date == date(2026, 11, 20)
    assert result.entities.time_of_day == TimeOfDay.AFTERNOON

    nxt = rule_based_intake("reschedule to next week in the morning", TODAY)
    assert nxt.entities.preferred_date == date(2026, 11, 23)
    assert nxt.entities.time_of_day == TimeOfDay.MORNING


def test_same_weekday_means_next_week() -> None:
    assert rule_based_intake("reschedule to monday", TODAY).entities.preferred_date == date(
        2026, 11, 23
    )


def test_mixed_intent_is_low_confidence() -> None:
    result = rule_based_intake("refill my lisinopril and move my appointment", TODAY)
    assert result.confidence < 0.75


def test_contact_tokens_only_extracted_for_contact_requests() -> None:
    assert rule_based_intake("call me at [PHONE_1] about my bill", TODAY).entities.new_phone is None
    assert rule_based_intake("my new number is [PHONE_1]", TODAY).entities.new_phone == "[PHONE_1]"


def _intake(rt: RequestType, confidence: float = 0.9) -> Intake:
    return Intake(request_type=rt, summary="x", confidence=confidence)


def test_planner_policy() -> None:
    assert not make_plan(_intake(RequestType.REFILL), 0.75).requires_review
    assert make_plan(_intake(RequestType.REFILL, 0.5), 0.75).requires_review
    assert make_plan(_intake(RequestType.CONTACT), 0.75).requires_review
    # Handing off to staff never needs sign-off, however unsure intake was.
    assert not make_plan(_intake(RequestType.OTHER, 0.3), 0.75).requires_review


def test_crisis_line_only_for_self_harm() -> None:
    from careflow.agents.safety import urgent_reply

    assert "988" in urgent_reply(["self-harm"])
    assert "988" not in urgent_reply(["chest pain"])


def test_medication_alone_is_a_low_confidence_refill() -> None:
    # Named a drug but didn't ask for anything: route it to a person, don't auto-refill.
    result = rule_based_intake("sertraline is making me dizzy", TODAY)
    assert result.request_type == RequestType.REFILL
    assert result.confidence < 0.75
    assert result.entities.medication == "sertraline"
