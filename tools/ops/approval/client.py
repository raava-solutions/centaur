"""Centaur approval API client.

Talks to api-rs directly at ``CENTAUR_API_URL`` (plain HTTP, in-cluster).
The sandbox's egress policy allows this one destination, and the per-sandbox
proxy is bypassed: it rejects plain-HTTP forwards, so the client is built
with ``trust_env=False`` on purpose. Authentication is the approval-only
``CENTAUR_APPROVAL_API_KEY`` bearer token passed through from the control
plane; it can create and read approval requests but never decide them.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import httpx

from centaur_sdk import secret

DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

TERMINAL_STATUSES = {"approved", "denied", "expired"}


def action_hash_for(command: str) -> str:
    """Stable hash of the exact action string an approval decision binds to.

    Any byte-level change to the action produces a different hash, which a
    new approval request must be created for — approvals are never portable
    across actions.
    """
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


class ApprovalClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        thread_key: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or secret("CENTAUR_API_URL", "")).rstrip("/")
        if not self.base_url:
            raise RuntimeError("CENTAUR_API_URL is not set; not running in a centaur sandbox.")
        self._api_key = api_key if api_key is not None else secret("CENTAUR_APPROVAL_API_KEY", "")
        if not self._api_key:
            raise RuntimeError(
                "CENTAUR_APPROVAL_API_KEY is not set; this sandbox has no approval credential."
            )
        self._thread_key = (
            thread_key if thread_key is not None else secret("CENTAUR_THREAD_KEY", "")
        ).strip()
        if not self._thread_key:
            raise RuntimeError("CENTAUR_THREAD_KEY is not set; approvals need the current thread.")
        self.timeout = timeout
        self._http = http_client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Any = None,
    ) -> Any:
        client = self._http
        owns_client = client is None
        if owns_client:
            # trust_env=False: never route through the sandbox's egress proxy;
            # api-rs is plain HTTP and the proxy rejects such forwards.
            client = httpx.Client(
                base_url=self.base_url, timeout=self.timeout, trust_env=False
            )
        try:
            try:
                response = client.request(
                    method, endpoint, headers=self._headers(), json=json_payload
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                body = exc.response.text
                if status_code in {401, 403}:
                    raise RuntimeError(
                        "approval API rejected the sandbox credential "
                        f"({status_code}): {body}"
                    ) from exc
                raise RuntimeError(f"approval API request failed ({status_code}): {body}") from exc
            except httpx.RequestError as exc:
                raise RuntimeError(f"approval API request failed: {exc}") from exc
        finally:
            if owns_client:
                client.close()
        if not response.content:
            return {}
        return response.json()

    def request(
        self,
        summary: str,
        action_hash: str,
        *,
        execution_id: str | None = None,
        requester: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Create an approval request; returns the durable record."""
        payload: dict[str, Any] = {
            "thread_key": self._thread_key,
            "action_summary": summary,
            "action_hash": action_hash,
        }
        if execution_id:
            payload["execution_id"] = execution_id
        if requester:
            payload["requester"] = requester
        if ttl_seconds:
            payload["ttl_seconds"] = ttl_seconds
        data = self._request("POST", "/api/approvals", json_payload=payload)
        approval = data.get("approval") if isinstance(data, dict) else None
        if not isinstance(approval, dict) or not approval.get("id"):
            raise RuntimeError(f"approval API returned an invalid record: {data!r}")
        return approval

    def get(self, approval_id: str) -> dict[str, Any]:
        """Fetch the current state of an approval request."""
        data = self._request("GET", f"/api/approvals/{approval_id}")
        approval = data.get("approval") if isinstance(data, dict) else None
        if not isinstance(approval, dict):
            raise RuntimeError(f"approval API returned an invalid record: {data!r}")
        return approval

    def request_and_wait(
        self,
        summary: str,
        action_hash: str,
        *,
        execution_id: str | None = None,
        requester: str | None = None,
        ttl_seconds: int | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Create the request, then poll until decided, expired, or timed out.

        Returns the final record. ``status`` is ``approved``, ``denied``, or
        ``expired``; a local poll timeout returns the last seen record with
        ``timed_out: true`` (the request itself still expires at its TTL).
        """
        approval = self.request(
            summary,
            action_hash,
            execution_id=execution_id,
            requester=requester,
            ttl_seconds=ttl_seconds,
        )
        deadline = time.monotonic() + (
            timeout_seconds
            if timeout_seconds is not None
            else float(ttl_seconds or DEFAULT_TTL_SECONDS)
        )
        while time.monotonic() < deadline:
            current = self.get(approval["id"])
            if current.get("status") in TERMINAL_STATUSES:
                return current
            time.sleep(poll_interval_seconds)
        return {**approval, "timed_out": True}
