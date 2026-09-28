"""CLI for Proxmox VE API."""

from dotenv import load_dotenv

load_dotenv()

import json  # noqa: E402

import typer  # noqa: E402
from rich.console import Console  # noqa: E402

app = typer.Typer(name="proxmox", help="Proxmox VE CLI — status and guest power control")
console = Console()


def get_client():
    from .client import ProxmoxClient

    return ProxmoxClient()


@app.command("version")
def version(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show Proxmox VE server version."""
    result = get_client().version()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[bold]Proxmox VE[/] {result.get('version', '?')} (release {result.get('release', '?')})")


@app.command("cluster-status")
def cluster_status(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show cluster status."""
    result = get_client().cluster_status()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for entry in result:
        console.print(f"[cyan]{entry.get('type', '')}[/] {entry.get('name', '')} — {entry.get('status', entry.get('quorate', ''))}")


@app.command("nodes")
def list_nodes(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List cluster nodes."""
    result = get_client().list_nodes()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for n in result:
        console.print(f"[cyan]{n.get('node', '')}[/] {n.get('status', '')} cpu={n.get('cpu', 0):.1%} uptime={n.get('uptime', 0)}s")


@app.command("vms")
def list_vms(
    node: str = typer.Option(None, "--node", "-n", help="Filter by node name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List VMs and containers."""
    result = get_client().list_vms(node=node)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for vm in result:
        console.print(
            f"[cyan]{vm.get('vmid', '')}[/] {vm.get('name', '')} "
            f"[dim]({vm.get('type', '')}@{vm.get('node', '')})[/] {vm.get('status', '')}"
        )


@app.command("vm-status")
def vm_status(
    vmid: int = typer.Argument(..., help="Numeric guest id"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show current status of a VM or container."""
    result = get_client().get_vm_status(vmid)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[bold]{result.get('name', vmid)}[/] status={result.get('status', '?')} cpu={result.get('cpu', 0):.1%} mem={result.get('mem', 0)}")


def _power(action: str, vmid: int, json_output: bool):
    result = getattr(get_client(), f"{action}_vm")(vmid)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[green]{action}[/] vmid={result['vmid']} upid={result['upid']}")


@app.command("start")
def start_vm(
    vmid: int = typer.Argument(..., help="Numeric guest id"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Start a VM or container."""
    _power("start", vmid, json_output)


@app.command("stop")
def stop_vm(
    vmid: int = typer.Argument(..., help="Numeric guest id"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Force-stop a VM or container."""
    _power("stop", vmid, json_output)


@app.command("shutdown")
def shutdown_vm(
    vmid: int = typer.Argument(..., help="Numeric guest id"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Gracefully shut down a VM or container."""
    _power("shutdown", vmid, json_output)


@app.command("reboot")
def reboot_vm(
    vmid: int = typer.Argument(..., help="Numeric guest id"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Reboot a VM or container."""
    _power("reboot", vmid, json_output)


@app.command("storage")
def list_storage(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List storage pools."""
    result = get_client().list_storage()

    if json_output:
        print(json.dumps(result, indent=2))
        return

    for s in result:
        console.print(
            f"[cyan]{s.get('storage', '')}[/] {s.get('type', '')}@{s.get('node', '')} "
            f"{s.get('status', '')} used={s.get('disk', 0)}/{s.get('maxdisk', 0)}"
        )


@app.command("node-status")
def node_status(
    node: str = typer.Argument(..., help="Node name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show detailed status for one node."""
    result = get_client().get_node_status(node)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(f"[bold]{node}[/] uptime={result.get('uptime', 0)}s cpu={result.get('cpu', 0):.1%}")


if __name__ == "__main__":
    app()
