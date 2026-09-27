"""outreach_send -- the system's sole email transport and send gate.

This is the ONLY place in Centaur that transmits email. Sending is a
two-call protocol enforced in code:

1. stage(...) records the exact message payload and returns a short-lived token.
2. send(confirm_token, requester_id=...) validates approver, token, cap, and
   live-send arming before it calls AgentMail.

The prompt may guide the operator flow, but this module is the structural gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from centaur_sdk import secret

_BASE_URL = "https://api.agentmail.to"
_SEND_PATH = "/v0/inboxes/{inbox}/messages"
_DEFAULT_INBOX = "outreach"
_DEFAULT_TOKEN_TTL_SECONDS = 5 * 60
_DEFAULT_MAX_SENDS_PER_DAY = 5
_STATE_VERSION = 1


def _default_state_path() -> Path:
    return Path(
        os.environ.get(
            "OUTREACH_SEND_STATE_PATH",
            str(Path(tempfile.gettempdir()) / "centaur-outreach-send-state.json"),
        )
    )


def _canonical_cc(cc: list[str] | None) -> list[str]:
    return list(cc or [])


def _payload_bytes(
    *,
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
) -> bytes:
    """Canonical bytes bound to a confirm token.

    The hash deliberately covers only the exact approval surface requested by
    U7: to, subject, body, and cc.
    """
    return json.dumps(
        {
            "to": to,
            "subject": subject,
            "body": body,
            "cc": _canonical_cc(cc),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_hash(
    *,
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
) -> str:
    return hashlib.sha256(
        _payload_bytes(to=to, subject=subject, body=body, cc=cc)
    ).hexdigest()


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class OutreachSendClient:
    """Authenticated AgentMail client guarded by a staged approval protocol.

    ``requester_id`` is the explicit unit-testable interface for the approving
    turn context. The Slack/Centaur turn plumbing can pass the human Slack
    ``user_id`` here without changing this gate.
    """

    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        token_ttl_seconds: int = _DEFAULT_TOKEN_TTL_SECONDS,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._http_client: httpx.Client | None = None
        self._state_path = Path(state_path) if state_path is not None else _default_state_path()
        self._token_ttl_seconds = token_ttl_seconds
        self._now = now_fn or time.time

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #

    def _empty_state(self) -> dict[str, Any]:
        return {"version": _STATE_VERSION, "tokens": {}, "counts": {}}

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return self._empty_state()
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(data, dict):
            return self._empty_state()
        data.setdefault("version", _STATE_VERSION)
        data.setdefault("tokens", {})
        data.setdefault("counts", {})
        if not isinstance(data["tokens"], dict) or not isinstance(data["counts"], dict):
            return self._empty_state()
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self._state_path)

    def _prune_expired(self, state: dict[str, Any], now: float) -> None:
        tokens = state.get("tokens", {})
        if not isinstance(tokens, dict):
            state["tokens"] = {}
            return
        for token, record in list(tokens.items()):
            if not isinstance(record, dict):
                tokens.pop(token, None)
                continue
            expires_at = float(record.get("expires_at", 0))
            consumed = bool(record.get("consumed", False))
            if expires_at < now and consumed:
                tokens.pop(token, None)

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #

    def _env(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    def _approver_ids(self) -> set[str]:
        raw = self._env("OUTREACH_APPROVER_USER_IDS", "")
        return {part.strip() for part in raw.split(",") if part.strip()}

    def _max_sends_per_day(self) -> int:
        raw = self._env("MAX_SENDS_PER_DAY", str(_DEFAULT_MAX_SENDS_PER_DAY))
        try:
            value = int(raw)
        except ValueError:
            return _DEFAULT_MAX_SENDS_PER_DAY
        return max(value, 0)

    def _live_send_armed(self) -> bool:
        return _truthy(self._env("OUTREACH_LIVE_SEND_ENABLED")) and bool(
            self._env("CAN_SPAM_ADDRESS", "").strip()
        )

    # ------------------------------------------------------------------ #
    # AgentMail helpers
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
        if self._http_client is None:
            self._http_client = self._build_client()
        return self._http_client

    def _transmit(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_inbox: str | None = None,
        reply_to: str | None = None,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send a single email via AgentMail.

        Never raises. This preserves the previous degrade-safe transport
        contract, but is only reachable after the structural gate passes.
        """
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

        try:
            data = response.json()
            msg_id = data.get("id") if isinstance(data, dict) else None
        except (ValueError, httpx.DecodingError):
            return {"sent": False, "reason": "non-JSON response from AgentMail"}

        return {"sent": True, "id": msg_id}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def stage(
        self,
        entry_id: str,
        to: str,
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a short-lived single-use confirm token for exact payload bytes."""
        now = self._now()
        token = secrets.token_urlsafe(18)
        payload_hash = _payload_hash(to=to, subject=subject, body=body, cc=cc)

        state = self._load_state()
        self._prune_expired(state, now)
        state["tokens"][token] = {
            "entry_id": entry_id,
            "to": to,
            "subject": subject,
            "body": body,
            "cc": _canonical_cc(cc),
            "payload_hash": payload_hash,
            "created_at": now,
            "expires_at": now + self._token_ttl_seconds,
            "consumed": False,
        }
        self._save_state(state)
        return {
            "staged": True,
            "confirm_token": token,
            "expires_at": state["tokens"][token]["expires_at"],
            "payload_hash": payload_hash,
        }

    def send(
        self,
        confirm_token: str,
        *,
        requester_id: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        cc: list[str] | None = None,
        from_inbox: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Transmit a staged message only after all send-safety gates pass.

        ``to``/``subject``/``body``/``cc`` are optional verification fields for
        callers that want to echo the exact message back into the final call.
        When provided, they must hash to the same payload bytes staged earlier.
        """
        now = self._now()
        approvers = self._approver_ids()
        if not requester_id:
            return self._refusal("missing_requester_id")
        if requester_id not in approvers:
            return self._refusal("requester_not_approver", requester_id=requester_id)
        if not self._live_send_armed():
            return self._refusal("live_send_not_armed")

        state = self._load_state()
        token_record = state["tokens"].get(confirm_token)
        if not isinstance(token_record, dict):
            return self._refusal("invalid_confirm_token")
        if bool(token_record.get("consumed", False)):
            return self._refusal("confirm_token_consumed")
        if float(token_record.get("expires_at", 0)) < now:
            return self._refusal("confirm_token_expired")

        staged_hash = str(token_record.get("payload_hash", ""))
        if any(value is not None for value in (to, subject, body, cc)):
            if to is None or subject is None or body is None:
                return self._refusal("payload_verification_incomplete")
            attempted_hash = _payload_hash(to=to, subject=subject, body=body, cc=cc)
            if attempted_hash != staged_hash:
                return self._refusal("payload_hash_mismatch")

        recipient = str(token_record["to"])
        cap_refusal = self._cap_refusal(state, recipient, now)
        if cap_refusal is not None:
            return cap_refusal

        result = self._transmit(
            to=recipient,
            subject=str(token_record["subject"]),
            body=str(token_record["body"]),
            from_inbox=from_inbox,
            reply_to=reply_to,
            cc=list(token_record.get("cc") or []),
        )
        if result.get("sent") is True:
            token_record["consumed"] = True
            token_record["consumed_at"] = now
            self._increment_counts(state, recipient, now)
            self._save_state(state)
        return result

    def _cap_refusal(self, state: dict[str, Any], recipient: str, now: float) -> dict[str, Any] | None:
        max_sends = self._max_sends_per_day()
        day_key = _utc_day(now)
        day_counts = state.setdefault("counts", {}).setdefault(
            day_key, {"total": 0, "recipients": {}}
        )
        if not isinstance(day_counts, dict):
            state["counts"][day_key] = day_counts = {"total": 0, "recipients": {}}
        recipients = day_counts.setdefault("recipients", {})
        if int(day_counts.get("total", 0)) >= max_sends:
            return self._refusal("daily_send_cap_reached", max_sends_per_day=max_sends)
        if int(recipients.get(recipient, 0)) >= 1:
            return self._refusal("recipient_already_sent_today", recipient=recipient)
        return None

    def _increment_counts(self, state: dict[str, Any], recipient: str, now: float) -> None:
        day_key = _utc_day(now)
        day_counts = state.setdefault("counts", {}).setdefault(
            day_key, {"total": 0, "recipients": {}}
        )
        recipients = day_counts.setdefault("recipients", {})
        day_counts["total"] = int(day_counts.get("total", 0)) + 1
        recipients[recipient] = int(recipients.get(recipient, 0)) + 1

    def _refusal(self, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "sent": False,
            "refused": True,
            "reason": reason,
            **extra,
        }


def _client() -> OutreachSendClient:
    """Factory for tool SDK integration."""
    return OutreachSendClient()
