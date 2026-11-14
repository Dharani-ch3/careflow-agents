"""A small mock of the systems a clinic's agents would touch: scheduling, pharmacy,
billing, patient demographics and a ticket queue.

Everything here is reached by the agents through the MCP server, never directly.
Each tool takes the caller's ``patient_id`` and refuses to touch records that belong
to anyone else. That rule lives here, next to the data, rather than in a prompt.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, dob TEXT,
    phone TEXT, email TEXT, mrn TEXT
);
CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY, name TEXT, specialty TEXT);
CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY, provider_id TEXT, starts_at TEXT, is_open INTEGER
);
CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY, patient_id TEXT, provider_id TEXT, slot_id TEXT,
    starts_at TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS prescriptions (
    id TEXT PRIMARY KEY, patient_id TEXT, medication TEXT, dose TEXT,
    refills_remaining INTEGER, controlled INTEGER, last_filled TEXT
);
CREATE TABLE IF NOT EXISTS refill_requests (
    id TEXT PRIMARY KEY, prescription_id TEXT, requested_at TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    number TEXT PRIMARY KEY, patient_id TEXT, issued_on TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice_number TEXT, description TEXT, charge_cents INTEGER, insurance_paid_cents INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY, patient_id TEXT, category TEXT, priority TEXT,
    summary TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS executed_actions (
    key TEXT PRIMARY KEY, tool TEXT, result TEXT, created_at TEXT
);
"""

