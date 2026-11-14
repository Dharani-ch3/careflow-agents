"""Verifier agent: a second pair of eyes before anything is written.

Two layers:

1. **Hard checks in code.** Is the slot still open (fresh read, not the executor's
   cached copy)? Does it match the day and time the patient asked for? These catch
   the failures that matter most, and they don't depend on a model.
2. **A model critique**, when a model is configured. It is asked one narrow question:
   does this action actually address the request? It catches misreads the hard
   checks can't, like the right slot for the wrong appointment.

A failed check comes back with structured ``feedback`` (for example slot ids to
avoid), so the executor's next attempt is different, not just repeated.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from careflow.agents import prompts
from careflow.gateway import ToolGateway, ToolRefused
from careflow.llm import LLM
from careflow.models import Critique, Intake, ProposedAction, RequestType, TimeOfDay, Verdict


async def verify(
    *,
    gateway: ToolGateway,
    llm: LLM | None,
    request_id: str,
    masked_message: str,
    intake: Intake,
    proposed: ProposedAction | None,
    ctx: dict[str, Any],
    previous_feedback: dict[str, Any],
) -> Verdict:
    issues: list[str] = list(ctx.get("issues", []))
    feedback: dict[str, Any] = {
        "exclude_slot_ids": list(previous_feedback.get("exclude_slot_ids", [])),
    }
    confidence = intake.confidence
    e = intake.entities

    if intake.request_type == RequestType.RESCHEDULE and proposed is not None:
        slot_id = proposed.args["slot_id"]
        try:
            slot = await gateway.call(request_id, "get_slot", {"slot_id": slot_id})
        except ToolRefused as exc:
            slot = None
            issues.append(exc.message)
        if slot is not None:
            starts = datetime.fromisoformat(slot["starts_at"])
            problems = []
            if not slot["is_open"]:
                problems.append(f"slot {slot_id} was taken since it was offered")
            if slot["provider_id"] != ctx["appointment"]["provider_id"]:
                problems.append("slot is with a different provider")
            if e.time_of_day == TimeOfDay.MORNING and starts.hour >= 12:
                problems.append("patient asked for a morning slot")
            if e.time_of_day == TimeOfDay.AFTERNOON and starts.hour < 12:
                problems.append("patient asked for an afternoon slot")
            if e.preferred_date and starts.date() < e.preferred_date:
                problems.append("slot is earlier than the date the patient asked for")
            if problems:
                issues.extend(problems)
                feedback["exclude_slot_ids"].append(slot_id)
        if ctx.get("ambiguity"):
            confidence = min(confidence, 0.6)

    if intake.request_type == RequestType.BILLING and not ctx.get("invoice"):
        issues.append("couldn't find the invoice the patient is asking about")
        feedback["retryable"] = False

    if proposed is None and intake.request_type not in (RequestType.BILLING,):
        if not issues:
            issues.append("no action could be proposed")
        # Nothing to try differently next time, so don't burn another attempt.
        feedback["retryable"] = False

    if not issues and llm is not None and proposed is not None:
        critique = await llm.structured(
            system=prompts.CRITIQUE,
            user=(
                f"Patient request:\n{masked_message}\n\n"
                f"Intake summary: {intake.summary}\n"
                f"Proposed action: {proposed.description}\n"
                f"Tool call: {proposed.tool} {json.dumps(proposed.args)}"
            ),
            schema=Critique,
        )
        confidence = min(confidence, critique.confidence)
        if not critique.addresses_request:
            issues.extend(critique.issues or ["reviewer model says the action misses the request"])
            if proposed.tool == "reschedule_appointment":
                feedback["exclude_slot_ids"].append(proposed.args["slot_id"])

    passed = not issues
    return Verdict(
        passed=passed,
        confidence=confidence if passed else min(confidence, 0.5),
        issues=issues,
        feedback=feedback,
    )
