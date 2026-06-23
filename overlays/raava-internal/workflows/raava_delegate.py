"""Workflow: function-lead delegation to private Raava specialists."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from pathlib import Path
import sys
from typing import Any

from api.runtime_control import ControlPlaneError
from api.workflow_engine import WorkflowContext


WORKFLOW_NAME = "raava_delegate"


@dataclass
class Input:
    manager_persona: str
    request: str
    specialists: list[dict[str, Any]] = field(default_factory=list)
    user_id: str | None = None
    thread_key: str | None = None


def _load_roles_module():
    path = Path(__file__).with_name("_raava_roles.py")
    spec = importlib.util.spec_from_file_location("raava_overlay_roles_for_delegate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Raava role registry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_roles = _load_roles_module()


def _agent_result_text(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    direct = result.get("result_text") or result.get("text")
    if isinstance(direct, str):
        return direct
    output = result.get("output_json")
    if isinstance(output, dict):
        nested = output.get("result_text")
        if isinstance(nested, str):
            return nested
        execution = output.get("execution")
        if isinstance(execution, dict) and isinstance(execution.get("result_text"), str):
            return execution["result_text"]
    return ""


def _brief_text(manager: str, request: str, specialist: dict[str, Any]) -> str:
    role = specialist["role"]
    brief = specialist.get("brief") or request
    return (
        f"You are a private Raava specialist supporting `{manager}`.\n"
        f"Specialist role: `{role}`.\n"
        f"User request: {request}\n\n"
        f"Bounded brief: {brief}\n\n"
        "Return evidence, recommendation, risks, and any open questions. "
        "Do not write to Slack directly."
    )


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    manager = _roles.normalize(inp.manager_persona)
    if not _roles.is_function_lead(manager):
        raise ControlPlaneError(
            "INVALID_RAAVA_MANAGER",
            "manager_persona must be an approved Raava function lead",
            422,
        )
    if not inp.request.strip():
        raise ControlPlaneError("INVALID_RAAVA_REQUEST", "request is required", 422)
    if not inp.specialists:
        raise ControlPlaneError(
            "INVALID_RAAVA_SPECIALISTS",
            "at least one specialist brief is required",
            422,
        )

    grounding = await ctx.call_tool(
        "raava_gbrain",
        "search_decisions",
        {"query": inp.request, "limit": 5},
    )

    specialist_results: list[dict[str, Any]] = []
    for index, specialist in enumerate(inp.specialists, start=1):
        role = _roles.normalize(str(specialist.get("role") or ""))
        role_info = _roles.resolve_role(role)
        if role_info is None or role_info["kind"] in (
            "function_lead",
            "standalone_persona",
        ):
            raise ControlPlaneError(
                "INVALID_RAAVA_SPECIALIST",
                f"specialist role must be private or redirected: {role}",
                422,
            )
        result = await ctx.run_agent(
            f"specialist_{index}_{role}",
            text=_brief_text(manager, inp.request, {**specialist, "role": role}),
            persona=role_info["persona"],
            thread_key=(
                f"raava-delegate:{ctx.run_id}:{index}"
                if not inp.thread_key
                else f"{inp.thread_key}:delegate:{index}"
            ),
            user_id=inp.user_id,
            metadata={
                "raava_manager_persona": manager,
                "raava_specialist_role": role,
                "private_specialist": True,
            },
            delivery={"platform": "dev"},
        )
        specialist_results.append(
            {
                "role": role,
                "owner": role_info["persona"],
                "status": result.get("status", "completed"),
                "result_text": _agent_result_text(result),
                "raw": result,
            }
        )

    failed = [item for item in specialist_results if item["status"] not in {"completed", "ok"}]
    return {
        "manager_persona": manager,
        "request": inp.request,
        "grounding": grounding,
        "specialist_results": specialist_results,
        "synthesis": {
            "owner": manager,
            "specialists_consulted": [item["role"] for item in specialist_results],
            "failed_specialists": [item["role"] for item in failed],
            "instruction": (
                "Synthesize these private findings into one manager-owned "
                "Slack answer with conclusions, evidence, risks, and next actions."
            ),
        },
    }
