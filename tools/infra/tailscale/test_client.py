from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

spec = importlib.util.spec_from_file_location("tailscale_client", Path(__file__).with_name("client.py"))
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TailscaleClient = module.TailscaleClient
TOKEN_URL = module.TOKEN_URL


class FakeResponse:
    def __init__(self, payload: Any = None, text: str = "", status_code: int = 200):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class RecordingTailscaleClient(TailscaleClient):
    """Captures HTTP calls (including token mints) instead of hitting the network."""

    def __init__(self) -> None:
        super().__init__(
            client_id="read-id",
            client_secret="read-secret",
            audit_client_id="audit-id",
            audit_client_secret="audit-secret",
        )
        self.calls: list[dict[str, Any]] = []
        self.token_payload: dict[str, Any] = {"access_token": "tok-123", "expires_in": 3600}
        self.response: Any = {}
        self.response_text = ""

    def _http(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": kwargs.get("params"),
                "headers": kwargs.get("headers", {}),
                "auth": kwargs.get("auth"),
                "data": kwargs.get("data"),
            }
        )
        if "oauth/token" in url:
            return FakeResponse(self.token_payload)
        return FakeResponse(self.response, text=self.response_text)

    @property
    def token_mints(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "oauth/token" in c["url"]]

    @property
    def api_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "oauth/token" not in c["url"]]


def _devices() -> dict[str, Any]:
    return {
        "devices": [
            {
                "id": "dev-1",
                "name": "pve-03.tailf55d56.ts.net",
                "hostname": "pve-03",
                "user": "ops@example.com",
                "os": "linux",
                "addresses": ["100.64.0.3"],
                "lastSeen": "2026-09-26T10:00:00Z",
                "authorized": True,
                "extra": "ignored",
            },
            {
                "id": "dev-2",
                "name": "devbox.tailf55d56.ts.net",
                "hostname": "devbox",
                "user": "tagged-device",
                "os": "linux",
                "addresses": ["100.64.0.4"],
                "lastSeen": "2026-09-26T11:00:00Z",
                "authorized": True,
            },
        ]
    }


def test_oauth_token_minted_with_basic_auth_and_cached() -> None:
    client = RecordingTailscaleClient()
    client.response = {"devices": []}

    client.list_devices()
    client.list_devices()

    assert len(client.token_mints) == 1
    mint = client.token_mints[0]
    assert mint["method"] == "POST"
    assert mint["url"] == TOKEN_URL
    assert mint["data"] == {"grant_type": "client_credentials"}
    assert mint["auth"] == ("read-id", "read-secret")
    for call in client.api_calls:
        assert call["headers"]["Authorization"] == "Bearer tok-123"


def test_expired_token_reminted() -> None:
    client = RecordingTailscaleClient()
    client.response = {"devices": []}

    client.list_devices()
    # Force expiry
    kind = "read"
    token, _ = client._tokens[kind]
    client._tokens[kind] = (token, 0.0)
    client.list_devices()

    assert len(client.token_mints) == 2


def test_list_devices_path_and_trimming() -> None:
    client = RecordingTailscaleClient()
    client.response = _devices()

    devices = client.list_devices()

    assert client.api_calls[0]["url"] == "https://api.tailscale.com/api/v2/tailnet/-/devices"
    assert devices == [
        {
            "id": "dev-1",
            "name": "pve-03.tailf55d56.ts.net",
            "hostname": "pve-03",
            "user": "ops@example.com",
            "os": "linux",
            "addresses": ["100.64.0.3"],
            "lastSeen": "2026-09-26T10:00:00Z",
            "authorized": True,
        },
        {
            "id": "dev-2",
            "name": "devbox.tailf55d56.ts.net",
            "hostname": "devbox",
            "user": "tagged-device",
            "os": "linux",
            "addresses": ["100.64.0.4"],
            "lastSeen": "2026-09-26T11:00:00Z",
            "authorized": True,
        },
    ]


def test_get_device_path() -> None:
    client = RecordingTailscaleClient()
    client.response = {"id": "dev-1"}

    result = client.get_device("dev-1")

    assert client.api_calls[0]["url"] == "https://api.tailscale.com/api/v2/device/dev-1"
    assert result == {"id": "dev-1"}


def test_get_policy_file_returns_raw_hujson() -> None:
    client = RecordingTailscaleClient()
    client.response_text = '{\n  // comment\n  "acls": []\n}'

    result = client.get_policy_file()

    call = client.api_calls[0]
    assert call["url"] == "https://api.tailscale.com/api/v2/tailnet/-/acl"
    assert call["headers"]["Accept"] == "application/hujson"
    assert result == {"raw": '{\n  // comment\n  "acls": []\n}'}


def test_list_dns_nameservers_path() -> None:
    client = RecordingTailscaleClient()
    client.response = {"dns": ["100.100.100.100"], "magicDNS": True}

    result = client.list_dns_nameservers()

    assert client.api_calls[0]["url"] == "https://api.tailscale.com/api/v2/tailnet/-/dns/nameservers"
    assert result == {"dns": ["100.100.100.100"], "magicDNS": True}


def test_audit_logs_uses_audit_client_and_returns_logs_list() -> None:
    client = RecordingTailscaleClient()
    client.response = {"logs": [{"action": "ADD", "eventTime": "2026-09-26T01:00:00Z"}]}

    logs = client.audit_logs("2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")

    assert logs == [{"action": "ADD", "eventTime": "2026-09-26T01:00:00Z"}]
    mint = client.token_mints[0]
    assert mint["auth"] == ("audit-id", "audit-secret")
    call = client.api_calls[0]
    assert call["url"] == "https://api.tailscale.com/api/v2/tailnet/-/logging/configuration"
    assert call["params"] == {"start": "2026-09-26T00:00:00Z", "end": "2026-09-27T00:00:00Z"}


def test_audit_logs_end_defaults_to_now() -> None:
    client = RecordingTailscaleClient()
    client.response = {"logs": []}

    client.audit_logs("2026-09-26T00:00:00Z")

    params = client.api_calls[0]["params"]
    assert params["start"] == "2026-09-26T00:00:00Z"
    assert params["end"].endswith("Z")
    assert "T" in params["end"]


def test_read_and_audit_tokens_cached_separately() -> None:
    client = RecordingTailscaleClient()
    client.response = {"devices": [], "logs": []}

    client.list_devices()
    client.audit_logs("2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")
    client.list_devices()
    client.audit_logs("2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")

    assert len(client.token_mints) == 2
    auths = {m["auth"] for m in client.token_mints}
    assert auths == {("read-id", "read-secret"), ("audit-id", "audit-secret")}
