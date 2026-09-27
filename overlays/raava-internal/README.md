# Raava Internal Centaur Overlay

> **Rebased on the Rust control plane.** Upstream replaced the Python control plane
> with `services/api-rs` and `services/slackbot` with `services/slackbotv2`. Tools
> and workflows are still Python, but they run through new mechanisms: sandbox CLI
> shims and a workflow host that fabricates the old `api.workflow_engine` module.
> Read `deploy/READINESS.md` for the per-component status before changing this
> overlay — four pieces are currently inert, and a green pytest run hides it. Read
> `deploy/RELEASE.md` before any production change. The pre-Rust working tree is
> preserved as the `archive/hermes-slack-20260821` tag.

This overlay packages Raava's internal Centaur product surface:

- eight Slack-visible function leads: Chief, Vera, Priya, Enoch, Elena,
  Heathcliffe, Vivian, and Argus
- private specialist routing for Hana, Isaac, demoted roles, and decomposed
  execution roles
- Raava Slack channel defaults
- a gbrain grounding tool with deterministic local fallback
- a manager delegation workflow that can run without Slack

The overlay is kept under `overlays/raava-internal` for local dogfood. It uses
the same file shape as an external overlay repo, so it can later move into a
dedicated Raava overlay repository without changing the base Centaur extension
model.

## Source-control boundary

`paradigmxyz/centaur` is the upstream base platform. Raava-specific commits are
not meant to be pushed there. This directory is a local dogfood overlay that
should be shared by exporting the patch or by pushing to a Raava-owned overlay
repo or fork.

Before publishing overlay work, verify the remote target:

```bash
git remote -v
git branch --show-current
```

If `origin` points at `github.com/paradigmxyz/centaur.git`, do not push the
Raava branch to `origin`. Add or use a Raava-owned remote for overlay work
instead.

## Upstream vs overlay ownership

Base Centaur behavior lives under paths such as `services/api/`,
`services/slackbot/`, and `services/sandbox/`. Raava behavior lives in this
overlay under `overlays/raava-internal/`: personas, the gbrain wrapper, Slack
routing, manager delegation, sandbox prompt guidance, and Raava skills.

The boundary is resolved at runtime. When `TOOL_DIRS` and `WORKFLOW_DIRS`
include this overlay after the base directories, Raava tools and workflows are
discovered from the overlay and same-named entries such as
`slack_thread_turn` intentionally shadow the base implementation for that
deployment.

## Local API discovery

From the repo root, point the API test process at the overlay directories:

```bash
export TOOL_DIRS="$PWD/tools:$PWD/overlays/raava-internal/tools"
export WORKFLOW_DIRS="$PWD/workflows:$PWD/overlays/raava-internal/workflows"
export RAAVA_CENTAUR_CHANNEL_DEFAULTS='{"C0123ENGINEERING":"enoch","C0456QA":"vivian"}'
```

Then run the focused tests:

```bash
cd services/api
uv run pytest \
  tests/test_raava_internal_overlay.py \
  tests/test_raava_internal_slack_routing.py \
  tests/test_raava_internal_delegation.py
```

The tests do not require Slack credentials or hosted gbrain access. The
`raava_gbrain` tool falls back to the local roster baseline when
`RAAVA_GBRAIN_BASE_URL` is unset.

## Docker overlay image

Build the overlay image from this directory:

```bash
docker build -t raava-centaur-overlay:local overlays/raava-internal
```

For a Helm-backed local deployment, set:

```yaml
overlay:
  image:
    repository: raava-centaur-overlay
    tag: local
    pullPolicy: IfNotPresent
    sourcePath: /overlay
```

Centaur mounts the overlay into the API and sandbox pods. Verify API-side
discovery from the API container:

```bash
echo "$TOOL_DIRS"
echo "$WORKFLOW_DIRS"
ls -la /app/overlay/org
curl -fsS http://localhost:8000/tools/personas | jq
```

Expected personas are `chief`, `vera`, `priya`, `enoch`, `elena`,
`heathcliffe`, `vivian`, and `argus`. `hana` and `isaac` should not appear as
top-level personas.

## Workflow smoke without Slack

Invoke manager delegation directly through the workflow API:

```bash
curl -s "$CENTAUR_API_URL/workflows/runs" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d '{
    "workflow_name": "raava_delegate",
    "eager_start": true,
    "input": {
      "manager_persona": "priya",
      "request": "Pressure-test the Raava Internal Centaur v1 product shape.",
      "specialists": [
        {"role": "hana", "brief": "Check product UX and interaction risk."},
        {"role": "qa", "brief": "Check release and verification risk."}
      ]
    }
  }' | jq
```

