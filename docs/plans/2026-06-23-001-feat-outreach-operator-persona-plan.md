---
title: "feat: Outreach operator persona on Centaur (raava-internal overlay)"
status: completed
date: 2026-06-23
type: feat
origin: ../../raava-outreach/docs/brainstorms/2026-06-23-outreach-operator-agent-requirements.md
---

# feat: Outreach operator persona on Centaur

> **⚠️ Architecture correction (2026-06-24) — supersedes Decision 1 ("the deterministic loop does … sending") and the KTD "send gated at the persona, not omitted."**
> A build harness ran the loop with live AgentMail creds and emitted 6 test emails to a real domain (all bounced — no prospect reached). Root cause: *send lived in the worker behind an in-band gate.* Corrected model: **the loop/worker and its Centaur `raava_outreach` tool hold NO send capability and NO email creds; "send" exists solely as a GTM-operator action, fired only on the human's explicit "go."** Daily cadence: routine drafts → report → GTM persona surfaces it in Slack → human go/no → GTM crafts+sends (go) or captures feedback to memory (no). Governing principle [brain: `concepts/reference/harness-engineering`] — no production writes from the worker; consequential/irreversible actions are policy-gated and owned by the accountable seat. Full model: brain `concepts/engineering/outreach-send-authority-model`.

**Target repo:** `centaur` — all work lands in `overlays/raava-internal/` (the existing Raava overlay; the role registry is already built). Wraps the `raava-outreach` CLI as a Centaur tool. Origin: the outreach-operator requirements doc (see origin).

## Summary

Ship **one working conversational outreach operator** as a Centaur persona on the existing `raava-internal` overlay. Zay @-mentions it in `#raava-outreach`; it answers from loop state, drives the loop's safe commands, and explains — **but it can never approve or send a cold email.** Sends stay solely behind Zay's ✅ reaction (the loop's existing `watch-slack-approvals` path). This is the vertical slice that proves the overlay (persona + tools + secrets + Slack routing); the general 8-lead roster + delegation workflows stay with the existing internal-Centaur plan.

## Problem Frame

The signal loop works and posts to Slack, but it's operated by hand — every `produce`, "why'd we skip X," and config tweak is a CLI command. Centaur is already partly stood up (`overlays/raava-internal/` registry done) and is Slack-native + has the iron-proxy secret boundary. Wiring an operator persona over the loop makes outreach *answerable* in the place Zay already lives — and proves the Centaur overlay with its first real persona.

## Requirements

- **R1** — A conversational `outreach-operator` persona, reachable by @mention in `#raava-outreach`, that owns the loop (origin Outcome).
- **R2** — The operator drives the loop's **safe** surface (produce / queue / triage / preflight / reject / re-score / explain) via a Centaur tool wrapping the `raava-outreach` CLI.
- **R3 (the locked send boundary)** — The operator **approves/sends only on Zay's explicit in-chat instruction** ("send #3", "send the top 2") — a typed instruction *is* the approval. It **never sends on its own initiative, never auto-sends, and never infers a send from ambiguous/casual messages**; it **echoes the specific draft(s) and confirms before sending.** The loop's own send guards (proof_cleared, bench, cap, suppression) remain the backstop. (Locked with Zay 2026-06-23; origin Decision 5 = sends stay human-initiated.)
- **R4** — **Supermemory** = the operator's working/conversational memory across sessions (origin Decision 3).
- **R5** — **gbrain** tool: read ICP / strategy / who-owns-what / roster; write learnings + outcomes (origin Decision 3).
- **R6** — Secrets resolve via Centaur's **iron-proxy** op-boundary; the agent never sees raw keys (origin Decision 3).
- **R7** — Reuse Centaur's native slackbot; the existing outbound `raava-outreach` bot keeps posting the loop's reports (origin Slack integration).

## Key Technical Decisions

- **Build on the existing `raava-internal` overlay**, don't re-plan it. The role registry (`overlays/raava-internal/workflows/_raava_roles.py`) is done; the persona/slack-routing/gbrain patterns are specified in `docs/plans/2026-06-11-001-feat-raava-internal-centaur-plan.md` but unbuilt. This plan builds the **outreach-operator vertical slice** of that.
- **The send path is gated by explicit instruction + confirmation, not omitted** (locked with Zay). The `raava_outreach` tool *does* expose `approve`/`send_approved`, but the **persona contract + a mandatory pre-send confirmation** gate them: the operator approves/sends **only** when Zay explicitly says so in chat, echoes which draft(s) first, and never infers a send. The loop's `send_approved` guards stay the backstop.
- **Supermemory as a Centaur tool** the persona calls (fits the `tools/<name>/client.py` model), not a bespoke harness memory. (Default — locking with Zay.)
- **A dedicated `outreach-operator` persona**, not an overload of an existing function-lead seat — its contract (own the loop, never send) is specific. (Default — locking with Zay.)
- **Channel-default routing:** `#raava-outreach` → `outreach-operator` via the registry's `CHANNEL_DEFAULTS` (+ a `slack_thread_turn` overlay shadow only if the channel-default seam isn't enough).
- **Centaur's own Slack app** (`overlays/raava-internal/deploy/slack-app-manifest.json`, `centaur.raava.dev/api/webhooks/slack`) is the *inbound* conversational surface — separate from the outbound `raava-outreach` posting bot. Two apps, two jobs.

