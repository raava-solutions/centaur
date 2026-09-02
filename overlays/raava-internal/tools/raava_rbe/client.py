"""Raava RBE grounding tool.

Cloudflare RBE is the company knowledge read surface. A deterministic local
roster baseline keeps local tests and degraded deployments honest when RBE is
unavailable.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx

from centaur_sdk import secret

DEFAULT_RBE_URL = "https://brain.raava.dev"


def _load_roles_module():
    path = Path(__file__).resolve().parents[2] / "workflows" / "_raava_roles.py"
    spec = importlib.util.spec_from_file_location("raava_overlay_roles_for_tool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Raava role registry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RaavaRbeClient:
    def __init__(self) -> None:
        self._roles = _load_roles_module()
        self.base_url = os.getenv("RAAVA_RBE_BASE_URL", DEFAULT_RBE_URL).rstrip("/")
        self.api_key = self._optional_secret("RAAVA_RBE_API_KEY")

    def _optional_secret(self, key: str, default: str = "") -> str:
        try:
            return secret(key)
        except KeyError:
            return default

    def roster_baseline(self) -> dict[str, Any]:
        remote = self._remote_query(
            "active Raava function leads and private specialist roster"
        )
        if remote:
            return remote
        return {
            "source": "local-baseline",
            "decision": "decisions/2026-05-25-agent-roster-lean-down-and-restructure",
            "function_leads": self._roles.public_roster(),
            "private_specialists": list(self._roles.private_specialist_names()),
        }

    def lookup_role(self, name: str) -> dict[str, Any]:
        remote = self._remote_query(f"Raava role lookup for {name}")
        if remote:
            return remote
        record = self._roles.role_record(name)
        if record is None:
            return {
                "source": "local-baseline",
                "found": False,
                "name": name,
                "message": "No Raava role match found in the local baseline.",
            }
        return {"source": "local-baseline", "found": True, "role": record}

    def search_decisions(self, query: str, limit: int = 5) -> dict[str, Any]:
        remote = self._remote_query(query, limit=max(1, min(limit, 20)))
        if remote:
            return remote
        return {
            "source": "local-baseline",
            "query": query,
            "results": [
                {
                    "path": "decisions/2026-05-25-agent-roster-lean-down-and-restructure",
                    "title": "Agent roster lean-down and restructure",
                    "summary": (
                        "Keep the function-lead layer Slack-visible and use "
                        "specialists as on-demand subagents."
                    ),
                }
            ],
        }

    def lookup(self, query: str, limit: int = 5) -> dict[str, Any]:
        return self.search_decisions(query, limit=limit)

    def read_page(self, slug: str) -> dict[str, Any]:
        if not self.base_url:
            return self._offline_page(slug)
        remote = self._remote_tool("get", {"slug": slug})
        if remote is not None:
            return remote
        return self._offline_page(slug)

    def _offline_page(self, slug: str) -> dict[str, Any]:
        return {
            "source": "offline",
            "slug": slug,
            "unavailable": True,
            "message": "Cloudflare RBE page reads are unavailable in this environment.",
        }

    def _remote_query(
        self, query: str, *, limit: int | None = None
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"q": query}
        if limit is not None:
            payload["limit"] = limit
        data = self._remote_tool("query", payload)
        if data is None:
            return None
        return self._normalize_tool_result(data, query=query, limit=limit)

    def _remote_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.base_url:
            return None
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        bearer = self._bearer_token()
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            response = httpx.post(
                f"{self.base_url}/mcp",
                json=payload,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            data = self._decode_remote_response(response)
        except Exception:
            return None
        return data if isinstance(data, dict) else {"result": data}

    def _bearer_token(self) -> str:
        return self.api_key

    def _normalize_tool_result(
        self, data: dict[str, Any], *, query: str, limit: int | None
    ) -> dict[str, Any]:
        text = self._extract_text(data)
        results = self._extract_results(data)
        result: dict[str, Any] = {
            "source": "rbe",
            "query": query,
            "results": results,
            "raw": data,
        }
        if limit is not None:
            result["limit"] = limit
        if text and not results:
            result["summary"] = text
            result["results"] = [{"summary": text}]
        elif text:
            result["summary"] = text
        return result

    def _extract_results(self, data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        results = data.get("results")
        if isinstance(results, list):
            return results
        result = data.get("result")
        if isinstance(result, list):
            return result
        return []

    def _extract_text(self, data: Any) -> str:
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            text = data.get("text")
            if isinstance(text, str):
                return text.strip()
            content = data.get("content")
            if isinstance(content, list):
                return "\n".join(
                    item["text"].strip()
                    for item in content
                    if isinstance(item, dict)
                    and isinstance(item.get("text"), str)
                    and item["text"].strip()
                )
        return ""

    def _decode_remote_response(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._decode_sse(response.text)
        try:
            data = response.json()
        except ValueError:
            text = response.text.strip()
            if text.startswith("data:"):
                return self._decode_sse(text)
            return {"text": text} if text else {}
        return self._normalize_remote_payload(data)

    def _decode_sse(self, text: str) -> Any:
        last_payload: Any = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            raw = stripped.removeprefix("data:").strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                last_payload = self._normalize_remote_payload(json.loads(raw))
            except json.JSONDecodeError:
                last_payload = {"text": raw}
        return last_payload

    def _normalize_remote_payload(self, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "jsonrpc" in data and "result" in data:
            return self._normalize_remote_payload(data["result"])
        content = data.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
            text = "".join(text_parts).strip()
            if text:
                try:
                    return self._normalize_remote_payload(json.loads(text))
                except json.JSONDecodeError:
                    return {"text": text}
        return data


def _client() -> RaavaRbeClient:
    return RaavaRbeClient()