Inspect the run:

```bash
curl -s "$CENTAUR_API_URL/workflows/runs/$RUN_ID" \
  -H "X-Api-Key: $CENTAUR_API_KEY" | jq
```

## Slack CLI smoke

Use Slack CLI only after the Slack app is installed and the local or deployed
Slackbot is reachable by Slack.

1. Confirm the app has bot token, signing secret, and event subscription values
   configured for the target workspace.
2. Set `RAAVA_CENTAUR_CHANNEL_DEFAULTS` with Slack channel IDs mapped to
   approved leads, for example `{"C0123ENGINEERING":"enoch"}`.
3. Mention Centaur in a mapped channel such as `#engineering`.
4. Verify the API creates a `slack_thread_turn` workflow run with
   `raava_function_lead=enoch` in metadata.
5. Mention an approved persona selector such as `--vivian` in an engineering
   channel and verify it overrides the channel default.
6. Mention a private specialist selector such as `--hana` and verify the final
   Slack answer is owned by Enoch.

The Slack smoke should validate routing and UX only. Workflow delegation and
gbrain fallback are covered by API tests and direct workflow smoke.

---

## Outreach Operator — local-run prerequisites and go-live smoke

### Go-live prerequisites (NOT YET MET on this host)

The following must be in place before `overlays/raava-internal` can run in a live
Centaur deployment:

1. **`OP_SERVICE_ACCOUNT_TOKEN` and `OP_VAULT`** — the 1Password Service Account
   token and vault name that Centaur uses for iron-proxy secret injection.
   These are not yet set on this host.  Create them via the 1Password CLI and
   export them into the shell / Helm values before starting the overlay.

2. **`RAAVA_OUTREACH_BIN`** (optional, defaults to
   `/Users/master/raava-outreach/.venv/bin/raava-outreach`) — the `raava_outreach`
   tool shells out to the raava-outreach CLI binary.  The binary is responsible
   for its own secrets (DB, email, Slack).  Centaur never injects those secrets
   through iron-proxy.  Ensure the binary is installed and reachable at the path
   before using the outreach-operator persona.

3. **`SUPERMEMORY_API_KEY`** — injected by iron-proxy via the `http` secret
   declaration in `tools/raava_supermemory/pyproject.toml`.  Obtain the key from
   Supermemory's dashboard; without it the `remember`/`recall` methods degrade
   gracefully (no crash, no stored data).

### Running the overlay test suite locally

```bash
cd /Users/master/centaur
python3 -m pytest overlays/raava-internal -q
```

All tests mock subprocess and HTTP calls — no live binary or API access is
required.

### Automatable safety proof

The following test assertions provide automated safety coverage:

- **U1 guard-binding test** (`tools/raava_outreach/test_client.py ::
  test_send_approved_guard_binding_blocked`): asserts the tool surfaces
  `blocked=1, sent=0` when the loop CLI reports a guard-blocked send.  The tool
  cannot override the loop's guards.

- **U2 persona-contract test** (`tools/personas/outreach-operator/
  test_persona_loads.py`): asserts the PROMPT.md encodes the
  explicit-instruction-only, echo-and-confirm-before-send, and
  never-on-ambiguous rules.

### Manual go-live smoke checklist (requires live Centaur + Slack)

Run this smoke after the prerequisites above are met and Centaur is running:

1. **Basic routing:** post an unselected message in `#raava-outreach`.
   Verify the Centaur bot replies as `outreach-operator`.

2. **Queue review:** ask "show me the pending queue".  Operator must list
   entries without sending anything.

3. **Guarded send — explicit instruction path:**
   - Type "send #3".
   - Operator must ECHO which draft and recipient it will send, then WAIT for
     your explicit confirmation before calling `send_approved`.
   - Reply "yes, send" (or equivalent explicit go).
   - Verify only entry #3 is sent; `sent=1, blocked=0`.

4. **Guarded send — ambiguous text rejected:**
   - Type "looks good" or "that's fine".
   - Operator must NOT call `send_approved`.  It may ask for clarification, but
     it must never auto-send.

5. **Operator never auto-sends unprompted:**
   - Without any "send" instruction, ask "what's the open rate on the last
     campaign?".
   - Verify no send is triggered.

6. **Loop guard backstop:**
   - Attempt to send an entry that has not cleared proof_cleared/bench.
   - Verify `blocked=1` in the CLI output and that the operator reports the
     block without marking it as sent.
