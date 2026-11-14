"""PII handling.

Two jobs:

1. **Reversible pseudonymization for the LLM.** The model never sees a raw phone
   number, email, SSN, MRN, date of birth or the patient's name. It sees tokens such
   as ``[PHONE_1]``. The token-to-value map (the "vault") stays in our own process,
   and we swap the real values back in only at the moment a tool needs them.

2. **One-way masking for logs and the audit trail.** Anything written to a log goes
   through :func:`mask` first.

Regexes won't catch every identifier in free text. That's why the patient's known
name is passed in explicitly, and why the audit log masks again even though the
inputs were already pseudonymized.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("MRN", re.compile(r"\bMRN[:#\s-]*\d{5,10}\b", re.IGNORECASE)),
    (
        "DOB",
        re.compile(
            r"(?i)(?<=\bdob[:\s])\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
            r"|(?<=\bborn on )\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
            r"|(?<=\bdate of birth[:\s])\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
        ),
    ),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")),
]

_TOKEN = re.compile(r"\[(?:EMAIL|SSN|MRN|DOB|PHONE|NAME)_\d+\]")


@dataclass
class Redaction:
    text: str
    vault: dict[str, str] = field(default_factory=dict)


def redact(
    text: str,
    known_names: Iterable[str] = (),
    vault: Mapping[str, str] | None = None,
) -> Redaction:
    """Replace PII in ``text`` with stable tokens. The same value always gets the same token."""
    out_vault: dict[str, str] = dict(vault or {})
    reverse = {v: k for k, v in out_vault.items()}
    counters: dict[str, int] = {}
    for token in out_vault:
        kind, _, n = token.strip("[]").rpartition("_")
        counters[kind] = max(counters.get(kind, 0), int(n))

    def token_for(kind: str, value: str) -> str:
        value = value.strip()
        if value in reverse:
            return reverse[value]
        counters[kind] = counters.get(kind, 0) + 1
        token = f"[{kind}_{counters[kind]}]"
        out_vault[token] = value
        reverse[value] = token
        return token

    masked = text
    # Longest names first so "Maria Lopez" wins over "Maria".
    for name in sorted({n for n in known_names if n and len(n) > 1}, key=len, reverse=True):
        masked = re.sub(
            rf"\b{re.escape(name)}\b",
            lambda m: token_for("NAME", m.group(0)),
            masked,
            flags=re.IGNORECASE,
        )
    for kind, pattern in _PATTERNS:
        masked = pattern.sub(lambda m, k=kind: token_for(k, m.group(0)), masked)  # type: ignore[misc]
    return Redaction(masked, out_vault)


def restore(value: Any, vault: Mapping[str, str]) -> Any:
    """Swap tokens back to real values, recursively through dicts and lists."""
    if isinstance(value, str):
        return _TOKEN.sub(lambda m: vault.get(m.group(0), m.group(0)), value)
    if isinstance(value, dict):
        return {k: restore(v, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [restore(v, vault) for v in value]
    return value


def mask(value: Any, known_names: Iterable[str] = ()) -> Any:
    """One-way masking for logs. Tokens stay as tokens; raw PII becomes a token with no vault."""
    names = tuple(known_names)
    if isinstance(value, str):
        return redact(value, names).text
    if isinstance(value, dict):
        return {k: mask(v, names) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask(v, names) for v in value]
    return value
