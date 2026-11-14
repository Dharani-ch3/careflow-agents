"""Intake agent: what is the patient asking for, and what details did they give?"""

from __future__ import annotations

import re
from datetime import date, timedelta

from careflow.agents import prompts
from careflow.llm import LLM
from careflow.models import Entities, Intake, RequestType, TimeOfDay

_KEYWORDS: dict[RequestType, re.Pattern[str]] = {
    RequestType.RESCHEDULE: re.compile(
        r"reschedul|move my appointment|change my appointment|push (back )?my appointment"
        r"|another (day|time)|different (day|time)|can'?t make (it|my appointment)"
        r"|cannot make|move it to"
    ),
    RequestType.REFILL: re.compile(
        r"refill|renew|running (low|out)|ran out|run out|out of my|more of my|prescription"
        r"|\b(need|get|send|want|have) (some )?more\b|almost out\b"
    ),
    RequestType.BILLING: re.compile(
        r"\bbill|invoice|\binv-?\d|charge|\bowe\b|statement|copay|insurance (paid|cover)"
    ),
    RequestType.CONTACT: re.compile(
        r"new (phone|number|cell|email)|update my (phone|number|email|contact)"
        r"|changed my (phone|number|email)|my (phone|number|email) (is|has) changed"
    ),
}

# A small formulary is enough for the offline fallback. A real deployment would
# match against the patient's own medication list (the executor does that too).
_MEDICATIONS = [
    "albuterol", "amlodipine", "atorvastatin", "levothyroxine", "lisinopril", "losartan",
    "metformin", "metoprolol", "omeprazole", "oxycodone", "sertraline", "simvastatin",
    "gabapentin", "hydrocodone", "insulin", "montelukast", "escitalopram", "fluoxetine",
]  # fmt: skip

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _preferred_date(text: str, today: date) -> date | None:
    if m := re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})\b", text):
        month, day = int(m[1]), int(m[2])
        if 1 <= month <= 12 and 1 <= day <= 31:
            candidate = date(today.year, month, day)
            return candidate if candidate >= today else date(today.year + 1, month, day)
    if "tomorrow" in text:
        return today + timedelta(days=1)
    if "next week" in text:
        return today + timedelta(days=7 - today.weekday())
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", text):
            ahead = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def rule_based_intake(masked_message: str, today: date) -> Intake:
    text = masked_message.lower()
    hits = {rt: len(p.findall(text)) for rt, p in _KEYWORDS.items()}
    matched = {rt: n for rt, n in hits.items() if n}

    medication = next((m for m in _MEDICATIONS if m in text), None)

    if not matched and medication:
        # A medication is named but nothing says what the patient wants done with it
        # ("sertraline is making me dizzy" is not a refill). Treat it as a likely refill,
        # but with low confidence, so a person confirms before anything is sent.
        request_type, confidence = RequestType.REFILL, 0.6
    elif not matched:
        request_type, confidence = RequestType.OTHER, 0.5
    else:
        request_type = max(matched, key=lambda rt: matched[rt])
        # One clear category: confident. Several competing: not.
        confidence = 0.9 if len(matched) == 1 else 0.6

    time_of_day = None
    if "morning" in text or re.search(r"\bbefore (noon|lunch)\b", text):
        time_of_day = TimeOfDay.MORNING
    elif re.search(r"afternoon|after (lunch|work|noon)|evening", text):
        time_of_day = TimeOfDay.AFTERNOON

    invoice = re.search(r"\binv-?(\d{3,6})\b", text)
    phone = re.search(r"\[PHONE_\d+\]", masked_message)
    email = re.search(r"\[EMAIL_\d+\]", masked_message)

    entities = Entities(
        medication=medication,
        preferred_date=_preferred_date(text, today),
        time_of_day=time_of_day,
        invoice_number=f"INV-{invoice[1]}" if invoice else None,
        new_phone=phone[0] if phone and request_type == RequestType.CONTACT else None,
        new_email=email[0] if email and request_type == RequestType.CONTACT else None,
    )
    summary = {
        RequestType.RESCHEDULE: "Patient wants to move an upcoming appointment.",
        RequestType.REFILL: "Patient is asking for a prescription refill.",
        RequestType.BILLING: "Patient has a question about a bill.",
        RequestType.CONTACT: "Patient wants to update their contact details.",
        RequestType.OTHER: "Message doesn't fit an automated workflow; needs a person.",
    }[request_type]
    return Intake(
        request_type=request_type, summary=summary, entities=entities, confidence=confidence
    )


async def run_intake(masked_message: str, today: date, llm: LLM | None) -> Intake:
    if llm is None:
        return rule_based_intake(masked_message, today)
    user = f"Today is {today:%A, %Y-%m-%d}.\n\nPatient message:\n{masked_message}"
    return await llm.structured(system=prompts.INTAKE, user=user, schema=Intake)
