"""Tests for outreach_send.client.

All HTTP calls are mocked via httpx.MockTransport — no live network traffic.

Import note: conftest.py at overlays/raava-internal/conftest.py inserts both
the repo root (for centaur_sdk) and the overlay tools dir on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

# Fallback path insertion for direct execution
_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from outreach_send.client import OutreachSendClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client_with_handler(handler, api_key: str = "test-key") -> OutreachSendClient:
    """Build an OutreachSendClient whose HTTP layer is driven by handler."""
    c = OutreachSendClient()
    # Inject a pre-built httpx.Client using the mock transport
    c._http_client = httpx.Client(
        base_url="https://api.agentmail.to",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )
    return c


# ---------------------------------------------------------------------------
# Happy path — 2xx with JSON id
# ---------------------------------------------------------------------------

def test_send_happy_path():
    """2xx response with JSON body → {sent: True, id: ...}."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "outreach" in request.url.path or "inboxes" in request.url.path
        assert "Bearer test-key" in request.headers.get("Authorization", "")
        import json
        body = json.loads(request.content)
        assert body["to"] == "prospect@example.com"
        assert body["subject"] == "Hello"
        assert body["body"] == "Nice to meet you."
        return httpx.Response(200, json={"id": "msg-abc123", "status": "queued"}, request=request)

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _make_client_with_handler(handler)
        result = client.send(
            to="prospect@example.com",
            subject="Hello",
            body="Nice to meet you.",
        )

    assert result == {"sent": True, "id": "msg-abc123"}


def test_send_includes_optional_fields():
    """Optional reply_to and cc are forwarded in the POST body."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body.get("reply_to") == "reply@example.com"
        assert body.get("cc") == ["cc@example.com"]
        return httpx.Response(201, json={"id": "msg-xyz"}, request=request)

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _make_client_with_handler(handler)
        result = client.send(
            to="prospect@example.com",
            subject="Hi",
            body="Body text",
            reply_to="reply@example.com",
            cc=["cc@example.com"],
        )

    assert result["sent"] is True
    assert result["id"] == "msg-xyz"


# ---------------------------------------------------------------------------
# Missing key — degrade gracefully, never raise
# ---------------------------------------------------------------------------

def test_send_missing_key_degrades():
    """When AGENTMAIL_API_KEY is absent, send() returns {sent: False} — no crash."""
    with patch("outreach_send.client.secret", return_value=""):
        client = OutreachSendClient()
        result = client.send(to="x@example.com", subject="s", body="b")

    assert result == {"sent": False, "reason": "no AGENTMAIL_API_KEY"}


# ---------------------------------------------------------------------------
# Non-2xx — structured error, never raise
# ---------------------------------------------------------------------------

def test_send_non_2xx_returns_structured_error():
    """4xx/5xx surfaces as {sent: False, status, reason} — never crashes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"message": "Recipient is suppressed"},
            request=request,
        )

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _make_client_with_handler(handler)
        result = client.send(to="x@example.com", subject="s", body="b")

    assert result["sent"] is False
    assert result["status"] == 422
    assert "suppressed" in result["reason"].lower()


def test_send_500_returns_structured_error():
    """500 surfaces as a structured error without raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error", request=request)

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _make_client_with_handler(handler)
        result = client.send(to="x@example.com", subject="s", body="b")

    assert result["sent"] is False
    assert result["status"] == 500


# ---------------------------------------------------------------------------
# Network error — structured error, never raise
# ---------------------------------------------------------------------------

def test_send_network_error_returns_structured_error():
    """A network-level error surfaces as {sent: False, reason} — never crashes."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _make_client_with_handler(handler)
        result = client.send(to="x@example.com", subject="s", body="b")

    assert result["sent"] is False
    assert "Connection refused" in result["reason"]


# ---------------------------------------------------------------------------
# JSON decode error on 2xx — structured error, never raise
# ---------------------------------------------------------------------------

def test_send_non_json_2xx_returns_structured_error():
    """If AgentMail returns 2xx with non-JSON body, contract holds — no crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy intercept</html>", request=request)

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _make_client_with_handler(handler)
        result = client.send(to="x@example.com", subject="s", body="b")

    assert result["sent"] is False
    assert "non-JSON" in result["reason"]
