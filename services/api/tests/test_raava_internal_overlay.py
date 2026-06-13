from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx

from api.tool_manager import ToolManager


REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_ROOT = REPO_ROOT / "overlays" / "raava-internal"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_raava_role_registry_exposes_only_function_leads(monkeypatch) -> None:
    monkeypatch.delenv("RAAVA_CENTAUR_CHANNEL_DEFAULTS", raising=False)
    roles = _load_module(
        "test_raava_roles_registry",
        OVERLAY_ROOT / "workflows" / "_raava_roles.py",
    )

    assert roles.function_lead_names() == (
        "chief",
        "vera",
        "priya",
        "enoch",
        "elena",
        "heathcliffe",
        "vivian",
        "argus",
    )
    assert roles.private_specialist_names() == ("hana", "isaac")
    assert not roles.is_function_lead("hana")
    assert not roles.is_function_lead("isaac")
    assert roles.resolve_role("hana") == {
        "requested": "hana",
        "persona": "enoch",
        "kind": "private_specialist",
    }
    assert roles.resolve_role("darnell") == {
        "requested": "darnell",
        "persona": "enoch",
        "kind": "redirected_role",
    }
    assert roles.default_persona_for_channel("engineering") == "enoch"
    assert roles.default_persona_for_channel("quality") == "vivian"


def test_raava_channel_defaults_accept_configured_slack_ids(monkeypatch) -> None:
    monkeypatch.setenv(
        "RAAVA_CENTAUR_CHANNEL_DEFAULTS",
        '{"C0123ENGINEERING": "enoch", "C0456QA": "vivian", "bad": "hana"}',
    )
    roles = _load_module(
        "test_raava_roles_channel_config",
        OVERLAY_ROOT / "workflows" / "_raava_roles.py",
    )

    assert roles.default_persona_for_channel("C0123ENGINEERING") == "enoch"
    assert roles.default_persona_for_channel("C0456QA") == "vivian"
    assert roles.default_persona_for_channel("bad") is None


def test_raava_personas_are_discoverable_without_private_specialists() -> None:
    manager = ToolManager(OVERLAY_ROOT / "tools")
    manager.discover()

    assert set(manager.personas) == {
        "chief",
        "vera",
        "priya",
        "enoch",
        "elena",
        "heathcliffe",
        "vivian",
        "argus",
    }
    assert "hana" not in manager.personas
    assert "isaac" not in manager.personas
    assert "raava_gbrain" in manager.tools
    assert "proposal-first" in manager.get_persona("argus").prompt_content
    assert "do not report to the engineering persona" in manager.get_persona(
        "vivian"
    ).prompt_content.lower()


def test_raava_gbrain_tool_uses_local_baseline(monkeypatch) -> None:
    monkeypatch.delenv("RAAVA_GBRAIN_BASE_URL", raising=False)
    client_module = _load_module(
        "test_raava_gbrain_client",
        OVERLAY_ROOT / "tools" / "raava_gbrain" / "client.py",
    )
    client = client_module.RaavaGbrainClient()

    roster = client.roster_baseline()
    assert roster["source"] == "local-baseline"
    assert [lead["name"] for lead in roster["function_leads"]] == [
        "chief",
        "vera",
        "priya",
        "enoch",
        "elena",
        "heathcliffe",
        "vivian",
        "argus",
    ]

    hana = client.lookup_role("hana")
    assert hana["found"] is True
    assert hana["role"]["kind"] == "private_specialist"
    assert hana["role"]["owner"] == "enoch"

    missing = client.lookup_role("unknown-role")
    assert missing["found"] is False

    decisions = client.search_decisions("active Raava leads")
    assert decisions["results"][0]["path"] == (
        "decisions/2026-05-25-agent-roster-lean-down-and-restructure"
    )


def test_raava_gbrain_tool_decodes_hosted_sse_jsonrpc_payload(monkeypatch) -> None:
    monkeypatch.delenv("RAAVA_GBRAIN_BASE_URL", raising=False)
    client_module = _load_module(
        "test_raava_gbrain_client_sse",
        OVERLAY_ROOT / "tools" / "raava_gbrain" / "client.py",
    )
    client = client_module.RaavaGbrainClient()
    payload = {
        "jsonrpc": "2.0",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "source": "hosted-gbrain",
                            "results": [{"path": "decisions/example"}],
                        }
                    ),
                }
            ]
        },
    }
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"event: message\ndata: {json.dumps(payload)}\n\n".encode(),
    )

    assert client._decode_remote_response(response) == {
        "source": "hosted-gbrain",
        "results": [{"path": "decisions/example"}],
    }


