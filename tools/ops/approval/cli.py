"""CLI for the approval tool: durable, one-shot operator approvals."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

app = typer.Typer(name="approval", help="Request operator approval for an action and wait for the decision")
console = Console(stderr=True)

EXIT_DENIED = 1
EXIT_EXPIRED_OR_TIMEOUT = 2


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _client():
    from .client import ApprovalClient

    return ApprovalClient()


@app.command("request")
def request(
    summary: Annotated[str, typer.Option(help="Human-readable summary shown on the Slack approval card")],
    action_hash: Annotated[str | None, typer.Option(help="Stable hash of the exact action being approved")] = None,
    action: Annotated[str | None, typer.Option(help="Exact action string; hashed for you (alternative to --action-hash)")] = None,
    requester: Annotated[str | None, typer.Option(help="Who is asking (defaults to the sandbox persona)")] = None,
    execution_id: Annotated[str | None, typer.Option(help="Owning execution id, when known")] = None,
    ttl_seconds: Annotated[int | None, typer.Option(help="Request lifetime before it expires (default 1800)")] = None,
    timeout: Annotated[float | None, typer.Option(help="Local wait deadline in seconds (default: the TTL)")] = None,
):
    """Create an approval request and block until it is approved, denied, or expired.

    The decision is made by a designated approver clicking the signed Approve
    or Deny button in the Slack thread — a typed chat message is NOT approval.
    Exit code 0 = approved, 1 = denied, 2 = expired or local timeout.
    """
    import os

    if bool(action) == bool(action_hash):
        console.print("[red]pass exactly one of --action or --action-hash[/]")
        raise typer.Exit(2)
    from .client import action_hash_for

    resolved_hash = action_hash or action_hash_for(action or "")
    resolved_requester = requester or os.environ.get("CENTAUR_PERSONA_ID") or os.environ.get(
        "AGENT_PERSONA"
    )
    try:
        result = _client().request_and_wait(
            summary,
            resolved_hash,
            execution_id=execution_id,
            requester=resolved_requester,
            ttl_seconds=ttl_seconds,
            timeout_seconds=timeout,
        )
    except Exception as exc:
        console.print(f"[red]approval request failed:[/] {exc}")
        raise typer.Exit(2) from exc
    _print_json(result)
    status = result.get("status")
    if status == "approved":
        return
    if status == "denied":
        raise typer.Exit(EXIT_DENIED)
    raise typer.Exit(EXIT_EXPIRED_OR_TIMEOUT)


@app.command("status")
def status(approval_id: Annotated[str, typer.Argument(help="Approval request id (apr_...)")]):
    """Fetch the current state of an approval request without waiting."""
    try:
        payload = _client().get(approval_id)
    except Exception as exc:
        console.print(f"[red]approval status failed:[/] {exc}")
        raise typer.Exit(2) from exc
    _print_json(payload)


@app.command("hash")
def hash_action(action: Annotated[str, typer.Argument(help="Exact action string to hash")]):
    """Print the stable action hash for an action string."""
    from .client import action_hash_for

    _print_json({"action_hash": action_hash_for(action)})
