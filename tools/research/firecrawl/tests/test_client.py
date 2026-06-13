from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from firecrawl.client import FirecrawlClient


def test_search_posts_v2_payload_and_normalizes_results(monkeypatch) -> None:
    client = FirecrawlClient(api_key="firecrawl-test")
    calls = []

    def fake_post(endpoint, payload, timeout_seconds):
        calls.append((endpoint, payload, timeout_seconds))
        return {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": "https://example.com",
                        "title": "Example",
                        "description": "Result description",
                        "category": "web",
                    }
                ]
            },
        }

    monkeypatch.setattr(client, "_post", fake_post)

    result = client.search(
        "firecrawl docs",
        limit=200,
        include_domains=["docs.firecrawl.dev"],
        timeout_seconds=12.5,
    )

    assert calls == [
        (
            "/search",
            {
                "query": "firecrawl docs",
                "limit": 100,
                "includeDomains": ["docs.firecrawl.dev"],
            },
            12.5,
        )
    ]
    assert result["results"] == [
        {
            "url": "https://example.com",
            "title": "Example",
            "description": "Result description",
            "category": "web",
            "raw": {
                "url": "https://example.com",
                "title": "Example",
                "description": "Result description",
                "category": "web",
            },
        }
    ]


def test_scrape_posts_markdown_payload(monkeypatch) -> None:
    client = FirecrawlClient(api_key="firecrawl-test")
    calls = []

    def fake_post(endpoint, payload, timeout_seconds):
        calls.append((endpoint, payload, timeout_seconds))
        return {
            "success": True,
            "data": {
                "markdown": "# Page",
                "metadata": {"title": "Page"},
            },
        }

    monkeypatch.setattr(client, "_post", fake_post)

    result = client.scrape("https://example.com/page", timeout_seconds=20)

    assert calls == [
        (
            "/scrape",
            {
                "url": "https://example.com/page",
                "formats": ["markdown"],
                "onlyMainContent": True,
            },
            20,
        )
    ]
    assert result["markdown"] == "# Page"
    assert result["metadata"] == {"title": "Page"}
