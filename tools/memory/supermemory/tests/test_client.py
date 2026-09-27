from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from supermemory.client import SupermemoryClient


def test_write_posts_document_with_container_tag(monkeypatch) -> None:
    client = SupermemoryClient(api_key="supermemory-test")
    calls = []

    def fake_request(method, endpoint, *, params=None, json_payload=None, timeout_seconds=None):
        calls.append((method, endpoint, params, json_payload, timeout_seconds))
        return {"id": "mem_123", "status": "queued"}

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.write(
        "remember this",
        container_tag="thread-1",
        metadata={"source": "test"},
        timeout_seconds=10,
    )

    assert calls == [
        (
            "POST",
            "/v3/documents",
            None,
            {
                "content": "remember this",
                "containerTags": ["thread-1"],
                "metadata": {"source": "test"},
            },
            10,
        )
    ]
    assert result["container_tags"] == ["thread-1"]
    assert result["raw"]["id"] == "mem_123"


def test_recall_gets_search_with_bounded_limit(monkeypatch) -> None:
    client = SupermemoryClient(api_key="supermemory-test")
    calls = []

    def fake_request(method, endpoint, *, params=None, json_payload=None, timeout_seconds=None):
        calls.append((method, endpoint, params, json_payload, timeout_seconds))
        return {
            "results": [{"documentId": "doc_1", "score": 0.9}],
            "total": 1,
            "timing": 12.3,
        }

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.recall("project context", container_tag="raava", limit=99)

    assert calls == [
        (
            "POST",
            "/v4/search",
            None,
            {"q": "project context", "limit": 20, "containerTag": "raava"},
            None,
        )
    ]
    assert result["results"] == [{"documentId": "doc_1", "score": 0.9}]
    assert result["total"] == 1
