"""The MCP server that gives agents scoped access to the clinic's systems.

Run it on its own over stdio (``careflow-mcp``) and point any MCP client at it,
for example Claude Desktop or the MCP Inspector, or let the app host it in-process.

Read-only tools are marked ``readOnlyHint``; tools that change records are marked
``destructiveHint`` and take an ``idempotency_key``, so a retried step can't book
the same slot twice.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from careflow.config import get_settings
from careflow.ehr import EHR
from careflow.seed import seed

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

INSTRUCTIONS = """\
Tools for a primary-care clinic: scheduling, pharmacy refills, billing and patient
contact details. Every tool is scoped to one patient_id; records that belong to
another patient are never returned or changed. Write tools accept an
idempotency_key; reuse the same key when retrying."""


def build_server(ehr: EHR) -> MCPServer:
    server = MCPServer("careflow-clinic", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(annotations=READ)
    def get_patient_profile(patient_id: str) -> dict[str, Any]:
        """Name and current contact details for a patient."""
        return ehr.get_patient_profile(patient_id)

    @server.tool(annotations=READ)
    def list_appointments(patient_id: str) -> list[dict[str, Any]]:
        """Upcoming booked appointments for a patient, soonest first."""
        return ehr.list_appointments(patient_id)

    @server.tool(annotations=READ)
    def find_open_slots(
        provider_id: str,
        on_or_after: str | None = None,
        time_of_day: str | None = None,
        exclude_slot_ids: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Open slots for a provider. on_or_after is YYYY-MM-DD; time_of_day is
        'morning' or 'afternoon'."""
        return ehr.find_open_slots(provider_id, on_or_after, time_of_day, exclude_slot_ids, limit)

    @server.tool(annotations=READ)
    def get_slot(slot_id: str) -> dict[str, Any]:
        """Current state of one slot, including whether it is still open."""
        return ehr.get_slot(slot_id)

    @server.tool(annotations=READ)
    def list_prescriptions(patient_id: str) -> list[dict[str, Any]]:
        """Active prescriptions, refills remaining and whether each is a controlled substance."""
        return ehr.list_prescriptions(patient_id)

    @server.tool(annotations=READ)
    def list_invoices(patient_id: str) -> list[dict[str, Any]]:
        """Invoices on the patient's account, newest first."""
        return ehr.list_invoices(patient_id)

    @server.tool(annotations=READ)
    def get_invoice(patient_id: str, invoice_number: str) -> dict[str, Any]:
        """Line-by-line breakdown of one invoice, including what insurance covered."""
        return ehr.get_invoice(patient_id, invoice_number)

    @server.tool(annotations=WRITE)
    def reschedule_appointment(
        patient_id: str, appointment_id: str, slot_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Move an appointment to an open slot with the same provider."""
        return ehr.reschedule_appointment(patient_id, appointment_id, slot_id, idempotency_key)

    @server.tool(annotations=WRITE)
    def request_refill(
        patient_id: str, prescription_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Send a refill request to the pharmacy. Fails if no refills remain."""
        return ehr.request_refill(patient_id, prescription_id, idempotency_key)

    @server.tool(annotations=WRITE)
    def update_contact_info(
        patient_id: str,
        phone: str | None = None,
        email: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Change the phone number and/or email on file."""
        return ehr.update_contact_info(patient_id, phone, email, idempotency_key)

    @server.tool(annotations=WRITE)
    def create_ticket(
        patient_id: str,
        category: str,
        summary: str,
        priority: str = "normal",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Open a ticket for clinic staff. priority is 'normal' or 'urgent'."""
        return ehr.create_ticket(patient_id, category, summary, priority, idempotency_key)

    return server


def main() -> None:
    """Entry point for ``careflow-mcp``: serve the tools over stdio."""
    # stdout carries the MCP protocol, so logs must go to stderr.
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    if settings.ehr_db.exists():
        ehr = EHR(settings.ehr_db, today=settings.today)
    else:
        ehr = seed(settings.ehr_db, today=settings.current_date())
    build_server(ehr).run("stdio")


if __name__ == "__main__":
    main()
