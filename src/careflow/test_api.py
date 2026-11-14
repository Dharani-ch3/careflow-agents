from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from careflow.api import create_app
from careflow.config import Settings

KEY = {"X-Reviewer-Key": "s3cret"}


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    settings.reviewer_api_key = SecretStr("s3cret")
    with TestClient(create_app(settings)) as c:
        yield c


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_submit_and_fetch(client: TestClient) -> None:
    r = client.post("/requests", json={"patient_id": "P001", "message": "refill my lisinopril"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "completed"
    again = client.get(f"/requests/{body['request_id']}").json()
    assert again["reply"] == body["reply"]
    audit = client.get(f"/requests/{body['request_id']}/audit").json()
    assert audit[0]["action"] == "call"


def test_review_flow_requires_key(client: TestClient) -> None:
    r = client.post("/requests", json={"patient_id": "P002", "message": "refill my oxycodone"})
    rid = r.json()["request_id"]
    assert r.json()["status"] == "awaiting_review"

    assert client.get("/reviews").status_code == 401
    pending = client.get("/reviews", headers=KEY).json()
    assert [x["request_id"] for x in pending] == [rid]
    assert "controlled substance" in " ".join(pending[0]["pending_review"]["why_review"])

    decision = {"approved": True, "reviewer": "nurse.kim"}
    assert (
        client.post(f"/reviews/{rid}", json=decision, headers={"X-Reviewer-Key": "no"}).status_code
        == 401
    )
    done = client.post(f"/reviews/{rid}", json=decision, headers=KEY)
    assert done.json()["status"] == "completed"
    # Can't approve twice.
    assert client.post(f"/reviews/{rid}", json=decision, headers=KEY).status_code == 409


def test_errors(client: TestClient) -> None:
    assert client.post("/requests", json={"patient_id": "P999", "message": "hi"}).status_code == 404
    assert client.post("/requests", json={"patient_id": "P001", "message": ""}).status_code == 422
    assert client.get("/requests/REQ-nope").status_code == 404


def test_web_app_and_chat_history(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "CareFlow" in page.text
    assert client.get("/static/app.js").status_code == 200

    names = {p["patient_id"]: p["name"] for p in client.get("/patients").json()}
    assert names["P001"] == "Maria Lopez"

    client.post("/requests", json={"patient_id": "P001", "message": "refill my lisinopril"})
    client.post("/requests", json={"patient_id": "P001", "message": "where do I park?"})
    client.post("/requests", json={"patient_id": "P002", "message": "refill my metformin"})

    history = client.get("/patients/P001/requests").json()
    assert [h["message"] for h in history] == ["refill my lisinopril", "where do I park?"]
    assert all(h["reply"] for h in history)
    assert len(client.get("/requests").json()) == 3
    assert client.get("/patients/P999/requests").status_code == 404
