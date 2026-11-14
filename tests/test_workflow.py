"""End-to-end tests through the real graph, the real MCP server and the checkpointer."""

from __future__ import annotations

from careflow.config import Settings
from careflow.ehr import EHR
from careflow.models import Critique, ReviewDecision, Status
from careflow.service import CareFlow
from tests.conftest import TODAY
from tests.fakes import FakeLLM

APPROVE = ReviewDecision(approved=True, reviewer="nurse.kim")
REJECT = ReviewDecision(approved=False, reviewer="nurse.kim", note="calling the patient")


def _ehr(settings: Settings) -> EHR:
    return EHR(settings.ehr_db, today=TODAY)


async def test_reschedule_completes_without_review(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit(
            "P001", "Can we move my appointment to next week, afternoon please?"
        )
        assert view.status == Status.COMPLETED
        assert "you're all set" in (view.reply or "")
        appt = _ehr(settings).list_appointments("P001")[0]
        assert appt["starts_at"] >= "2026-11-23" and int(appt["starts_at"][11:13]) >= 12


async def test_controlled_refill_waits_for_a_human_and_writes_nothing(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P002", "Please refill my oxycodone")
        assert view.status == Status.AWAITING_REVIEW
        assert view.pending_review is not None
        assert "controlled substance" in " ".join(view.pending_review["why_review"])
        assert _ehr(settings).count("refill_requests") == 0
        assert [r["id"] for r in app.pending_reviews()] == [view.request_id]

        done = await app.review(view.request_id, APPROVE)
        assert done.status == Status.COMPLETED
        assert _ehr(settings).count("refill_requests") == 1
        assert app.pending_reviews() == []


async def test_rejection_writes_nothing(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P002", "Please refill my oxycodone")
        done = await app.review(view.request_id, REJECT)
        assert done.status == Status.REJECTED
        assert _ehr(settings).count("refill_requests") == 0
        assert "follow up" in (done.reply or "")


async def test_red_flags_short_circuit_everything(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P004", "chest pain since this morning, also need to reschedule")
        assert view.status == Status.ESCALATED
        assert "911" in (view.reply or "")
        assert view.request_type is None  # no intake, no tools beyond the urgent ticket
        assert _ehr(settings).count("tickets") == 1


async def test_contact_change_uses_real_value_only_at_commit(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P003", "My new number is (555) 010-0199, this is Aisha Khan")
        assert view.status == Status.AWAITING_REVIEW
        assert view.pending_review is not None
        assert "010-0199" not in str(view.pending_review)
        assert "Aisha" not in view.pending_review["message"]

        await app.review(view.request_id, APPROVE)
        assert _ehr(settings).get_patient_profile("P003")["phone"] == "(555) 010-0199"


async def test_audit_trail_is_masked(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P003", "My new number is (555) 010-0199, this is Aisha Khan")
        await app.review(view.request_id, APPROVE)
        trail = app.audit_trail(view.request_id)
        assert "010-0199" not in str(trail)
        actors = [e["actor"] for e in trail]
        assert "human:nurse.kim" in actors
        assert actors.index("human:nurse.kim") < actors.index("tool:update_contact_info")


async def test_verifier_sends_work_back_when_the_slot_is_taken(settings: Settings) -> None:
    """Someone else books the chosen slot between proposal and verification.
    The verifier's fresh read catches it and the executor picks again."""
    grabbed: list[str] = []

    def someone_else_books(slot_id: str) -> None:
        if not grabbed:
            grabbed.append(slot_id)
            _ehr(settings).book_slot_directly(slot_id)

    llm = FakeLLM(TODAY, on_choose=someone_else_books)
    async with CareFlow(settings, llm=llm) as app:
        view = await app.submit("P001", "Please move my appointment to next week in the afternoon")

    assert view.status == Status.COMPLETED
    assert any("failed attempt 1" in step and "was taken" in step for step in view.trail)
    new_time = _ehr(settings).list_appointments("P001")[0]
    assert grabbed and new_time["starts_at"] != _ehr(settings).get_slot(grabbed[0])["starts_at"]


async def test_model_critique_can_block_an_action(settings: Settings) -> None:
    llm = FakeLLM(
        TODAY,
        critique=Critique(addresses_request=False, confidence=0.3, issues=["wrong medication"]),
    )
    async with CareFlow(settings, llm=llm) as app:
        view = await app.submit("P001", "I need a refill on my lisinopril")
    assert view.status == Status.AWAITING_REVIEW
    assert "wrong medication" in view.pending_review["verifier_issues"]  # type: ignore[index]
    assert _ehr(settings).count("refill_requests") == 0


async def test_models_never_see_raw_pii(settings: Settings) -> None:
    llm = FakeLLM(TODAY)
    async with CareFlow(settings, llm=llm) as app:
        view = await app.submit(
            "P001", "Hi it's Maria Lopez, refill my lisinopril. Call me on 555-010-0177."
        )
    sent = "\n".join(llm.prompts)
    assert {"Intake", "Critique", "DraftReply"} <= set(llm.calls)
    for raw in ("Maria", "Lopez", "010-0177"):
        assert raw not in sent
    assert view.reply == "Hi Maria, this is handled."  # token swapped back afterwards


async def test_paused_request_survives_a_restart(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P002", "Please refill my oxycodone")
        assert view.status == Status.AWAITING_REVIEW

    async with CareFlow(settings) as app:  # new process, same data dir
        assert (await app.get(view.request_id)).status == Status.AWAITING_REVIEW
        done = await app.review(view.request_id, APPROVE)
    assert done.status == Status.COMPLETED


async def test_world_changing_before_commit_hands_off_cleanly(settings: Settings) -> None:
    async with CareFlow(settings) as app:
        view = await app.submit("P004", "reschedule my appointment to friday morning")
        assert view.status == Status.AWAITING_REVIEW  # two appointments -> ambiguous
        # While it waits, the proposed slot gets taken.
        pending = await app.get(view.request_id)
        state = await app.graph.aget_state({"configurable": {"thread_id": view.request_id}})
        _ehr(settings).book_slot_directly(state.values["proposed"]["args"]["slot_id"])

        done = await app.review(pending.request_id, APPROVE)
        assert done.status == Status.HANDED_OFF
        assert any("refused" in step for step in done.trail)


async def test_works_over_stdio_transport(settings: Settings) -> None:
    """Same workflow, but the MCP server runs as a separate process, like it would
    for any external MCP client."""
    stdio = settings.model_copy(update={"mcp_transport": "stdio"})
    async with CareFlow(stdio) as app:
        view = await app.submit("P001", "I need a refill on my lisinopril please")
    assert view.status == Status.COMPLETED
    assert _ehr(settings).count("refill_requests") == 1
