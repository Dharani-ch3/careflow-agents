"""Red-flag screen.

This runs first, on the raw message, and doesn't involve a model. If a patient
describes symptoms that could be an emergency, we don't want that to depend on a
classifier's mood or on an API being up.

The list is intentionally conservative. A false positive costs a nurse a phone call;
a false negative costs much more.
"""

from __future__ import annotations

import re

RED_FLAGS: dict[str, re.Pattern[str]] = {
    "chest pain": re.compile(r"\bchest (pain|pressure|tightness)\b"),
    "breathing": re.compile(
        r"\b(can'?t|cannot|can not|trouble|hard to|struggling to) breath|shortness of breath"
    ),
    "stroke signs": re.compile(r"\b(stroke|face (is )?droop|slurred speech|one side (is )?numb)"),
    "self-harm": re.compile(
        r"\b(suicid|kill myself|end my life|hurt myself|self[- ]harm|don'?t want to live)"
    ),
    "overdose": re.compile(r"\b(overdos|took too many|too many pills)"),
    "severe bleeding": re.compile(r"\b(bleeding (heavily|a lot|won'?t stop)|heavy bleeding)"),
    "loss of consciousness": re.compile(r"\b(passed out|fainted|unconscious|seizure)"),
    "anaphylaxis": re.compile(r"\b(throat (is )?(closing|swelling)|anaphyla)"),
}


def screen(message: str) -> list[str]:
    """Return the red flags found in a message (empty list if none)."""
    text = message.lower()
    return [name for name, pattern in RED_FLAGS.items() if pattern.search(text)]


URGENT_REPLY = (
    "What you're describing may need urgent care. If this is an emergency, please call 911 "
    "now or go to the nearest emergency room. We've also flagged your message for a nurse, "
    "who will contact you as soon as possible."
)

CRISIS_LINE = (
    " If you're thinking about harming yourself, you can call or text 988 any time, "
    "day or night, to reach the Suicide & Crisis Lifeline."
)


def urgent_reply(flags: list[str]) -> str:
    """Fixed wording on purpose: this message should never be paraphrased by a model."""
    if {"self-harm", "overdose"} & set(flags):
        return URGENT_REPLY + CRISIS_LINE
    return URGENT_REPLY
