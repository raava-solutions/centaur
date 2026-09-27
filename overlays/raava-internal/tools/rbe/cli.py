"""CLI for the Raava Brain Engine (RBE)."""

from dotenv import load_dotenv

load_dotenv()

import json

import typer
from rich.console import Console

app = typer.Typer(name="rbe", help="Query the Raava Brain Engine knowledge base")
console = Console()


def get_client():
    from .client import RbeClient

    return RbeClient()


def _emit(result, json_output: bool):
    if json_output:
        print(json.dumps(result, indent=2, default=str))
    else:
        console.print_json(json.dumps(result, default=str))


@app.command()
def query(
    q: str = typer.Argument(..., help="Question or search phrase"),
    limit: int = typer.Option(10, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Hybrid query with cited hits."""
    _emit(get_client().query(q, limit=limit), json_output)


@app.command()
def search(
    q: str = typer.Argument(..., help="Search phrase"),
    limit: int = typer.Option(10, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Raw hybrid search."""
    _emit(get_client().search(q, limit=limit), json_output)


@app.command()
def get_page(
    slug: str = typer.Argument(..., help="Page slug"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Fetch one page by slug."""
    _emit(get_client().get_page(slug), json_output)


@app.command()
def list_pages(
    type: str = typer.Option(None, "--type", help="Page type filter"),
    tag: str = typer.Option(None, "--tag", help="Tag filter"),
    limit: int = typer.Option(20, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
):
    """List pages with optional filters."""
    _emit(get_client().list_pages(type=type, tag=tag, limit=limit), json_output)


@app.command()
def facts(
    entity_slug: str = typer.Argument(..., help="Entity slug"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Fetch fact rows for an entity."""
    _emit(get_client().facts(entity_slug), json_output)


@app.command()
def backlinks(
    slug: str = typer.Argument(..., help="Page slug"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Fetch inbound wikilinks to a page."""
    _emit(get_client().backlinks(slug), json_output)


@app.command()
def timeline(
    entity_slug: str = typer.Argument(..., help="Entity slug"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Fetch the fact timeline for an entity."""
    _emit(get_client().timeline(entity_slug), json_output)


if __name__ == "__main__":
    app()
