"""Firecrawl client for bounded search and scrape workflows."""

from __future__ import annotations

from typing import Any

import httpx

from centaur_sdk import secret


class FirecrawlClient:
    """Client for Firecrawl v2 search and scrape endpoints."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.firecrawl.dev/v2",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _optional_secret(self, key: str, default: str | None = None) -> str | None:
        try:
            return secret(key)
        except KeyError:
            return default

    def _require_api_key(self) -> str:
        api_key = self._api_key or self._optional_secret("FIRECRAWL_API_KEY")
        if not api_key:
            raise RuntimeError("FIRECRAWL_API_KEY not set.")
        return api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_api_key()}",
            "Content-Type": "application/json",
        }

    def _post(self, endpoint: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        with httpx.Client(base_url=self.base_url, timeout=timeout_seconds) as client:
            try:
                response = client.post(endpoint, headers=self._headers(), json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                body = exc.response.text
                if status_code in {401, 403}:
                    raise RuntimeError(
                        "Firecrawl authentication failed. Check FIRECRAWL_API_KEY "
                        "in .env or env injection."
                    ) from exc
                raise RuntimeError(f"Firecrawl request failed ({status_code}): {body}") from exc
            except httpx.RequestError as exc:
                raise RuntimeError(f"Firecrawl request failed: {exc}") from exc
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            error = data.get("error") or data.get("message") or "unknown Firecrawl error"
            raise RuntimeError(f"Firecrawl request failed: {error}")
        return data

    def _normalize_search_results(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        payload = data.get("data")
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = []
            for value in payload.values():
                if isinstance(value, list):
                    rows.extend(value)
        else:
            rows = []

        results: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            results.append(
                {
                    "url": url,
                    "title": str(item.get("title") or url),
                    "description": str(
                        item.get("description")
                        or item.get("markdown")
                        or item.get("content")
                        or ""
                    ),
                    "category": item.get("category"),
                    "raw": item,
                }
            )
        return results

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        sources: list[str] | None = None,
        categories: list[str] | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Search the web through Firecrawl.

        Use this when the task needs Firecrawl's search index or search results
        that can feed a follow-up scrape. It does not run Firecrawl crawl or
        browser-agent jobs.
        """
        normalized_query = query.strip()
        if not normalized_query:
            raise RuntimeError("query cannot be empty.")
        bounded_limit = max(1, min(int(limit), 100))
        payload: dict[str, Any] = {
            "query": normalized_query,
            "limit": bounded_limit,
        }
        if sources:
            payload["sources"] = sources
        if categories:
            payload["categories"] = categories
        if include_domains:
            payload["includeDomains"] = include_domains
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        data = self._post("/search", payload, timeout_seconds)
        return {
            "query": normalized_query,
            "results": self._normalize_search_results(data),
            "raw": data,
        }

    def scrape(
        self,
        url: str,
        *,
        formats: list[str] | None = None,
        only_main_content: bool = True,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Scrape a single URL through Firecrawl and return markdown by default."""
        normalized_url = url.strip()
        if not normalized_url:
            raise RuntimeError("url cannot be empty.")
        requested_formats = formats or ["markdown"]
        payload = {
            "url": normalized_url,
            "formats": requested_formats,
            "onlyMainContent": only_main_content,
        }
        data = self._post("/scrape", payload, timeout_seconds)
        result = data.get("data") if isinstance(data.get("data"), dict) else data
        return {
            "url": normalized_url,
            "markdown": result.get("markdown") if isinstance(result, dict) else None,
            "html": result.get("html") if isinstance(result, dict) else None,
            "metadata": result.get("metadata", {}) if isinstance(result, dict) else {},
            "raw": data,
        }


def _client() -> FirecrawlClient:
    """Factory for tool loader."""
    return FirecrawlClient()
