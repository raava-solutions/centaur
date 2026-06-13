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
from typing import Any

import httpx

from centaur_sdk import secret


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
        self.api_key = self._optional_secret("RAAVA_GBRAIN_API_KEY", "")

    def _optional_secret(self, key: str, default: str = "") -> str:
        try:
            return secret(key)
        except KeyError:
            return default

    def roster_baseline(self) -> dict[str, Any]:
        remote = self._remote_get("/roster/baseline")
        if remote:
            return remote
        return {
            "source": "local-baseline",
            "decision": "decisions/2026-05-25-agent-roster-lean-down-and-restructure",
            "function_leads": self._roles.public_roster(),
            "private_specialists": list(self._roles.private_specialist_names()),
        }

    def lookup_role(self, name: str) -> dict[str, Any]:
        remote = self._remote_get("/roles/lookup", params={"name": name})
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
        remote = self._remote_get(
            "/decisions/search",
            params={"q": query, "limit": str(max(1, min(limit, 20)))},
        )
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

    def _remote_get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if not self.base_url:
            return None
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                params=params,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            data = self._decode_remote_response(response)
        except Exception:
            return None
        return data if isinstance(data, dict) else {"result": data}

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
