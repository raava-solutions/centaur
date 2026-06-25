"""Raava outreach discovery surface over the worker HTTP bridge.

Discovery and curation ONLY. This tool cannot send email; sending is the
exclusive province of the outreach_send tool, used only by the GTM operator
on an explicit human "go."
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from centaur_sdk import secret

_DEFAULT_BASE_URL = "http://host.docker.internal:8770"


class RaavaOutreachClient:
    """Safe HTTP surface over the raava-outreach worker — discovery only.

    Exposes produce/queue/draft/triage/preflight/curation_audit/reject/mark_handled.
    approve() and send_approved() are intentionally absent: this tool cannot
    send email. Sending is performed exclusively by the outreach_send tool
    at the GTM operator's explicit request.
    """

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        """Return the cached HTTP client, building it after secrets are injected."""
        if self._client is not None:
            return self._client

        token = secret("RAAVA_OUTREACH_HTTP_TOKEN", "")
        self._client = httpx.Client(
            base_url=os.getenv("RAAVA_OUTREACH_BASE_URL", _DEFAULT_BASE_URL).rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        context: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http().request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"raava-outreach {context} transport error: {exc}") from exc

        if response.status_code >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {"error": response.text}
            message = error.get("error") or error.get("message") or response.text
            raise RuntimeError(
                f"raava-outreach {context} failed ({response.status_code}): {message}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"raava-outreach {context}: could not parse JSON response: {exc}\n"
                f"body={response.text!r}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"raava-outreach {context}: expected JSON object, got {data!r}")
        return data

    # ------------------------------------------------------------------ #
    # Produce
    # ------------------------------------------------------------------ #

    def produce(self, dry_run: bool = True) -> dict[str, Any]:
        """Run the produce step (generate outreach drafts).

        Args:
            dry_run: When True (default) runs with dry_run=true; set False for live.

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request("POST", "/produce", context="produce", json={"dry_run": dry_run})

    # ------------------------------------------------------------------ #
    # Queue
    # ------------------------------------------------------------------ #

    def queue(
        self,
        status: str | None = None,
        track: str | None = None,
    ) -> dict[str, Any]:
        """List the outreach queue.

        Args:
            status: Filter by status (e.g. "pending", "approved", "sent").
            track: Filter by track/campaign name.

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request(
            "POST",
            "/queue",
            context="queue",
            json={"status": status, "track": track},
        )

    # ------------------------------------------------------------------ #
    # Draft
    # ------------------------------------------------------------------ #

    def draft(self, entry_id: str) -> dict[str, Any]:
        """Fetch the fresh full draft for a queued outreach entry.

        Args:
            entry_id: The queue entry ID to fetch.

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request(
            "POST",
            "/draft",
            context=f"draft {entry_id}",
            json={"entry_id": str(entry_id)},
        )

    # ------------------------------------------------------------------ #
    # Triage
    # ------------------------------------------------------------------ #

    def triage(self) -> dict[str, Any]:
        """Run triage (re-score and sort the queue).

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request("POST", "/triage", context="triage", json={})

    # ------------------------------------------------------------------ #
    # Preflight
    # ------------------------------------------------------------------ #

    def preflight(self) -> dict[str, Any]:
        """Run preflight checks before sending.

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request("POST", "/preflight", context="preflight", json={})

    # ------------------------------------------------------------------ #
    # Curation audit
    # ------------------------------------------------------------------ #

    def curation_audit(self) -> dict[str, Any]:
        """Run a curation audit of the outreach queue.

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request("POST", "/curation-audit", context="curation-audit", json={})

    # ------------------------------------------------------------------ #
    # Reject
    # ------------------------------------------------------------------ #

    def reject(self, entry_id: str) -> dict[str, Any]:
        """Reject an outreach entry by ID.

        Args:
            entry_id: The queue entry ID to reject.

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request(
            "POST",
            "/reject",
            context=f"reject {entry_id}",
            json={"entry_id": str(entry_id)},
        )

    # ------------------------------------------------------------------ #
    # Mark handled
    # ------------------------------------------------------------------ #

    def mark_handled(self, entry_id: str, outcome: str) -> dict[str, Any]:
        """Record the terminal human outcome for an outreach entry.

        The bridge route enforces the allowed outcomes. This client exposes
        bookkeeping only; it does not approve or send.

        Args:
            entry_id: The queue entry ID to mark.
            outcome: Terminal outcome, usually "delivered" or "rejected".

        Returns:
            Parsed JSON dict from the worker discovery service.
        """
        return self._request(
            "POST",
            "/mark-handled",
            context=f"mark-handled {entry_id}",
            json={"entry_id": str(entry_id), "outcome": str(outcome)},
        )


def _client() -> RaavaOutreachClient:
    """Factory for tool SDK integration."""
    return RaavaOutreachClient()
