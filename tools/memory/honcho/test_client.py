"""Tests for the Honcho tool client.

Run from this directory: uv run --no-project --with pytest pytest test_client.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

# client.py imports centaur_sdk for secret resolution. The behaviors under test
# never resolve real secrets, so stub the module before loading the client as a
# standalone file (mirrors how the sandbox plugin loader sees it).
if "centaur_sdk" not in sys.modules:
    centaur_sdk_mod = types.ModuleType("centaur_sdk")

    def _secret(key: str, default: str | None = None) -> str | None:
        return default

    centaur_sdk_mod.secret = _secret
    sys.modules["centaur_sdk"] = centaur_sdk_mod

spec = importlib.util.spec_from_file_location(
    "honcho_client", Path(__file__).with_name("client.py")
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
HonchoClient = module.HonchoClient
sanitize_id = module.sanitize_id
slack_peer_id = module.slack_peer_id
agent_peer_id = module.agent_peer_id
session_id_for_thread = module.session_id_for_thread


def test_sanitize_id_replaces_illegal_characters() -> None:
    assert sanitize_id("slack:C0TEST12345:1791670752.873056") == (
        "slack-C0TEST12345-1791670752-873056"
    )
    assert sanitize_id("slack:U0123ABC") == "slack-U0123ABC"
    assert sanitize_id("agent:chief") == "agent-chief"


def test_sanitize_id_rejects_empty() -> None:
    with pytest.raises(RuntimeError):
        sanitize_id(":::...")


def test_id_helpers() -> None:
    assert slack_peer_id("U0123ABC") == "slack-U0123ABC"
    assert agent_peer_id("chief") == "agent-chief"
    assert session_id_for_thread("slack:C1:2.3") == "slack-C1-2-3"


def _make_client(handler, **kwargs) -> HonchoClient:
    transport = httpx.MockTransport(handler)
    return HonchoClient(
        base_url="https://honcho.test",
        workspace="centaur",
        api_key="stub",
        http_client=httpx.Client(
            base_url="https://honcho.test", transport=transport
        ),
        **kwargs,
    )


def test_peer_create_posts_sanitized_id() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "slack-U1"})

    client = _make_client(handler)
    result = client.peer_create("slack:U1")

    assert result == {"id": "slack-U1"}
    assert seen["url"] == "https://honcho.test/v3/workspaces/centaur/peers"
    assert b'"slack-U1"' in seen["body"]


def test_peer_get_returns_none_when_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "total": 0})

    assert _make_client(handler).peer_get("slack:UX") is None


def test_session_create_attaches_peers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "s1"})

    client = _make_client(handler)
    client.session_create("slack:C1:2.3", peers=["slack:U1", "agent:chief"])
    body = seen["body"]
    assert b'"slack-C1-2-3"' in body
    assert b'"slack-U1"' in body
    assert b'"agent-chief"' in body


def test_messages_add_normalizes_batch() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json=[{"id": "m1"}])

    client = _make_client(handler)
    client.messages_add(
        "s1", [{"peer_id": "slack:U1", "content": "hello", "metadata": {"k": "v"}}]
    )
    body = seen["body"]
    assert b'"slack-U1"' in body
    assert b'"hello"' in body
    assert b'"k": "v"' in body or b'"k":"v"' in body


def test_messages_add_rejects_empty_content() -> None:
    client = _make_client(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(RuntimeError):
        client.messages_add("s1", [{"peer_id": "slack:U1", "content": "  "}])


def test_auth_header_uses_bearer_stub() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "ok"})

    _make_client(handler).health()
    assert seen["authorization"] == "Bearer stub"


def test_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    client = _make_client(handler)
    with pytest.raises(RuntimeError, match="authentication failed"):
        client.health()
