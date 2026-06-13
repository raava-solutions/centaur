"""CLI for the Supermemory tool."""

from __future__ import annotations

import json

import typer
from dotenv import load_dotenv
from rich.console import Console

from .client import _client

load_dotenv()

app = typer.Typer(name="supermemory", help="Supermemory recall/write/status")
console = Console(stderr=True)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def recall(
    query: str = typer.Argument(..., help="Search query"),
    container_tag: str | None = typer.Option(None, "--container-tag", help="Memory container"),
    limit: int = typer.Option(5, "--limit", "-n", help="Maximum results"),
):
    """Search Supermemory."""
    try:
        payload = _client().recall(query=query, container_tag=container_tag, limit=limit)
    except Exception as exc:  # pragma: no cover - CLI surface
        console.print(f"[red]recall failed:[/] {exc}")
        raise typer.Exit(1) from exc
    _print_json(payload)


@app.command()
def write(
    content: str = typer.Argument(..., help="Content to store"),
    container_tag: str | None = typer.Option(None, "--container-tag", help="Memory container"),
):
    """Write content to Supermemory."""
    try:
        payload = _client().write(content=content, container_tag=container_tag)
    except Exception as exc:  # pragma: no cover - CLI surface
        console.print(f"[red]write failed:[/] {exc}")
        raise typer.Exit(1) from exc
    _print_json(payload)


@app.command()
def status(memory_id: str = typer.Argument(..., help="Supermemory document id")):
    """Check document status."""
    try:
        payload = _client().status(memory_id)
    except Exception as exc:  # pragma: no cover - CLI surface
        console.print(f"[red]status failed:[/] {exc}")
        raise typer.Exit(1) from exc
    _print_json(payload)


if __name__ == "__main__":
    app()

