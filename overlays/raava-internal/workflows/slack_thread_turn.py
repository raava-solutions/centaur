"""Raava Slack thread workflow with function-lead routing."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any

from api.workflows.slack_thread_turn import Input, handler as base_handler


WORKFLOW_NAME = "slack_thread_turn"

_FLAG_RE = re.compile(
    r"(^|\s)(`?)(--|[\u2013\u2014])([a-z][a-z0-9-]*)(?=\s|`|$)",
    re.IGNORECASE,
)


def _load_roles_module():
    path = Path(__file__).with_name("_raava_roles.py")
    spec = importlib.util.spec_from_file_location("raava_overlay_roles_for_slack", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Raava role registry")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_roles = _load_roles_module()


def _strip_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    cleaned = text
    for start, end in sorted(ranges, reverse=True):
        cleaned = f"{cleaned[:start]} {cleaned[end:]}"
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_raava_role_flag(text: str) -> tuple[dict[str, str] | None, str]:
    ranges: list[tuple[int, int]] = []
    selected: dict[str, str] | None = None
    for match in _FLAG_RE.finditer(text):
        leading = match.group(1) or ""
        opening_tick = match.group(2) or ""
        marker = match.group(3) or ""
        flag = match.group(4).lower()
        resolved = _roles.resolve_role(flag)
        if resolved is None:
            continue
        flag_start = match.start() + len(leading) + len(opening_tick)
        flag_end = flag_start + len(marker) + len(flag)
        strip_start = flag_start - len(opening_tick) if opening_tick else flag_start
        strip_end = flag_end + 1 if flag_end < len(text) and text[flag_end] == "`" else flag_end
        ranges.append((strip_start, strip_end))
        selected = resolved
    return selected, _strip_ranges(text, ranges) if ranges else text.strip()


def _extract_raava_selection(
    parts: list[dict[str, Any]],
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    selected: dict[str, str] | None = None
    cleaned_parts: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") != "text" or not isinstance(part.get("text"), str):
            cleaned_parts.append(part)
            continue
        part_selected, cleaned = _extract_raava_role_flag(part["text"])
        if part_selected is not None:
            selected = part_selected
        if cleaned:
            cleaned_parts.append({**part, "text": cleaned})
    return selected, cleaned_parts or parts


def _channel_candidates(inp: Input) -> list[str | None]:
    metadata = inp.metadata if isinstance(inp.metadata, dict) else {}
    delivery = inp.delivery
    return [
        getattr(delivery, "channel", None),
        getattr(delivery, "channel_id", None),
        metadata.get("channel_name"),
        metadata.get("channel"),
        metadata.get("channel_id"),
    ]


def _routing_note(selection: dict[str, str]) -> str | None:
    kind = selection.get("kind")
    requested = selection.get("requested")
    persona = selection.get("persona")
    if kind == "function_lead" or requested == persona:
        return None
    return (
        f"Raava routing note: `{requested}` is not a top-level Slack persona in "
        f"v1. Route this through `{persona}` as the accountable function lead."
    )


async def handler(inp: Input, ctx) -> dict[str, Any]:
    persona = inp.persona
    parts = inp.effective_parts
    metadata = dict(inp.metadata or {})

    explicit = _roles.resolve_role(persona)
    if explicit is not None:
        persona = explicit["persona"]
        metadata["raava_requested_persona"] = explicit["requested"]
        metadata["raava_routing_kind"] = explicit["kind"]
    else:
        selected, parts = _extract_raava_selection(parts)
        if selected is not None:
            persona = selected["persona"]
            metadata["raava_requested_persona"] = selected["requested"]
            metadata["raava_routing_kind"] = selected["kind"]
            note = _routing_note(selected)
            if note:
                parts = [{"type": "text", "text": note}, *parts]
        elif not persona:
            default_persona = _roles.default_persona_for_channel(*_channel_candidates(inp))
            if default_persona:
                persona = default_persona
                metadata["raava_routing_kind"] = "channel_default"

    if persona:
        metadata["raava_function_lead"] = persona

    routed = replace(inp, parts=parts, persona=persona, metadata=metadata)
    return await base_handler(routed, ctx)
