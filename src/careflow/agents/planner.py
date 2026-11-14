"""Planner: decide which playbook runs and whether a person has to sign off.

This is policy, not judgment, so it is plain code. Rules like "contact changes are
always confirmed" should be easy to read, test and audit, and should not depend
on how a prompt is worded.
"""

from __future__ import annotations

from careflow.models import Intake, Plan, RequestType


def make_plan(intake: Intake, confidence_threshold: float) -> Plan:
    reasons: list[str] = []
    if intake.request_type == RequestType.OTHER:
        # The "action" here is handing the message to a person, which is already the
        # safe default. Asking a person to approve handing it to a person adds nothing.
        return Plan(playbook=intake.request_type, requires_review=False)
    if intake.confidence < confidence_threshold:
        reasons.append(
            f"intake confidence {intake.confidence:.2f} is below {confidence_threshold:.2f}"
        )
    if intake.request_type == RequestType.CONTACT:
        reasons.append("contact-detail changes are always confirmed by staff")
    return Plan(playbook=intake.request_type, requires_review=bool(reasons), reasons=reasons)
