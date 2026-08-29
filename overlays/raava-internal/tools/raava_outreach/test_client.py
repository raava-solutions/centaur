"""Tests for the raava_outreach HTTP discovery client.

All HTTP calls use httpx MockTransport — no live network.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

# Fallback: ensure the overlay tools dir is on path even when running this
# test file directly (conftest.py does this under pytest, but direct execution
# needs it too).
_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from raava_outreach.client import RaavaOutreachClient  # noqa: E402


def _client(handler) -> RaavaOutreachClient:
    client = RaavaOutreachClient()
    client._client = httpx.Client(
        base_url="http://host.docker.internal:8770",
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
        transport=httpx.MockTransport(handler),
    )
    return client


def _request_json(request: httpx.Request) -> dict:
    request.read()
    return json.loads(request.content)


# ---------------------------------------------------------------------------
# Safety: approve() and send_approved() must NOT exist on the client
# ---------------------------------------------------------------------------


def test_approve_method_absent():
    """approve() must not be exposed — only the operator's outreach_send can send."""
    client = RaavaOutreachClient()
    assert not hasattr(client, "approve"), (
        "RaavaOutreachClient must NOT expose approve(); "
        "sending authority belongs to outreach_send only"
    )


def test_send_approved_method_absent():
    """send_approved() must not be exposed — only the operator's outreach_send can send."""
    client = RaavaOutreachClient()
    assert not hasattr(client, "send_approved"), (
        "RaavaOutreachClient must NOT expose send_approved(); "
        "sending authority belongs to outreach_send only"
    )


def test_send_method_absent():
    """send() must not be exposed on the worker bridge client."""
    client = RaavaOutreachClient()
    assert not hasattr(client, "send")


# ---------------------------------------------------------------------------
# Happy paths: exact discovery HTTP routes and shapes
# ---------------------------------------------------------------------------


def test_produce_posts_dry_run():
    payload = {"campaign": "q3-roofing", "drafts": 5}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/produce"
        assert _request_json(request) == {"dry_run": True}
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, request=request, json=payload)

    result = _client(handler).produce(dry_run=True)
    assert result == payload


def test_queue_posts_filters_and_returns_queue():
    payload = {"queue": [{"id": "abc", "status": "pending", "company": "Acme"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/queue"
        assert _request_json(request) == {"status": "pending", "track": "gtm"}
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).queue(status="pending", track="gtm") == payload


def test_queue_posts_null_filters_by_default():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/queue"
        assert _request_json(request) == {"status": None, "track": None}
        return httpx.Response(200, request=request, json={"queue": []})

    assert _client(handler).queue() == {"queue": []}


def test_draft_posts_entry_id_and_returns_full_draft():
    payload = {
        "entry_id": "q-123",
        "draft": {
            "to": "buyer@example.com",
            "subject": "Roofing crews",
            "body": "Fresh body",
            "cc": ["ops@example.com"],
        },
        "suppression": {"suppressed": False, "already_sent": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/draft"
        assert _request_json(request) == {"entry_id": "q-123"}
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).draft("q-123") == payload


def test_triage_posts_empty_body_and_returns_triage():
    payload = {"triage": [{"id": "abc", "score": 92}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/triage"
        assert _request_json(request) == {}
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).triage() == payload


def test_preflight_posts_empty_body_and_returns_checks():
    payload = {"checks": [{"name": "slack", "ok": True}], "ready": True}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/preflight"
        assert _request_json(request) == {}
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).preflight() == payload


def test_curation_audit_posts_empty_body_and_returns_audit():
    payload = {"count": 1, "drops": [{"entry_id": "drop-1"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/curation-audit"
        assert _request_json(request) == {}
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).curation_audit() == payload


def test_reject_posts_entry_id_and_returns_result():
    payload = {"ok": True, "result": {"entry_id": "abc", "status": "rejected"}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/reject"
        assert _request_json(request) == {"entry_id": "abc"}
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).reject("abc") == payload


def test_mark_handled_posts_entry_id_and_outcome():
    payload = {"ok": True, "entry_id": "abc", "outcome": "delivered", "status": "delivered"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/mark-handled"
        assert _request_json(request) == {"entry_id": "abc", "outcome": "delivered"}
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, request=request, json=payload)

    assert _client(handler).mark_handled("abc", "delivered") == payload


# ---------------------------------------------------------------------------
# Error handling: HTTP/transport/JSON failures surface as RuntimeError
# ---------------------------------------------------------------------------


def test_non_2xx_raises_runtime_error_with_structured_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, json={"error": "DB connection failed"})

    with pytest.raises(RuntimeError, match="DB connection failed"):
        _client(handler).queue()


def test_transport_error_raises_runtime_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RuntimeError, match="transport error"):
        _client(handler).produce()


def test_bad_json_raises_runtime_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text="this is not json")

    with pytest.raises(RuntimeError, match="JSON"):
        _client(handler).queue()
