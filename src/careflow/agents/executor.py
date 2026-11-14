"""Executor agent: gather what's needed through the tools, then propose one action.

"Propose" is literal. The executor never writes anything. It returns a
:class:`ProposedAction`; the verifier checks it, a human may approve it, and only the
commit step calls a write tool. Splitting it this way is what makes the verifier
and human-review steps meaningful.

The model chooses (which slot fits "Thursday afternoon"?); code does the lookups and
builds the tool arguments. That keeps tool use on a deterministic path even when a
model is in the loop.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from careflow.agents import prompts
from careflow.gateway import ToolGateway, ToolRefused
from careflow.llm import LLM
from careflow.models import Intake, OptionChoice, ProposedAction, RequestType


async def gather(
    gateway: ToolGateway,
    request_id: str,
    patient_id: str,
    intake: Intake,
    feedback: dict[str, Any],
) -> dict[str, Any]:
    """Read-only lookups for the playbook. Returns a context dict for :func:`propose`."""
    ctx: dict[str, Any] = {"issues": []}
    e = intake.entities

    async def call(tool: str, **args: Any) -> Any:
        try:
            return await gateway.call(request_id, tool, args)
        except ToolRefused as exc:
            ctx["issues"].append(exc.message)
            return None

    match intake.request_type:
        case RequestType.RESCHEDULE:
            appts = await call("list_appointments", patient_id=patient_id) or []
            ctx["appointments"] = appts
            if not appts:
                ctx["issues"].append("patient has no upcoming appointments to move")
                return ctx
            target = appts[0]
            if len(appts) > 1:
                ctx["ambiguity"] = (
                    f"patient has {len(appts)} upcoming appointments; assumed the soonest "
                    f"({target['provider_name']}, {target['starts_at']})"
                )
            ctx["appointment"] = target
            ctx["slots"] = (
                await call(
                    "find_open_slots",
                    provider_id=target["provider_id"],
                    on_or_after=e.preferred_date.isoformat() if e.preferred_date else None,
                    time_of_day=e.time_of_day.value if e.time_of_day else None,
                    exclude_slot_ids=feedback.get("exclude_slot_ids", []),
                    limit=5,
                )
                or []
            )
        case RequestType.REFILL:
            ctx["prescriptions"] = await call("list_prescriptions", patient_id=patient_id) or []
        case RequestType.BILLING:
            invoices = await call("list_invoices", patient_id=patient_id) or []
            ctx["invoices"] = invoices
            number = e.invoice_number
            if number is None:
                open_ones = [i for i in invoices if i["status"] == "open"]
                number = (open_ones or invoices or [{"number": None}])[0]["number"]
            if number:
                ctx["invoice"] = await call(
                    "get_invoice", patient_id=patient_id, invoice_number=number
                )
            else:
                ctx["issues"].append("no invoices on this account")
        case RequestType.CONTACT:
            ctx["profile"] = await call("get_patient_profile", patient_id=patient_id)
        case RequestType.OTHER:
            pass
    return ctx


def _pretty(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    return f"{dt:%a %b} {dt.day} at {dt:%I:%M %p}".replace(" 0", " ")


async def _choose_slot(llm: LLM | None, masked_message: str, slots: list[dict[str, Any]]) -> str:
    if llm is None or len(slots) == 1:
        return str(slots[0]["slot_id"])
    options = "\n".join(f"- {s['slot_id']}: {_pretty(s['starts_at'])}" for s in slots)
    choice = await llm.structured(
        system=prompts.CHOOSE_SLOT,
        user=f"Patient asked:\n{masked_message}\n\nOptions:\n{options}",
        schema=OptionChoice,
    )
    valid = {s["slot_id"] for s in slots}
    # A model picking an id that isn't on the list is caught here, not by the tool.
    return choice.option_id if choice.option_id in valid else str(slots[0]["slot_id"])


async def propose(
    llm: LLM | None, masked_message: str, intake: Intake, ctx: dict[str, Any]
) -> ProposedAction | None:
    """Turn gathered context into one proposed write, or None if the answer is read-only."""
    e = intake.entities
    match intake.request_type:
        case RequestType.RESCHEDULE:
            if not ctx.get("slots"):
                if "appointment" in ctx:
                    ctx["issues"].append("no open slots match the patient's preference")
                return None
            slot_id = await _choose_slot(llm, masked_message, ctx["slots"])
            slot = next(s for s in ctx["slots"] if s["slot_id"] == slot_id)
            appt = ctx["appointment"]
            reasons = [ctx["ambiguity"]] if "ambiguity" in ctx else []
            return ProposedAction(
                tool="reschedule_appointment",
                args={"appointment_id": appt["appointment_id"], "slot_id": slot_id},
                description=(
                    f"Move {appt['provider_name']} appointment from {_pretty(appt['starts_at'])} "
                    f"to {_pretty(slot['starts_at'])}"
                ),
                review_reasons=reasons,
            )

        case RequestType.REFILL:
            rxs: list[dict[str, Any]] = ctx.get("prescriptions", [])
            if e.medication:
                matches = [r for r in rxs if r["medication"].lower() == e.medication.lower()]
            else:
                matches = rxs if len(rxs) == 1 else []
            if not matches:
                ctx["issues"].append(
                    f"couldn't match '{e.medication}' to an active prescription"
                    if e.medication
                    else "couldn't tell which prescription the patient means"
                )
                return None
            rx = matches[0]
            if rx["refills_remaining"] <= 0:
                # Not a failure: the right move is to ask the prescriber to renew.
                return ProposedAction(
                    tool="create_ticket",
                    args={
                        "category": "refill_renewal",
                        "summary": f"Renewal needed: {rx['medication']} {rx['dose']} "
                        "(no refills left)",
                    },
                    description=f"Ask the care team to renew {rx['medication']} (no refills left)",
                )
            reasons = (
                ["controlled substance: refills need clinician sign-off"]
                if rx["controlled"]
                else []
            )
            return ProposedAction(
                tool="request_refill",
                args={"prescription_id": rx["prescription_id"]},
                description=f"Send refill for {rx['medication']} {rx['dose']} to pharmacy",
                review_reasons=reasons,
            )

        case RequestType.BILLING:
            return None  # read-only: the answer is the invoice breakdown

        case RequestType.CONTACT:
            args = {k: v for k, v in (("phone", e.new_phone), ("email", e.new_email)) if v}
            if not args:
                ctx["issues"].append("no new phone number or email found in the message")
                return None
            return ProposedAction(
                tool="update_contact_info",
                args=args,
                description="Update " + " and ".join(args) + " on file",
            )

        case RequestType.OTHER:
            return ProposedAction(
                tool="create_ticket",
                args={"category": "general", "summary": intake.summary},
                description="Open a ticket for the front-desk team",
            )
    return None  # pragma: no cover
