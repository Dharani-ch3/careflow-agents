"""Command-line entry point.

careflow demo                         walk through a handful of requests
careflow ask P001 "move my appointment to Friday"
careflow pending                      list requests waiting for review
careflow review REQ-... --approve --reviewer nurse.kim
careflow trail REQ-...                print the audit trail
careflow evals                        run the offline evaluation suite
careflow seed                         reset the demo data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import textwrap
from pathlib import Path

from careflow.config import Settings
from careflow.evals import runner
from careflow.models import ReviewDecision
from careflow.seed import seed
from careflow.service import CareFlow, NotAwaitingReview, NotFound, RequestView, UnknownPatient

DEMO = [
    ("P001", "Hi, I can't make my appointment this week. Could we move it to next week, "
             "afternoon if possible?"),
    ("P002", "Why do I owe money on INV-1042? I thought insurance covered it."),
    ("P001", "Can you refill my atorvastatin? I'm almost out."),
    ("P002", "Please refill my oxycodone."),
    ("P004", "I've had chest pain since this morning, should I still come in Thursday?"),
]  # fmt: skip


def _settings(args: argparse.Namespace) -> Settings:
    overrides = {}
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = Path(args.data_dir)
    return Settings(**overrides)  # type: ignore[arg-type]


def _print_view(view: RequestView) -> None:
    print(f"  {view.request_id}  ->  {view.status}  ({view.request_type or 'n/a'})")
    for step in view.trail:
        print(f"    · {step}")
    if view.reply:
        print("  Reply to patient:")
        print(textwrap.indent(textwrap.fill(view.reply, 88), "    "))
    if view.pending_review:
        print("  Waiting for a reviewer because:")
        for reason in view.pending_review["why_review"] or view.pending_review["verifier_issues"]:
            print(f"    - {reason}")
        print(f"  If approved: {view.pending_review['if_approved']}")


async def _demo(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="careflow-"))
    settings = Settings(data_dir=data_dir)
    seed(settings.ehr_db, today=settings.current_date()).close()
    print(f"LLM: {settings.llm_provider}   data: {data_dir}\n")
    async with CareFlow(settings) as app:
        waiting = []
        for patient_id, message in DEMO:
            print(f"[{patient_id}] {message}")
            view = await app.submit(patient_id, message)
            _print_view(view)
            if view.pending_review:
                waiting.append(view.request_id)
            print()
        for request_id in waiting:
            print(f"Reviewer approves {request_id}")
            _print_view(
                await app.review(request_id, ReviewDecision(approved=True, reviewer="demo"))
            )
            print()
    return 0


async def _ask(args: argparse.Namespace) -> int:
    async with CareFlow(_settings(args)) as app:
        try:
            _print_view(await app.submit(args.patient_id, args.message))
        except UnknownPatient:
            print(f"Unknown patient {args.patient_id}. Try P001-P005.", file=sys.stderr)
            return 2
    return 0


async def _pending(args: argparse.Namespace) -> int:
    async with CareFlow(_settings(args)) as app:
        rows = app.pending_reviews()
        if not rows:
            print("Nothing waiting for review.")
        for r in rows:
            print(f"{r['id']}  {r['patient_id']}  {r['request_type']}  {r['summary']}")
    return 0


async def _review(args: argparse.Namespace) -> int:
    decision = ReviewDecision(approved=args.approve, reviewer=args.reviewer, note=args.note or "")
    async with CareFlow(_settings(args)) as app:
        try:
            _print_view(await app.review(args.request_id, decision))
        except NotFound:
            print(f"No request {args.request_id}", file=sys.stderr)
            return 2
        except NotAwaitingReview:
            print(f"{args.request_id} is not waiting for review", file=sys.stderr)
            return 2
    return 0


async def _trail(args: argparse.Namespace) -> int:
    async with CareFlow(_settings(args)) as app:
        try:
            for e in app.audit_trail(args.request_id):
                detail = json.dumps(e["detail"]) if e["detail"] else ""
                print(f"{e['at']}  {e['actor']:<28} {e['action']:<16} {detail[:120]}")
        except NotFound:
            print(f"No request {args.request_id}", file=sys.stderr)
            return 2
    return 0


async def _evals(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else None
    results, summary = await runner.run(data_dir=data_dir)
    print(runner.render(results, summary, markdown=args.markdown))
    return 1 if runner.failed_gates(summary) else 0


def _seed(args: argparse.Namespace) -> int:
    settings = _settings(args)
    for db in (settings.ehr_db, settings.app_db, settings.checkpoint_db):
        db.unlink(missing_ok=True)
    seed(settings.ehr_db, today=settings.current_date()).close()
    print(f"Fresh demo data in {settings.data_dir}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="careflow", description=__doc__.splitlines()[0] if __doc__ else None
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="show audit log lines as they happen"
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--data-dir", help="override CAREFLOW_DATA_DIR")
        return sp

    add("demo", "run a short scripted walkthrough on fresh data")
    ask = add("ask", "submit one patient message")
    ask.add_argument("patient_id")
    ask.add_argument("message")
    add("pending", "list requests waiting for review")
    rv = add("review", "approve or reject a paused request")
    rv.add_argument("request_id")
    choice = rv.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--reject", dest="approve", action="store_false")
    rv.add_argument("--reviewer", required=True)
    rv.add_argument("--note")
    tr = add("trail", "print the audit trail for a request")
    tr.add_argument("request_id")
    ev = add("evals", "run the offline evaluation suite")
    ev.add_argument("--markdown", action="store_true", help="GitHub-flavoured table output")
    add("seed", "reset the demo data")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("careflow").setLevel(logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)

    if args.command == "seed":
        return _seed(args)
    handler = {
        "demo": _demo,
        "ask": _ask,
        "pending": _pending,
        "review": _review,
        "trail": _trail,
        "evals": _evals,
    }[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
