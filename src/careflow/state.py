"""Graph state and shared dependencies."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, TypedDict

from careflow.audit import AppStore
from careflow.config import Settings
from careflow.gateway import ToolGateway
from careflow.llm import LLM


class CareState(TypedDict, total=False):
    # Set by the caller.
    request_id: str
    patient_id: str
    message: str  # raw text; never sent to a model

    # Privacy: what the models see, and how to map tokens back.
    masked_message: str
    vault: dict[str, str]
    known_names: list[str]
    first_name: str

    # Agent outputs. Stored as plain dicts (model_dump) so checkpoints stay
    # readable and portable; each node validates them back into models.
    urgent: bool
    intake: dict[str, Any]
    plan: dict[str, Any]
    context: dict[str, Any]
    proposed: dict[str, Any] | None
    verdict: dict[str, Any]
    attempts: int
    review: dict[str, Any] | None
    outcome: dict[str, Any] | None
    reply: str
    status: str

    # Short human-readable notes, appended by each step.
    trail: Annotated[list[str], operator.add]


class ExecState(TypedDict, total=False):
    """What the executor subgraph is allowed to see and change."""

    request_id: str
    patient_id: str
    masked_message: str
    intake: dict[str, Any]
    verdict: dict[str, Any]
    context: dict[str, Any]
    proposed: dict[str, Any] | None


@dataclass
class Deps:
    settings: Settings
    gateway: ToolGateway
    store: AppStore
    llm: LLM | None = None

    @property
    def today(self) -> date:
        return self.settings.current_date()
