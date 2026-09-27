---
date: 2026-06-11
topic: raava-internal-centaur
---

# Raava Internal Centaur Requirements

## Summary

Raava Internal Centaur is a Slack-native executive function layer for the Raava team. The visible Slack personas are function leads, while specialist agents run as private subagents that managers spawn, supervise, and synthesize into one accountable Slack answer.

---

## Problem Frame

Raava already has a rich agent roster, but exposing every decomposed role in Slack would recreate the coordination problem the roster lean-down was meant to solve. People should not have to decide whether a question belongs to a pod engineer, QA specialist, advisor, or fleet operator before they can get useful help.

The useful product shape is closer to an executive staff layer. A person asks the right function lead in Slack, that lead handles context, decides whether deeper work is needed, delegates to specialists when useful, and returns a concise synthesized result with evidence.

Centaur is a strong fit because it already has Slack ingress, durable thread runtimes, persona overlays, workflow-spawned agent turns, and organization overlays. The product work is to package Raava's operating model into those primitives without forking the base platform.

---

## Key Decisions

- **Function leads are the Slack surface.** V1 exposes the manager layer, not the full historical roster, so Slack remains navigable and each answer has a clear owner.
- **Specialists are private execution capacity.** Decomposed roles such as pod engineers, QA engineers, and on-demand specialists are invoked by manager workflows, not selected directly by users in Slack.
- **Manager synthesis is required.** When a manager delegates, the Slack user still receives one synthesized answer from the manager persona, not a pile-on of raw specialist replies.
- **Raava behavior ships as an overlay.** Personas, skills, routing, and workflows belong in a Raava Centaur overlay rather than a bespoke fork of the base Centaur platform.
- **Workflow support is part of v1.** Delegation must be represented as product behavior with workflow support, not only as prompt text telling a manager to "ask subagents."

---

## Actors

- A1. Raava operator: A human teammate asking for help in Slack.
- A2. Function lead persona: A visible Slack persona that owns a domain, routes work, and returns the final answer.
- A3. Specialist subagent: A private, task-scoped persona spawned by a function lead for deeper work.
- A4. Centaur platform: The Slack runtime, workflow engine, persona loader, sandbox manager, and durable event store.
- A5. gbrain: The source of truth for Raava org roles, roster decisions, and institutional context.

---

## Requirements

**Persona Surface**

- R1. V1 must expose exactly these top-level Slack personas by default: Chief, Vera, Priya, Enoch, Elena, Heathcliffe, Vivian, and Argus.
- R2. Each top-level persona must define its domain, allowed channels, default output shape, escalation rules, and when it may spawn specialists.
- R3. Hana and Isaac must not be default top-level Slack personas in v1, but they may be available as manager-spawnable specialist managers under engineering or fleet operations flows.
- R4. Retired, demoted, advisory, pod-engineer, and QA-specialist roles must not appear as first-class Slack personas unless a later requirements pass explicitly promotes them.

**Slack Interaction**

- R5. A Slack user must interact with a function lead through explicit app mention, persona selector, or channel default.
- R6. Channel defaults must route common work to the right function lead without forcing users to memorize persona flags.
- R7. Users may request a different function lead when the requested persona is one of the approved v1 leads.
- R8. Slack output must identify the accountable function lead and summarize any specialist work without exposing unnecessary subagent chatter.

**Delegation**

- R9. A function lead must be able to spawn one or more specialist subagents for bounded subtasks when the task needs parallel research, adversarial review, implementation analysis, QA, or operations diagnosis.
- R10. Specialist subagents must receive a narrow brief, return evidence and recommendations to the function lead, and terminate or release their runtime after the task.
- R11. Function leads must synthesize specialist outputs into one answer that includes conclusions, evidence, open risks, and next actions.
- R12. Specialist outputs must be durable enough for debugging and audit, but they should not be the primary Slack UX.

**Grounding And Safety**

- R13. Function leads must query gbrain before making claims about Raava roles, org structure, prior decisions, client context, or operating rules.
- R14. Argus must use proposal-first behavior for fleet or runtime changes: read freely, propose changes with blast radius and rollback, and execute only when allowed by policy.
- R15. Vivian must remain an independent quality gate persona and should not report to the engineering persona in behavior or routing.
- R16. The system must preserve the current lean-roster decision unless gbrain or a human operator supplies a newer accepted org decision.

**Packaging**

- R17. Raava-specific personas, workflows, skills, prompts, and tool wrappers must live in a Raava overlay.
- R18. The base Centaur repo must remain reusable and boring; Raava-specific behavior should not require a base-platform fork.
- R19. The overlay must make loaded personas inspectable through Centaur's persona discovery surface.
- R20. Workflow-backed delegation must be testable without Slack by triggering an agent workflow directly.

---

## Key Flows

- F1. Ask a function lead in Slack
  - **Trigger:** A user mentions Centaur in a mapped Slack channel or selects an approved persona.
  - **Actors:** A1, A2, A4.
  - **Steps:** Centaur resolves the channel or selector to a function lead, starts or reuses the Slack thread runtime, injects the persona prompt, and runs the turn.
  - **Outcome:** The function lead answers in-thread with its domain-specific output shape.
  - **Covers:** R1, R2, R5, R6, R7.

