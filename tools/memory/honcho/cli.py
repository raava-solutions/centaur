"""CLI for the Honcho peer-memory tool."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(name="honcho", help="Honcho peer memory for centaur agents")
console = Console(stderr=True)


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _run(action: str, fn):
    try:
        payload = fn()
    except Exception as exc:  # pragma: no cover - CLI surface
        console.print(f"[red]{action} failed:[/] {exc}")
        raise typer.Exit(1) from exc
    _print_json(payload)


def _client():
    from .client import HonchoClient

    return HonchoClient()


def _parse_metadata(metadata: str | None) -> dict | None:
    if not metadata:
        return None
    parsed = json.loads(metadata)
    if not isinstance(parsed, dict):
        raise typer.BadParameter("--metadata must be a JSON object")
    return parsed


@app.command()
def health():
    """Check Honcho connectivity."""
    _run("health", lambda: _client().health())


@app.command()
def capability():
    """Show non-secret tool configuration (base URL, workspace)."""
    _run("capability", lambda: _client().capability())


@app.command("workspace-ensure")
def workspace_ensure():
    """Create the configured workspace if missing (idempotent)."""
    _run("workspace-ensure", lambda: _client().ensure_workspace())


@app.command("peer-create")
def peer_create(
    peer_id: str = typer.Argument(..., help="Peer id, e.g. slack:U0123ABC or agent:chief"),
    metadata: str | None = typer.Option(None, "--metadata", help="JSON object"),
):
    """Create a peer (idempotent)."""
    _run("peer-create", lambda: _client().peer_create(peer_id, metadata=_parse_metadata(metadata)))


@app.command("peer-get")
def peer_get(
    peer_id: str = typer.Argument(..., help="Peer id"),
):
    """Fetch a peer by id; prints null when unknown."""
    _run("peer-get", lambda: _client().peer_get(peer_id))


@app.command("session-create")
def session_create(
    session_id: str = typer.Argument(..., help="Session id, e.g. the centaur thread key"),
    peers: Annotated[
        list[str] | None, typer.Option("--peer", help="Peer id to attach (repeatable)")
    ] = None,
    metadata: str | None = typer.Option(None, "--metadata", help="JSON object"),
):
    """Create a session (idempotent)."""
    _run(
        "session-create",
        lambda: _client().session_create(
            session_id, peers=peers or None, metadata=_parse_metadata(metadata)
        ),
    )


@app.command("session-get")
def session_get(
    session_id: str = typer.Argument(..., help="Session id"),
):
    """Fetch a session by id; prints null when unknown."""
    _run("session-get", lambda: _client().session_get(session_id))


@app.command("session-add-peer")
def session_add_peer(
    session_id: str = typer.Argument(..., help="Session id"),
    peers: Annotated[
        list[str], typer.Option("--peer", help="Peer id to add (repeatable)")
    ] = ...,
):
    """Add peers to an existing session."""
    _run("session-add-peer", lambda: _client().session_add_peers(session_id, peers))


@app.command("message-add")
def message_add(
    session_id: str = typer.Argument(..., help="Session id"),
    peer_id: str = typer.Option(..., "--peer", help="Author peer id"),
    content: Annotated[
        list[str], typer.Option("--content", help="Message text (repeatable)")
    ] = ...,
):
    """Append one or more messages from a peer to a session."""
    _run(
        "message-add",
        lambda: _client().messages_add(
            session_id, [{"peer_id": peer_id, "content": c} for c in content]
        ),
    )


@app.command("messages-list")
def messages_list(
    session_id: str = typer.Argument(..., help="Session id"),
):
    """List a session's recorded messages."""
    _run("messages-list", lambda: _client().messages_list(session_id))


@app.command("peer-context")
def peer_context(
    peer_id: str = typer.Argument(..., help="Observer peer id"),
    target: str | None = typer.Option(None, "--target", help="Target peer id"),
    search_query: str | None = typer.Option(None, "--search-query", help="Semantic filter"),
):
    """Peer-level context: representation and peer card."""
    _run(
        "peer-context",
        lambda: _client().peer_context(peer_id, target=target, search_query=search_query),
    )


@app.command("peer-representation")
def peer_representation(
    peer_id: str = typer.Argument(..., help="Peer id"),
    session_id: str | None = typer.Option(None, "--session", help="Scope to a session"),
):
    """What Honcho has derived about this peer (metamemory)."""
    _run("peer-representation", lambda: _client().peer_representation(peer_id, session_id=session_id))


@app.command("peer-chat")
def peer_chat(
    peer_id: str = typer.Argument(..., help="Peer id"),
    query: str = typer.Option(..., "--query", "-q", help="Natural-language question"),
    session_id: str | None = typer.Option(None, "--session", help="Scope to a session"),
):
    """Ask Honcho a natural-language question about a peer."""
    _run("peer-chat", lambda: _client().peer_chat(peer_id, query, session_id=session_id))


@app.command("session-context")
def session_context(
    session_id: str = typer.Argument(..., help="Session id"),
    tokens: int | None = typer.Option(None, "--tokens", help="Token budget"),
):
    """Session-level context (messages, summary, representations)."""
    _run("session-context", lambda: _client().session_context(session_id, tokens=tokens))


@app.command("queue-status")
def queue_status():
    """Deriver queue depth (pending metamemory work)."""
    _run("queue-status", lambda: _client().queue_status())


if __name__ == "__main__":
    app()
