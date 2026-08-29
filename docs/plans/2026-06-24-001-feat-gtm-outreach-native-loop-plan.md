---
status: active
date: 2026-06-24
type: feat
origin: docs/brainstorms/2026-06-24-gtm-routine-vs-centaur-requirements.md
target_repos: [centaur, raava-outreach]
reviewed: 2026-06-24 (cto architecture + security-review send-safety; findings folded)
---

# feat: GTM outreach — native routine → landing zone → Centaur loop (v1)

Implements the two-plane model from the origin doc on Centaur's native primitives. **Two repos:** `centaur` (this doc's home — workflow, persona, slackbot, send tool) and `raava-outreach` (the routine/worker — bridge, produce-only strip). Each unit tags its repo; paths are repo-relative to the tagged repo.

## Summary

A non-agentic scheduled **routine** produces the outreach report into a Slack **landing zone**; **Centaur** (the GTM persona) reads it, briefs Zay, takes conversational approve/deny, and sends. v1 re-platforms the daily schedule onto a native Centaur scheduled workflow, retires the worker's vestigial reaction-approval loop, and — critically — **re-establishes the send gate as deterministic code inside `outreach_send` (the single send funnel)**, not prose in the persona prompt. Interactive Block Kit buttons + suspend/resume "inbox" are deferred to v2.

> **Why the send-gate emphasis:** the post-incident rebuild made the *worker* structurally send-incapable. The first draft of this plan let the Centaur-side gate be model-interpreted prose — which an adversarial send-safety review flagged as a P0 regression from the old loop's deterministic per-user allow-list. v1 restores hard gates inside the one tool all sends funnel through.

---

## Problem Frame

We conflated the scheduled producer with the conversational AI that manages it; the worker carries leftover approval/posting machinery; and the send gate must be structural, not prose. The origin doc locked the separation: **Routine → [Landing Zone in Slack] ← Centaur (GTM persona)**. This plan makes the code match the model, uses Centaur's native scheduler + Block Kit primitives, and re-hardens the send path.

**Verified current state (2026-06-24):**
- The **bridge itself posts the landing zone today**: `http_service.py` `produce_discovery()` calls `post_morning_list` whenever `dry_run` is true, and `_produce` defaults `dry_run=True`. `post_morning_list` is also called from `cli.py`'s `produce` command. So "stop the routine posting" is a rewrite of the produce path + CLI, not just a file deletion. `[raava-outreach]`
- The worker still runs a Slack ✅-reaction approval poller (`raava_loop/approvals_slack.py`) with a real **per-user approver allow-list** (`_approver_user_ids`, refuses if empty) → `queue.approve()`. U1 deletes this; **v1 must replace the allow-list with an equivalent hard gate on the Centaur side, not prose.** `[raava-outreach]`
- Email send is already stripped from the worker (no `send_approved`, no transports; `tests/test_no_send.py` green) — the worker is structurally send-incapable. `[raava-outreach]`
- `outreach_send` (the Centaur send tool) transmits on call the moment `AGENTMAIL_API_KEY` is present — **no confirm token, no approver check, no idempotency key, no cap, no go-live gate**. The entire gate is prose in `PROMPT.md`. `[centaur]`
- Centaur has **no per-persona tool-belt manifest** and `agent_turn` has no tool-scoping parameter — so a scheduled GTM-persona turn carries the same belt (incl. `outreach_send`) as an interactive one. `[centaur]`
- Native primitives exist to copy: `workflows/paradigm_pulse_daily.py` (`CRON`+`SLACK_CHANNEL`+`handler(inp, ctx)`, posts via `ctx.call_tool("slack","send_message")`); the bridge exposes only **summary** queue rows. `[centaur]`

---

## Requirements

Carried from the origin doc; R8–R10 added from the send-safety review.

