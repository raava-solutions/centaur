"""Proxmox VE HTTP API client — cluster status and guest power control.

Wraps the Proxmox VE REST API with PVEAPIToken auth: cluster/node status,
VM/CT inventory (via cluster resources), storage listing, and guest power
actions (start/stop/shutdown/reboot). Proxmox wraps every response in a
top-level ``data`` field; ``_request`` unwraps it.
"""

import os
from typing import Any

import httpx

from centaur_sdk import secret

try:  # urllib3 is not a hard dependency; only used to silence TLS warnings
    import urllib3
except ImportError:  # pragma: no cover
    urllib3 = None  # type: ignore[assignment]

_VM_FIELDS = ("vmid", "name", "node", "type", "status", "cpus", "maxmem", "maxdisk", "uptime")
_STORAGE_FIELDS = ("storage", "node", "type", "status", "shared", "disk", "maxdisk")


class ProxmoxClient:
    """Client for the Proxmox VE REST API.

    Read and guest power-control only. This tool intentionally exposes no
    create, delete, or configuration methods.

    Authenticates with ``Authorization: PVEAPIToken=<token_id>=<token_secret>``.
    Reads ``CENTAUR_PROXMOX_BASE_URL`` (default ``https://pve-03:8006/api2/json``),
    ``CENTAUR_PROXMOX_TOKEN_ID`` (default ``centaur@pve!ops``), and
    ``CENTAUR_PROXMOX_TOKEN_SECRET``. TLS verification is off by default
    (self-signed PVE certs); set ``CENTAUR_PROXMOX_VERIFY_TLS=true`` to enable.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
        verify_tls: bool | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = base_url
        self._token_id = token_id
        self._token_secret = token_secret
        self._verify_tls = verify_tls
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def base_url(self) -> str:
        return (
            self._base_url
            or os.getenv("CENTAUR_PROXMOX_BASE_URL", "https://pve-03:8006/api2/json")
        ).rstrip("/")  # noqa: TID251

    @property
    def token_id(self) -> str:
        return self._token_id or os.getenv("CENTAUR_PROXMOX_TOKEN_ID", "centaur@pve!ops")

    def _token_secret_value(self) -> str:
        return self._token_secret or secret("CENTAUR_PROXMOX_TOKEN_SECRET", "")

    def _auth_headers(self) -> dict[str, str]:
        token_secret = self._token_secret_value()
        if not token_secret:
            return {}
        return {"Authorization": f"PVEAPIToken={self.token_id}={token_secret}"}

    def _verify(self) -> bool:
        if self._verify_tls is not None:
            return self._verify_tls
        return os.getenv("CENTAUR_PROXMOX_VERIFY_TLS", "").lower() in ("1", "true")

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            verify = self._verify()
            if not verify and urllib3 is not None:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self._auth_headers(),
                timeout=self.timeout,
                verify=verify,
                follow_redirects=True,
            )
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self.client.request(method, path, params=clean)
        if resp.status_code >= 400:
            raise RuntimeError(f"Proxmox API error ({resp.status_code}): {resp.text}")
        payload = resp.json()
        # Proxmox wraps every response in {"data": ...}
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # -- Cluster / nodes -------------------------------------------------------

    def version(self) -> dict:
        """Get the Proxmox VE server version and release info."""
        return self._request("GET", "/version")

    def cluster_status(self) -> list:
        """Get cluster status entries (cluster quorum info plus per-node entries)."""
        return self._request("GET", "/cluster/status")

    def list_nodes(self) -> list:
        """List cluster nodes with status, cpu/memory usage, and uptime."""
        return self._request("GET", "/nodes")

    def get_node_status(self, node: str) -> dict:
        """Get detailed status for one node (cpu, memory, storage, kernel version).

        Args:
            node: Node name, e.g. 'pve-03' (see list_nodes).
        """
        return self._request("GET", f"/nodes/{node}/status")

    # -- Guests (VMs and containers) -------------------------------------------

    def list_vms(self, node: str | None = None) -> list:
        """List VMs and containers across the cluster, trimmed to key fields.

        Args:
            node: Restrict to guests on this node name. Omit for all nodes.

        Returns entries with: vmid, name, node, type ('qemu' or 'lxc'), status,
        cpus, maxmem, maxdisk, uptime.
        """
        resources = self._request("GET", "/cluster/resources", params={"type": "vm"})
        guests = [
            {k: r.get(k) for k in _VM_FIELDS}
            for r in resources
            if isinstance(r, dict)
        ]
        if node is not None:
            guests = [g for g in guests if g.get("node") == node]
        return guests

    def _resolve_vm(self, vmid: int) -> tuple[str, str]:
        for guest in self.list_vms():
            if guest.get("vmid") == vmid:
                return guest["node"], guest["type"]
        raise RuntimeError(f"vm {vmid} not found")

    def get_vm_status(self, vmid: int) -> dict:
        """Get current status of a VM or container (state, cpu, mem, uptime, ...).

        Args:
            vmid: Numeric guest id (see list_vms). Works for both qemu and lxc.
        """
        node, type_ = self._resolve_vm(vmid)
        return self._request("GET", f"/nodes/{node}/{type_}/{vmid}/status/current")

    def _power(self, vmid: int, action: str) -> dict:
        node, type_ = self._resolve_vm(vmid)
        upid = self._request("POST", f"/nodes/{node}/{type_}/{vmid}/status/{action}")
        return {
            "vmid": vmid,
            "action": action,
            "upid": upid if isinstance(upid, str) else str(upid),
        }

    def start_vm(self, vmid: int) -> dict:
        """Start a stopped VM or container. Returns {'vmid', 'action', 'upid'}.

        Args:
            vmid: Numeric guest id (see list_vms).
        """
        return self._power(vmid, "start")

    def stop_vm(self, vmid: int) -> dict:
        """Force-stop a VM or container (like pulling the plug). Prefer shutdown_vm.

        Args:
            vmid: Numeric guest id (see list_vms).
        """
        return self._power(vmid, "stop")

    def shutdown_vm(self, vmid: int) -> dict:
        """Gracefully shut down a VM or container (ACPI/guest-agent shutdown).

        Args:
            vmid: Numeric guest id (see list_vms).
        """
        return self._power(vmid, "shutdown")

    def reboot_vm(self, vmid: int) -> dict:
        """Reboot a VM or container (graceful shutdown then start).

        Args:
            vmid: Numeric guest id (see list_vms).
        """
        return self._power(vmid, "reboot")

    # -- Storage -----------------------------------------------------------------

    def list_storage(self) -> list:
        """List storage pools across the cluster, trimmed to key fields.

        Returns entries with: storage, node, type, status, shared, disk (used
        bytes), maxdisk (total bytes).
        """
        resources = self._request("GET", "/cluster/resources", params={"type": "storage"})
        return [
            {k: r.get(k) for k in _STORAGE_FIELDS}
            for r in resources
            if isinstance(r, dict)
        ]

    # -- Lifecycle -----------------------------------------------------------------

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> ProxmoxClient:
    return ProxmoxClient()
