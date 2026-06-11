from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from api.runtime_control import ControlPlaneError


REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_ROOT = REPO_ROOT / "overlays" / "raava-internal"


def _load_delegate_module():
    name = "test_raava_delegate_workflow"
    path = OVERLAY_ROOT / "workflows" / "raava_delegate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _StubCtx:
    run_id = "wfr_test"

    def __init__(self, agent_result: dict | None = None):
        self.tool_calls: list[tuple[str, str, dict]] = []
        self.agent_calls: list[dict] = []
        self.agent_result = agent_result or {
            "status": "completed",
            "result_text": "Evidence: viable. Risk: test coverage.",
        }

    async def call_tool(self, tool: str, method: str, args: dict):
        self.tool_calls.append((tool, method, args))
        return {"results": [{"path": "decisions/2026-05-25-agent-roster-lean-down-and-restructure"}]}

    async def run_agent(self, name: str, **kwargs):
        self.agent_calls.append({"name": name, **kwargs})
        return self.agent_result


@pytest.mark.asyncio
async def test_manager_delegation_runs_private_specialist_turns() -> None:
    module = _load_delegate_module()
    ctx = _StubCtx()

    result = await module.handler(
        module.Input(
            manager_persona="priya",
            request="Should we ship the Raava Centaur overlay?",
            specialists=[
                {"role": "hana", "brief": "Check product UX risk."},
                {"role": "qa", "brief": "Check release risk."},
            ],
            user_id="U123",
        ),
        ctx,
    )

    assert ctx.tool_calls == [
        (
            "raava_gbrain",
            "search_decisions",
            {"query": "Should we ship the Raava Centaur overlay?", "limit": 5},
        )
    ]
    assert [call["persona"] for call in ctx.agent_calls] == ["enoch", "vivian"]
    assert result["manager_persona"] == "priya"
    assert result["synthesis"]["owner"] == "priya"
    assert result["synthesis"]["specialists_consulted"] == ["hana", "qa"]
    assert result["specialist_results"][0]["result_text"].startswith("Evidence")


@pytest.mark.asyncio
async def test_delegation_rejects_private_specialist_as_manager() -> None:
    module = _load_delegate_module()

    with pytest.raises(ControlPlaneError, match="manager_persona"):
        await module.handler(
            module.Input(
                manager_persona="hana",
                request="Review this",
                specialists=[{"role": "qa", "brief": "Check it"}],
            ),
            _StubCtx(),
        )


@pytest.mark.asyncio
async def test_delegation_rejects_function_lead_as_specialist() -> None:
    module = _load_delegate_module()

    with pytest.raises(ControlPlaneError, match="specialist role"):
        await module.handler(
            module.Input(
                manager_persona="priya",
                request="Review this",
                specialists=[{"role": "enoch", "brief": "Check it"}],
            ),
            _StubCtx(),
        )


@pytest.mark.asyncio
async def test_delegation_surfaces_specialist_failures() -> None:
    module = _load_delegate_module()
    ctx = _StubCtx({"status": "failed", "error_text": "boom"})

    result = await module.handler(
        module.Input(
            manager_persona="priya",
            request="Review this",
            specialists=[{"role": "hana", "brief": "Check it"}],
        ),
        ctx,
    )

    assert result["specialist_results"][0]["status"] == "failed"
    assert result["synthesis"]["failed_specialists"] == ["hana"]