- **R1** The routine produces the report to its store only — no Slack posting, no approval, no send. Send-incapable by construction.
- **R2** A native Centaur scheduled workflow runs the routine daily and publishes the report as the Slack landing zone.
- **R3** The GTM persona reads the landing zone (on demand + a daily pre-read) and briefs Zay.
- **R4** Approve/deny is conversational; on "go" the persona crafts the final email, echoes the **freshly-fetched** draft, confirms, then sends; on "no" it captures feedback. Nothing sends without Zay's confirm.
- **R5** Send lives only in Centaur, behind a single tool (`outreach_send`). The routine cannot send.
- **R6** Persona selection is an explicit dynamic switch (`/persona`); `CHANNEL_DEFAULTS` stays as fallback. (task #14)
- **R7** Vocabulary holds: routine = tool, Centaur = interface, landing zone = the seam.
- **R8** The send gate is **deterministic code inside `outreach_send`**, not prose: a staged confirm-token bound to the exact echoed bytes, single-use (idempotency), TTL-bounded; plus a daily/per-recipient cap and a go-live arming precondition (`OUTREACH_LIVE_SEND_ENABLED` + `CAN_SPAM_ADDRESS`).
- **R9** Send requires an **allow-listed human approver identity** (`OUTREACH_APPROVER_USER_IDS`, keyed to Zay's Slack user id). A non-approver — or any turn with no human requester id — cannot send.
- **R10** Unattended/scheduled turns are **send-incapable**: the daily pre-read runs without `outreach_send` in its belt, and R9's requester-id check refuses any automation-origin turn as a backstop.

---

## High-Level Technical Design

The daily produce + conversational approve/send flow. The routine never posts or sends; Centaur owns the Slack surface; the send funnels through one hard-gated tool.

```mermaid
sequenceDiagram
    participant Wf as Centaur workflow (CRON 08:00)
    participant Br as raava-outreach bridge
    participant Rt as Routine (producer)
    participant Lz as Slack landing zone
    participant Pe as GTM persona (interactive)
    participant Zay
    participant Os as outreach_send (hard gate)
    participant Am as AgentMail

    Wf->>Br: POST /produce (returns ProducerResult, NO Slack post)
    Br->>Rt: run_producer()
    Rt-->>Br: ranked drafts → queue store
    Wf->>Lz: post Block Kit report (pending only) + read-only pre-read brief
    Note over Wf,Os: scheduled turn has no approver id + no send tool → cannot send
    Zay->>Pe: "send #3"
    Pe->>Br: POST /draft (fresh full draft + suppression status)
    Pe->>Os: stage(entry_id, to, subject, body, cc) → confirm_token (hash of bytes, TTL)
    Pe->>Zay: echo the fresh draft + confirm?
    Zay-->>Pe: confirm (as allow-listed approver)
    Pe->>Os: send(confirm_token)
    Os->>Os: gate: approver id ∈ allow-list? token valid+unconsumed? cap ok? live-send armed?
    Os->>Am: transmit (only if all pass)
    Pe->>Br: POST /mark-handled (delivered)
    Note over Rt: routine never posts or sends — produce only
```

---

## Key Technical Decisions

- **KTD1 — Native scheduler over launchd.** A Centaur scheduled workflow (`CRON`) replaces host launchd and invokes the routine via the bridge `/produce`.
- **KTD2 — Routine writes its store; Centaur is the sole writer of the landing zone.** The routine's "write" is to its queue/draft store; Centaur reads via `/produce` and posts the Slack landing zone. Once the produce path is post-stripped (U1), Centaur is the only thing that writes Slack — so the origin invariant ("routine writes [its output], Centaur reads + renders") holds honestly.
- **KTD3 — v1 approve/deny is conversational (Zay's pick).** Buttons + `wait_for_event` suspend/resume deferred to v2 (would be the first consumer of that primitive on a send-adjacent path).
- **KTD4 — The send gate is structural, in `outreach_send` (keystone, U7).** The one tool all sends funnel through enforces, in code: (a) an **approver-identity allow-list** (R9); (b) a **staged confirm-token** bound to the exact echoed bytes — single-use, TTL-bounded — so "echo then confirm" is a *protocol*, not a prompt suggestion, and injected "approval" text in a draft cannot mint a token; (c) a **daily/per-recipient cap**; (d) a **go-live arming flag** + CAN-SPAM precondition. The prompt guides UX; the tool enforces safety.
- **KTD5 — Bridge gains read + bookkeeping, stays send-free.** `/draft` returns the full draft (+ suppression/already-sent status) for crafting; `/mark-handled` records an outcome ∈ {delivered, rejected} — named so no reading implies transmission, fenced at the route so it can't backdoor-approve. Matching `draft()`/`mark_handled()` methods are added to the Centaur `raava_outreach` tool client (without them the persona has nothing to call). Neither route makes a mail-host call.
- **KTD6 — Persona routing as a command-router branch.** `/persona` dispatches on `payload.command` (the existing handler is Linear-feedback-only and hard-requires `LINEAR_API_KEY`); switching to a send-capable persona is approver-gated.
- **KTD7 — Routine is hard send-incapable.** Asserted structurally: no mail-host client constructible in the worker process and `AGENTMAIL_API_KEY` is not loaded there — not merely symbol-absence (which can regress under a renamed transport).

---

## Implementation Units

### U1. Strip routine to produce-only — produce path, CLI, and a hard creds assert `[raava-outreach]`

- **Goal:** The routine produces drafts to its store and nothing else. Post-incapability and send-incapability are tested as structural properties.
- **Requirements:** R1, R5, R7, KTD7
- **Dependencies:** none — but **ships in the same deploy as U2 + U3** (deleting the post path before U3 publishes would leave no landing zone).
- **Files:**
  - `src/raava_loop/approvals_slack.py` (delete — reaction poller + the per-user allow-list it carried; the gate moves to U7/U9 on the Centaur side)
  - `src/raava_loop/producer.py` (remove `post_morning_list`, `format_morning_list`, `_format_approval_message`; keep `run_producer` pure)
  - `src/raava_outreach/cli.py` (strip morning-list posting from the `produce` command — second posting call-site)
  - `src/raava_outreach/http_service.py` (`produce_discovery`: drop the `post_morning_list` import + `slack=` key + dry-run posting; return the bare `ProducerResult`)
  - `src/raava_outreach/slack_client.py` (remove reaction reads + posting helpers; delete the module if now unused)
  - `src/raava_outreach/queue.py`, `src/raava_outreach/models.py` (resolve the orphaned `approved` state — see Approach)
  - `state/slack-morning-list.json`, `state/slack-approval-reactions.json` (retire)
  - tests: update `tests/test_raava_loop_producer.py`, `tests/test_produce_cli.py`, `tests/test_http_service.py`; new `tests/test_routine_produce_only.py`
- **Approach:** Producer's sole output is `queue.add_draft()`. `produce_discovery` returns the model dump with zero Slack calls. **Orphaned `approved` state:** v1 goes `pending → {delivered, rejected}` directly (no worker-side approve). Remove `approve()`/`approve_all()` and drop `approved` from the active path; document the transition model in `queue.py`. Keep `clear_pending` (it preserves non-pending entries).
- **Execution note:** Characterization-first — assert produce-only + send-incapable, then remove, keeping the suite green.
- **Patterns to follow:** existing `tests/test_no_send.py` (symbol-absence) — extend it to structural asserts.
- **Test scenarios:**
  - `run_producer` on a fixture produces queue entries and makes **zero** Slack/network posting calls (assert the Slack client is never constructed/invoked).
  - `POST /produce` returns the `ProducerResult` and makes **no** Slack call.
  - No mail-host client is constructible in the worker process and `AGENTMAIL_API_KEY` is not read by the worker config (structural send-incapability — KTD7).
  - No send symbols (`send_approved`, `send_draft`, transport `.send`, `gmail_send`, `agentmail_send`) anywhere in `src/`.
  - `cli produce` performs no Slack post; updated CLI/producer tests reflect the stripped behavior.
  - `/queue?status=approved` is empty and `approve()` is gone (transition model is `pending → delivered|rejected`).
- **Verification:** Suite green; `test_routine_produce_only.py` passes; grep confirms no posting/approval/send path remains in the routine or the produce route.

### U2. Bridge: full-draft read + mark-handled, plus Centaur tool-client methods `[raava-outreach + centaur]`

- **Goal:** Give Centaur what it needs to craft and retire an entry — with zero send capability anywhere.
- **Requirements:** R4, R5, KTD5
- **Dependencies:** none (parallel with U1; ships with U1+U3)
- **Files:**
  - `[raava-outreach]` `src/raava_outreach/http_service.py` (add `/draft` + `/mark-handled` routes), `tests/test_http_service.py`
  - `[centaur]` `overlays/raava-internal/tools/raava_outreach/client.py` (add `draft(entry_id)` + `mark_handled(entry_id, outcome)` methods — without these the persona has no way to call the routes), test alongside
- **Approach:** `POST /draft {entry_id}` → full `Draft` (lead, subject, body, to, cc, attachments, critique) **plus suppression/already-sent status** so the persona and `outreach_send` can refuse a suppressed/contacted recipient. `POST /mark-handled {entry_id, outcome}` with `outcome ∈ {delivered, rejected}`, **enforced at the route** (reject any other value, including `approved`, so it can't backdoor-approve). Re-marking an already-handled id is a no-op 200 (idempotent). On `/draft` errors, return a generic message + server-side log (no raw exception / PII leak). Bearer-authed like the rest; neither route makes a mail-host call.
- **Execution note:** Test-first on the route contracts (mirror existing `test_*_maps_to_worker_fn`).
- **Test scenarios:**
  - `/draft` returns full draft fields + suppression status for a known id; unknown id → clean 404; error path returns a generic message (no `str(exc)`/PII).
  - `/mark-handled {outcome:"delivered"}` transitions the entry; it no longer renders as pending. Re-mark is a no-op 200.
  - `/mark-handled` rejects `approved` and any value ∉ {delivered, rejected} (400) — no backdoor approve.
  - Negative test: `/send`, `/send-approved`, `/approve`, `/approve-all` all 404; `/mark-handled` makes no mail-host network call.
  - Both new routes require the bearer (401 without).
  - Tool client `draft()`/`mark_handled()` call the right routes with the bearer and surface failures.
- **Verification:** Extended bridge + client tests green; the persona can fetch a full draft and mark an outcome through the Centaur tool; no send route exists.

### U3. Native scheduled workflow: produce → publish landing zone + send-incapable pre-read `[centaur]`

- **Goal:** A native daily workflow runs the routine and publishes the Block Kit report (pending only) with a read-only persona pre-read.
- **Requirements:** R2, R3, R7, R10
- **Dependencies:** **U1 and U2** (jointly — `/produce` is only safe-to-publish once post-stripped); ships with them.
- **Files:**
  - `overlays/raava-internal/workflows/gtm_outreach_daily.py` (new — `WORKFLOW_NAME`/`CRON = "0 8 * * *"`/`SLACK_CHANNEL`, `handler(inp, ctx)`)
  - `overlays/raava-internal/workflows/_gtm_report_blocks.py` (new — Block Kit render helper, plain import, **not** a second workflow)
  - test alongside (handler unit)
- **Approach:** Mirror `workflows/paradigm_pulse_daily.py` exactly (module-global discovery contract; `handler(inp: dict, ctx)`). Call `/produce` via the `raava_outreach` tool; render `ProducerResult.kept_drafts` (rank, account, why_now, score, queue_id) — filtered to **`status=pending` only** so approved/handled entries never re-render — into a Block Kit section list, each carrying a **draft fingerprint** (hash) for stale-card detection. Post via `ctx.call_tool("slack","send_message", {channel, text, blocks})` (the verified path — not `ctx.post_to_slack`). Then a **read-only pre-read brief**: run the `agent_turn` with a read-only persona/harness that does **not** expose `outreach_send` (R10), so the unattended 8am turn is structurally send-incapable; U7's requester-id gate is the backstop. Empty result → clean "no signals today."
- **Patterns to follow:** `workflows/paradigm_pulse_daily.py` (schedule + `call_tool` + `agent_turn`); slackbot Block Kit arrays.
- **Test scenarios:**
  - 3 pending drafts → one Block Kit report with rank/account/why_now/score + queue ids + fingerprints.
  - Approved/handled entries are excluded from the render (pending-only).
  - Empty result → "no signals" message, no error; `/produce` failure → clean failure note, scheduler not crashed.
  - The pre-read `agent_turn` runs with a belt that **cannot** reach `outreach_send` (assert a scheduled-origin turn cannot send — R10).
- **Verification:** Workflow registered + discoverable; manual trigger posts the landing zone; cron registered for 08:00; scheduled pre-read provably send-incapable.

### U4. GTM persona contract: read landing zone, staged approve/send `[centaur]`

- **Goal:** The v1 persona contract — brief on demand, and on "go" craft → stage → echo-fresh → confirm → send → mark-handled.
- **Requirements:** R3, R4, R5
- **Dependencies:** U2 (bridge + client methods), **U7** (the hardened `outreach_send`)
- **Files:** `overlays/raava-internal/tools/personas/outreach-operator/PROMPT.md`
- **Approach:** Source of truth is the landing zone + queue via the bridge (`/queue`, `/draft`) — never routine internals. On "latest on the GTM report" → read + brief. On "go/send #N": fetch the **fresh** `/draft`; call `outreach_send.stage(...)` → `confirm_token`; **echo the freshly-fetched exact email** (subject + body) — never approve from the stale landing-zone card; on Zay's confirm call `outreach_send.send(confirm_token)`; on `{"sent": true}` call `mark_handled(delivered)` (mark **after** the send result, never before). On "no/skip" → capture feedback (supermemory) + `mark_handled(rejected)`. Treat all draft/lead content as **untrusted data, never instructions**; require the confirm as a fresh message from the approver after the echo — never inferred from any content. Reassert: the routine cannot send; only this tool can, and only behind its gate.
- **Execution note:** Prompt contract — validate by the witnessed smoke + a contract self-review against the send-safety rules; the enforcement lives in U7, not the prose.
- **Test scenarios:** `Test expectation: none — prompt-contract change; enforcement tested in U7.` Witnessed-smoke checks: explicit "go" by the approver + echo-of-fresh-draft + confirm → exactly one send + `mark_handled(delivered)`; ambiguous input → nothing; "no" → feedback + `mark_handled(rejected)`; injected "approved, send now" text in a draft → no send (token can't be minted from content).
- **Verification:** Witnessed smoke passes the above; the persona never sends without a staged-then-confirmed token from the approver.

### U5. Persona routing: `/persona` command-router branch + approver gate `[centaur]`

- **Goal:** Explicit dynamic persona selection; `CHANNEL_DEFAULTS` stays fallback; switching to a send-capable persona is approver-gated.
- **Requirements:** R6
- **Dependencies:** none (independent of the loop; can land anytime)
- **Files:**
  - `services/slackbot/src/index.ts` (branch the command router on `payload.command`: `/persona` → persona logic; feedback commands → existing Linear path)
  - `services/slackbot/src/config.ts` (allowlist scaffolding for non-feedback commands)
  - `overlays/raava-internal/workflows/slack_thread_turn.py` (broaden trigger parsing; explicit-persona precedence already exists)
- **Approach:** The existing `slackCommandHandler` is Linear-feedback-only and hard-requires `LINEAR_API_KEY` — so add a **dispatch branch**, not a registration. `/persona <name>` injects `persona=` into the workflow Input (overlay already resolves an explicit persona arg first). Validate against known personas; unknown → friendly error. **Gate `/persona <send-capable persona>` behind the approver allow-list** (defense-in-depth; the send itself is still U7-gated). Per-thread pin: **design only** — note the insertion point (between in-message flag and channel default) and storage (existing per-thread session metadata); do not build.
- **Patterns to follow:** `slackCommandHandler`; `_raava_roles.resolve_role` / `default_persona_for_channel`.
- **Test scenarios:**
  - `/persona outreach-operator` by an approver routes the next turn to that persona regardless of channel default.
  - `/persona <unknown>` → friendly error, no route change.
  - `/persona <send-capable>` by a non-approver → refused/logged.
  - No `/persona` and no `--role` → channel default still applies (regression).
  - Feedback commands still route to the Linear path unchanged.
- **Verification:** `/persona` works as a dispatch branch; send-capable switch is approver-gated; `CHANNEL_DEFAULTS` fallback intact.

### U6. Cutover: decommission host launchd produce job `[centaur / ops]`

- **Goal:** Retire the host launchd produce job once the Centaur workflow is verified; keep the bridge.
- **Requirements:** R2
- **Dependencies:** U3 live; go-live gate (U7 armed + vault items + witnessed smoke)
- **Files:** ops — `~/Library/LaunchAgents/com.raava.outreach-daily-produce.plist` (unload/remove); the bridge launchd (`com.raava.outreach-http-bridge`) stays.
- **Approach:** Cutover after U3 verified posting. The plist no longer has a morning-list fallback to fall back to (U1 deletes it), so verify U3 first. `OUTREACH_LIVE_SEND_ENABLED` (U7) is the live-send kill-switch for the cutover.
- **Test scenarios:** `Test expectation: none — ops cutover.`
- **Verification:** launchd produce job unloaded; the Centaur workflow is the sole daily trigger; bridge still serving `/produce`.

### U7. Keystone: harden `outreach_send` into the structural send gate `[centaur]`

- **Goal:** Make send safety deterministic code in the single send funnel — not prose. This is the unit that closes the send-safety P0s; **live send is not armed until this lands and is tested.**
- **Requirements:** R5, R8, R9, R10
- **Dependencies:** requires the requester Slack `user_id` to be plumbed into the persona turn context (available at the slackbot boundary today)
- **Files:**
  - `overlays/raava-internal/tools/outreach_send/client.py` + `pyproject.toml` (the gate + new secrets/config)
  - turn-context plumbing for the requester id (slackbot → persona turn metadata)
  - tests alongside (the enforcement suite)
- **Approach:** Replace the single `send()` with a **two-call staged handshake** plus hard preconditions:
  - `stage(entry_id, to, subject, body, cc)` → returns a short-lived, single-use `confirm_token` bound to a hash of the exact payload bytes (TTL, e.g. minutes).
  - `send(confirm_token)` transmits **only if** all hold: (1) the approving turn's requester `user_id` ∈ `OUTREACH_APPROVER_USER_IDS` (R9; a turn with no human requester id — i.e. scheduled/automation — fails here, satisfying R10); (2) the token is unconsumed, unexpired, and its hash matches the staged payload (binds confirm to the echoed bytes; single-use = idempotency, no double-send on retry); (3) the per-day / per-recipient cap is not exceeded (`MAX_SENDS_PER_DAY`); (4) live send is armed: `OUTREACH_LIVE_SEND_ENABLED=true` and `CAN_SPAM_ADDRESS` present (go-live gate enforced in code, not advisory — also the cutover kill-switch).
  - On any failure, return a structured refusal (no send), never a silent pass.
- **Execution note:** Test-first — the enforcement is the product of this unit.
- **Test scenarios:**
  - Non-approver requester id → refused, no transmit.
  - Scheduled/automation turn (no requester id) → refused (R10 backstop).
  - Valid token → sends once; **replaying the same token → refused** (idempotency / no double-send).
  - Expired token → refused. Hash mismatch (sent bytes ≠ staged/echoed bytes) → refused.
  - Day cap reached → refused; per-recipient already-sent → refused.
  - `OUTREACH_LIVE_SEND_ENABLED` false or `CAN_SPAM_ADDRESS` absent → refused regardless of token.
  - Injected "approval" text inside a draft body cannot mint or substitute for a token.
- **Verification:** The enforcement suite is green; a code-level gate (not prompt prose) blocks every unauthorized/unconfirmed/over-cap/disarmed send; live send stays disarmed until `OUTREACH_LIVE_SEND_ENABLED` is set at the witnessed smoke.

---

## Scope Boundaries

**In scope (v1):** routine produce-only strip (incl. produce path + CLI + hard creds assert); native scheduled workflow + Block Kit landing zone (pending-only) + send-incapable pre-read; conversational persona approve/deny; bridge `/draft` + `/mark-handled` + tool-client methods; **hardened `outreach_send` gate (U7)**; `/persona` switch; launchd cutover.

### Deferred to Follow-Up Work
- **Interactive approval (v2):** Block Kit Approve/Deny buttons + slackbot `block_actions` wiring + `wait_for_event` suspend/resume "inbox."
- **One-click send** (vs. echo+confirm).
- **Per-thread persona pin** (designed in U5) and **intent-classification routing**.
- **Centaur acting on the system from direction:** "update the signals" → ticket to an eng pod or direct tool update; "we need evals" → build evals. (Origin Plane-2 behaviors; separate plan.)
- **Per-persona tool-belt manifest in Centaur** (would let U3's pre-read scope its belt natively instead of via a read-only persona) — platform enhancement, separate.

### Outside this product's identity
- The routine never becomes agentic, never posts to Slack, never sends. Centaur is never "the tool." The send gate is never prose-only.

---

## Risks & Dependencies

- **Send-safety (highest) — the throughline.** The worker is structurally send-incapable; the gap was that the Centaur-side gate was prose. v1 closes it by making `outreach_send` (U7) the hard funnel: approver-identity allow-list, staged hash-bound single-use token, daily cap, go-live arming. Until U7 lands and is tested, **live send stays disarmed** (`OUTREACH_LIVE_SEND_ENABLED` unset). v1 also avoids new interactive send-trigger plumbing (deferred to v2).
- **Blast radius.** Bridge compromise → at worst PII draft exposure via `/draft` (bearer-authed, localhost) + spurious state transitions; **no transmit** (no transport). Persona compromise/injection → bounded by U7 to "at most the drafts an allow-listed human explicitly echoed-and-confirmed this session, within cap" — not arbitrary outbound.
- **Cross-repo / sequencing.** Land **U1 + U2 + U3 in one deploy** (U1 strips posting; U3 replaces it; no landing-zone gap). U4 depends on U2 **and U7**. U6 cutover after U3 verified + U7 armed.
- **Go-live gates (block live send, not the build):** the 4 `Engineering`-vault items (`OPENAI_CODEX_*` ×3 + `RAAVA_OUTREACH_HTTP_TOKEN`) + `OUTREACH_APPROVER_USER_IDS` + `OUTREACH_LIVE_SEND_ENABLED` + `CAN_SPAM_ADDRESS` (+ `PROOF_CLEARED`, cleared bench) + Stage 3d witnessed send smoke (task #13).
- **PII / token hygiene.** `/draft` exposes recipient emails + personalized bodies → generic error messages, scrubbed monitoring, and the bridge bearer is a gated vault item.

---

## Open Questions

1. **Landing-zone Slack channel** — exact channel for the daily report (default: a dedicated GTM channel, e.g. `#raava-outreach`). Confirm against the shared context/articles. (Config-only; does not block build.)
2. **Read-only pre-read persona** — does a suitable read-only persona/harness already exist to run U3's brief without `outreach_send`, or do we add a minimal one? (Resolve in U3; the requester-id gate is the backstop either way.)
3. **Pre-read depth** — how much the daily brief should reason/triage vs. summarize (v1: one-line top-pick + thin-flags).

---

## Operational / Rollout Notes

- **Order:** (U1 ‖ U2 ‖ U7 ‖ U5) → **deploy U1+U2+U3 together** → U4 → (go-live gate: U7 armed + vault items + witnessed smoke) → U6. U5 and U7 are independent of the loop and can land first.
- **Go-live (live send) is gated** on task #13: Zay adds the vault items + `OUTREACH_APPROVER_USER_IDS`, then `OUTREACH_LIVE_SEND_ENABLED=true`, then the witnessed smoke ("send #N" by the approver → echo-fresh → confirm → one send; ambiguous → nothing; non-approver → refused). Until then the loop runs produce + land + brief only (send disarmed in code).
- **Rollback:** U6 keeps the launchd plist reloadable until the Centaur workflow is proven; `OUTREACH_LIVE_SEND_ENABLED=false` is the instant send kill-switch.

---

## Sources & Research

- Origin: `docs/brainstorms/2026-06-24-gtm-routine-vs-centaur-requirements.md`
- Adversarial reviews (2026-06-24): cto architecture (produce-path double-post, missing tool-client methods, sequencing, orphaned `approved` state, stale-card divergence, `/status` fencing) + security-review send-safety (approver-allow-list regression, unattended scheduled `agent_turn` with send belt, prose-only `outreach_send` gate, idempotency/cap/go-live binding).
- Centaur `[centaur]`: `workflows/paradigm_pulse_daily.py`, `services/api/api/workflow_engine.py` (`agent_turn`, `wait_for_event`—v2), `services/slackbot/src/slack/client.ts`+`render.ts`, `services/slackbot/src/index.ts` (`slackCommandHandler` Linear-only), `overlays/raava-internal/tools/outreach_send/client.py` (the gate target), `overlays/raava-internal/tools/raava_outreach/client.py` (tool-client methods), `overlays/raava-internal/workflows/_raava_roles.py`+`slack_thread_turn.py`.
- Worker `[raava-outreach]`: `src/raava_outreach/http_service.py` (produce posts today), `src/raava_loop/producer.py`+`approvals_slack.py` (post + allow-list being removed), `src/raava_outreach/cli.py` (2nd post call-site), `src/raava_outreach/queue.py`+`models.py`, `tests/test_no_send.py`, `HANDOFF.md` (go-live gates).
