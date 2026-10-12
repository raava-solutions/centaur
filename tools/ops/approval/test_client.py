"""Tests for the approval tool client.

Run from this directory: uv run --no-project --with pytest --with httpx pytest test_client.py
"""

from __future__ import annotations

import importlib.util
import json
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
    "approval_client", Path(__file__).with_name("client.py")
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ApprovalClient = module.ApprovalClient
action_hash_for = module.action_hash_for


def make_client(handler, **overrides) -> ApprovalClient:
    transport = httpx.MockTransport(handler)
    return ApprovalClient(
        base_url=overrides.pop("base_url", "http://api:8000"),
        api_key="approval-key",
        thread_key="slack:C1:1700.0001",
        http_client=httpx.Client(
            base_url="http://api:8000", transport=transport, trust_env=False
        ),
        **overrides,
    )


def test_action_hash_is_stable_and_sensitive() -> None:
    assert action_hash_for("qm destroy 410") == action_hash_for("qm destroy 410")
    assert action_hash_for("qm destroy 410") != action_hash_for("qm destroy 411")
    assert action_hash_for("x").startswith("sha256:")


def test_request_posts_thread_key_summary_and_hash() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"ok": True, "approval": {"id": "apr_x", "status": "pending"}})

    client = make_client(handler)
    approval = client.request("destroy vm 410", "sha256:abc", requester="chief")
    assert approval["id"] == "apr_x"
    assert seen["url"] == "http://api:8000/api/approvals"
    assert seen["authorization"] == "Bearer approval-key"
    assert seen["body"] == {
        "thread_key": "slack:C1:1700.0001",
        "action_summary": "destroy vm 410",
        "action_hash": "sha256:abc",
        "requester": "chief",
    }


def test_request_and_wait_polls_until_decided() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"ok": True, "approval": {"id": "apr_x", "status": "pending"}})
        calls["n"] += 1
        status = "pending" if calls["n"] < 2 else "approved"
        return httpx.Response(
            200, json={"ok": True, "approval": {"id": "apr_x", "status": status, "approver": "U0ZAY"}}
        )

    client = make_client(handler)
    result = client.request_and_wait("x", "sha256:abc", poll_interval_seconds=0.01)
    assert result["status"] == "approved"
    assert result["approver"] == "U0ZAY"


def test_request_and_wait_times_out_with_pending_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"ok": True, "approval": {"id": "apr_x", "status": "pending"}})
        return httpx.Response(200, json={"ok": True, "approval": {"id": "apr_x", "status": "pending"}})

    client = make_client(handler)
    result = client.request_and_wait(
        "x", "sha256:abc", timeout_seconds=0.05, poll_interval_seconds=0.01
    )
    assert result["status"] == "pending"
    assert result["timed_out"] is True


def test_auth_failures_raise_actionable_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "error": "forbidden"})

    client = make_client(handler)
    with pytest.raises(RuntimeError, match="rejected the sandbox credential"):
        client.get("apr_x")


def test_missing_env_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="CENTAUR_API_URL"):
        ApprovalClient()
