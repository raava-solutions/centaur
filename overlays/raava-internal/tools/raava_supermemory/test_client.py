"""Tests for the raava_supermemory client.

All HTTP calls are mocked via httpx.MockTransport — no live network.

Import note: conftest.py at overlays/raava-internal/conftest.py inserts both
the repo root (for centaur_sdk) and the overlay tools dir (for raava_supermemory)
onto sys.path, so plain "from raava_supermemory.client import ..." works.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

# Fallback: ensure the overlay tools dir is on path when running directly.
_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from raava_supermemory.client import SupermemoryClient  # noqa: E402


# ---------------------------------------------------------------------------
# Happy-path: remember
# ---------------------------------------------------------------------------

def test_remember_happy():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/v3/memories" in request.url.path
        body = json.loads(request.content)
        assert body["content"] == "ICP for Q3 is mid-market roofing contractors"
        return httpx.Response(
            201,
            request=request,
            json={"id": "mem-abc", "stored": True},
        )

    client = SupermemoryClient(api_key="test-key")
    client._client = httpx.Client(
        base_url="https://api.supermemory.ai",
        transport=httpx.MockTransport(handler),
    )

    result = client.remember(
        "ICP for Q3 is mid-market roofing contractors",
        tags=["icp", "q3"],
    )
    assert result["stored"] is True
    assert result["id"] == "mem-abc"


# ---------------------------------------------------------------------------
# Happy-path: recall
# ---------------------------------------------------------------------------

def test_recall_happy():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/v3/search" in request.url.path
        body = json.loads(request.content)
        assert body["q"] == "ICP roofing"
        assert body["limit"] == 3
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"id": "mem-abc", "content": "ICP for Q3 is mid-market roofing contractors"},
                ]
            },
        )

    client = SupermemoryClient(api_key="test-key")
    client._client = httpx.Client(
        base_url="https://api.supermemory.ai",
        transport=httpx.MockTransport(handler),
    )

    result = client.recall("ICP roofing", limit=3)
    assert len(result["results"]) == 1
    assert "roofing" in result["results"][0]["content"]


# ---------------------------------------------------------------------------
# Degrade: missing API key → no crash, structured fallback
# ---------------------------------------------------------------------------

def test_remember_no_key_degrades():
    """remember() must return {stored: False, reason: 'no key'} when key is absent."""
    client = SupermemoryClient(api_key="")
    result = client.remember("some memory text")
    assert result == {"stored": False, "reason": "no key"}


def test_recall_no_key_degrades():
    """recall() must return {results: [], reason: 'no key'} when key is absent."""
    client = SupermemoryClient(api_key="")
    result = client.recall("any query")
    assert result == {"results": [], "reason": "no key"}


# ---------------------------------------------------------------------------
# Remember with tags passes metadata
# ---------------------------------------------------------------------------

def test_remember_with_tags_sends_metadata():
    received_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received_body.update(json.loads(request.content))
        return httpx.Response(201, request=request, json={"id": "m1", "stored": True})

    client = SupermemoryClient(api_key="key-xyz")
    client._client = httpx.Client(
        base_url="https://api.supermemory.ai",
        transport=httpx.MockTransport(handler),
    )
    client.remember("test memory", tags=["outreach", "q3"])
    assert "metadata" in received_body
    assert "tags" in received_body["metadata"]
    assert "outreach" in received_body["metadata"]["tags"]
