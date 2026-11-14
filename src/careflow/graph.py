"""The workflow, wired together as a LangGraph state machine.

    screen ─┬─▶ (red flag) ─▶ END
            └─▶ intake ─▶ plan ─▶ execute ─▶ verify ─┬─▶ commit ─▶ respond ─▶ END
                                   ▲  [subgraph]     ├─▶ execute   (self-correction)
                                   └─────────────────┤
                                                     └─▶ human_review ─┬─▶ commit
                                                                       └─▶ respond

* **Checkpointer**: every step is saved, so a request paused for human review can be
  resumed hours later, even after a restart.
* **Retries**: steps that call models or tools retry on transient errors only.
* **Subgraph**: ``execute`` is its own small graph (gather, then propose), which keeps
  the read-only phase visibly separate from the decision.
* **Interrupt**: ``human_review`` pauses the run and waits for a reviewer's decision.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy, interrupt
from pydantic import ValidationError

from careflow.agents import executor, intake, planner, responder, safety, verifier
from careflow.gateway import ToolRefused, ToolUnavailable
from careflow.models import (
    Intake,
    Plan,
    ProposedAction,
    RequestType,
    ReviewDecision,
    Status,
    Verdict,
)
from careflow.privacy import redact, restore
from careflow.state import CareState, Deps, ExecState


def _transient(exc: Exception) -> bool:
    if isinstance(exc, ToolUnavailable | ValidationError | TimeoutError | ConnectionError):
        return True
    # Provider SDK errors (rate limits, dropped connections) without importing every SDK.
    name = type(exc).__name__
    return any(k in name for k in ("RateLimit", "APIConnection", "Timeout", "Overloaded"))


TOOL_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.2, retry_on=ToolUnavailable)
MODEL_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.5, retry_on=_transient)


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    gw, store, llm, settings = deps.gateway, deps.store, deps.llm, deps.settings

    # ------------------------------------------------------------------ nodes

    async def screen(state: CareState) -> dict[str, Any]:
        rid, pid = state["request_id"], state["patient_id"]
        profile = await gw.call(rid, "get_patient_profile", {"patient_id": pid})
        names = [profile["first_name"], profile["last_name"]]
        names.append(" ".join(names))
        red = redact(state["message"], known_names=names)
        # Make sure the first name has its own token, so replies can greet the patient
        # without the model ever seeing the name.
        vault = redact(profile["first_name"], [profile["first_name"]], vault=red.vault).vault
        update: dict[str, Any] = {
            "masked_message": red.text,
            "vault": vault,
            "known_names": names,
            "first_name": profile["first_name"],
            "attempts": 0,
            "status": Status.IN_PROGRESS.value,
        }
        if flags := safety.screen(state["message"]):
            store.record(rid, "agent:safety", "red_flag", {"flags": flags})
            ticket = await gw.call(
                rid,
                "create_ticket",
                {
                    "patient_id": pid,
                    "category": "clinical_triage",
                    "priority": "urgent",
                    "summary": f"Red flags in patient message: {', '.join(flags)}",
                    "idempotency_key": f"{rid}:urgent",
                },
            )
            return update | {
                "urgent": True,
                "status": Status.ESCALATED.value,
                "outcome": ticket,
                "reply": safety.urgent_reply(flags),
                "trail": [f"safety: red flags {flags}; nurse ticket {ticket['ticket_id']}"],
            }
        return update | {"urgent": False, "trail": ["safety: no red flags"]}

    async def run_intake(state: CareState) -> dict[str, Any]:
        result = await intake.run_intake(state["masked_message"], deps.today, llm)
        store.record(state["request_id"], "agent:intake", "classified", result.model_dump())
        return {
            "intake": result.model_dump(mode="json"),
            "trail": [f"intake: {result.request_type} (confidence {result.confidence:.2f})"],
        }

    async def plan(state: CareState) -> dict[str, Any]:
        result = planner.make_plan(Intake(**state["intake"]), settings.confidence_threshold)
        store.record(state["request_id"], "agent:planner", "planned", result.model_dump())
        note = f"plan: {result.playbook}" + (
            f", review required ({'; '.join(result.reasons)})" if result.requires_review else ""
        )
        return {"plan": result.model_dump(mode="json"), "trail": [note]}

    async def gather(state: ExecState) -> dict[str, Any]:
        feedback = state.get("verdict", {}).get("feedback", {})
        ctx = await executor.gather(
            gw, state["request_id"], state["patient_id"], Intake(**state["intake"]), feedback
        )
        return {"context": ctx}

    async def propose(state: ExecState) -> dict[str, Any]:
        ctx = dict(state["context"])
        ctx["issues"] = list(ctx.get("issues", []))
        action = await executor.propose(
            llm, state["masked_message"], Intake(**state["intake"]), ctx
        )
        detail = action.model_dump() if action else {"issues": ctx["issues"]}
        store.record(state["request_id"], "agent:executor", "proposed", detail)
        return {"context": ctx, "proposed": action.model_dump() if action else None}

    async def verify(state: CareState) -> dict[str, Any]:
        proposed = state.get("proposed")
        verdict = await verifier.verify(
            gateway=gw,
            llm=llm,
            request_id=state["request_id"],
            masked_message=state["masked_message"],
            intake=Intake(**state["intake"]),
            proposed=ProposedAction(**proposed) if proposed else None,
            ctx=state["context"],
            previous_feedback=state.get("verdict", {}).get("feedback", {}),
        )
        attempts = state.get("attempts", 0) + 1
        store.record(state["request_id"], "agent:verifier", "verdict", verdict.model_dump())
        proposal = (
            f"executor: proposed {proposed['description']}"
            if proposed
            else "executor: no action proposed"
        )
        note = (
            f"verifier: passed (confidence {verdict.confidence:.2f})"
            if verdict.passed
            else f"verifier: failed attempt {attempts}: {'; '.join(verdict.issues)}"
        )
        return {"verdict": verdict.model_dump(), "attempts": attempts, "trail": [proposal, note]}

    async def human_review(state: CareState) -> dict[str, Any]:
        verdict = Verdict(**state["verdict"])
        plan_ = Plan(**state["plan"])
        proposed = state.get("proposed")
        reasons = list(plan_.reasons)
        if proposed:
            reasons += ProposedAction(**proposed).review_reasons
        if verdict.passed and verdict.confidence < settings.confidence_threshold:
            reasons.append(f"verifier confidence {verdict.confidence:.2f}")
        # Everything before interrupt() runs again on resume, so keep this side-effect free.
        decision_raw = interrupt(
            {
                "request_id": state["request_id"],
                "summary": state["intake"]["summary"],
                "request_type": state["intake"]["request_type"],
                "message": state["masked_message"],
                "proposed_action": proposed["description"] if proposed else None,
                "if_approved": (
                    proposed["description"]
                    if proposed
                    else "open a ticket so staff can handle it manually"
                ),
                "why_review": reasons,
                "verifier_issues": verdict.issues,
            }
        )
        decision = ReviewDecision.model_validate(decision_raw)
        store.record(
            state["request_id"], f"human:{decision.reviewer}", "reviewed", decision.model_dump()
        )
        verb = "approved" if decision.approved else "rejected"
        return {
            "review": decision.model_dump(),
            "trail": [f"review: {verb} by {decision.reviewer}"],
        }

    async def commit(state: CareState) -> dict[str, Any]:
        rid, pid = state["request_id"], state["patient_id"]
        proposed = state.get("proposed")
        verdict = Verdict(**state["verdict"])
        rtype = RequestType(state["intake"]["request_type"])

        if proposed is None and rtype == RequestType.BILLING and verdict.passed:
            # Read-only answer; nothing to write.
            return {"status": Status.COMPLETED.value, "outcome": {}, "trail": ["commit: read-only"]}

        if proposed is None:
            action = ProposedAction(
                tool="create_ticket",
                args={"category": "needs_staff", "summary": state["intake"]["summary"]},
                description="Hand off to staff",
            )
        else:
            action = ProposedAction(**proposed)

        args = restore(action.args, state.get("vault", {}))
        args |= {"patient_id": pid, "idempotency_key": f"{rid}:{action.tool}"}
        try:
            outcome = await gw.call(rid, action.tool, args)
        except ToolRefused as exc:
            # The world changed between proposal and commit. Don't guess; hand it to a person.
            ticket = await gw.call(
                rid,
                "create_ticket",
                {
                    "patient_id": pid,
                    "category": "needs_staff",
                    "summary": f"Automated step failed at commit: {exc.message}",
                    "idempotency_key": f"{rid}:fallback_ticket",
                },
            )
            return {
                "status": Status.HANDED_OFF.value,
                "outcome": {**ticket, "error": exc.message},
                "trail": [f"commit: {action.tool} refused ({exc.message}); handed off"],
            }

        status = Status.HANDED_OFF if action.tool == "create_ticket" else Status.COMPLETED
        if action.tool == "create_ticket":
            outcome = {**outcome, "category": action.args.get("category")}
        return {
            "status": status.value,
            "outcome": outcome,
            "trail": [f"commit: {action.tool} done"],
        }

    async def respond(state: CareState) -> dict[str, Any]:
        review = state.get("review")
        status = Status(state["status"])
        if review and not review["approved"]:
            status = Status.REJECTED
        facts = responder.facts_for(
            status,
            RequestType(state["intake"]["request_type"]),
            state.get("outcome"),
            state.get("context", {}),
        )
        vault = state.get("vault", {})
        name_token = next(t for t, v in vault.items() if v == state["first_name"])
        text = await responder.write_reply(llm, name_token, state["first_name"], facts)
        text = restore(text, vault)
        store.record(state["request_id"], "agent:responder", "replied", {"facts": facts})
        return {"reply": text, "status": status.value, "trail": ["responder: reply drafted"]}

    # ---------------------------------------------------------------- routing

    def after_screen(state: CareState) -> Literal["intake", "__end__"]:
        return "__end__" if state.get("urgent") else "intake"

    def after_verify(state: CareState) -> Literal["execute", "human_review", "commit"]:
        verdict = Verdict(**state["verdict"])
        if not verdict.passed:
            retryable = verdict.feedback.get("retryable", True)
            if retryable and state["attempts"] < settings.max_attempts:
                return "execute"
            return "human_review"
        proposed = state.get("proposed")
        if proposed and proposed["tool"] == "create_ticket" and not proposed.get("review_reasons"):
            return "commit"  # a hand-off to staff is always safe to do without sign-off
        needs_review = (
            Plan(**state["plan"]).requires_review
            or bool(proposed and proposed.get("review_reasons"))
            or verdict.confidence < settings.confidence_threshold
        )
        return "human_review" if needs_review else "commit"

    def after_review(state: CareState) -> Literal["commit", "respond"]:
        review = state.get("review") or {}
        return "commit" if review.get("approved") else "respond"

    # ------------------------------------------------------------------ wiring

    # The subgraph gets its own, narrower state. It can read what it needs and write
    # only context/proposed, which also keeps the parent's append-only trail from
    # being merged twice.
    execute = StateGraph(ExecState)
    execute.add_node("gather", gather, retry_policy=TOOL_RETRY)
    execute.add_node("propose", propose, retry_policy=MODEL_RETRY)
    execute.add_edge(START, "gather")
    execute.add_edge("gather", "propose")
    execute.add_edge("propose", END)

    g = StateGraph(CareState)
    g.add_node("screen", screen, retry_policy=TOOL_RETRY)
    g.add_node("intake", run_intake, retry_policy=MODEL_RETRY)
    g.add_node("plan", plan)
    g.add_node("execute", execute.compile())
    g.add_node("verify", verify, retry_policy=MODEL_RETRY)
    g.add_node("human_review", human_review)
    g.add_node("commit", commit, retry_policy=TOOL_RETRY)
    g.add_node("respond", respond, retry_policy=MODEL_RETRY)

    g.add_edge(START, "screen")
    g.add_conditional_edges("screen", after_screen)
    g.add_edge("intake", "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges("verify", after_verify)
    g.add_conditional_edges("human_review", after_review)
    g.add_edge("commit", "respond")
    g.add_edge("respond", END)

    return g.compile(checkpointer=checkpointer, name="careflow")
