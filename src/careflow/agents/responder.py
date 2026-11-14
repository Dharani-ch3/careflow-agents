"""Responder agent: write the reply the patient sees.

Offline, it uses plain templates. With a model, the model gets a short list of facts
(already pseudonymized) and writes the message; tokens are swapped back to real
values afterwards, so the model never sees the patient's name.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from careflow.agents import prompts
from careflow.agents.safety import URGENT_REPLY
from careflow.llm import LLM
from careflow.models import DraftReply, RequestType, Status


def _when(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    return f"{dt:%A, %B} {dt.day} at {dt:%I:%M %p}".replace(" 0", " ")


def facts_for(
    status: Status,
    request_type: RequestType | None,
    outcome: dict[str, Any] | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    """The only information the reply is allowed to use."""
    facts: dict[str, Any] = {
        "status": status.value,
        "request_type": getattr(request_type, "value", None),
    }
    outcome = outcome or {}
    if "ticket_id" in outcome:
        facts["ticket_id"] = outcome["ticket_id"]
    if outcome.get("error"):
        facts["problem"] = outcome["error"]
    if request_type == RequestType.RESCHEDULE and "new_time" in outcome:
        facts["provider"] = context.get("appointment", {}).get("provider_name")
        facts["old_time"] = _when(outcome["old_time"])
        facts["new_time"] = _when(outcome["new_time"])
    if request_type == RequestType.REFILL and "medication" in outcome:
        facts["medication"] = outcome["medication"]
        facts["refills_left"] = outcome["refills_left_after_this"]
    if request_type == RequestType.BILLING and context.get("invoice"):
        inv = context["invoice"]
        facts["invoice"] = {
            k: inv[k] for k in ("number", "total_charged", "insurance_paid", "patient_owes")
        }
        facts["invoice_lines"] = inv["lines"]
    if request_type == RequestType.CONTACT and "updated" in outcome:
        facts["updated_fields"] = outcome["updated"]
    if outcome.get("category") == "refill_renewal":
        facts["renewal_requested"] = True
    return facts


def template_reply(name: str, facts: dict[str, Any]) -> str:
    hi = f"Hi {name}," if name else "Hi,"
    status, rtype = facts["status"], facts.get("request_type")

    if status == Status.ESCALATED:
        return URGENT_REPLY
    if status == Status.REJECTED:
        return (
            f"{hi} thanks for your message. A member of our team looked at this and will "
            "follow up with you directly."
        )
    if status == Status.HANDED_OFF:
        ticket = facts.get("ticket_id", "")
        if facts.get("renewal_requested"):
            return (
                f"{hi} that prescription has no refills left, so we've asked your care team "
                f"to review a renewal (reference {ticket}). Someone will get back to you within "
                "two business days."
            )
        return (
            f"{hi} thanks for reaching out. I've passed your message to our front-desk team "
            f"(reference {ticket}), and they'll follow up within one business day."
        )
    if status == Status.COMPLETED:
        if rtype == RequestType.RESCHEDULE:
            return (
                f"{hi} you're all set. Your appointment with {facts['provider']} has moved from "
                f"{facts['old_time']} to {facts['new_time']}. If the new time stops working, "
                "just reply here."
            )
        if rtype == RequestType.REFILL:
            left = facts["refills_left"]
            return (
                f"{hi} I've sent your {facts['medication']} refill to the pharmacy. It's usually "
                f"ready within one business day. You'll have {left} "
                f"refill{'s' if left != 1 else ''} left after this one."
            )
        if rtype == RequestType.BILLING:
            inv = facts["invoice"]
            lines = "; ".join(
                f"{line['description']}: {line['charge']} charged, insurance paid "
                f"{line['insurance_paid']}"
                for line in facts["invoice_lines"]
            )
            return (
                f"{hi} here's the breakdown for {inv['number']}. {lines}. In total, "
                f"{inv['total_charged']} was charged and insurance paid {inv['insurance_paid']}, "
                f"so your share is {inv['patient_owes']}. Reply here if something looks off "
                "and our billing team will take a look."
            )
        if rtype == RequestType.CONTACT:
            labels = {"phone": "phone number", "email": "email address"}
            fields = [labels.get(f, f) for f in facts.get("updated_fields", [])]
            what = " and ".join(fields) or "contact details"
            verb = "have" if len(fields) > 1 else "has"
            return (
                f"{hi} your {what} {verb} been updated. If you didn't ask for this change, "
                "please call the clinic right away."
            )
    return f"{hi} we've received your message and a member of our team will follow up."


async def write_reply(
    llm: LLM | None, name_token: str, first_name: str, facts: dict[str, Any]
) -> str:
    if llm is None or facts["status"] == Status.ESCALATED:
        # The urgent message is fixed text on purpose; it should never be paraphrased.
        return template_reply(first_name, facts)
    draft = await llm.structured(
        system=prompts.REPLY,
        user=f"Patient's first name: {name_token}\nFacts:\n{json.dumps(facts, indent=2)}",
        schema=DraftReply,
    )
    return draft.text
