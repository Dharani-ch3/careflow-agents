"""Request registry and audit trail.

Every agent step and every tool call writes one row here, so for any request you can
answer "what did the system do, in what order, and who approved it?". Details are
masked before they are stored; the audit log is not a second copy of the patient record.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from careflow.privacy import mask

log = logging.getLogger("careflow.audit")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, patient_id TEXT, status TEXT, request_type TEXT,
    summary TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, at TEXT,
    actor TEXT, action TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_request ON audit(request_id, seq);
CREATE INDEX IF NOT EXISTS requests_by_status ON requests(status, updated_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class AppStore:
    def __init__(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- requests

    def upsert_request(
        self,
        request_id: str,
        *,
        patient_id: str | None = None,
        status: str | None = None,
        request_type: str | None = None,
        summary: str | None = None,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO requests (id, patient_id, status, request_type, summary,
                                         created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     status = COALESCE(excluded.status, requests.status),
                     request_type = COALESCE(excluded.request_type, requests.request_type),
                     summary = COALESCE(excluded.summary, requests.summary),
                     updated_at = excluded.updated_at""",
                (request_id, patient_id, status, request_type, summary, now, now),
            )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_requests(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM requests"
        args: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- audit

    def record(self, request_id: str, actor: str, action: str, detail: Any = None) -> None:
        safe = mask(detail) if detail is not None else None
        payload = json.dumps(safe, default=str) if safe is not None else None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit (request_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
                (request_id, _now(), actor, action, payload),
            )
        log.info("%s %s %s", request_id, actor, action)

    def trail(self, request_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, actor, action, detail FROM audit WHERE request_id = ? ORDER BY seq",
                (request_id,),
            ).fetchall()
        return [
            {**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows
        ]
