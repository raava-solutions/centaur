"""CLI for the Firecrawl research tool."""

from __future__ import annotations

import json

import typer
from dotenv import load_dotenv
from rich.console import Console

from .client import _client

load_dotenv()

app = typer.Typer(name="firecrawl", help="Firecrawl search and single-page scrape")
console = Console(stderr=True)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results"),
    timeout_seconds: float = typer.Option(30.0, "--timeout-seconds", help="Request timeout"),
):
    """Search the web through Firecrawl."""
    try:
        payload = _client().search(query=query, limit=limit, timeout_seconds=timeout_seconds)
    except Exception as exc:  # pragma: no cover - CLI surface
        console.print(f"[red]search failed:[/] {exc}")
        raise typer.Exit(1) from exc
    _print_json(payload)


@app.command()
def scrape(
    url: str = typer.Argument(..., help="URL to scrape"),
    timeout_seconds: float = typer.Option(60.0, "--timeout-seconds", help="Request timeout"),
):
    """Scrape one URL through Firecrawl."""
    try:
        payload = _client().scrape(url=url, timeout_seconds=timeout_seconds)
    except Exception as exc:  # pragma: no cover - CLI surface
        console.print(f"[red]scrape failed:[/] {exc}")
        raise typer.Exit(1) from exc
    _print_json(payload)


if __name__ == "__main__":
    app()

