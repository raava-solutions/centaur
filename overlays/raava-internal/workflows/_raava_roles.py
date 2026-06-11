"""Raava role registry for the internal Centaur overlay."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re


@dataclass(frozen=True)
class FunctionLead:
    name: str
    title: str
    domain: str
    channels: tuple[str, ...]
    output_shape: str
    escalation: str
    specialist_rules: str


@dataclass(frozen=True)
class Specialist:
    name: str
    title: str
    owner: str
    use_when: str


FUNCTION_LEADS: dict[str, FunctionLead] = {
    "chief": FunctionLead(
        name="chief",
        title="Chief",
        domain="executive synthesis, priority calls, and cross-functional routing",
        channels=("leadership", "exec", "raava"),
        output_shape="decision, rationale, risks, owner, next action",
        escalation="Escalate unresolved domain conflicts to the human operator.",
        specialist_rules="Spawn function leads for domain pressure tests.",
    ),
    "vera": FunctionLead(
        name="vera",
        title="Vera",
        domain="design systems, interface quality, and product experience",
        channels=("design", "product-design", "brand"),
        output_shape="design readout, tradeoffs, recommendation, next artifact",
        escalation="Escalate product-priority conflicts to Priya or Chief.",
        specialist_rules="Spawn design or research specialists for critique and options.",
    ),
    "priya": FunctionLead(
        name="priya",
        title="Priya",
        domain="product strategy, requirements, roadmap shape, and user value",
        channels=("product", "pm", "roadmap"),
        output_shape="product decision, user impact, evidence, risks, next step",
        escalation="Escalate technical feasibility to Enoch and quality gates to Vivian.",
        specialist_rules="Spawn engineering, QA, and research specialists for pressure tests.",
    ),
    "enoch": FunctionLead(
        name="enoch",
        title="Enoch",
        domain="engineering architecture, implementation, debugging, and code quality",
        channels=("engineering", "eng", "dev", "backend", "frontend"),
        output_shape="technical finding, evidence, implementation path, validation",
        escalation="Escalate fleet/runtime operations to Argus.",
        specialist_rules="Spawn Hana, Isaac, or bounded engineering specialists.",
    ),
    "elena": FunctionLead(
        name="elena",
        title="Elena",
        domain="go-to-market, customer narrative, positioning, and comms",
        channels=("gtm", "sales", "marketing", "comms"),
        output_shape="audience, message, proof, objection, next action",
        escalation="Escalate product-truth gaps to Priya.",
        specialist_rules="Spawn research or customer-context specialists.",
    ),
    "heathcliffe": FunctionLead(
        name="heathcliffe",
        title="Heathcliffe",
        domain="finance, operations planning, and business analysis",
        channels=("finance", "ops", "bizops"),
        output_shape="financial readout, assumptions, sensitivity, recommendation",
        escalation="Escalate runtime execution to Argus and product tradeoffs to Priya.",
        specialist_rules="Spawn analysis specialists for model checks.",
    ),
    "vivian": FunctionLead(
        name="vivian",
        title="Vivian",
        domain="independent QA, release gates, risk review, and adversarial validation",
        channels=("qa", "quality", "release", "review"),
        output_shape="quality verdict, evidence, blockers, residual risk",
        escalation="Escalate unresolved release-risk ownership to Chief.",
        specialist_rules="Spawn QA specialists without reporting through engineering.",
    ),
    "argus": FunctionLead(
        name="argus",
        title="Argus",
        domain="fleet operations, runtime state, incidents, deploys, and infrastructure",
        channels=("fleet", "infra", "incident", "runtime", "deploys"),
        output_shape="state, evidence, proposal, blast radius, rollback",
        escalation="Escalate destructive or high-blast-radius changes to a human operator.",
        specialist_rules="Spawn diagnostics specialists before proposing changes.",
    ),
}

SPECIALISTS: dict[str, Specialist] = {
    "hana": Specialist(
        name="hana",
        title="Hana Park",
        owner="enoch",
        use_when="frontend, UX implementation, and product-engineering detail work",
    ),
    "isaac": Specialist(
        name="isaac",
        title="Isaac Delgado",
        owner="argus",
        use_when="fleet, runtime, and operations diagnostics",
    ),
}

ROLE_REDIRECTS: dict[str, str] = {
    "darnell": "enoch",
    "alice": "priya",
    "lumen": "vera",
    "sol": "vivian",
    "qa": "vivian",
    "pod-engineer": "enoch",
    "engineer": "enoch",
    "designer": "vera",
    "ops": "argus",
}

CHANNEL_DEFAULTS: dict[str, str] = {
    "leadership": "chief",
    "exec": "chief",
    "design": "vera",
    "product-design": "vera",
    "product": "priya",
    "pm": "priya",
    "engineering": "enoch",
    "eng": "enoch",
    "dev": "enoch",
    "gtm": "elena",
    "sales": "elena",
    "marketing": "elena",
    "finance": "heathcliffe",
    "bizops": "heathcliffe",
    "qa": "vivian",
    "quality": "vivian",
    "release": "vivian",
    "fleet": "argus",
    "infra": "argus",
    "incident": "argus",
    "runtime": "argus",
    "deploys": "argus",
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_CHANNEL_DEFAULTS_ENV = "RAAVA_CENTAUR_CHANNEL_DEFAULTS"


def normalize(value: str | None) -> str:
    return _NORMALIZE_RE.sub("-", (value or "").strip().lower()).strip("-")


def function_lead_names() -> tuple[str, ...]:
    return tuple(FUNCTION_LEADS)


def private_specialist_names() -> tuple[str, ...]:
    return tuple(SPECIALISTS)


def is_function_lead(value: str | None) -> bool:
    return normalize(value) in FUNCTION_LEADS


def is_private_specialist(value: str | None) -> bool:
    return normalize(value) in SPECIALISTS


def resolve_role(value: str | None) -> dict[str, str] | None:
    role = normalize(value)
    if not role:
        return None
    if role in FUNCTION_LEADS:
        return {"requested": role, "persona": role, "kind": "function_lead"}
    if role in SPECIALISTS:
        return {
            "requested": role,
            "persona": SPECIALISTS[role].owner,
            "kind": "private_specialist",
        }
    if role in ROLE_REDIRECTS:
        return {
            "requested": role,
            "persona": ROLE_REDIRECTS[role],
            "kind": "redirected_role",
        }
    return None


def default_persona_for_channel(*candidates: str | None) -> str | None:
    defaults = channel_defaults()
    for candidate in candidates:
        channel = normalize(candidate)
        if not channel:
            continue
        if channel in defaults:
            return defaults[channel]
    return None


def channel_defaults() -> dict[str, str]:
    defaults = dict(CHANNEL_DEFAULTS)
    defaults.update(_configured_channel_defaults())
    return defaults


def _configured_channel_defaults() -> dict[str, str]:
    raw = os.getenv(_CHANNEL_DEFAULTS_ENV, "").strip()
    if not raw:
        return {}
    parsed: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            pairs = data.items()
        else:
            return {}
    else:
        pairs = (
            item.split("=", 1)
            for item in raw.split(",")
            if item.strip() and "=" in item
        )
    for channel, persona in pairs:
        normalized_channel = normalize(str(channel))
        normalized_persona = normalize(str(persona))
        if normalized_channel and normalized_persona in FUNCTION_LEADS:
            parsed[normalized_channel] = normalized_persona
    return parsed


def public_roster() -> list[dict[str, str]]:
    return [
        {
            "name": lead.name,
            "title": lead.title,
            "domain": lead.domain,
            "output_shape": lead.output_shape,
        }
        for lead in FUNCTION_LEADS.values()
    ]


def role_record(value: str) -> dict[str, str] | None:
    role = normalize(value)
    if role in FUNCTION_LEADS:
        lead = FUNCTION_LEADS[role]
        return {
            "name": lead.name,
            "title": lead.title,
            "kind": "function_lead",
            "owner": lead.name,
            "domain": lead.domain,
        }
    if role in SPECIALISTS:
        specialist = SPECIALISTS[role]
        return {
            "name": specialist.name,
            "title": specialist.title,
            "kind": "private_specialist",
            "owner": specialist.owner,
            "domain": specialist.use_when,
        }
    resolved = resolve_role(role)
    if resolved:
        return {
            "name": role,
            "title": role,
            "kind": resolved["kind"],
            "owner": resolved["persona"],
            "domain": "redirected to accountable function lead",
        }
    return None
