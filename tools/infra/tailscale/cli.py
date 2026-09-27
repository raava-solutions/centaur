"""CLI for Tailscale API."""

from dotenv import load_dotenv

load_dotenv()

import json  # noqa: E402

import typer  # noqa: E402
from rich.console import Console  # noqa: E402

app = typer.Typer(name="tailscale", help="Tailscale CLI — devices, ACL, DNS, audit logs (read-only)")
console = Console()


def get_client():
    from .client import TailscaleClient

    return TailscaleClient()


@app.command("devices")
def list_devices(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List tailnet devices."""
    result = get_client().list_devices()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for d in result:
        addrs = ",".join(d.get("addresses", []) or [])
        console.print(
            f"[cyan]{d.get('hostname', '')}[/] {d.get('name', '')} "
            f"[dim]{addrs}[/] user={d.get('user', '')} os={d.get('os', '')} "
            f"authorized={d.get('authorized', '')} lastSeen={d.get('lastSeen', '')}"
        )


@app.command("device")
def get_device(
    device_id: str = typer.Argument(..., help="Device id"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show one device."""
    result = get_client().get_device(device_id)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[bold]{result.get('hostname', device_id)}[/]")
    console.print(f"  name: {result.get('name', '')}")
    console.print(f"  addresses: {', '.join(result.get('addresses', []) or [])}")
    console.print(f"  user: {result.get('user', '')}")
    console.print(f"  lastSeen: {result.get('lastSeen', '')}")


@app.command("policy")
def get_policy_file(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Print the tailnet ACL policy file (raw HuJSON)."""
    result = get_client().get_policy_file()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(result["raw"])


@app.command("dns")
def list_dns_nameservers(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show tailnet DNS nameservers."""
    result = get_client().list_dns_nameservers()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"nameservers: {', '.join(result.get('dns', []) or [])}")
    console.print(f"magicDNS: {result.get('magicDNS', '')}")


@app.command("audit-logs")
def audit_logs(
    start: str = typer.Argument(..., help="Window start, RFC3339, e.g. 2026-09-26T00:00:00Z"),
    end: str = typer.Option(None, "--end", "-e", help="Window end, RFC3339 (default: now)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Fetch tailnet audit logs for a time window."""
    result = get_client().audit_logs(start=start, end=end)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    if not result:
        console.print("[yellow]No audit log entries in window[/]")
        return

    for entry in result:
        console.print(
            f"[dim]{entry.get('eventTime', '')}[/] {entry.get('action', '')} "
            f"actor={entry.get('actor', {})} target={entry.get('target', {})}"
        )


if __name__ == "__main__":
    app()
