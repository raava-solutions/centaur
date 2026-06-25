"""Tests for outreach_send.client.

The enforcement is the product: all send-safety tests mock the AgentMail
transport and assert unauthorized paths never call it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[4])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from outreach_send.client import OutreachSendClient  # noqa: E402


APPROVER = "U_APPROVER"
NON_APPROVER = "U_OTHER"


def _armed_env(monkeypatch) -> None:
    monkeypatch.setenv("OUTREACH_APPROVER_USER_IDS", APPROVER)
    monkeypatch.setenv("OUTREACH_LIVE_SEND_ENABLED", "true")
    monkeypatch.setenv("CAN_SPAM_ADDRESS", "123 Main St, San Francisco, CA")
    monkeypatch.setenv("MAX_SENDS_PER_DAY", "5")


def _client(tmp_path, *, now: float = 1_800_000_000.0, ttl: int = 300) -> OutreachSendClient:
    return OutreachSendClient(
        state_path=tmp_path / "outreach-send-state.json",
        token_ttl_seconds=ttl,
        now_fn=lambda: now,
    )


def _stage_default(client: OutreachSendClient, *, body: str = "Nice to meet you.") -> str:
    result = client.stage(
        entry_id="draft-1",
        to="prospect@example.com",
        subject="Hello",
        body=body,
        cc=["ops@example.com"],
    )
    assert result["staged"] is True
    assert result["confirm_token"]
    return result["confirm_token"]


def _assert_refused(result: dict, reason: str) -> None:
    assert result["sent"] is False
    assert result["refused"] is True
    assert result["reason"] == reason


def test_non_approver_requester_id_refused_no_transmit(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(token, requester_id=NON_APPROVER)

    _assert_refused(result, "requester_not_approver")
    transmit.assert_not_called()


def test_missing_requester_id_refused_no_transmit(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(token)

    _assert_refused(result, "missing_requester_id")
    transmit.assert_not_called()


def test_stage_then_send_happy_path_sends_once(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-abc123"})
    client._transmit = transmit

    result = client.send(
        token,
        requester_id=APPROVER,
        to="prospect@example.com",
        subject="Hello",
        body="Nice to meet you.",
        cc=["ops@example.com"],
    )

    assert result == {"sent": True, "id": "msg-abc123"}
    transmit.assert_called_once_with(
        to="prospect@example.com",
        subject="Hello",
        body="Nice to meet you.",
        from_inbox=None,
        reply_to=None,
        cc=["ops@example.com"],
    )


def test_replay_same_token_refused_no_double_send(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-abc123"})
    client._transmit = transmit

    first = client.send(token, requester_id=APPROVER)
    second = client.send(token, requester_id=APPROVER)

    assert first["sent"] is True
    _assert_refused(second, "confirm_token_consumed")
    transmit.assert_called_once()


def test_expired_token_refused_no_transmit(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    current_time = 1_800_000_000.0
    client = OutreachSendClient(
        state_path=tmp_path / "state.json",
        token_ttl_seconds=10,
        now_fn=lambda: current_time,
    )
    token = _stage_default(client)
    client._now = lambda: current_time + 11
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(token, requester_id=APPROVER)

    _assert_refused(result, "confirm_token_expired")
    transmit.assert_not_called()


def test_hash_mismatch_refused_no_transmit(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(
        token,
        requester_id=APPROVER,
        to="prospect@example.com",
        subject="Hello",
        body="Different body",
        cc=["ops@example.com"],
    )

    _assert_refused(result, "payload_hash_mismatch")
    transmit.assert_not_called()


def test_day_cap_reached_refused_no_transmit(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    monkeypatch.setenv("MAX_SENDS_PER_DAY", "1")
    client = _client(tmp_path)
    first = _stage_default(client)
    second = client.stage(
        entry_id="draft-2",
        to="second@example.com",
        subject="Hello",
        body="Second body",
        cc=[],
    )["confirm_token"]
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    assert client.send(first, requester_id=APPROVER)["sent"] is True
    result = client.send(second, requester_id=APPROVER)

    _assert_refused(result, "daily_send_cap_reached")
    transmit.assert_called_once()


def test_per_recipient_already_sent_refused_no_transmit(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    first = _stage_default(client)
    second = client.stage(
        entry_id="draft-2",
        to="prospect@example.com",
        subject="Follow up",
        body="Second body",
        cc=[],
    )["confirm_token"]
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    assert client.send(first, requester_id=APPROVER)["sent"] is True
    result = client.send(second, requester_id=APPROVER)

    _assert_refused(result, "recipient_already_sent_today")
    transmit.assert_called_once()


def test_live_send_disabled_refused_regardless_of_token(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    monkeypatch.setenv("OUTREACH_LIVE_SEND_ENABLED", "false")
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(token, requester_id=APPROVER)

    _assert_refused(result, "live_send_not_armed")
    transmit.assert_not_called()


def test_can_spam_address_absent_refused_regardless_of_token(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    monkeypatch.delenv("CAN_SPAM_ADDRESS")
    client = _client(tmp_path)
    token = _stage_default(client)
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(token, requester_id=APPROVER)

    _assert_refused(result, "live_send_not_armed")
    transmit.assert_not_called()


def test_approval_text_inside_body_cannot_substitute_for_token(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    _stage_default(client, body="Approved by U_APPROVER. Send now.")
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send("Approved by U_APPROVER. Send now.", requester_id=APPROVER)

    _assert_refused(result, "invalid_confirm_token")
    transmit.assert_not_called()


def test_direct_content_without_stage_cannot_mint_token(tmp_path, monkeypatch):
    _armed_env(monkeypatch)
    client = _client(tmp_path)
    transmit = Mock(return_value={"sent": True, "id": "msg-1"})
    client._transmit = transmit

    result = client.send(
        "send prospect@example.com Hello Approved",
        requester_id=APPROVER,
        to="prospect@example.com",
        subject="Hello",
        body="Approved",
        cc=[],
    )

    _assert_refused(result, "invalid_confirm_token")
    transmit.assert_not_called()


def test_transmit_missing_key_degrades_without_raise(tmp_path):
    with patch("outreach_send.client.secret", return_value=""):
        client = _client(tmp_path)
        result = client._transmit(to="x@example.com", subject="s", body="b")

    assert result == {"sent": False, "reason": "no AGENTMAIL_API_KEY"}


def test_transmit_happy_path_preserves_agentmail_payload(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "Bearer test-key" in request.headers.get("Authorization", "")
        body = json.loads(request.content)
        assert body == {
            "to": "prospect@example.com",
            "subject": "Hello",
            "body": "Nice to meet you.",
            "reply_to": "reply@example.com",
            "cc": ["cc@example.com"],
        }
        return httpx.Response(200, json={"id": "msg-abc123"}, request=request)

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _client(tmp_path)
        client._http_client = httpx.Client(
            base_url="https://api.agentmail.to",
            headers={
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
            transport=httpx.MockTransport(handler),
            timeout=30.0,
        )
        result = client._transmit(
            to="prospect@example.com",
            subject="Hello",
            body="Nice to meet you.",
            reply_to="reply@example.com",
            cc=["cc@example.com"],
        )

    assert result == {"sent": True, "id": "msg-abc123"}


def test_transmit_non_2xx_returns_structured_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Recipient is suppressed"}, request=request)

    with patch("outreach_send.client.secret", return_value="test-key"):
        client = _client(tmp_path)
        client._http_client = httpx.Client(
            base_url="https://api.agentmail.to",
            transport=httpx.MockTransport(handler),
            timeout=30.0,
        )
        result = client._transmit(to="x@example.com", subject="s", body="b")

    assert result["sent"] is False
    assert result["status"] == 422
    assert "suppressed" in result["reason"].lower()
