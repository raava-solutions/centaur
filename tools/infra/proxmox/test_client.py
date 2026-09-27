from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

spec = importlib.util.spec_from_file_location("proxmox_client", Path(__file__).with_name("client.py"))
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ProxmoxClient = module.ProxmoxClient


class RecordingProxmoxClient(ProxmoxClient):
    """Captures requests instead of hitting the network."""

    def __init__(self) -> None:
        super().__init__(
            base_url="https://pve-03:8006/api2/json",
            token_id="centaur@pve!ops",
            token_secret="secret-uuid",
        )
        self.calls: list[dict[str, Any]] = []
        self.response: Any = []

    def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        self.calls.append({"method": method, "path": path, "params": clean})
        return self.response

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


def _guests() -> list[dict[str, Any]]:
    return [
        {
            "vmid": 100,
            "name": "web-01",
            "node": "pve-01",
            "type": "qemu",
            "status": "running",
            "cpus": 4,
            "maxmem": 8589934592,
            "maxdisk": 68719476736,
            "uptime": 3600,
            "id": "qemu/100",
            "template": 0,
            "extra_field": "ignored",
        },
        {
            "vmid": 200,
            "name": "db-ct",
            "node": "pve-02",
            "type": "lxc",
            "status": "stopped",
            "cpus": 2,
            "maxmem": 4294967296,
            "maxdisk": 34359738368,
            "uptime": 0,
            "id": "lxc/200",
        },
    ]


def test_auth_header_uses_pveapitoken_format() -> None:
    client = ProxmoxClient(
        base_url="https://pve-03:8006/api2/json",
        token_id="centaur@pve!ops",
        token_secret="secret-uuid",
    )
    assert client._auth_headers() == {
        "Authorization": "PVEAPIToken=centaur@pve!ops=secret-uuid"
    }


def test_auth_header_empty_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # No constructor value and no resolvable secret -> no Authorization header.
    monkeypatch.setattr(module, "secret", lambda key, default=None: default or "")
    client = ProxmoxClient(base_url="https://pve-03:8006/api2/json", token_secret=None)
    assert client._auth_headers() == {}


def test_verify_tls_defaults_false_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CENTAUR_PROXMOX_VERIFY_TLS", raising=False)
    assert ProxmoxClient()._verify() is False
    monkeypatch.setenv("CENTAUR_PROXMOX_VERIFY_TLS", "true")
    assert ProxmoxClient()._verify() is True
    assert ProxmoxClient(verify_tls=False)._verify() is False


def test_version_path() -> None:
    client = RecordingProxmoxClient()

    client.version()

    assert client.last == {"method": "GET", "path": "/version", "params": {}}


def test_cluster_status_path() -> None:
    client = RecordingProxmoxClient()

    client.cluster_status()

    assert client.last["path"] == "/cluster/status"


def test_list_nodes_path() -> None:
    client = RecordingProxmoxClient()

    client.list_nodes()

    assert client.last["path"] == "/nodes"


def test_get_node_status_path() -> None:
    client = RecordingProxmoxClient()

    client.get_node_status("pve-03")

    assert client.last["path"] == "/nodes/pve-03/status"


def test_list_vms_trims_fields() -> None:
    client = RecordingProxmoxClient()
    client.response = _guests()

    guests = client.list_vms()

    assert client.last["path"] == "/cluster/resources"
    assert client.last["params"] == {"type": "vm"}
    assert guests == [
        {
            "vmid": 100,
            "name": "web-01",
            "node": "pve-01",
            "type": "qemu",
            "status": "running",
            "cpus": 4,
            "maxmem": 8589934592,
            "maxdisk": 68719476736,
            "uptime": 3600,
        },
        {
            "vmid": 200,
            "name": "db-ct",
            "node": "pve-02",
            "type": "lxc",
            "status": "stopped",
            "cpus": 2,
            "maxmem": 4294967296,
            "maxdisk": 34359738368,
            "uptime": 0,
        },
    ]


def test_list_vms_filters_by_node() -> None:
    client = RecordingProxmoxClient()
    client.response = _guests()

    guests = client.list_vms(node="pve-02")

    assert len(guests) == 1
    assert guests[0]["vmid"] == 200


def test_get_vm_status_resolves_node_and_type() -> None:
    client = RecordingProxmoxClient()
    client.response = _guests()
    client.get_vm_status(100)

    # Second call is the actual status request (first resolved via list_vms)
    assert client.calls[-1]["path"] == "/nodes/pve-01/qemu/100/status/current"


def test_get_vm_status_resolves_lxc() -> None:
    client = RecordingProxmoxClient()
    client.response = _guests()

    client.get_vm_status(200)

    assert client.calls[-1]["path"] == "/nodes/pve-02/lxc/200/status/current"


def test_vm_resolution_failure_raises() -> None:
    client = RecordingProxmoxClient()
    client.response = _guests()

    with pytest.raises(RuntimeError, match="vm 999 not found"):
        client.get_vm_status(999)


def test_power_actions_post_to_status_endpoint() -> None:
    for action in ("start", "stop", "shutdown", "reboot"):
        client = RecordingProxmoxClient()
        client.response = _guests()

        result = getattr(client, f"{action}_vm")(100)

        assert result["vmid"] == 100
        assert result["action"] == action
        assert client.calls[-1]["method"] == "POST"
        assert client.calls[-1]["path"] == f"/nodes/pve-01/qemu/100/status/{action}"


class QueuedProxmoxClient(ProxmoxClient):
    """Returns canned responses per call, in order."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(
            base_url="https://pve-03:8006/api2/json",
            token_id="centaur@pve!ops",
            token_secret="secret-uuid",
        )
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        self.calls.append({"method": method, "path": path})
        return self.responses.pop(0)


def test_start_vm_returns_upid_dict() -> None:
    client = QueuedProxmoxClient([_guests(), "UPID:pve-01:00001234:start"])

    result = client.start_vm(100)

    assert result == {"vmid": 100, "action": "start", "upid": "UPID:pve-01:00001234:start"}
    assert client.calls[1] == {"method": "POST", "path": "/nodes/pve-01/qemu/100/status/start"}


def test_list_storage_trims_fields() -> None:
    client = RecordingProxmoxClient()
    client.response = [
        {
            "storage": "local-zfs",
            "node": "pve-03",
            "type": "zfspool",
            "status": "available",
            "shared": 0,
            "disk": 100,
            "maxdisk": 200,
            "id": "storage/pve-03/local-zfs",
            "plugintype": "zfspool",
        }
    ]

    storage = client.list_storage()

    assert client.last["params"] == {"type": "storage"}
    assert storage == [
        {
            "storage": "local-zfs",
            "node": "pve-03",
            "type": "zfspool",
            "status": "available",
            "shared": 0,
            "disk": 100,
            "maxdisk": 200,
        }
    ]
