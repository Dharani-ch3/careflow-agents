"""The application layer: one object that owns the tool server, the checkpointer and
the compiled graph. The API, the CLI and the eval runner all go through it.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from types import TracebackType
from typing import Any

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from careflow.audit import AppStore
from careflow.config import Settings
from careflow.ehr import EHR
from careflow.gateway import ToolGateway, ToolRefused, stdio_target
from careflow.graph import build_graph
from careflow.llm import LLM, make_llm
from careflow.mcp_server import build_server
from careflow.models import ReviewDecision, Status
from careflow.seed import seed
from careflow.state import Deps


class UnknownPatient(Exception):
    pass


class NotFound(Exception):
    pass


class NotAwaitingReview(Exception):
    pass


@dataclass
class RequestView:
    request_id: str
    patient_id: str
    status: str
    request_type: str | None = None
    summary: str | None = None
    reply: str | None = None
    pending_review: dict[str, Any] | None = None
    trail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _encrypted_serde(passphrase: str) -> Any:
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

    key = hashlib.sha256(passphrase.encode()).digest()  # 32 bytes -> AES-256
    return EncryptedSerializer.from_pycryptodome_aes(key=key)


class CareFlow:
    """Usage::

    async with CareFlow(settings) as app:
        view = await app.submit("P001", "Can I move my appointment to Friday?")
    """

    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLM | None = None,
        use_settings_llm: bool = True,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm if llm is not None or not use_settings_llm else make_llm(settings)
        self._checkpointer = checkpointer
        self._stack = AsyncExitStack()
        self.ehr: EHR | None = None
        self.store: AppStore
        self.gateway: ToolGateway
        self.graph: CompiledStateGraph

    async def __aenter__(self) -> CareFlow:
        s = self.settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        if not s.ehr_db.exists():
            seed(s.ehr_db, today=s.current_date()).close()

        self.store = AppStore(s.app_db)
        self._stack.callback(self.store.close)

        if s.mcp_transport == "stdio":
            target: Any = stdio_target(str(s.data_dir.resolve()), s.today)
        else:
            self.ehr = EHR(s.ehr_db, today=s.today)
            self._stack.callback(self.ehr.close)
            target = build_server(self.ehr)
        self.gateway = await self._stack.enter_async_context(ToolGateway(target, self.store))

        checkpointer = self._checkpointer
        if checkpointer is None:
            conn = await aiosqlite.connect(str(s.checkpoint_db))
            self._stack.push_async_callback(conn.close)
            serde = (
                _encrypted_serde(s.checkpoint_key.get_secret_value()) if s.checkpoint_key else None
            )
            checkpointer = AsyncSqliteSaver(conn, serde=serde)

        deps = Deps(settings=s, gateway=self.gateway, store=self.store, llm=self.llm)
        self.graph = build_graph(deps, checkpointer)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stack.aclose()

    # ------------------------------------------------------------------ public

    async def submit(self, patient_id: str, message: str) -> RequestView:
        request_id = f"REQ-{uuid.uuid4().hex[:10]}"
        try:
            await self.gateway.call(request_id, "get_patient_profile", {"patient_id": patient_id})
        except ToolRefused as exc:
            raise UnknownPatient(patient_id) from exc

        self.store.upsert_request(request_id, patient_id=patient_id, status=Status.IN_PROGRESS)
        self.store.record(request_id, f"patient:{patient_id}", "submitted", {"message": message})
        await self.graph.ainvoke(
            {"request_id": request_id, "patient_id": patient_id, "message": message, "trail": []},
            self._config(request_id),
        )
        return await self._sync(request_id)

    async def review(self, request_id: str, decision: ReviewDecision) -> RequestView:
        view = await self.get(request_id)
        if view.status != Status.AWAITING_REVIEW:
            raise NotAwaitingReview(request_id)
        await self.graph.ainvoke(Command(resume=decision.model_dump()), self._config(request_id))
        return await self._sync(request_id)

    async def get(self, request_id: str) -> RequestView:
        if self.store.get_request(request_id) is None:
            raise NotFound(request_id)
        return await self._view(request_id)

    def pending_reviews(self) -> list[dict[str, Any]]:
        return self.store.list_requests(status=Status.AWAITING_REVIEW)

    def audit_trail(self, request_id: str) -> list[dict[str, Any]]:
        if self.store.get_request(request_id) is None:
            raise NotFound(request_id)
        return self.store.trail(request_id)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _config(request_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": request_id}}

    async def _view(self, request_id: str) -> RequestView:
        row = self.store.get_request(request_id) or {}
        snap = await self.graph.aget_state(self._config(request_id))
        values: dict[str, Any] = snap.values or {}
        pending = snap.interrupts[0].value if snap.interrupts else None
        status = (
            Status.AWAITING_REVIEW.value
            if pending
            else str(values.get("status") or row.get("status") or Status.IN_PROGRESS.value)
        )
        intake = values.get("intake") or {}
        return RequestView(
            request_id=request_id,
            patient_id=values.get("patient_id", row.get("patient_id", "")),
            status=status,
            request_type=intake.get("request_type"),
            summary=intake.get("summary"),
            reply=values.get("reply"),
            pending_review=pending,
            trail=values.get("trail", []),
        )

    async def _sync(self, request_id: str) -> RequestView:
        view = await self._view(request_id)
        self.store.upsert_request(
            request_id, status=view.status, request_type=view.request_type, summary=view.summary
        )
        if view.status == Status.AWAITING_REVIEW:
            self.store.record(request_id, "workflow", "awaiting_review", view.pending_review)
        elif view.status != Status.IN_PROGRESS:
            self.store.record(request_id, "workflow", view.status)
        return view
