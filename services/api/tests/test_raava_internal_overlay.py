from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