- F2. Manager delegates to specialists
  - **Trigger:** A function lead determines the task needs deeper work than one direct answer.
  - **Actors:** A2, A3, A4, A5.
  - **Steps:** The lead queries gbrain for relevant context, starts specialist agent turns with bounded briefs, waits for results, and synthesizes them.
  - **Outcome:** Slack receives one manager-owned answer, with specialist findings compressed into evidence and next actions.
  - **Covers:** R8, R9, R10, R11, R12, R13.

- F3. Operations or fleet action proposal
  - **Trigger:** A user asks Argus or another operations lead to inspect runtime, fleet, or deployment state.
  - **Actors:** A1, A2, A3, A4, A5.
  - **Steps:** The lead gathers live evidence, optionally spawns diagnostics specialists, then returns a proposal with blast radius and rollback.
  - **Outcome:** The user gets an operator-grade recommendation without silent destructive action.
  - **Covers:** R11, R12, R13, R14.

---

## Acceptance Examples

- AE1. Covers R1, R4. Given a user asks for a decomposed role such as Darnell or Sol directly, when v1 handles the request, then Centaur routes through the relevant function lead or explains that direct specialist selection is not a v1 Slack surface.
- AE2. Covers R6, R8. Given a user asks an engineering question in an engineering-mapped Slack channel, when Centaur responds, then Enoch owns the reply even if Hana, Isaac, or other specialists contributed privately.
- AE3. Covers R9, R11. Given Priya receives a product-scope question that needs QA and engineering pressure tests, when she delegates, then the final Slack reply is Priya's synthesized decision with QA and engineering evidence summarized.
- AE4. Covers R13, R16. Given a user asks who the active Raava function leads are, when the persona answers, then it grounds the answer in gbrain and does not resurrect retired or demoted agents as active leads.
- AE5. Covers R14. Given a user asks Argus to fix a fleet issue, when Argus finds a likely repair, then the first response proposes the action with evidence, blast radius, and rollback rather than executing silently.

---

## Success Criteria

- A teammate can ask for product, engineering, design, QA, GTM, operations, or fleet help in Slack without choosing from more than the eight function leads.
- A manager can run at least one workflow-backed specialist delegation and return one synthesized Slack answer.
- The system can show which persona handled a turn and which workflow or specialist runs were created underneath it.
- gbrain-backed roster grounding prevents retired or demoted agents from appearing as active top-level personas.
- Raava-specific behavior is packaged in an overlay and can be inspected through the running Centaur persona and workflow discovery surfaces.

---

## Scope Boundaries

### Deferred For Later

- Promoting Hana, Isaac, or other specialist managers to first-class Slack personas.
- A full Slack UI for browsing every available specialist.
- Per-user persona permissions beyond the deployment's current Slack and Centaur auth model.
- Rich visual role cards or a dashboard persona browser.
- Autonomous unmentioned thread follow-ups, unless a later product pass changes Centaur's explicit-mention trigger model.

### Outside V1 Product Identity

- Recreating the full historical Slock roster in Slack.
- Letting every specialist reply directly into a user thread by default.
- Treating Centaur as a replacement for gbrain's canonical org knowledge.
- Forking Centaur base platform for Raava-specific behavior.

### Upstreaming Policy

- Base Centaur changes are only for reusable platform primitives.
- Raava personas, routing, skills, gbrain grounding, and manager delegation
  behavior stay in the Raava overlay.
- Publishing Raava overlay work means using a Raava-owned overlay repo or fork,
  not pushing Raava-specific branches to upstream `paradigmxyz/centaur`.
- Creating a dedicated external Raava overlay repo is a packaging decision after
  dogfood validation, not an upstream contribution path.

---

## Dependencies And Assumptions

- Centaur's existing Slack trigger path, persona loading, workflow engine, and agent-turn primitives remain available.
- A Raava overlay can package function-lead persona prompts, specialist prompts, workflow handlers, and skills.
- gbrain remains the canonical source for org roster, role authority, and prior decisions.
- The active roster decision from 2026-05-25 remains the accepted org baseline unless superseded.
- Some specialist behavior may be implemented first as workflow prompts and later hardened into stricter typed workflows.

---

## Sources And Research

- Centaur Slack persona selection currently parses persona flags and applies persona overlays in `services/api/api/workflows/slack_thread_turn.py`.
- Centaur workflows currently support child workflows, `ctx.start_agent`, and `ctx.run_agent` in `services/api/api/workflow_engine.py`.
- Centaur persona discovery currently loads `type = "persona"` entries and exposes `/personas` in `services/api/api/tool_manager.py`.
- Centaur overlay docs describe organization-specific tools, workflows, skills, personas, and sandbox prompt overlays in `docs/pages/extend/overlay.mdx`.
- gbrain source `decisions/2026-05-25-agent-roster-lean-down-and-restructure` establishes the lean roster and on-demand subagent direction.
- gbrain source `concepts/engineering/centaur-agent-platform` confirms the prior Raava Centaur POC and explicit Slack app mention behavior.
- gbrain source `companies/integrity-wealth-partners/foao-sow-2026-05-26` validates the pod-leader plus ephemeral-subagent operating model.