def test_raava_gbrain_tool_calls_hosted_mcp_query(monkeypatch) -> None:
    monkeypatch.setenv(
        "RAAVA_GBRAIN_BASE_URL", "https://raava-brain-gbrain-lmbn6fkciq-ue.a.run.app"
    )
    client_module = _load_module(
        "test_raava_gbrain_client_mcp",
        OVERLAY_ROOT / "tools" / "raava_gbrain" / "client.py",
    )
    client = client_module.RaavaGbrainClient()
    calls: list[dict[str, object]] = []

    def fake_post(url, *, json, headers, timeout):
        calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        payload = {
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "FitRoofingCo call context from hosted gbrain.",
                    }
                ]
            },
        }
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(client_module.httpx, "post", fake_post)

    result = client.search_decisions("FitRoofingCo recent call notes", limit=3)

    assert calls[0]["url"] == (
        "https://raava-brain-gbrain-lmbn6fkciq-ue.a.run.app/mcp"
    )
    assert calls[0]["json"] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "query",
            "arguments": {"query": "FitRoofingCo recent call notes", "limit": 3},
        },
    }
    assert calls[0]["headers"]["accept"] == "application/json, text/event-stream"
    assert result["source"] == "hosted-gbrain"
    assert result["results"][0]["summary"] == (
        "FitRoofingCo call context from hosted gbrain."
    )


def test_raava_gbrain_tool_mints_local_oauth_token(monkeypatch) -> None:
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "http://127.0.0.1:8087")
    monkeypatch.setenv(
        "RAAVA_GBRAIN_OAUTH_JSON",
        json.dumps({"client_id": "client-id", "client_secret": "client-secret"}),
    )
    client_module = _load_module(
        "test_raava_gbrain_client_local_oauth",
        OVERLAY_ROOT / "tools" / "raava_gbrain" / "client.py",
    )
    client = client_module.RaavaGbrainClient()
    calls: list[dict[str, object]] = []

    def fake_post(url, *, json=None, data=None, headers, timeout):
        calls.append(
            {
                "url": url,
                "json": json,
                "data": data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if url.endswith("/token"):
            return httpx.Response(
                200,
                json={"access_token": "minted-token", "expires_in": 3600},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"content": [{"type": "text", "text": "Hosted context"}]},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(client_module.httpx, "post", fake_post)

    result = client.search_decisions("agent roster", limit=2)

    assert calls[0]["url"] == "http://127.0.0.1:8087/token"
    assert calls[0]["data"] == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scope": "read",
    }
    assert calls[1]["url"] == "http://127.0.0.1:8087/mcp"
    assert calls[1]["headers"]["authorization"] == "Bearer minted-token"
    assert result["source"] == "hosted-gbrain"
    assert result["results"] == [{"summary": "Hosted context"}]


def test_raava_gbrain_tool_preserves_hosted_result_list(monkeypatch) -> None:
    monkeypatch.setenv("RAAVA_GBRAIN_BASE_URL", "http://127.0.0.1:8087")
    monkeypatch.setenv(
        "RAAVA_GBRAIN_OAUTH_JSON",
        json.dumps({"client_id": "client-id", "client_secret": "client-secret"}),
    )
    client_module = _load_module(
        "test_raava_gbrain_client_hosted_results",
        OVERLAY_ROOT / "tools" / "raava_gbrain" / "client.py",
    )
    client = client_module.RaavaGbrainClient()
    hosted_results = [
        {
            "slug": "decisions/2026-05-25-agent-roster-lean-down-and-restructure",
            "title": "Agent roster lean-down",
            "chunk_text": "Keep function leads Slack-visible.",
        }
    ]

    def fake_post(url, *, json=None, data=None, headers, timeout):
        if url.endswith("/token"):
            return httpx.Response(
                200,
                json={"access_token": "minted-token", "expires_in": 3600},
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "content": [
                        {"type": "text", "text": json_module.dumps(hosted_results)}
                    ]
                },
            },
            request=httpx.Request("POST", url),
        )

    json_module = json
    monkeypatch.setattr(client_module.httpx, "post", fake_post)

    result = client.search_decisions("agent roster", limit=1)

    assert result["source"] == "hosted-gbrain"
    assert result["results"] == hosted_results
