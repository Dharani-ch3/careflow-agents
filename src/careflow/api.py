"""HTTP API.

    POST /requests                 patient submits a message
    GET  /requests/{id}            status, reply, and the step-by-step trail
    GET  /requests/{id}/audit      full audit log (masked)
    GET  /requests                 recent activity across all patients
    GET  /reviews                  everything waiting on a human
    POST /reviews/{id}             approve or reject
    GET  /patients                 the demo patients
    GET  /patients/{id}/requests   one patient's conversation, oldest first
    GET  /                         the web app (patient chat + staff review desk)

Run with ``uvicorn careflow.api:app --reload`` and open http://localhost:8000
(or http://localhost:8000/docs for the raw API).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from careflow import __version__
from careflow.config import Settings, get_settings
from careflow.models import ReviewDecision
from careflow.seed import PATIENTS
from careflow.service import CareFlow, NotAwaitingReview, NotFound, RequestView, UnknownPatient

WEB_DIR = Path(__file__).parent / "web"


class SubmitRequest(BaseModel):
    patient_id: str = Field(examples=["P001"])
    message: str = Field(
        min_length=1,
        max_length=4000,
        examples=["Can I move my appointment to next Tuesday afternoon?"],
    )


class RequestOut(BaseModel):
    request_id: str
    patient_id: str
    status: str
    message: str | None
    created_at: str | None
    request_type: str | None
    summary: str | None
    reply: str | None
    pending_review: dict[str, Any] | None
    trail: list[str]

    @classmethod
    def of(cls, view: RequestView) -> RequestOut:
        return cls(**view.as_dict())


def _careflow(request: Request) -> CareFlow:
    careflow: CareFlow = request.app.state.careflow
    return careflow


Flow = Annotated[CareFlow, Depends(_careflow)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with CareFlow(settings) as careflow:
            app.state.careflow = careflow
            yield

    app = FastAPI(
        title="CareFlow Agents",
        version=__version__,
        description="Multi-agent workflows for healthcare service requests (synthetic data).",
        lifespan=lifespan,
    )

    def reviewer_auth(x_reviewer_key: Annotated[str | None, Header()] = None) -> None:
        expected = settings.reviewer_api_key
        if expected is None:
            return
        if x_reviewer_key is None or not secrets.compare_digest(
            x_reviewer_key, expected.get_secret_value()
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or wrong X-Reviewer-Key")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def web_app() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/patients")
    async def patients() -> list[dict[str, str]]:
        # Demo data only: a real portal knows who is signed in and never lists patients.
        return [{"patient_id": p[0], "name": f"{p[1]} {p[2]}"} for p in PATIENTS]

    @app.get("/patients/{patient_id}/requests")
    async def patient_history(patient_id: str, cf: Flow) -> list[RequestOut]:
        if patient_id not in {p[0] for p in PATIENTS}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown patient {patient_id}")
        return [RequestOut.of(v) for v in await cf.history(patient_id)]

    @app.get("/requests")
    async def recent(cf: Flow, limit: int = 50) -> list[dict[str, Any]]:
        return cf.recent_requests(limit=min(max(limit, 1), 200))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "llm": settings.llm_provider}

    @app.post("/requests", status_code=status.HTTP_201_CREATED)
    async def submit(body: SubmitRequest, cf: Flow) -> RequestOut:
        try:
            return RequestOut.of(await cf.submit(body.patient_id, body.message))
        except UnknownPatient as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown patient {exc}") from exc

    @app.get("/requests/{request_id}")
    async def get_request(request_id: str, cf: Flow) -> RequestOut:
        try:
            return RequestOut.of(await cf.get(request_id))
        except NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request") from exc

    @app.get("/requests/{request_id}/audit")
    async def audit(request_id: str, cf: Flow) -> list[dict[str, Any]]:
        try:
            return cf.audit_trail(request_id)
        except NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request") from exc

    @app.get("/reviews", dependencies=[Depends(reviewer_auth)])
    async def pending(cf: Flow) -> list[RequestOut]:
        return [RequestOut.of(v) for v in await cf.pending_views()]

    @app.post("/reviews/{request_id}", dependencies=[Depends(reviewer_auth)])
    async def decide(request_id: str, decision: ReviewDecision, cf: Flow) -> RequestOut:
        try:
            return RequestOut.of(await cf.review(request_id, decision))
        except NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request") from exc
        except NotAwaitingReview as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "Request is not awaiting review") from exc

    return app


app = create_app()
