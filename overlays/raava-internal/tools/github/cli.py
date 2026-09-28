"""CLI for GitHub App (centaur-ops) API."""

from dotenv import load_dotenv

load_dotenv()

import json  # noqa: E402

import typer  # noqa: E402
from rich.console import Console  # noqa: E402

app = typer.Typer(
    name="github",
    help="GitHub CLI (App auth) — repos, files, branches, PRs, workflow runs",
)
console = Console()


def get_client():
    from .client import GitHubClient

    return GitHubClient()


@app.command("installations")
def list_installations(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List GitHub App installations."""
    result = get_client().list_installations()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for inst in result:
        console.print(
            f"[cyan]{(inst.get('account') or {}).get('login', '')}[/] "
            f"id={inst.get('id')} selection={inst.get('repository_selection', '')}"
        )


@app.command("repos")
def list_repos(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List repositories across all installations."""
    result = get_client().list_repos()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for r in result:
        vis = "private" if r.get("private") else "public"
        console.print(f"[cyan]{r.get('full_name', '')}[/] [{vis}] default={r.get('default_branch', '')}")


@app.command("repo")
def get_repo(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show one repository."""
    result = get_client().get_repo(owner, repo)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[bold]{result.get('full_name', '')}[/] default={result.get('default_branch', '')}")
    if result.get("description"):
        console.print(f"  {result['description']}")


@app.command("file")
def get_file(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    path: str = typer.Argument(..., help="File path"),
    ref: str = typer.Option(None, "--ref", "-r", help="Branch/tag/sha"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Read a file from a repository."""
    result = get_client().get_file(owner, repo, path, ref=ref)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[dim]# {result['path']} (sha {result['sha'][:8]})[/]")
    console.print(result["content"])


@app.command("ls")
def list_dir(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    path: str = typer.Argument("", help="Directory path ('' for root)"),
    ref: str = typer.Option(None, "--ref", "-r", help="Branch/tag/sha"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List a directory in a repository."""
    result = get_client().list_dir(owner, repo, path, ref=ref)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for e in result:
        marker = "/" if e.get("type") == "dir" else ""
        console.print(f"[cyan]{e.get('name', '')}{marker}[/] [dim]{e.get('size', '')}[/]")


@app.command("create-branch")
def create_branch(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    branch: str = typer.Argument(..., help="New branch name"),
    from_ref: str = typer.Option(None, "--from-ref", help="Source ref (default: default branch)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create a branch."""
    result = get_client().create_branch(owner, repo, branch, from_ref=from_ref)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    if result.get("existed"):
        console.print(f"[yellow]branch '{result['branch']}' already exists[/]")
    else:
        console.print(f"[green]created branch '{result['branch']}'[/] from {result.get('sha', '')[:8]}")


@app.command("put-file")
def put_file(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    path: str = typer.Argument(..., help="File path"),
    content: str = typer.Argument(..., help="New file content"),
    message: str = typer.Option(..., "--message", "-m", help="Commit message"),
    branch: str = typer.Option(..., "--branch", "-b", help="Target branch (not the default branch)"),
    sha: str = typer.Option(None, "--sha", help="Existing file sha (for updates)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Create or update a file on a non-default branch."""
    result = get_client().put_file(owner, repo, path, content, message, branch, sha=sha)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[green]committed[/] {result.get('path', '')} sha={result.get('sha', '')[:8]} commit={str(result.get('commit_sha', ''))[:8]}")


@app.command("create-pr")
def create_pull_request(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    title: str = typer.Option(..., "--title", "-t", help="PR title"),
    head: str = typer.Option(..., "--head", help="Head branch"),
    base: str = typer.Option(None, "--base", help="Base branch (default: default branch)"),
    body: str = typer.Option("", "--body", help="PR body"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Open a pull request."""
    result = get_client().create_pull_request(owner, repo, title, head, base=base, body=body)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[green]PR #{result.get('number')}[/] {result.get('head')} → {result.get('base')}")
    console.print(result.get("url", ""))


@app.command("prs")
def list_pull_requests(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    state: str = typer.Option("open", "--state", "-s", help="open|closed|all"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List pull requests."""
    result = get_client().list_pull_requests(owner, repo, state=state)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for pr in result:
        draft = " [draft]" if pr.get("draft") else ""
        console.print(
            f"[cyan]#{pr.get('number')}[/] {pr.get('title', '')}{draft} "
            f"[dim]{pr.get('head')} → {pr.get('base')} by {pr.get('user')}[/]"
        )


@app.command("pr")
def get_pull_request(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    number: int = typer.Argument(..., help="PR number"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show one pull request."""
    result = get_client().get_pull_request(owner, repo, number)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[bold]#{result.get('number')} {result.get('title', '')}[/]")
    console.print(f"  {result.get('head')} → {result.get('base')} state={result.get('state')} merged={result.get('merged')} mergeable={result.get('mergeable')}")


@app.command("workflow-runs")
def list_workflow_runs(
    owner: str = typer.Argument(..., help="Repository owner"),
    repo: str = typer.Argument(..., help="Repository name"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max runs"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List recent GitHub Actions workflow runs."""
    result = get_client().list_workflow_runs(owner, repo, limit=limit)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for run in result:
        console.print(
            f"[cyan]{run.get('name', '')}[/] {run.get('status', '')}/{run.get('conclusion', '')} "
            f"[dim]{run.get('head_branch', '')} {run.get('created_at', '')}[/]"
        )


if __name__ == "__main__":
    app()