PHONE_RE = re.compile(r"^\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


class ToolError(MCPToolError):
    """A tool refused the call: bad input, wrong patient, or a business rule.

    Subclassing the MCP SDK's ToolError means the message reaches the agent as a
    normal tool error it can act on, instead of a generic crash.
    """


def _cents(n: int) -> str:
    return f"${n / 100:,.2f}"


class EHR:
    def __init__(self, path: Path | str, today: date | None = None) -> None:
        self.path = str(path)
        self._today = today
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    @property
    def today(self) -> date:
        return self._today or date.today()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    def _rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = self._rows(sql, *args)
        return rows[0] if rows else None

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ guards

    def _require_patient(self, patient_id: str) -> dict[str, Any]:
        patient = self._one("SELECT * FROM patients WHERE id = ?", patient_id)
        if patient is None:
            raise ToolError(f"Unknown patient {patient_id}")
        return patient

    def _already_done(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        row = self._one("SELECT result FROM executed_actions WHERE key = ?", key)
        return json.loads(row["result"]) if row else None

    def _remember(self, conn: sqlite3.Connection, key: str | None, tool: str, result: dict) -> None:
        if key:
            conn.execute(
                "INSERT INTO executed_actions VALUES (?, ?, ?, ?)",
                (key, tool, json.dumps(result), datetime.now().isoformat(timespec="seconds")),
            )

    # ------------------------------------------------------------- read tools

    def get_patient_profile(self, patient_id: str) -> dict[str, Any]:
        p = self._require_patient(patient_id)
        return {
            "patient_id": p["id"],
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "phone": p["phone"],
            "email": p["email"],
        }

    def list_appointments(self, patient_id: str) -> list[dict[str, Any]]:
        self._require_patient(patient_id)
        return self._rows(
            """SELECT a.id AS appointment_id, a.starts_at, a.provider_id,
                      p.name AS provider_name, p.specialty
               FROM appointments a JOIN providers p ON p.id = a.provider_id
               WHERE a.patient_id = ? AND a.status = 'booked' AND a.starts_at >= ?
               ORDER BY a.starts_at""",
            patient_id,
            self.today.isoformat(),
        )

    def find_open_slots(
        self,
        provider_id: str,
        on_or_after: str | None = None,
        time_of_day: str | None = None,
        exclude_slot_ids: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        start = max(
            date.fromisoformat(on_or_after) if on_or_after else self.today,
            self.today + timedelta(days=1),
        )
        rows = self._rows(
            """SELECT id AS slot_id, starts_at, provider_id FROM slots
               WHERE provider_id = ? AND is_open = 1 AND starts_at >= ?
               ORDER BY starts_at""",
            provider_id,
            start.isoformat(),
        )
        excluded = set(exclude_slot_ids or [])
        out = []
        for r in rows:
            hour = datetime.fromisoformat(r["starts_at"]).hour
            if time_of_day == "morning" and hour >= 12:
                continue
            if time_of_day == "afternoon" and hour < 12:
                continue
            if r["slot_id"] in excluded:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def get_slot(self, slot_id: str) -> dict[str, Any]:
        slot = self._one(
            "SELECT id AS slot_id, provider_id, starts_at, is_open FROM slots WHERE id = ?",
            slot_id,
        )
        if slot is None:
            raise ToolError(f"Unknown slot {slot_id}")
        slot["is_open"] = bool(slot["is_open"])
        return slot

    def list_prescriptions(self, patient_id: str) -> list[dict[str, Any]]:
        self._require_patient(patient_id)
        rows = self._rows(
            """SELECT id AS prescription_id, medication, dose, refills_remaining,
                      controlled, last_filled
               FROM prescriptions WHERE patient_id = ? ORDER BY medication""",
            patient_id,
        )
        for r in rows:
            r["controlled"] = bool(r["controlled"])
        return rows

    def list_invoices(self, patient_id: str) -> list[dict[str, Any]]:
        self._require_patient(patient_id)
        return self._rows(
            "SELECT number, issued_on, status FROM invoices WHERE patient_id = ? "
            "ORDER BY issued_on DESC",
            patient_id,
        )

    def get_invoice(self, patient_id: str, invoice_number: str) -> dict[str, Any]:
        inv = self._one(
            "SELECT * FROM invoices WHERE number = ? AND patient_id = ?",
            invoice_number.upper(),
            patient_id,
        )
        if inv is None:
            raise ToolError(f"No invoice {invoice_number} on this patient's account")
        lines = self._rows(
            "SELECT description, charge_cents, insurance_paid_cents FROM invoice_lines "
            "WHERE invoice_number = ?",
            inv["number"],
        )
        charged = sum(line["charge_cents"] for line in lines)
        insurance = sum(line["insurance_paid_cents"] for line in lines)
        return {
            "number": inv["number"],
            "issued_on": inv["issued_on"],
            "status": inv["status"],
            "lines": [
                {
                    "description": line["description"],
                    "charge": _cents(line["charge_cents"]),
                    "insurance_paid": _cents(line["insurance_paid_cents"]),
                    "patient_owes": _cents(line["charge_cents"] - line["insurance_paid_cents"]),
                }
                for line in lines
            ],
            "total_charged": _cents(charged),
            "insurance_paid": _cents(insurance),
            "patient_owes": _cents(charged - insurance),
        }

    # ------------------------------------------------------------ write tools

    def reschedule_appointment(
        self, patient_id: str, appointment_id: str, slot_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if (done := self._already_done(idempotency_key)) is not None:
            return done
        appt = self._one(
            "SELECT * FROM appointments WHERE id = ? AND patient_id = ? AND status = 'booked'",
            appointment_id,
            patient_id,
        )
        if appt is None:
            raise ToolError("Appointment not found for this patient")
        slot = self.get_slot(slot_id)
        if not slot["is_open"]:
            raise ToolError(f"Slot {slot_id} is no longer open")
        if slot["provider_id"] != appt["provider_id"]:
            raise ToolError("Slot belongs to a different provider")
        with self._tx() as conn:
            conn.execute("UPDATE slots SET is_open = 1 WHERE id = ?", (appt["slot_id"],))
            conn.execute("UPDATE slots SET is_open = 0 WHERE id = ?", (slot_id,))
            conn.execute(
                "UPDATE appointments SET slot_id = ?, starts_at = ? WHERE id = ?",
                (slot_id, slot["starts_at"], appointment_id),
            )
            result = {
                "appointment_id": appointment_id,
                "old_time": appt["starts_at"],
                "new_time": slot["starts_at"],
            }
            self._remember(conn, idempotency_key, "reschedule_appointment", result)
        return result

    def request_refill(
        self, patient_id: str, prescription_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if (done := self._already_done(idempotency_key)) is not None:
            return done
        rx = self._one(
            "SELECT * FROM prescriptions WHERE id = ? AND patient_id = ?",
            prescription_id,
            patient_id,
        )
        if rx is None:
            raise ToolError("Prescription not found for this patient")
        if rx["refills_remaining"] <= 0:
            raise ToolError("No refills remaining; the prescriber has to renew it")
        with self._tx() as conn:
            req_id = f"RR-{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO refill_requests VALUES (?, ?, ?, 'sent_to_pharmacy')",
                (req_id, prescription_id, datetime.now().isoformat(timespec="seconds")),
            )
            conn.execute(
                "UPDATE prescriptions SET refills_remaining = refills_remaining - 1 WHERE id = ?",
                (prescription_id,),
            )
            result = {
                "refill_request_id": req_id,
                "medication": rx["medication"],
                "dose": rx["dose"],
                "refills_left_after_this": rx["refills_remaining"] - 1,
            }
            self._remember(conn, idempotency_key, "request_refill", result)
        return result

    def update_contact_info(
        self,
        patient_id: str,
        phone: str | None = None,
        email: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if (done := self._already_done(idempotency_key)) is not None:
            return done
        self._require_patient(patient_id)
        if not phone and not email:
            raise ToolError("Nothing to update")
        if phone and not PHONE_RE.match(phone.strip()):
            raise ToolError("That doesn't look like a valid phone number")
        if email and not EMAIL_RE.match(email.strip()):
            raise ToolError("That doesn't look like a valid email address")
        with self._tx() as conn:
            if phone:
                conn.execute(
                    "UPDATE patients SET phone = ? WHERE id = ?", (phone.strip(), patient_id)
                )
            if email:
                conn.execute(
                    "UPDATE patients SET email = ? WHERE id = ?", (email.strip(), patient_id)
                )
            result = {"updated": [f for f, v in (("phone", phone), ("email", email)) if v]}
            self._remember(conn, idempotency_key, "update_contact_info", result)
        return result

    def create_ticket(
        self,
        patient_id: str,
        category: str,
        summary: str,
        priority: str = "normal",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if (done := self._already_done(idempotency_key)) is not None:
            return done
        if priority not in {"normal", "urgent"}:
            raise ToolError("priority must be 'normal' or 'urgent'")
        with self._tx() as conn:
            ticket_id = f"TCK-{uuid.uuid4().hex[:6].upper()}"
            conn.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    patient_id,
                    category,
                    priority,
                    summary,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            result = {"ticket_id": ticket_id, "priority": priority}
            self._remember(conn, idempotency_key, "create_ticket", result)
        return result

    # ------------------------------------------------------------ test helpers

    def book_slot_directly(self, slot_id: str) -> None:
        """Simulate someone else grabbing a slot (used to test the verifier)."""
        with self._tx() as conn:
            conn.execute("UPDATE slots SET is_open = 0 WHERE id = ?", (slot_id,))

    def count(self, table: str) -> int:
        if table not in {"tickets", "refill_requests", "executed_actions"}:
            raise ValueError(table)
        row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
        return int(row["n"]) if row else 0
