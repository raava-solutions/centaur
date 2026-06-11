from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api.workflow_engine import Delivery
from api.workflows.slack_thread_turn import Input


REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_ROOT = REPO_ROOT / "overlays" / "raava-internal"


def _load_slack_module():
    name = "test_raava_overlay_slack_thread_turn"
    path = OVERLAY_ROOT / "workflows" / "slack_thread_turn.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_channel_default_routes_to_function_lead(monkeypatch) -> None:
    monkeypatch.setenv("RAAVA_CENTAUR_CHANNEL_DEFAULTS", "C123=enoch")
    module = _load_slack_module()
    base_handler = AsyncMock(return_value={"ok": True})
    module.base_handler = base_handler

    await module.handler(
        Input(
            thread_key="slack:C123:1.0",
            text="review this service boundary",
            delivery=Delivery.slack("C123", "1.0", user_id="U123"),
        ),
        object(),
    )

    routed = base_handler.await_args.args[0]
    assert routed.persona == "enoch"
    assert routed.metadata["raava_routing_kind"] == "channel_default"
    assert routed.metadata["raava_function_lead"] == "enoch"


@pytest.mark.asyncio
async def test_private_specialist_selector_redirects_to_owner() -> None:
    module = _load_slack_module()
    base_handler = AsyncMock(return_value={"ok": True})
    module.base_handler = base_handler

    await module.handler(
        Input(
            thread_key="slack:C123:1.0",
            text="--hana pressure test this UI",
            delivery=Delivery.slack("product", "1.0", user_id="U123"),
        ),
        object(),
    )

    routed = base_handler.await_args.args[0]
    assert routed.persona == "enoch"
    assert routed.metadata["raava_requested_persona"] == "hana"
    assert routed.metadata["raava_routing_kind"] == "private_specialist"
    assert routed.parts[0]["text"].startswith("Raava routing note")
    assert routed.parts[1]["text"] == "pressure test this UI"


@pytest.mark.asyncio
async def test_explicit_function_lead_selector_overrides_channel_default() -> None:
    module = _load_slack_module()
    base_handler = AsyncMock(return_value={"ok": True})
    module.base_handler = base_handler

    await module.handler(
        Input(
            thread_key="slack:C123:1.0",
            text="--vivian release-readiness check",
            delivery=Delivery.slack("engineering", "1.0", user_id="U123"),
        ),
        object(),
    )

    routed = base_handler.await_args.args[0]
    assert routed.persona == "vivian"
    assert routed.metadata["raava_routing_kind"] == "function_lead"
    assert routed.parts == [{"type": "text", "text": "release-readiness check"}]


@pytest.mark.asyncio
async def test_unknown_non_raava_flags_are_preserved() -> None:
    module = _load_slack_module()
    base_handler = AsyncMock(return_value={"ok": True})
    module.base_handler = base_handler

    await module.handler(
        Input(
            thread_key="slack:C123:1.0",
            text="--rpc-url https://example.test check this",
            delivery=Delivery.slack("random", "1.0", user_id="U123"),
        ),
        object(),
    )

    routed = base_handler.await_args.args[0]
    assert routed.persona is None
    assert routed.parts == [
        {"type": "text", "text": "--rpc-url https://example.test check this"}
    ]
