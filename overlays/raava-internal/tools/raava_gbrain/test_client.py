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


def test_read_page_no_base_url_degrades_offline(monkeypatch):
    monkeypatch.delenv("RAAVA_GBRAIN_BASE_URL", raising=False)

    client = RaavaGbrainClient()

    result = client.read_page("decisions/example")

    assert "unavailable" in result
    assert result["unavailable"] is True
    assert result["path"] == "decisions/example"


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
