"""A scripted stand-in for a real model, so agent behaviour can be tested exactly."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel

from careflow.agents.intake import rule_based_intake
from careflow.models import Critique, DraftReply, Intake, OptionChoice

T = TypeVar("T", bound=BaseModel)


class FakeLLM:
    name = "fake"

    def __init__(
        self,
        today: date,
        on_choose: Callable[[str], None] | None = None,
        critique: Critique | None = None,
    ) -> None:
        self.today = today
        self.on_choose = on_choose
        self.critique = critique or Critique(addresses_request=True, confidence=0.95)
        self.prompts: list[str] = []
        self.calls: list[str] = []

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        self.prompts.append(system + "\n" + user)
        self.calls.append(schema.__name__)
        result: Any
        if schema is Intake:
            message = user.split("Patient message:\n", 1)[1]
            result = rule_based_intake(message, self.today)
        elif schema is OptionChoice:
            first = re.search(r"- (S-[\w-]+):", user)
            assert first, user
            if self.on_choose:
                self.on_choose(first[1])
            result = OptionChoice(option_id=first[1], reason="first option")
        elif schema is Critique:
            result = self.critique
        elif schema is DraftReply:
            name = re.search(r"first name: (\S+)", user)
            result = DraftReply(text=f"Hi {name[1] if name else ''}, this is handled.")
        else:  # pragma: no cover
            raise AssertionError(f"unexpected schema {schema}")
        assert isinstance(result, schema)
        return result
