---
status: draft
date: 2026-06-24
owner: Zay
type: requirements (brainstorm)
supersedes_framing_in: docs/plans/2026-06-23-001-feat-outreach-operator-persona-plan.md
---

# GTM Outreach — the Routine vs. Centaur (two-plane model)

## Why this doc exists

We repeatedly conflated the scheduled outreach **producer** with the conversational AI that **manages** it. This doc fixes the vocabulary and the boundary so it stops. It is the canonical model; anything that contradicts it is wrong, including earlier framing in the plan above.

## The model

> **Routine → [ Landing Zone in Slack ] ← Centaur (GTM persona)**

Two planes. One seam. They communicate **only** through the landing zone.

### Plane 1 — The Routine (a tool, not an agent)

- The outreach **signal generator + report generator**.
- Runs on a **schedule** (daily).
- Uses **LLM reasoning AND deterministic mechanisms** internally. That intelligence does **not** make it agentic.
- **Entire contract:** produce the report → drop it in the landing zone. Full stop.
- It does **not** approve, send, manage, converse, or wait on a human. It produces and exits.
- Today this is the `raava-outreach` worker (already structurally send-incapable).

### Plane 2 — Centaur (the internal AI interface)

- The AI the whole Raava team talks to in Slack — **all personas + every native primitive**.
- Is **aware of** the reports in the landing zone.
- Runs its **own daily workflow**: the **GTM persona** pre-reads each day's report and reasons about / manages it — *before* Zay looks. (Report lands ~9am; if Zay doesn't get to it until 12pm, the persona has already been working it.)
- **On demand:** "Centaur, latest on the GTM report" → reads the landing zone → briefs.
- **Approve / deny:** Zay decides; on approval the GTM persona acts.
- **Acts on the system itself** from Zay's direction:
  - "update the signals like this" → **files a ticket to an engineering pod, or updates the tool directly**.
  - "we need evals for this" → **builds them, with Zay**.

### The seam — the Landing Zone (a Slack surface)

- The routine **only writes** to it. Centaur **only reads** from it.
- Neither plane reaches into the other at runtime. The landing zone is the entire interface.
- It is a durable Slack surface (per the context/articles already shared — exact substrate to confirm in planning).

## Send authority (unchanged)

Send is a **Centaur / GTM-persona** capability, fired only on Zay's explicit approval. The routine is **send-incapable** by construction (no transport, no creds). This is the existing `outreach-send-authority-model`; the persona referred to there *is* the Centaur GTM persona.

## Scope boundaries — the anti-conflation rules (non-goals)

- The Routine is **never** called an agent or operator. Intelligence inside a scheduled deterministic tool ≠ agentic.
- The Routine does **not** send, approve, manage, or hold a conversation.
- Centaur does **not** drive the routine at runtime. It influences the routine only **out-of-band** — a ticket to an engineering pod, or a deliberate tool update — never by either side calling the other during a produce run.
- The Landing Zone is the **only** channel between the planes. No back-channel.
- Centaur is **never** called "the tool."

## Success criteria

- A report lands in the landing zone daily with **zero** agentic behavior from the producer.
- Asking Centaur "latest on the GTM report" returns a brief **sourced from the landing zone**.
- The GTM persona has **already reasoned over** today's report before Zay opens it.
- Approve/deny flows through Centaur; **nothing sends without Zay's approval**, and the routine *cannot* send.
- "Change the signals" → a ticket or a direct tool update results; "we need evals" → evals get built — both conversationally, via Centaur.
- The vocabulary holds: docs, code, and team all keep "routine/tool" and "Centaur/interface" distinct.

## Platform direction — use native Centaur primitives

"Use Centaur primitives properly" is a first-class requirement. A read-only architecture sweep (2026-06-24) found Centaur ships **native** support for each leg, and the current setup bolts on host scaffolding instead:

- **Schedule** → native scheduled-workflow primitive (replaces the host launchd produce job).
- **Centaur's daily awareness + the approve/deny "inbox"** → native durable workflow wait/resume (the persona's pre-read and Zay's verdict are native concepts, not custom queue/poll state).
- **Landing surface** → native durable Block Kit messages + the existing Slack interactivity endpoint. No Slack-canvas primitive exists (and isn't needed).

Direction: re-platform the schedule and the landing/approval surface onto native primitives; keep the worker's store as the **draft catalog** behind it. **Detailed design deferred to `/ce-plan`.**

## Open questions (for planning)

1. **Landing-zone substrate** — exact Slack surface (channel of Block Kit messages / thread / other), confirmed against the shared context.
2. **Persona routing (#14)** — routing is already half-dynamic (`--role` flags; `CHANNEL_DEFAULTS` is only the fallback). Options: (A) `/persona` switch + richer triggers [lowest cost], (B) per-thread persona pin [best UX, adds state — warrants an ADR], (C) intent classification [defer; non-deterministic, governance risk near send authority]. Lean A now, design B.
3. **Approval UX** — one-click approve a vetted draft vs. see-the-final-email-before-it-leaves.
4. **Producer scheduler substrate** — native Centaur scheduled workflow (preferred, "primitives proper") vs. keep host launchd for now.

## Where this stands today

Shipped: worker send-strip; discovery bridge (host HTTP, send-free); daily produce schedule (host launchd); Centaur image rolled out with the GTM persona; iron-proxy `8770` egress open + Connect re-synced (verified 2026-06-24). Blocked: 4 `Engineering`-vault items (`OPENAI_CODEX_ACCOUNT_ID` / `_BLOB` / `_CLIENT_ID` + `RAAVA_OUTREACH_HTTP_TOKEN`) — Zay to add; then the witnessed live-send safety smoke.
