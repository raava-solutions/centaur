"""Honcho peer-memory client (Honcho v3 API)."""

from __future__ import annotations

import re
from typing import Any

import httpx

from centaur_sdk import secret

# Honcho resource ids must match ^[a-zA-Z0-9_-]+$ (max 512 chars).
_ID_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
_ID_MAX_LENGTH = 512


def sanitize_id(raw: str) -> str:
    """Map a free-form identifier onto Honcho's id alphabet.

    Centaur thread keys look like ``slack:C0TEST12345:1791670752.873056`` and
    Slack user ids are conventionally prefixed ``slack:U0123ABC``; neither
    colons nor dots are legal Honcho id characters, so they become ``-``.
    """
    cleaned = _ID_INVALID_CHARS.sub("-", raw.strip()).strip("-")
    if not cleaned:
        raise RuntimeError("id cannot be empty after sanitization.")
    return cleaned[:_ID_MAX_LENGTH]


def slack_peer_id(slack_user_id: str) -> str:
    """Canonical Honcho peer id for a Slack user: ``slack-<U...>``."""
    return sanitize_id(f"slack:{slack_user_id}")


def agent_peer_id(persona: str) -> str:
    """Canonical Honcho peer id for a centaur persona: ``agent-<persona>``."""
    return sanitize_id(f"agent:{persona}")


def session_id_for_thread(thread_key: str) -> str:
    """Canonical Honcho session id for a centaur thread key."""
    return sanitize_id(thread_key)


