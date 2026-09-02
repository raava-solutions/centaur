"""Tests for the raava_rbe client."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from raava_rbe.client import RaavaRbeClient  # noqa: E402


def test_lookup_calls_rbe_query_with_static_bearer(monkeypatch):
    hits = [{"slug": "decisions/example", "title": "Example"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/mcp"
        assert request.headers["authorization"] == "Bearer rbe-token"
        body = json.loads(request.content)
        assert body["params"] == {
            "name": "query",
            "arguments": {"q": "agent roster", "limit": 2},
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "jsonrpc": "2.0",
                "result": {"query": "agent roster", "results": hits},
            },
        )

    mocked_http = httpx.Client(
        base_url="https://brain.raava.dev",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaRbeClient()
    client.api_key = "rbe-token"

    result = client.lookup("agent roster", limit=2)

    assert result["source"] == "rbe"
    assert result["results"] == hits


def test_proxy_placeholder_is_sent_as_bearer():
    client = RaavaRbeClient()
    client.api_key = "RAAVA_RBE_API_KEY"

    assert client._bearer_token() == "RAAVA_RBE_API_KEY"


def test_roster_baseline_local_baseline_regression(monkeypatch):
    monkeypatch.setenv("RAAVA_RBE_BASE_URL", "")

    client = RaavaRbeClient()

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
    monkeypatch.setenv("RAAVA_RBE_BASE_URL", "")

    result = RaavaRbeClient().read_page("decisions/example")

    assert result == {
        "source": "offline",
        "slug": "decisions/example",
        "unavailable": True,
        "message": "Cloudflare RBE page reads are unavailable in this environment.",
    }


def test_read_page_calls_rbe_get(monkeypatch):
    page = {"page": {"slug": "decisions/example", "title": "Example"}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["params"] == {
            "name": "get",
            "arguments": {"slug": "decisions/example"},
        }
        return httpx.Response(
            200,
            request=request,
            json={"jsonrpc": "2.0", "result": page},
        )

    mocked_http = httpx.Client(
        base_url="https://brain.raava.dev",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    client = RaavaRbeClient()
    client.api_key = "rbe-token"

    assert client.read_page("decisions/example") == page


def test_remote_failure_degrades_to_local_baseline(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, json={"error": "unavailable"})

    mocked_http = httpx.Client(
        base_url="https://brain.raava.dev",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(httpx, "post", mocked_http.post)

    result = RaavaRbeClient().search_decisions("agent roster")

    assert result["source"] == "local-baseline"


def test_decodes_sse_jsonrpc_payload():
    client = RaavaRbeClient()
    payload = {
        "jsonrpc": "2.0",
        "result": {"query": "agent roster", "results": [{"slug": "roles/enoch"}]},
    }
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"event: message\ndata: {json.dumps(payload)}\n\n".encode(),
    )

    assert client._decode_remote_response(response) == payload["result"]


def test_rbe_tool_is_read_only():
    assert not hasattr(RaavaRbeClient(), "write_learning")
