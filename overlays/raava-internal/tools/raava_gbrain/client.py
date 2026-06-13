"""Raava gbrain grounding tool.

The tool has a deterministic local baseline so tests and local dogfood can run
without network access. When ``RAAVA_GBRAIN_BASE_URL`` is configured, methods
try the hosted service first and fall back to the local baseline on failure.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import httpx

from centaur_sdk import secret

DEFAULT_GBRAIN_URL = "https://raava-brain-gbrain-lmbn6fkciq-ue.a.run.app"


def _load_roles_module():
    path = Path(__file__).resolve().parents[2] / "workflows" / "_raava_roles.py"
    spec = importlib.util.spec_from_file_location("raava_overlay_roles_for_tool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Raava role registry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RaavaGbrainClient:
    def __init__(self) -> None:
        self._roles = _load_roles_module()
        self.base_url = os.getenv("RAAVA_GBRAIN_BASE_URL", "").rstrip("/")
        self.oauth_json = os.getenv("RAAVA_GBRAIN_OAUTH_JSON", "")
        self._access_token = ""
        self._access_token_expires_at = 0.0
        # Prefer the proxy-injected OAuth bearer declared in pyproject.toml.
        # This legacy placeholder remains for break-glass deployments that
        # still provide a static hosted gbrain bearer.
        self.api_key = self._optional_secret("RAAVA_GBRAIN_API_KEY", "")

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

    def _remote_query(self, query: str, *, limit: int | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"query": query}
        if limit is not None:
            payload["limit"] = limit
        data = self._remote_tool("query", payload)
        if data is None:
            return None
        if isinstance(data, dict) and data.get("source") == "hosted-gbrain":
            return data
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
        access_token = self._bearer_token()
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"
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
        if self.api_key and self.api_key != "RAAVA_GBRAIN_API_KEY":
            return self.api_key
        if not self.oauth_json:
            return ""
        now = time.time()
        if self._access_token and now < self._access_token_expires_at - 30:
            return self._access_token
        try:
            credential = json.loads(self.oauth_json)
            client_id = credential["client_id"]
            client_secret = credential["client_secret"]
            response = httpx.post(
                f"{self.base_url}/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "read",
                },
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                return ""
            expires_in = data.get("expires_in")
            ttl = float(expires_in) if isinstance(expires_in, int | float) else 300.0
            self._access_token = token
            self._access_token_expires_at = now + ttl
            return token
        except Exception:
            return ""

    def _normalize_tool_result(
        self, data: dict[str, Any], *, query: str, limit: int | None
    ) -> dict[str, Any]:
        text = self._extract_text(data)
        results = self._extract_results(data)
        result: dict[str, Any] = {
            "source": "hosted-gbrain",
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


def _client() -> RaavaGbrainClient:
    return RaavaGbrainClient()
