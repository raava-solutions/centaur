"""Raava gbrain grounding tool.

The tool has a deterministic local baseline so tests and local dogfood can run
without network access. When ``RAAVA_GBRAIN_BASE_URL`` is configured, methods
try the hosted service first and fall back to the local baseline on failure.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from typing import Any

import httpx


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
        self.api_key = os.getenv("RAAVA_GBRAIN_API_KEY", "")

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
            data = response.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else {"result": data}


def _client() -> RaavaGbrainClient:
    return RaavaGbrainClient()
