"""Tailscale API client — devices, ACL policy, DNS, and audit logs.

Uses OAuth client-credentials: short-lived access tokens are minted from
``https://api.tailscale.com/api/v2/oauth/token`` and cached per credential pair
until 60s before expiry. Two credential pairs are supported: the read pair
(devices, ACL, DNS) and a separate audit pair (audit logs). The tailnet is
always ``-`` (the default tailnet for the OAuth client's tailnet).
"""

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from centaur_sdk import secret

TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"
API_BASE = "https://api.tailscale.com/api/v2"

_DEVICE_FIELDS = ("id", "name", "hostname", "user", "os", "addresses", "lastSeen", "authorized")


class TailscaleClient:
    """Client for the Tailscale API (read-only: no write methods are exposed).

    Credentials come from ``CENTAUR_TAILSCALE_CLIENT_ID`` /
    ``CENTAUR_TAILSCALE_CLIENT_SECRET`` (read scope) and
    ``CENTAUR_TAILSCALE_AUDIT_CLIENT_ID`` / ``CENTAUR_TAILSCALE_AUDIT_CLIENT_SECRET``
    (audit-log scope). Each pair mints and caches its own access token.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        audit_client_id: str | None = None,
        audit_client_secret: str | None = None,
        timeout: float = 30.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._audit_client_id = audit_client_id
        self._audit_client_secret = audit_client_secret
        self.timeout = timeout
        self._client: httpx.Client | None = None
        # kind -> (access_token, reuse-until epoch seconds)
        self._tokens: dict[str, tuple[str, float]] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def _http(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, url, **kwargs)

    def _credentials(self, kind: str) -> tuple[str, str]:
        if kind == "audit":
            return (
                self._audit_client_id or secret("CENTAUR_TAILSCALE_AUDIT_CLIENT_ID"),
                self._audit_client_secret or secret("CENTAUR_TAILSCALE_AUDIT_CLIENT_SECRET"),
            )
        return (
            self._client_id or secret("CENTAUR_TAILSCALE_CLIENT_ID"),
            self._client_secret or secret("CENTAUR_TAILSCALE_CLIENT_SECRET"),
        )

    def _token(self, kind: str = "read") -> str:
        cached = self._tokens.get(kind)
        if cached and time.time() < cached[1]:
            return cached[0]
        client_id, client_secret = self._credentials(kind)
        resp = self._http(
            "POST",
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Tailscale API error ({resp.status_code}): {resp.text}")
        payload = resp.json()
        token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        self._tokens[kind] = (token, time.time() + max(expires_in - 60, 0))
        return token

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        kind: str = "read",
        accept: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token(kind)}"}
        if accept:
            headers["Accept"] = accept
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self._http(method, f"{API_BASE}{path}", params=clean, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Tailscale API error ({resp.status_code}): {resp.text}")
        return resp

    # -- Devices ---------------------------------------------------------------

    def list_devices(self) -> list:
        """List devices in the tailnet, trimmed to key fields.

        Returns entries with: id, name, hostname, user, os, addresses,
        lastSeen, authorized.
        """
        resp = self._request("GET", "/tailnet/-/devices")
        devices = resp.json().get("devices", [])
        return [
            {k: d.get(k) for k in _DEVICE_FIELDS}
            for d in devices
            if isinstance(d, dict)
        ]

    def get_device(self, device_id: str) -> dict:
        """Get full details for a single device.

        Args:
            device_id: Device id (see list_devices, field 'id').
        """
        resp = self._request("GET", f"/device/{device_id}")
        return resp.json()

    # -- Policy / DNS ------------------------------------------------------------

    def get_policy_file(self) -> dict:
        """Get the tailnet ACL policy file as raw HuJSON text.

        Returns {'raw': <policy file text>}. HuJSON preserves comments, so the
        text is returned unparsed.
        """
        resp = self._request("GET", "/tailnet/-/acl", accept="application/hujson")
        return {"raw": resp.text}

    def list_dns_nameservers(self) -> dict:
        """Get the tailnet's global DNS nameservers and MagicDNS settings."""
        resp = self._request("GET", "/tailnet/-/dns/nameservers")
        return resp.json()

    # -- Audit logs ------------------------------------------------------------

    def audit_logs(self, start: str, end: str | None = None) -> list:
        """Get tailnet audit logs for a time window (uses the audit OAuth client).

        Args:
            start: Window start, RFC3339, e.g. '2026-09-26T00:00:00Z'.
            end: Window end, RFC3339. Defaults to now (UTC).
        """
        if end is None:
            end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = self._request(
            "GET",
            "/tailnet/-/logging/configuration",
            params={"start": start, "end": end},
            kind="audit",
        )
        return resp.json().get("logs", [])

    # -- Lifecycle ------------------------------------------------------------

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> TailscaleClient:
    return TailscaleClient()
