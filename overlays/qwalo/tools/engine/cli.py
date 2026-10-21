"""CLI for the Qwalo engine tool: read-only lead, verdict, queue, and metrics reads."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .client import _client


def _emit(result: Any) -> int:
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    return 1 if isinstance(result, dict) and "error" in result else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engine", description="Read Qwalo lead-qualification answers"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    leads = sub.add_parser("leads", help="this client's leads with their current verdicts")
    leads.add_argument("--limit", type=int, default=20)
    leads.add_argument("--status", default="", help="filter by engine status, e.g. needs_human")
    leads.add_argument("--cursor", default="", help="next-page cursor from the previous page")

    lead = sub.add_parser("lead", help="one lead: snapshots, verdicts, human label")
    lead.add_argument("external_id")

    verdict = sub.add_parser("verdict", help="the current verdict for one lead")
    verdict.add_argument("external_id")

    queue = sub.add_parser("queue", help="cases waiting for a human label")
    queue.add_argument("--limit", type=int, default=20)

    sub.add_parser("metrics", help="cascade counts and stage agreement")

    args = parser.parse_args(argv)
    client = _client()
    if args.command == "leads":
        return _emit(client.list_leads(limit=args.limit, status=args.status, cursor=args.cursor))
    if args.command == "lead":
        return _emit(client.lead(args.external_id))
    if args.command == "verdict":
        return _emit(client.verdict(args.external_id))
    if args.command == "queue":
        return _emit(client.review_queue(limit=args.limit))
    return _emit(client.metrics())


if __name__ == "__main__":
    raise SystemExit(main())
