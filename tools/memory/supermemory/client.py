"""Supermemory memory tool."""

from __future__ import annotations

from typing import Any

import httpx

from centaur_sdk import secret


class SupermemoryClient:
    """Narrow Supermemory wrapper for recall, write, and status checks."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.supermemory.ai/v3",
        default_container_tag: str = "raava-centaur",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_container_tag = default_container_tag
        self.timeout = timeout

    def _optional_secret(self, key: str, default: str | None = None) -> str | None:
        try:
            return secret(key)
        except KeyError:
            return default

    def _require_api_key(self) -> str:
        api_key = self._api_key or self._optional_secret("SUPERMEMORY_API_KEY")
        if not api_key:
            raise RuntimeError("SUPERMEMORY_API_KEY not set.")
        return api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_api_key()}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = timeout_seconds or self.timeout
        with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
            try:
                response = client.request(
                    method,
                    endpoint,
                    params=params,
                    headers=self._headers(),
                    json=json_payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                body = exc.response.text
                if status_code in {401, 403}:
                    raise RuntimeError(
                        "Supermemory authentication failed. Check SUPERMEMORY_API_KEY "
                        "in .env or env injection."
                    ) from exc
                raise RuntimeError(f"Supermemory request failed ({status_code}): {body}") from exc
            except httpx.RequestError as exc:
                raise RuntimeError(f"Supermemory request failed: {exc}") from exc
        data = response.json()
        return data if isinstance(data, dict) else {"result": data}

    def _container_tags(self, container_tag: str | None) -> list[str]:
        tag = (container_tag or self.default_container_tag).strip()
        if not tag:
            tag = self.default_container_tag
        return [tag]

    def write(
        self,
        content: str,
        *,
        container_tag: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        custom_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Write content into Supermemory.

        Use `container_tag` to isolate Raava project or Slack-thread memory
        spaces. The default is a deployment-level `raava-centaur` container.
        """
        normalized_content = content.strip()
        if not normalized_content:
            raise RuntimeError("content cannot be empty.")
        payload: dict[str, Any] = {
            "content": normalized_content,
            "containerTags": self._container_tags(container_tag),
        }
        if user_id:
            payload["userId"] = user_id
        if metadata:
            payload["metadata"] = metadata
        if custom_id:
            payload["customId"] = custom_id
        data = self._request(
            "POST",
            "/documents",
            json_payload=payload,
            timeout_seconds=timeout_seconds,
        )
        return {"status": "queued", "container_tags": payload["containerTags"], "raw": data}

    def recall(
        self,
        query: str,
        *,
        container_tag: str | None = None,
        limit: int = 5,
        threshold: float | None = None,
        include_documents: bool = False,
        include_summaries: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Search Supermemory for relevant memories."""
        normalized_query = query.strip()
        if not normalized_query:
            raise RuntimeError("query cannot be empty.")
        params: dict[str, Any] = {
            "q": normalized_query,
            "limit": max(1, min(int(limit), 20)),
            "containerTag": self._container_tags(container_tag)[0],
        }
        if threshold is not None:
            params["threshold"] = threshold
        include: dict[str, bool] = {}
        if include_documents:
            include["documents"] = True
        if include_summaries:
            include["summaries"] = True
        if include:
            params["include"] = include

        data = self._request("GET", "/search", params=params, timeout_seconds=timeout_seconds)
        return {
            "query": normalized_query,
            "container_tag": params["containerTag"],
            "results": data.get("results", []),
            "total": data.get("total"),
            "timing": data.get("timing"),
            "raw": data,
        }

    def status(self, memory_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Check a Supermemory document status by id."""
        normalized_id = memory_id.strip()
        if not normalized_id:
            raise RuntimeError("memory_id cannot be empty.")
        return self._request("GET", f"/documents/{normalized_id}", timeout_seconds=timeout_seconds)

    def capability(self) -> dict[str, Any]:
        """Return non-secret Supermemory bridge configuration."""
        return {
            "configured": bool(self._api_key or self._optional_secret("SUPERMEMORY_API_KEY")),
            "base_url": self.base_url,
            "default_container_tag": self.default_container_tag,
            "secret": "SUPERMEMORY_API_KEY",
        }


def _client() -> SupermemoryClient:
    """Factory for tool loader."""
    return SupermemoryClient()

