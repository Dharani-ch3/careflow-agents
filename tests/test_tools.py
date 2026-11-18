from pathlib import Path

import pytest
from mcp import Client

from careflow.ehr import EHR, ToolError
from careflow.mcp_server import build_server
from careflow.seed import seed
from tests.conftest import TODAY


@pytest.fixture
def ehr(tmp_path: Path) -> EHR:
    return seed(tmp_path / "ehr.db", today=TODAY)


def test_tools_are_scoped_to_the_patient(ehr: EHR) -> None:
    with pytest.raises(ToolError):
        ehr.get_invoice("P001", "INV-1042")  # belongs to P002
    with pytest.raises(ToolError):
        ehr.request_refill("P001", "RX-1004")  # P002's prescription
    with pytest.raises(ToolError):
        ehr.reschedule_appointment("P002", "APT-501", "S-OKA-1123-1400")  # P001's appointment


def test_refill_is_idempotent(ehr: EHR) -> None:
    first = ehr.request_refill("P001", "RX-1001", idempotency_key="REQ-1:request_refill")
    again = ehr.request_refill("P001", "RX-1001", idempotency_key="REQ-1:request_refill")
    assert first == again
    assert ehr.count("refill_requests") == 1
    assert ehr.list_prescriptions("P001")[1]["refills_remaining"] == 1


def test_refill_refused_when_none_left(ehr: EHR) -> None:
    with pytest.raises(ToolError, match="No refills"):
        ehr.request_refill("P001", "RX-1002")


def test_reschedule_frees_old_slot_and_takes_new_one(ehr: EHR) -> None:
    appt = ehr.list_appointments("P001")[0]
    slot = ehr.find_open_slots("DR-OKAFOR", time_of_day="afternoon")[0]
    ehr.reschedule_appointment("P001", appt["appointment_id"], slot["slot_id"])
    assert not ehr.get_slot(slot["slot_id"])["is_open"]
    assert ehr.list_appointments("P001")[0]["starts_at"] == slot["starts_at"]


def test_reschedule_refuses_taken_slot(ehr: EHR) -> None:
    slot = ehr.find_open_slots("DR-OKAFOR")[0]
    ehr.book_slot_directly(slot["slot_id"])
    with pytest.raises(ToolError, match="no longer open"):
        ehr.reschedule_appointment("P001", "APT-501", slot["slot_id"])


def test_slot_search_respects_filters(ehr: EHR) -> None:
    mornings = ehr.find_open_slots("DR-SATO", time_of_day="morning", limit=10)
    assert mornings and all(int(s["starts_at"][11:13]) < 12 for s in mornings)
    skip = mornings[0]["slot_id"]
    assert skip not in {
        s["slot_id"] for s in ehr.find_open_slots("DR-SATO", "2026-01-01", "morning", [skip])
    }


def test_contact_update_validates_input(ehr: EHR) -> None:
    with pytest.raises(ToolError):
        ehr.update_contact_info("P001", phone="not a phone")
    ehr.update_contact_info("P001", email="new@example.com")
    assert ehr.get_patient_profile("P001")["email"] == "new@example.com"


async def test_mcp_server_exposes_tools_with_hints(ehr: EHR) -> None:
    async with Client(build_server(ehr)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["list_appointments"].annotations.read_only_hint is True
        assert tools["request_refill"].annotations.destructive_hint is True
        assert tools["create_ticket"].annotations.destructive_hint is False

        hint_names = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")
        for tool in tools.values():
            for name in hint_names:
                assert getattr(tool.annotations, name) is not None, f"{tool.name}: no {name}"

        refused = await client.call_tool(
            "get_invoice", {"patient_id": "P001", "invoice_number": "INV-1042"}
        )
        assert refused.is_error
        assert "No invoice INV-1042" in refused.content[0].text
