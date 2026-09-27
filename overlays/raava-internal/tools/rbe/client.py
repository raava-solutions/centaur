"""Raava Brain Engine (RBE) client.

Thin wrapper over the RBE MCP endpoint (https://brain.raava.dev/mcp). Exposes
the read surface as plain methods: query, search, get_page, list_pages, facts,
backlinks, timeline. Read-only; RBE writes are not exposed here.

Auth is a static Bearer token. In sandboxes the token is a placeholder that
iron-proxy replaces in-flight for brain.raava.dev (see pyproject.toml).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from centaur_sdk import secret


class RbeClient:
    """Client for the Raava Brain Engine knowledge base (read-only)."""

    def __init__(self, base_url: str | None = None, api_token: str | None = None, timeout: float = 20.0):
        self._base_url = (base_url or os.getenv("RBE_BASE_URL", "https://brain.raava.dev")).rstrip("/")
        self._api_token = api_token
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._rpc_id = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _token(self) -> str:
        if self._api_token:
            return self._api_token
        return secret("RBE_API_TOKEN", "")

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one RBE MCP tool and unwrap the JSON-RPC/MCP envelope."""
        token = self._token()
        if not token:
            raise RuntimeError("RBE_API_TOKEN not set.")
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        try:
            resp = self.client.post(f"{self._base_url}/mcp", json=payload, headers=headers)
        except httpx.RequestError as e:
            raise RuntimeError(f"RBE request failed: {e}")
        if resp.status_code >= 400:
            raise RuntimeError(f"RBE API error ({resp.status_code}): {resp.text}")
        return self._unwrap(self._decode(resp))

    def _decode(self, resp: httpx.Response) -> Any:
        if "text/event-stream" in resp.headers.get("content-type", ""):
            last: Any = None
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw = line.removeprefix("data:").strip()
                    if raw and raw != "[DONE]":
                        try:
                            last = json.loads(raw)
                        except json.JSONDecodeError:
                            last = {"text": raw}
            return last
        return resp.json()

    def _unwrap(self, data: Any) -> Any:
        if isinstance(data, dict):
            if "error" in data:
                raise RuntimeError(f"RBE error: {data['error']}")
            if "result" in data and ("jsonrpc" in data or isinstance(data["result"], dict)):
                return self._unwrap(data["result"])
            content = data.get("content")
            if isinstance(content, list):
                text = "".join(
                    item["text"] for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ).strip()
                if text:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"text": text}
        return data

    # -- Knowledge surface -----------------------------------------------------

    def query(self, q: str, type: str | None = None, limit: int = 10, rerank: bool | None = None) -> Any:
        """Hybrid query over the knowledge base with cited hits. Best default for questions.

        Args:
            q: Natural-language question or search phrase.
            type: Optional page type filter.
            limit: Max results (1-50).
            rerank: Enable reranking for quality over speed.
        """
        args: dict[str, Any] = {"q": q, "limit": max(1, min(limit, 50))}
        if type:
            args["type"] = type
        if rerank is not None:
            args["rerank"] = rerank
        return self._call("query", args)

    def search(self, q: str, type: str | None = None, limit: int = 10, rerank: bool | None = None) -> Any:
        """Raw hybrid search. Use when you want unprocessed hits rather than a synthesized answer.

        Args:
            q: Search phrase.
            type: Optional page type filter.
            limit: Max results (1-50).
            rerank: Enable reranking.
        """
        args: dict[str, Any] = {"q": q, "limit": max(1, min(limit, 50))}
        if type:
            args["type"] = type
        if rerank is not None:
            args["rerank"] = rerank
        return self._call("search", args)

    def get_page(self, slug: str) -> Any:
        """Fetch one knowledge-base page by slug.

        Args:
            slug: Page slug, e.g. from a query/search hit.
        """
        return self._call("get", {"slug": slug})

    def list_pages(self, type: str | None = None, tag: str | None = None, limit: int = 20) -> Any:
        """List knowledge-base pages, optionally filtered.

        Args:
            type: Page type filter.
            tag: Tag filter.
            limit: Max pages.
        """
        args: dict[str, Any] = {"limit": max(1, min(limit, 50))}
        if type:
            args["type"] = type
        if tag:
            args["tag"] = tag
        return self._call("list", args)

    def facts(self, entity_slug: str) -> Any:
        """Fetch structured fact rows for an entity (person, system, project).

        Args:
            entity_slug: Entity slug.
        """
        return self._call("facts", {"entity_slug": entity_slug})

    def backlinks(self, slug: str) -> Any:
        """Fetch inbound wikilinks to a page — what references this page.

        Args:
            slug: Page slug.
        """
        return self._call("backlinks", {"slug": slug})

    def timeline(self, entity_slug: str) -> Any:
        """Fetch the fact timeline for an entity — how facts changed over time.

        Args:
            entity_slug: Entity slug.
        """
        return self._call("timeline", {"entity_slug": entity_slug})

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> RbeClient:
    return RbeClient()
