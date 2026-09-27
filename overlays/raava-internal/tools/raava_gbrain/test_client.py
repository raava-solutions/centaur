"""Tests for the raava_gbrain client."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

# Fallback: ensure imports work when this file is run directly.
_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from raava_gbrain.client import RaavaGbrainClient  # noqa: E402


def test_lookup_happy_path_returns_results_list(monkeypatch):
    hosted_results = [{"path": "decisions/example", "title": "Example"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/mcp"
        body = json.loads(request.content)
        assert body["params"] == {
            "name": "query",
            "arguments": {"query": "agent roster", "limit": 2},
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "content": [{"type": "text", "text": json.dumps(hosted_results)}]
                },
            },
        )

    mocked_http = httpx.Client(
        base_url="https://gbrain.example",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "https://gbrain.example")
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaGbrainClient()

    result = client.lookup("agent roster", limit=2)

    assert result["source"] == "hosted-gbrain"
    assert result["results"] == hosted_results


def test_roster_baseline_local_baseline_regression(monkeypatch):
    monkeypatch.delenv("RAAVA_GBRAIN_BASE_URL", raising=False)

    client = RaavaGbrainClient()

    result = client.roster_baseline()

    assert result["source"] == "local-baseline"
    assert [lead["name"] for lead in result["function_leads"]] == [
        "chief",
        "vera",
        "priya",
        "enoch",
        "elena",
        "heathcliffe",
        "vivian",
        "argus",
    ]


def test_read_page_no_base_url_degrades_offline(monkeypatch):
    monkeypatch.delenv("RAAVA_GBRAIN_BASE_URL", raising=False)

    client = RaavaGbrainClient()

    result = client.read_page("decisions/example")

    assert "unavailable" in result
    assert result["unavailable"] is True
    assert result["path"] == "decisions/example"


def test_write_learning_happy_path_calls_add_timeline_entry(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/mcp"
        assert request.headers["authorization"] == "Bearer gbrain-token"
        body = json.loads(request.content)
        assert body["params"] == {
            "name": "add_timeline_entry",
            "arguments": {
                "summary": "Customer asked for rollout owner.",
                "title": "Rollout owner question",
                "path": "timeline/customer-rollout-owner",
                "tags": ["customer", "rollout"],
                "metadata": {"source": "test"},
            },
        }
        return httpx.Response(
            200,
            request=request,
            json={"jsonrpc": "2.0", "result": {"entry_id": "entry_123"}},
        )

    mocked_http = httpx.Client(
        base_url="https://gbrain.example",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "https://gbrain.example")
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaGbrainClient()
    client.api_key = "gbrain-token"

    result = client.write_learning(
        "Customer asked for rollout owner.",
        title="Rollout owner question",
        path="timeline/customer-rollout-owner",
        tags=["customer", "rollout"],
        metadata={"source": "test"},
    )

    assert result == {"written": True, "result": {"entry_id": "entry_123"}}


def test_write_learning_no_base_url_degrades(monkeypatch):
    monkeypatch.delenv("RAAVA_GBRAIN_BASE_URL", raising=False)

    client = RaavaGbrainClient()
    client.api_key = "gbrain-token"

    assert client.write_learning("Learning") == {
        "written": False,
        "reason": "gbrain unavailable",
    }


def test_write_learning_remote_failure_degrades(monkeypatch):
    """base_url + auth present but the remote call fails -> degrade, no crash."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(503, request=request, json={"error": "unavailable"})

    mocked_http = httpx.Client(
        base_url="https://gbrain.example",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "https://gbrain.example")
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaGbrainClient()
    client.api_key = "gbrain-token"

    assert client.write_learning("Learning") == {
        "written": False,
        "reason": "gbrain unavailable",
    }
    # The write WAS attempted (not silently pre-failed before the call).
    assert attempts == ["/mcp"]


def test_write_learning_oauth_only_attempts_write(monkeypatch):
    """OAuth-only deployments (no static key) must still attempt the write.

    Regression guard for the bug where write_learning gated on bool(api_key)
    and silently dropped writes on the documented primary (OAuth) auth path.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                request=request,
                json={"access_token": "oauth-abc", "expires_in": 300},
            )
        assert request.url.path == "/mcp"
        assert request.headers["authorization"] == "Bearer oauth-abc"
        body = json.loads(request.content)
        assert body["params"]["name"] == "add_timeline_entry"
        return httpx.Response(
            200,
            request=request,
            json={"jsonrpc": "2.0", "result": {"entry_id": "entry_oauth"}},
        )

    mocked_http = httpx.Client(
        base_url="https://gbrain.example",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "https://gbrain.example")
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaGbrainClient()
    client.api_key = ""
    client.oauth_json = json.dumps(
        {"client_id": "cid", "client_secret": "csecret"}
    )

    result = client.write_learning("Learning")

    assert result == {"written": True, "result": {"entry_id": "entry_oauth"}}


def test_read_page_happy_path_forwards_remote_page(monkeypatch):
    page = {
        "path": "decisions/example",
        "title": "Example decision",
        "body": "Use the hosted gbrain page payload.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/mcp"
        body = json.loads(request.content)
        assert body["params"] == {
            "name": "get_page",
            "arguments": {"path": "decisions/example"},
        }
        return httpx.Response(200, request=request, json={"jsonrpc": "2.0", "result": page})

    mocked_http = httpx.Client(
        base_url="https://gbrain.example",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "https://gbrain.example")
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaGbrainClient()

    assert client.read_page("decisions/example") == page


def test_read_page_remote_none_degrades_offline(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, json={"error": "unavailable"})

    mocked_http = httpx.Client(
        base_url="https://gbrain.example",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "https://gbrain.example")
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaGbrainClient()

    result = client.read_page("decisions/example")

    assert "unavailable" in result
    assert result["unavailable"] is True
    assert result["path"] == "decisions/example"
