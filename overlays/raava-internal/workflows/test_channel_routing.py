"""Tests for Raava internal Slack channel persona routing."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_roles_module():
    path = Path(__file__).with_name("_raava_roles.py")
    spec = importlib.util.spec_from_file_location("raava_roles_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Raava role registry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


roles = _load_roles_module()


def test_default_persona_for_raava_outreach_channel():
    assert roles.default_persona_for_channel("raava-outreach") == "outreach-operator"
    assert roles.default_persona_for_channel("#raava-outreach") == "outreach-operator"


def test_resolve_outreach_operator_as_standalone_persona():
    resolved = roles.resolve_role("outreach-operator")

    assert resolved == {
        "requested": "outreach-operator",
        "persona": "outreach-operator",
        "kind": "standalone_persona",
    }


def test_env_channel_default_accepts_standalone_persona(monkeypatch):
    monkeypatch.setenv(
        "RAAVA_CENTAUR_CHANNEL_DEFAULTS",
        '{"growth-outreach": "outreach-operator"}',
    )

    assert roles.channel_defaults()["growth-outreach"] == "outreach-operator"
    assert roles.default_persona_for_channel("#growth-outreach") == "outreach-operator"


def test_resolve_function_lead_regression():
    resolved = roles.resolve_role("enoch")

    assert resolved is not None
    assert resolved["persona"] == "enoch"
    assert resolved["kind"] == "function_lead"


# raava_delegate rejects a specialist whose kind is function_lead OR
# standalone_persona (only private_specialist / redirected_role are eligible
# subordinates). This locks the registry contract that guard depends on, so a
# standalone persona like outreach-operator can never be submitted as a
# delegate specialist. (The async handler itself needs the full api package,
# which is not importable in this gate.)
_DELEGATE_BLOCKED_KINDS = {"function_lead", "standalone_persona"}
_DELEGATE_ELIGIBLE_KINDS = {"private_specialist", "redirected_role"}


def test_standalone_persona_is_not_delegate_eligible():
    assert roles.resolve_role("outreach-operator")["kind"] in _DELEGATE_BLOCKED_KINDS
    assert (
        roles.resolve_role("outreach-operator")["kind"]
        not in _DELEGATE_ELIGIBLE_KINDS
    )


def test_private_specialist_remains_delegate_eligible():
    # hana is a private specialist owned by enoch — still a valid subordinate.
    assert roles.resolve_role("hana")["kind"] in _DELEGATE_ELIGIBLE_KINDS
