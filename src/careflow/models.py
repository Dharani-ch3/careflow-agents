"""Typed contracts between the agents.

Every hand-off between agents goes through one of these models. If a model's output
doesn't validate, the step fails and is retried, instead of passing a half-formed
answer to the next agent.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RequestType(StrEnum):
    RESCHEDULE = "reschedule_appointment"
    REFILL = "prescription_refill"
    BILLING = "billing_question"
    CONTACT = "update_contact"
    OTHER = "other"


class TimeOfDay(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"


class Status(StrEnum):
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    HANDED_OFF = "handed_off"  # opened a ticket for staff; nothing automated
    REJECTED = "rejected"  # a reviewer said no
    ESCALATED = "escalated"  # urgent symptoms; routed to a clinician immediately


class Entities(BaseModel):
    """Details pulled out of the patient's message. Anything not mentioned stays None."""

    medication: str | None = Field(None, description="Medication name, lowercase, no dose.")
    preferred_date: date | None = Field(None, description="Earliest date the patient asked for.")
    time_of_day: TimeOfDay | None = None
    invoice_number: str | None = Field(None, description="Like INV-1042.")
    new_phone: str | None = Field(None, description="Placeholder token such as [PHONE_1].")
    new_email: str | None = Field(None, description="Placeholder token such as [EMAIL_1].")


class Intake(BaseModel):
    request_type: RequestType
    summary: str = Field(description="One sentence, written for a staff member.")
    entities: Entities = Field(default_factory=Entities)
    confidence: float = Field(ge=0.0, le=1.0)


class Plan(BaseModel):
    playbook: RequestType
    requires_review: bool
    reasons: list[str] = Field(default_factory=list)


class ProposedAction(BaseModel):
    """A write the executor wants to make. Nothing is written until commit."""

    tool: str
    args: dict[str, Any]
    description: str
    review_reasons: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    passed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)
    # Structured hints for the executor's next attempt, e.g. {"exclude_slot_ids": [...]}.
    feedback: dict[str, Any] = Field(default_factory=dict)


class ReviewDecision(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1)
    note: str = ""


# ---- shapes the LLM fills in (kept small on purpose) ----


class OptionChoice(BaseModel):
    option_id: str
    reason: str


class Critique(BaseModel):
    addresses_request: bool
    confidence: float = Field(ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)


class DraftReply(BaseModel):
    text: str


ReviewOutcome = Literal["approved", "rejected"]
