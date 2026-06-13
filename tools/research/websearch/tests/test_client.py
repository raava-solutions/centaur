from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from websearch.client import WebSearchClient


class _NullBackend:
    def get_sync(self, key: str):
        return None


def _run(coro):
    return asyncio.run(coro)


def _use_null_secret_backend(monkeypatch) -> None:
    from centaur_sdk.backends import registry

    monkeypatch.setattr(registry, "_backend", _NullBackend())


async def _fake_exa_search(_payload, *, timeout_seconds):
    return {
        "requestId": "exa-test",
        "costDollars": {"total": 0.01},
        "results": [
            {
                "url": "https://example.com/source",
                "title": "Example source",
                "highlights": ["Evidence that supports the answer."],
                "publishedDate": "2026-06-01",
            }
        ],
    }


def test_raw_search_does_not_require_synthesis_provider(monkeypatch) -> None:
    _use_null_secret_backend(monkeypatch)
    client = WebSearchClient(exa_api_key="exa-test")
    monkeypatch.setattr(client, "_exa_search_async", _fake_exa_search)

    result = _run(client.search("raw query", synthesize=False))

    assert result["answer_markdown"] is None
    assert result["results"][0]["url"] == "https://example.com/source"
    assert result["meta"]["synthesis_provider"] is None
    assert result["meta"]["partial_failures"] == []


def test_search_uses_openrouter_when_anthropic_is_absent(monkeypatch) -> None:
    _use_null_secret_backend(monkeypatch)
    client = WebSearchClient(
        exa_api_key="exa-test",
        openrouter_api_key="openrouter-test",
        openrouter_model="deepseek/deepseek-chat",
        anthropic_api_key=None,
    )
    calls: list[str] = []

    async def fake_openrouter_text(*, system_prompt, user_prompt, max_tokens):
        calls.append(system_prompt)
        if len(calls) == 1:
            return (
                '{"claims":[{"claim":"Supported claim","source_ids":[0],'
                '"support_level":"strong"}],"contradictions":[],'
                '"continue_research":false,"followup_queries":[]}'
            )
        return "The answer is grounded in the source [0].\n\n## Sources\n[0] Example source"

    monkeypatch.setattr(client, "_exa_search_async", _fake_exa_search)
    monkeypatch.setattr(client, "_call_openrouter_text", fake_openrouter_text)

    result = _run(client.search("synthesized query", synthesize=True))

    assert result["answer_markdown"].startswith("The answer is grounded")
    assert result["meta"]["synthesis_provider"] == "openrouter"
    assert result["meta"]["synthesis_model"] == "deepseek/deepseek-chat"
    assert result["meta"]["partial_failures"] == []
    assert len(calls) == 2


def test_search_reports_missing_synthesis_provider_without_failing_retrieval(
    monkeypatch,
) -> None:
    _use_null_secret_backend(monkeypatch)
    client = WebSearchClient(exa_api_key="exa-test", anthropic_api_key=None)
    monkeypatch.setattr(client, "_exa_search_async", _fake_exa_search)

    result = _run(client.search("needs synthesis", synthesize=True))

    assert result["answer_markdown"] is None
    assert result["results"][0]["title"] == "Example source"
    assert "OPENROUTER_API_KEY or ANTHROPIC_API_KEY" in result["meta"]["partial_failures"][0][
        "error"
    ]