class HonchoClient:
    """Narrow Honcho v3 wrapper for peers, sessions, messages, and metamemory."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        workspace: str | None = None,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (
            base_url or secret("HONCHO_BASE_URL", "http://localhost:8000")
        ).rstrip("/")
        self.workspace = sanitize_id(workspace or secret("HONCHO_WORKSPACE", "centaur"))
        # Auth is optional in Honcho (AUTH_USE_AUTH). When disabled the header
        # is ignored; iron-proxy swaps this stub for the vault value on hosts
        # declared in pyproject.toml, so enabling auth later is config-only.
        self._api_key = api_key if api_key is not None else secret(
            "HONCHO_API_KEY", "HONCHO_API_KEY"
        )
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
        params: dict[str, Any] | None = None,
        json_payload: Any = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        timeout = timeout_seconds or self.timeout
        client = self._http
        owns_client = client is None
        if owns_client:
            client = httpx.Client(base_url=self.base_url, timeout=timeout)
        try:
            try:
                response = client.request(
                    method,
                    endpoint,
                    params=params,
                    headers=self._headers(),
                    json=json_payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                body = exc.response.text
                if status_code in {401, 403}:
                    raise RuntimeError(
                        "Honcho authentication failed. Check HONCHO_API_KEY "
                        "in the secret source."
                    ) from exc
                raise RuntimeError(f"Honcho request failed ({status_code}): {body}") from exc
            except httpx.RequestError as exc:
                raise RuntimeError(f"Honcho request failed: {exc}") from exc
        finally:
            if owns_client:
                client.close()
        if not response.content:
            return {}
        return response.json()

    def _ws(self) -> str:
        return f"/v3/workspaces/{self.workspace}"

    # -- workspace -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Liveness check (not workspace-scoped)."""
        return self._request("GET", "/health")

    def ensure_workspace(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create the configured workspace if missing (idempotent)."""
        payload: dict[str, Any] = {"id": self.workspace}
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", "/v3/workspaces", json_payload=payload)

    # -- peers ---------------------------------------------------------------

    def peer_create(
        self, peer_id: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a peer (idempotent: an existing id returns the peer)."""
        payload: dict[str, Any] = {"id": sanitize_id(peer_id)}
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", f"{self._ws()}/peers", json_payload=payload)

    def peer_get(self, peer_id: str) -> dict[str, Any] | None:
        """Fetch a peer by exact id, or None when it does not exist."""
        data = self._request(
            "POST",
            f"{self._ws()}/peers/list",
            json_payload={"filters": {"id": sanitize_id(peer_id)}},
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return items[0] if items else None

    # -- sessions --------------------------------------------------------------

    def session_create(
        self,
        session_id: str,
        peers: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a session, optionally attaching peers (idempotent per id)."""
        payload: dict[str, Any] = {"id": sanitize_id(session_id)}
        if peers:
            payload["peers"] = {sanitize_id(p): {} for p in peers}
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", f"{self._ws()}/sessions", json_payload=payload)

    def session_get(self, session_id: str) -> dict[str, Any] | None:
        """Fetch a session by exact id, or None when it does not exist."""
        data = self._request(
            "POST",
            f"{self._ws()}/sessions/list",
            json_payload={"filters": {"id": sanitize_id(session_id)}},
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return items[0] if items else None

    def session_add_peers(self, session_id: str, peers: list[str]) -> Any:
        """Add peers to an existing session."""
        payload = {sanitize_id(p): {} for p in peers}
        return self._request(
            "POST",
            f"{self._ws()}/sessions/{sanitize_id(session_id)}/peers",
            json_payload=payload,
        )

    # -- messages --------------------------------------------------------------

    def messages_add(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Append messages to a session.

        Each message is ``{"peer_id": ..., "content": ...}`` with optional
        ``metadata``; ids are sanitized before sending. Max 100 per batch.
        """
        if not messages:
            raise RuntimeError("messages cannot be empty.")
        normalized = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if not content:
                raise RuntimeError("message content cannot be empty.")
            entry: dict[str, Any] = {
                "peer_id": sanitize_id(str(message.get("peer_id", ""))),
                "content": content,
            }
            if message.get("metadata"):
                entry["metadata"] = message["metadata"]
            normalized.append(entry)
        return self._request(
            "POST",
            f"{self._ws()}/sessions/{sanitize_id(session_id)}/messages",
            json_payload={"messages": normalized},
        )

    def messages_list(self, session_id: str) -> dict[str, Any]:
        """List a session's messages (newest page first)."""
        return self._request(
            "POST",
            f"{self._ws()}/sessions/{sanitize_id(session_id)}/messages/list",
            json_payload={},
        )

    # -- metamemory ------------------------------------------------------------

    def peer_context(
        self,
        peer_id: str,
        target: str | None = None,
        search_query: str | None = None,
    ) -> dict[str, Any]:
        """Peer-level context: representation + peer card (optionally of a target)."""
        params: dict[str, Any] = {}
        if target:
            params["target"] = sanitize_id(target)
        if search_query:
            params["search_query"] = search_query
        return self._request(
            "GET",
            f"{self._ws()}/peers/{sanitize_id(peer_id)}/context",
            params=params,
        )

    def peer_representation(
        self, peer_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """The peer's derived representation (what Honcho believes about them)."""
        payload: dict[str, Any] = {}
        if session_id:
            payload["session_id"] = sanitize_id(session_id)
        return self._request(
            "POST",
            f"{self._ws()}/peers/{sanitize_id(peer_id)}/representation",
            json_payload=payload,
        )

    def peer_chat(
        self,
        peer_id: str,
        query: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Dialectic query: ask Honcho a natural-language question about a peer."""
        normalized_query = query.strip()
        if not normalized_query:
            raise RuntimeError("query cannot be empty.")
        payload: dict[str, Any] = {"query": normalized_query}
        if session_id:
            payload["session_id"] = sanitize_id(session_id)
        return self._request(
            "POST",
            f"{self._ws()}/peers/{sanitize_id(peer_id)}/chat",
            json_payload=payload,
            timeout_seconds=180.0,
        )

    def session_context(self, session_id: str, tokens: int | None = None) -> dict[str, Any]:
        """Session-level context (messages, summary, representations)."""
        params: dict[str, Any] = {}
        if tokens:
            params["tokens"] = tokens
        return self._request(
            "GET",
            f"{self._ws()}/sessions/{sanitize_id(session_id)}/context",
            params=params,
        )

    def queue_status(self) -> dict[str, Any]:
        """Deriver queue depth for the workspace (pending metamemory work)."""
        return self._request("GET", f"{self._ws()}/queue/status")

    def capability(self) -> dict[str, Any]:
        """Return non-secret Honcho bridge configuration."""
        return {
            "base_url": self.base_url,
            "workspace": self.workspace,
            "auth_header_sent": bool(self._api_key),
            "secret": "HONCHO_API_KEY",
        }


def _client() -> HonchoClient:
    """Factory for tool loader."""
    return HonchoClient()