## High-Level Technical Design

```mermaid
flowchart TB
  Zay["🧑 Zay in #raava-outreach"]
  subgraph CENTAUR["Centaur (local, raava-internal overlay)"]
    SB["slackbot → slack_thread_turn<br/>(#raava-outreach → outreach-operator)"]
    P["🧠 outreach-operator persona<br/>(PROMPT.md: own the loop, NEVER send)"]
    subgraph TOOLS["overlay tools (via iron-proxy)"]
      RO["raava_outreach tool<br/>produce · queue · triage · reject · explain<br/>approve/send ONLY on explicit 'send it' + confirm"]
      GB["raava_gbrain tool<br/>read ICP/strategy · write learnings"]
      SM["supermemory tool<br/>working memory"]
    end
    IP["iron-proxy<br/>op secret-boundary (raw keys never reach agent)"]
  end
  LOOP["raava-outreach loop CLI + state<br/>(queue / signals / send_approved)"]
  SLACKBOT["raava-outreach bot<br/>(posts morning report + hot-leads)"]

  Zay -->|@mention| SB --> P
  P --> RO --> LOOP
  P --> GB
  P --> SM
  RO -.secrets.-> IP
  GB -.secrets.-> IP
  SM -.secrets.-> IP
  LOOP --> SLACKBOT --> Zay
  Zay -->|✅ reaction OR explicit confirmed 'send it'| LOOP
```

The operator reaches the loop through `raava_outreach`. A send fires **only on Zay's explicit, confirmed instruction** (or his ✅ reaction) — never on the operator's own initiative.

## Output Structure

```
overlays/raava-internal/
  tools/
    raava_outreach/        # NEW — the loop's safe surface as a Centaur tool
      pyproject.toml       #   [tool.centaur] + secrets[]
      client.py            #   _client() → methods: produce/queue/triage/preflight/reject (NO approve/send)
    raava_gbrain/          # NEW — read ICP/strategy, write learnings
      pyproject.toml
      client.py
    raava_supermemory/     # NEW — operator working memory
      pyproject.toml
      client.py
    personas/
      outreach-operator/   # NEW — the persona
        pyproject.toml     #   type = "persona"
        PROMPT.md          #   own the loop; never approve/send
  workflows/
    _raava_roles.py        # MODIFY — add #raava-outreach channel default → outreach-operator
```

---

## Implementation Units

### U1. `raava_outreach` Centaur tool — the safe loop surface (the safety seam)

