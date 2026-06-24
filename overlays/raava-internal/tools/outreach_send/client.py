"""outreach_send — the system's sole email transport.

This is the ONLY place in Centaur that transmits email. It carries no queue
logic, no autonomy, no discovery — it sends exactly one message it is handed,
on the GTM operator's explicit "go."

Mirrors the lazy-init pattern of tools/business/attio.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx

from centaur_sdk import secret

_BASE_URL = "https://api.agentmail.to"
_SEND_PATH = "/v0/inboxes/{inbox}/messages"
_DEFAULT_INBOX = "outreach"  # fallback inbox slug if from_inbox not supplied


class OutreachSendClient:
    """Authenticated AgentMail send client.

    Lazy-initialised: the httpx.Client is built on the first call so that
    iron-proxy secret injection has time to populate AGENTMAIL_API_KEY before
    the client is constructed.
    """

    def __init__(self) -> None:
        self._http_client: httpx.Client | None = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_client(self) -> httpx.Client:
        api_key = secret("AGENTMAIL_API_KEY", "")
        return httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _client(self) -> httpx.Client:
        """Return (or lazily create) the cached HTTP client."""
        if self._http_client is None:
            self._http_client = self._build_client()
        return self._http_client

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        from_inbox: str | None = None,
        reply_to: str | None = None,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send a single email via AgentMail.

        Never raises — all failure modes return a structured dict.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.
            from_inbox: AgentMail inbox slug to send from (defaults to
                the value of the AGENTMAIL_DEFAULT_INBOX env var or "outreach").
            reply_to: Optional reply-to address.
            cc: Optional list of CC addresses.

        Returns:
            On 2xx:          {"sent": True, "id": "<message_id>"}
            On missing key:  {"sent": False, "reason": "no AGENTMAIL_API_KEY"}
            On non-2xx:      {"sent": False, "status": <code>, "reason": <body>}
            On network err:  {"sent": False, "reason": "<description>"}
            On decode err:   {"sent": False, "reason": "non-JSON response from AgentMail"}
        """
        # Guard: degrade gracefully when the key has not been injected yet.
        api_key = secret("AGENTMAIL_API_KEY", "")
        if not api_key:
            return {"sent": False, "reason": "no AGENTMAIL_API_KEY"}

        inbox = from_inbox or _DEFAULT_INBOX
        path = _SEND_PATH.format(inbox=inbox)

        payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
        if reply_to:
            payload["reply_to"] = reply_to
        if cc:
            payload["cc"] = cc

        try:
            response = self._client().post(path, json=payload)
        except httpx.RequestError as exc:
            return {"sent": False, "reason": str(exc)}

        if response.status_code >= 400:
            try:
                detail = response.json()
                reason = detail.get("message") or detail.get("error") or response.text
            except (ValueError, httpx.DecodingError):
                reason = response.text
            return {"sent": False, "status": response.status_code, "reason": reason}

        # 2xx — parse the message id safely.
        try:
            data = response.json()
            msg_id = data.get("id") if isinstance(data, dict) else None
        except (ValueError, httpx.DecodingError):
            return {"sent": False, "reason": "non-JSON response from AgentMail"}

        return {"sent": True, "id": msg_id}


# ---------------------------------------------------------------------------
# Module-level factory (used by Centaur tool runner)
# ---------------------------------------------------------------------------

def _client() -> OutreachSendClient:
    """Factory for tool SDK integration."""
    return OutreachSendClient()
