"""Supermemory working-memory client for the Raava outreach operator.

Provides remember/recall over the Supermemory REST API.  Degrades gracefully
when the API key is absent — remember returns {stored: False} and recall returns
{results: []} with a reason string; no exception is raised.

Pattern mirrors tools/business/attio: lazy httpx.Client, centaur_sdk secret().
"""

from __future__ import annotations

from typing import Any

import httpx

from centaur_sdk import secret

_BASE_URL = "https://api.supermemory.ai"
_SENTINEL = object()  # sentinel for "no api_key arg passed"

# REST endpoints (isolated as constants so they're trivially correctable)
_REMEMBER_PATH = "/v3/memories"
_RECALL_PATH = "/v3/search"


class SupermemoryClient:
    """Lazy-init Supermemory client with graceful key-missing degradation.

    Key resolution:
    1. If api_key is supplied explicitly (non-None), use it as-is (even "").
       An empty string means "no key" → degrade mode.
    2. Otherwise resolve via centaur_sdk.secret() (iron-proxy / stub).
    """

    def __init__(self, api_key: Any = _SENTINEL) -> None:
        # Store None sentinel if no arg given, otherwise store the explicit value.
        self._explicit_key: str | None = None if api_key is _SENTINEL else api_key
        self._client: httpx.Client | None = None

    def _resolve_key(self) -> str:
        """Resolve the API key; returns "" when unavailable."""
        if self._explicit_key is not None:
            # Explicit override (may be "")
            return self._explicit_key
        # Iron-proxy / stub resolution — StubBackend echoes the key name,
        # treat any value that looks like a secret placeholder as "no key"
        val = secret("SUPERMEMORY_API_KEY", "")
        # If we're in stub/local mode, the stub returns the key name itself.
        # Treat the literal key name as "no key available".
        if not val or val == "SUPERMEMORY_API_KEY":
            return ""
        return val

    def _http(self) -> httpx.Client | None:
        """Return a cached HTTP client or None if the key is missing."""
        if self._client is not None:
            return self._client
        api_key = self._resolve_key()
        if not api_key:
            return None
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self._client

    def remember(
        self,
        text: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store a memory in Supermemory.

        Args:
            text: The text content to remember.
            tags: Optional list of tag strings for categorisation.

        Returns:
            API response dict on success, or {"stored": False, "reason": "no key"}
            when the API key is not configured.
        """
        http = self._http()
        if http is None:
            return {"stored": False, "reason": "no key"}

        body: dict[str, Any] = {"content": text}
        if tags:
            body["metadata"] = {"tags": tags}

        response = http.post(_REMEMBER_PATH, json=body)
        response.raise_for_status()
        data = response.json()
        data.setdefault("stored", True)
        return data

    def recall(
        self,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search stored memories.

        Args:
            query: The search query string.
            limit: Maximum number of results to return (default 5).

        Returns:
            Dict with "results" key on success, or {"results": [], "reason": "no key"}
            when the API key is not configured.
        """
        http = self._http()
        if http is None:
            return {"results": [], "reason": "no key"}

        body: dict[str, Any] = {"q": query, "limit": limit}
        response = http.post(_RECALL_PATH, json=body)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None


def _client() -> SupermemoryClient:
    """Factory for tool SDK integration."""
    return SupermemoryClient()