**Goal:** Wrap the `raava-outreach` CLI as a Centaur tool exposing reads + produce + safe-writes **plus** `approve`/`send_approved` — the send path gated at the *persona* layer (explicit instruction + confirmation), not omitted.
**Requirements:** R2, R3.
**Dependencies:** none.
**Files:** `overlays/raava-internal/tools/raava_outreach/pyproject.toml`, `overlays/raava-internal/tools/raava_outreach/client.py`, `overlays/raava-internal/tools/raava_outreach/test_client.py`.
**Approach:** `client.py` `_client()` → `RaavaOutreachClient` whose public methods shell out to (or import) the `raava-outreach` CLI: `produce(dry_run=True)`, `queue(status, track)`, `triage()`, `preflight()`, `curation_audit()`, `reject(entry_id)`, **`approve(entry_id)`**, and **`send_approved(cap=None)`** (the loop's gated send — its proof_cleared/bench/cap/suppression guards still apply). The tool faithfully exposes the CLI; the *never-send-unless-explicitly-told* behavior lives in the persona contract (U2), not by omitting the method. `pyproject.toml` `[tool.centaur]` declares the loop's config keys in `secrets[]` (resolved by iron-proxy). Methods return the CLI's JSON output.
**Patterns to follow:** `tools/business/attio/{pyproject.toml,client.py}`; the SDK `secret()` contract; `raava-outreach` `cli.py` command signatures.
**Execution note:** the send-gating behavior is tested at the persona + smoke layer (U2/U7); the tool layer tests that the loop's guards still bind.
**Test scenarios:**
- Happy: `queue(status="pending")` returns the parsed queue; `produce()` returns the campaign result; `approve(id)`/`send_approved()` invoke the matching CLI commands and surface the result + blocker list (CLI mocked).
- **Integration (mandatory):** `send_approved` still honors the loop's guards — a non-proof-cleared / suppressed / over-cap entry is **blocked** and the tool cannot override it.
- Edge: a CLI error surfaces as a tool error, not a crash.
**Verification:** the tool drives the full loop surface including a guarded send; the *never-without-explicit-instruction* rule is enforced in U2/U7.

### U2. `outreach-operator` persona

**Goal:** Define the persona — owns the loop, answers from state, drives the safe tool, and **never approves/sends** (surfaces drafts for the human ✅).
**Requirements:** R1, R3.
**Dependencies:** U1.
**Files:** `overlays/raava-internal/tools/personas/outreach-operator/pyproject.toml`, `overlays/raava-internal/tools/personas/outreach-operator/PROMPT.md`, `overlays/raava-internal/tools/personas/outreach-operator/test_persona_loads.py`.
**Approach:** `pyproject.toml` `[tool.centaur] type = "persona"` + `prompt = "PROMPT.md"`. `PROMPT.md` is the operating contract: what it owns (produce, queue triage, re-score, explain the judge, pause/resume via config, answer "how'd we do / why skip X"), its tools (`raava_outreach`, `raava_gbrain`, `supermemory`), and the **send rule: it approves/sends ONLY on Zay's explicit instruction ("send #3"); it first echoes exactly which draft(s) it will send and waits for go; it never infers a send from ambiguous text; it never sends on its own initiative.**
**Patterns to follow:** `tools/personas/eng/{pyproject.toml,PROMPT.md}`; the `_load_persona` discovery path.
**Test scenarios:**
- Persona loads via the overlay `TOOL_DIRS`; `get_persona("outreach-operator")` returns it.
- `Test expectation: PROMPT.md content` — assert the prompt encodes the explicit-instruction-only + confirm-before-send rule (string check).
**Verification:** the persona is discoverable and its contract forbids sending.

### U3. `raava_gbrain` tool — read company brain, write learnings

**Goal:** Give the operator gbrain access: read ICP / strategy / who-owns-what / roster; write learnings + outcomes.
**Requirements:** R5.
**Dependencies:** none.
**Files:** `overlays/raava-internal/tools/raava_gbrain/pyproject.toml`, `overlays/raava-internal/tools/raava_gbrain/client.py`, `overlays/raava-internal/tools/raava_gbrain/test_client.py`.
**Approach:** `client.py` methods `lookup(query)` / `read_page(slug)` / `write_learning(payload)` against the hosted gbrain (the existing public+OAuth-gated MCP/HTTP surface). `pyproject.toml` declares an `http`-type secret host-scoped to the gbrain endpoint (iron-proxy injects). Reads are unconditional; a write goes through gbrain's normal write path.
**Patterns to follow:** `tools/business/attio/client.py` (HTTP tool + `http` secret); the existing plan's U4 sketch.
**Test scenarios:**
- Happy: `lookup("ICP")` returns a result; `write_learning(...)` posts the expected payload (HTTP mocked).
- Degrade: missing gbrain secret → tool reports unavailable, no crash.
**Verification:** the operator can ground answers in gbrain and record outcomes.

### U4. `supermemory` tool — operator working memory

**Goal:** Persist the operator's working/conversational memory across sessions (decisions, preferences, what was discussed).
**Requirements:** R4.
**Dependencies:** none.
**Files:** `overlays/raava-internal/tools/raava_supermemory/pyproject.toml`, `overlays/raava-internal/tools/raava_supermemory/client.py`, `overlays/raava-internal/tools/raava_supermemory/test_client.py`.
**Approach:** `client.py` methods `remember(text, tags)` / `recall(query)` against the Supermemory API. `http`-type secret (Supermemory key) via iron-proxy. The persona prompt (U2) instructs when to remember/recall.
**Patterns to follow:** the HTTP-tool pattern (`tools/business/attio`).
**Test scenarios:**
- Happy: `remember(...)` posts; `recall(q)` returns hits (HTTP mocked).
- Degrade: missing key → no-op, no crash.
**Verification:** the operator recalls prior-session context.

### U5. Slack routing — `#raava-outreach` → `outreach-operator`

**Goal:** Route inbound @mentions in `#raava-outreach` to the operator persona.
**Requirements:** R1, R7.
**Dependencies:** U2.
**Files:** `overlays/raava-internal/workflows/_raava_roles.py` (modify `CHANNEL_DEFAULTS`), and — only if channel-default resolution is insufficient — `overlays/raava-internal/workflows/slack_thread_turn.py` (overlay shadow). Plus `overlays/raava-internal/tools/personas/outreach-operator/` registration if the registry requires it. Test: `overlays/raava-internal/workflows/test_channel_routing.py`.
**Approach:** add `#raava-outreach → outreach-operator` to `CHANNEL_DEFAULTS` (+ the `RAAVA_CENTAUR_CHANNEL_DEFAULTS` env override). Confirm `slack_thread_turn` resolves the channel default when no explicit `--persona` selector is given. Reuse Centaur's slack app manifest (no new app); the outbound `raava-outreach` bot is untouched.
**Patterns to follow:** `_raava_roles.py` `default_persona_for_channel()` + `CHANNEL_DEFAULTS`; base `slack_thread_turn.py` selection logic.
**Test scenarios:**
- Happy: a mention in `#raava-outreach` with no selector resolves to `outreach-operator`.
- Edge: an explicit `--{other-persona}` selector still overrides the channel default.
**Verification:** @mention in `#raava-outreach` reaches the operator.

### U6. Secrets + iron-proxy declarations + bootstrap prereqs

**Goal:** Declare every tool's secrets so iron-proxy resolves them; document the op bootstrap.
**Requirements:** R6.
**Dependencies:** U1, U3, U4.
**Files:** the three tools' `pyproject.toml` `secrets[]` blocks (raava-outreach config, gbrain endpoint, supermemory key), `overlays/raava-internal/README.md` (bootstrap notes).
**Approach:** each tool declares its credential(s) with the right `type` (`http` for gbrain/supermemory, env/`pg_dsn`/config for the loop) + `secret_ref` (`op://vault/...`) + host allowlist. Document the **prereq: `OP_SERVICE_ACCOUNT_TOKEN` + `OP_VAULT` are not yet on this host** — they must be created before the overlay runs.
**Patterns to follow:** `tools/business/attio/pyproject.toml` secret block; `tool_manager.py` secret schema.
**Test scenarios:**
- Each tool's secret declaration validates against the secret schema (load via `ToolManager`, mocked).
- `Test expectation: none` for README — covered by tool-load validation.
**Verification:** tools load with valid secret declarations; the op bootstrap is documented as a go-live prereq.

### U7. Local verification + smoke

**Goal:** Prove the operator works locally end to end — and prove it *can't* send.
**Requirements:** R1, R2, R3.
**Dependencies:** U1–U6.
**Files:** `overlays/raava-internal/README.md` (local-run + smoke steps), `overlays/raava-internal/tools/raava_outreach/test_client.py` (the safety-seam assertion, if not in U1).
**Approach:** `TOOL_DIRS` includes the overlay; bring Centaur up locally (per the `2026-06-11` plan's deploy + the `mac-mini-setup` path). Smoke: @mention the operator in `#raava-outreach` → it answers from `queue`; "run produce" → drafts post via the loop; "send #3" → it **echoes "sending draft to <recipient> — go?" then sends** via the guarded path; a casual/ambiguous message triggers **no** send. Confirm the loop's guards still bind.
**Patterns to follow:** the existing plan's U6 local-verification docs; Slack-CLI smoke.
**Test scenarios:**
- Smoke: operator answers a `queue` question from real state.
- Smoke: operator runs `produce` (dry-run) and reports the result.
- **Safety smoke (mandatory):** an explicit "send #3" sends exactly that one after a confirm; an ambiguous message ("looks good") sends nothing; the operator never sends unprompted.
**Verification:** a working conversational operator in `#raava-outreach` that drives the loop and sends only on an explicit, confirmed instruction.

---

## Scope Boundaries

**In scope:** the outreach-operator persona + the three tools (raava_outreach safe-surface, gbrain, supermemory) + `#raava-outreach` routing + secret declarations + local smoke — on the existing `raava-internal` overlay.

**Deferred to Follow-Up Work:** the general 8-function-lead roster personas + the manager-delegation workflows (the existing `2026-06-11` internal-Centaur plan owns these); durable **workflows** for the operator (e.g. a scheduled "morning briefing" workflow) — the v1 operator is conversational tool-calling; the cloud/k3s production deploy (local first); motions #2/#3.

**Outside this product's identity:** an operator that sends **autonomously** (without an explicit instruction) or auto-sends on a schedule; an operator that edits the loop's *code* (it operates the loop, doesn't rewrite it).

## Open Questions (lock with Zay)

- *(Send boundary — **LOCKED** 2026-06-23: approve/send only on Zay's explicit in-chat instruction + confirm-before-send; see R3.)*
- **Supermemory** as a tool (default) vs. the agent harness's built-in memory.
- **Persona** dedicated (default) vs. mapped onto an existing function-lead seat.
- **op bootstrap:** `OP_SERVICE_ACCOUNT_TOKEN` + `OP_VAULT` need creating (not on host).

## Risks & Dependencies

- **Operator sends on a misread/ambiguous instruction** → the mandatory echo-and-confirm-before-send step (U2) + the loop's `send_approved` guards (proof_cleared, bench, cap, suppression) are the mitigation; it never auto-sends.
- **Centaur not yet running locally** → depends on the `2026-06-11` deploy path (k3s + Postgres + 4 services) being stood up; flagged as a prereq.
- **Dependency:** the existing `raava-internal` overlay + registry; the `raava-outreach` loop CLI (merged to `main`); Centaur's tool/persona/iron-proxy model; an op service-account token.

## Sources & Research

- Origin requirements: `../../raava-outreach/docs/brainstorms/2026-06-23-outreach-operator-agent-requirements.md`.
- Existing internal plan (head start, partly built): `docs/plans/2026-06-11-001-feat-raava-internal-centaur-plan.md` (registry done; personas/slack/gbrain/delegation unbuilt).
- Centaur contracts (this session's recon): tool-plugin (`tools.toml`, `services/api/api/tool_manager.py`, `docs/pages/extend/tools.mdx`), persona (`tools/personas/eng/`), overlay (`docs/pages/extend/overlay.mdx`), slack routing (`services/api/api/workflows/slack_thread_turn.py`, `overlays/raava-internal/workflows/_raava_roles.py`), iron-proxy secrets (`tool_manager.py:78-240`), workflows (`services/api/api/workflow_engine.py`).
- raava-outreach CLI surface: `../../raava-outreach/src/raava_outreach/cli.py`; send gate `src/raava_outreach/send.py:39-55`.
