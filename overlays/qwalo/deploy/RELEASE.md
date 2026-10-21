# Release a Qwalo client — ERE worked end to end

> **Do not run this against a client yet.** Two preconditions, both in step 5: the
> engine's four read routes are not merged, and the engine cannot yet issue a
> read-only token. Until those close, this document is the plan, not a runbook.

Nothing in this repository deploys anything. CI builds images and runs Rust and
Slackbot tests; it never touches a cluster or Slack. Every step below is a human
action, done in order. The one step that can be rehearsed without a cluster is step 6a.

Estimated first time: 45–90 minutes, most of it waiting on Slack and 1Password.
Second client: about 15 minutes.

## What a client release is

```
client workspace (Slack app)  ->  https://<client>.qwalo.raava.dev  ->  namespace centaur-<client>
                                                                        api-rs + slackbotv2 + postgres
                                                                        one sandbox pool, one warm sandbox
                                       |
                                       +-- engine /v1 with a token held to account_scope=<client>
```

No Marketplace. No shared router. No SmarterContact credential in this release: the
engine reads SmarterContact through sc-connector, and the agent reads the engine.

## Decisions taken (2026-10-20, zay)

- Client slug `ere`; host `ere.qwalo.raava.dev`; vault `prod-qwalo-ere` exists and is
  empty (id `ylon56czut2scrqjb22rveszte`).
- Seller text may be shown to that client's reviewers. A reviewer cannot judge what
  they are approving from a count, so on 2026-10-21 the owner allowed the seller's text
  messages inside **this client's** Slack workspace and **this client's** Centaur
  release. That replaces the 2026-10-20 rule for this release.
  - It stays inside the client: one app, one workspace, one namespace, one
    `account_scope` (F1, F3, F11), and never into a shared surface or a third-party
    trace sink (F12).
  - **Centaur stores none of it** (same date). The release is a read path, not a
    place: the client's Postgres holds assignments, executions, events, and sessions —
    the control plane's own state — and holds no lead, transcript, or snapshot. Every
    number and message is fetched from the engine when the answer is written.
    `e2e/run_fake_engine.py` proves the overlay carries no database driver, no write
    statement, and no registered workflow, and that a lead read succeeds with no
    database reachable at all.
  - The one writer this overlay cannot police is the platform's own: `session_messages`
    and `session_events` are written in plain text with no redaction and no retention
    knob, so whatever the agent quotes into a reply is stored with the conversation.
    Keeping the decision literally true means answers point at records (lead id,
    conversation id) and the human opens them in Qwalo or SmarterContact, or the
    platform gains persistence-off / redaction-on. Which one is the owners' call in the
    persona pass; no values entry here decides it.
  - The sandbox prompt says nothing about seller text, and nothing in this overlay
    decides what the agent may show. That belongs to the overlay and persona pass the
    owners are running. Do not settle it from a runbook.
- Still a human step, and it stays one: the `sa-prod-qwalo-ere` service account is
  created in the 1Password web UI. Neither this script nor an agent creates it.
- No vault item is created until the engine issues ERE's read token. An empty vault is
  the correct state today: `deploy-client.sh` fails against it, and it should.

## Step 0 — what only a human may do

You need, and this release does not create or assume:

| Access | Used for |
|---|---|
| Slack workspace admin in the client's workspace (or a co-admin who installs the app) | create the app from the manifest, install it, set the event URL |
| Write access to the `prod-qwalo-<client>` 1Password vault | create the four items in step 3 |
| `op` CLI plus `OP_SERVICE_ACCOUNT_TOKEN` on the deploy machine | `deploy-client.sh` reads secrets at apply time |
| `kubectl` context for the cluster, `helm`, and `GITHUB_TOKEN` for the repo-cache clone of a private `raava-solutions/centaur` | the deploy itself |
| Wrangler access to `raava-solutions/qwaelo-ai-engine` | issue and register the engine token in step 4 |
| A 1Password **service account scoped to one vault**, `prod-qwalo-<client>` | the deploy reads that vault and nothing else |

One service account per client vault. `deploy-client.sh` lists the vaults its token can
see and refuses to run when the same token reaches another `prod-qwalo-*` vault: a
token that can read two clients' vaults turns one client's deploy into a cross-client
mistake waiting to happen.

The agent never gets any of this. It gets one read path and no credential of its own.

## Step 1 — name the client

The name is a lowercase DNS slug and is reused as the namespace, the Helm release, the
hostname label, the 1Password vault suffix, and the engine `account_scope`. ERE is
`ere`. Pick it with the client's Slack workspace in mind: it is what the owner will see.

```bash
export QWALO_DOMAIN=qwalo.raava.dev     # zone delegated to Cloudflare on the Raava account
```

DNS for `<client>.qwalo.raava.dev` must point at the cluster ingress (a Cloudflare
record, or a Tunnel endpoint, matching how `centaur.raava.dev` is published today).

## Step 2 — create the Slack app in the client's workspace

Render the manifest (this substitutes the client name and host; nothing else changes):

```bash
overlays/qwalo/deploy/deploy-client.sh --check ere    # prints what it will render
sed -e 's/<<CLIENT>>/ere/g' -e 's/<<HOST>>/ere.qwalo.raava.dev' \
    overlays/qwalo/deploy/slack-app-manifest.json.tmpl > /tmp/qwalo-ere.json
```

Render the manifest with the script. Do not re-render it by hand with `sed`; the
script is the only thing that knows the host shape:

```bash
overlays/qwalo/deploy/deploy-client.sh --manifest ere > /tmp/qwalo-ere.json
```

Create the app with the Slack CLI (`slack app manifest validate /create`) or by
importing that JSON at <https://api.slack.com/apps?new_app=1>, in the **client's**
workspace. Install it and grant the scopes.

The manifest asks for `app_mentions:read`, `chat:write`, channel read and history, and
`users:read`, and subscribes to exactly one event, `app_mention`. It does **not** ask
for `chat:write.public`, so the bot cannot post into a channel it was not invited to,
and it does **not** subscribe to `message.channels`, so the platform does not receive a
copy of everything the client's team writes. The bot sees a channel only when someone
mentions it. That is the minimum that can answer a question, and it is the difference
between a reader and a wiretap.

Accept the consequence knowingly: a follow-up in a thread the bot already joined does
not reach it unless someone mentions it again. If that reads wrong in practice, add
`message.channels` as a decision about how much of the client's conversation the
platform may see, not as a UX patch.

Event subscriptions cannot be enabled yet. Slack verifies `request_url` before it will
turn them on, and `https://<client>.qwalo.raava.dev` does not answer until step 6. That
failure is the safety property in `FAILURE_MODES.md` F5: an app created before the
deploy cannot receive mentions and cannot half-work.

## Step 3 — store the app's secrets in 1Password

Create vault `prod-qwalo-ere` (or ask the vault owner to), with one item per row.
The names are exact: `deploy-client.sh` refuses to deploy when one of them cannot be
read.

The item name is the secret name and the field is `credential`, in every row. That is
not a style choice: `centaur-iron-control`'s `registry.rs` builds the reference as
`op://{vault}/{SECRET_NAME}/credential`, so an item named in prose, or a field named
`password`, never resolves and the release answers 401 forever.

| Item | Field | What goes in it |
|---|---|---|
| `SLACK_BOT_TOKEN` | credential | the `xoxb-…` from step 2 |
| `SLACK_SIGNING_SECRET` | credential | the `Signing Secret` from Basic Information |
| `SLACKBOT_API_KEY` | credential | the Centaur API key this Slackbot authenticates with (`api_keys` row, `aiv2_*`) |
| `OPENAI_API_KEY` | credential | the key the codex harness runs on. The owner decides whose key a client's turns are billed to. |
| `QWALO_ENGINE_TOKEN` | credential | the bearer token from step 4 |

`deploy-client.sh` puts the first three into the `centaur-infra-env` Secret in the
client's namespace. It copies the last two **nowhere**. `OPENAI_API_KEY` and
`QWALO_ENGINE_TOKEN` are resolved by iron-proxy from `op://$OP_VAULT/<NAME>/credential`
and stamped in flight, for `api.openai.com` and the engine host respectively
(`CODEX_API_KEY_FRAGMENT` in `services/api-rs/crates/centaur-iron-proxy/src/fragment.rs`).
No pod gets a copy of either, and no environment variable for either exists in a
sandbox. The script only proves the two items exist before it deploys.

Never put any of these in a values file, a commit, a Slack message, or a shell history
line. They are diffed, screenshotted, and quoted.

## Step 4 — issue the engine token, scoped to this one client

In `raava-solutions/qwaelo-ai-engine`, the `/v1` surface authenticates with bearer
tokens whose SHA-256 hashes live in the Worker secret `SERVICE_TOKENS`:

```json
[{"caller": "centaur-ere", "sha256": "<64 hex>", "account_scopes": ["ere"]}]
```

```bash
TOKEN=$(openssl rand -hex 32)
printf '%s' "$TOKEN" | openssl dgst -sha256      # register the hex digest, keep the token
```

Add a slot with `"account_scopes": ["ere"]` and rotate the Worker secret
(`npx wrangler secret put SERVICE_TOKENS`). Keep at least two slots so the next rotation
has no downtime. Store the plaintext in vault item `prod-qwalo-ere/QWALO_ENGINE_TOKEN`,
field `credential` — the name iron-proxy looks up, not a description of the item.

Understand what this token is and is not:

- **It is scoped by account.** Requests for another `account_scope` get 403
  `wrong_scope`, and a lead outside the scope reads as 404. One client's token cannot
  enumerate another client's leads.
- **It is not scoped by verb, and the sandbox cannot be handed a read-only token.**
  `TokenSlot` in `src/v1/auth.ts` carries `caller`, `sha256`, and `account_scopes`
  only. The engine has since grown `caps`, and this step must use them; see below. And
  iron-proxy's inject mode matches a **host**, not a
  method or a path, so it stamps this bearer on any request from the sandbox to
  `qwalo.raava.dev`, including `curl -X POST .../v1/labels` typed in a shell. The
  read-only tool surface protects the tool path and nothing else.

  What this release waits on now is **issuance**, tracked as `qe-bre`. The capability
  exists — `SERVICE_TOKENS` slots carry `caps` and `requireCap("read")` answers 403
  `insufficient_capability` — but no `caps: ["read"]` token has been minted for a
  client, so today the only token a Qwalo vault could hold may also write. Step 5 blocks
  go-live until step 4 stores a read-only token and nothing else.

## Step 5 — two hard gates before anything reaches a client

**Gate A — the read API must be live.** Four of the five read routes below are thread
t-0027's work and are not merged. Do not deploy a client release until they answer on
the production Worker.

**Gate B — the token must be read-only.** Do not put a client live on a token that can
write. Today every engine service token can write, so today no client goes live (see
step 4). A release that passes Gate A and fails Gate B is a write-capable agent
holding one customer's leads and one customer's number.

Both gates fail loudly in the probes below, and both are written up in
`FAILURE_MODES.md` F2 and F8. "The stack came up" is not evidence about either one.

Confirm the engine answers for this scope before the client can see a page:

From inside the engine's own E2E harness or with the smoke token:

```bash
curl -sf "https://qwalo.raava.dev/v1/metrics?account_scope=ere" \
  -H "Authorization: Bearer $TOKEN" | jq
```

A `404 not_found` here means the read API is not deployed. A `503
service_auth_not_configured` means `SERVICE_TOKENS` is unset. Neither is a client
problem to discover later.

## Step 6 — deploy

6a. Rehearse the render. No cluster, no 1Password, no Slack:

```bash
overlays/qwalo/deploy/deploy-client.sh --check ere
```

It refuses a client name that is not a DNS slug, fails if a placeholder survives, and
fails if a secret-named key appears in the rendered values or manifest.

Release identity is pinned, and `--check` refuses `latest` and a moving branch:

```bash
export QWALO_IMAGE_TAG=2026.10.20-d441128   # the build this client runs
export QWALO_TOOL_REF=d441128e              # the tool tree this client runs
```

6b. Deploy for real:

```bash
export OP_SERVICE_ACCOUNT_TOKEN=... GITHUB_TOKEN=...   # vault-scoped token
overlays/qwalo/deploy/deploy-client.sh ere
```

It proves the four 1Password references read cleanly (into `/dev/null` — a credential
never lands in a log line), creates namespace `centaur-ere`, runs
`contrib/scripts/bootstrap-k8s-secrets.sh -n centaur-ere` to build `centaur-infra-env`
and the iron-proxy CA there, then `helm upgrade --install centaur-ere contrib/chart`
with the rendered values and `--set-file overlay.systemPrompt=…`. The rendered values
file lives in a `mktemp -d` directory and is deleted on exit.

If the cluster has no agent-sandbox controller yet, install it once
(`agentSandbox.enabled: true` in a throwaway values file, or the standalone manifest),
then leave it `false` in every client release. Two controllers fight over the same CRs.

## Step 7 — verify before anyone asks the bot a question

```bash
kubectl -n centaur-ere rollout status deploy/centaur-centaur-api-rs \
                                  deploy/centaur-centaur-slackbotv2
kubectl -n centaur-ere get secret centaur-infra-env -o 'jsonpath={.data}' | tr ',' '\n'   # key names only, never values
```

Two probes check that this release is the release the overlay means, because both are
properties a green rollout does not prove:

```bash
# 1. The database host the Secret names must be a Service that exists in this namespace.
#    The Secret is built by contrib/scripts/bootstrap-k8s-secrets.sh with the host baked
#    in, and it assumes the Helm release is named `centaur`.
kubectl -n centaur-ere get secret centaur-infra-env -o 'jsonpath={.data.DATABASE_URL}' \
  | base64 -d | sed 's|://[^@]*@|://<user:pass>@|'
kubectl -n centaur-ere get svc centaur-centaur-postgres -o name

# 2. The registered workflow set. The chart always emits the base tree first and this
#    release hides it with a later entry; hiding is a runtime behaviour, so read it
#    back from the container rather than trusting the render.
kubectl -n centaur-ere exec deploy/centaur-centaur-api-rs -- printenv WORKFLOW_DIRS
```

Expected: the first prints a host of `centaur-centaur-postgres`, and the second finds
that Service. If they disagree, the API has no database and every sandbox in the
namespace will fail its first tool call.

The third prints one path, ending `overlays/qwalo/workflows`. If it names the base
repo's `workflows` directory, the client release has registered the base tree: that
tree contains `slack_backfill` and `slack_sync`, which write Slack conversation text
into Postgres. Treat that as a live leak of exactly what must not be stored, stop
traffic to the release, and find out why the override stopped winning.

Now prove the token works **from a sandbox**, which is the only place iron-proxy is in
the path. Exec into a running sandbox pod and run the tool as the agent runs it:

```bash
POD=$(kubectl -n centaur-ere get pods -l centaur-agent=true -o jsonpath='{.items[0].metadata.name}')
kubectl -n centaur-ere exec "$POD" -- sh -lc 'engine metrics'
kubectl -n centaur-ere exec "$POD" -- sh -lc 'engine leads --limit 5'
kubectl -n centaur-ere exec "$POD" -- sh -lc 'env | grep -c QWALO_ENGINE_TOKEN'   # must print 0
```

Expected: `engine metrics` returns totals. `engine leads` returns the client's leads.
The last probe prints **0**: an inject-mode secret puts no variable in the sandbox at
all. Anything else means the release is misconfigured — and a printed token there is
the incident itself, not a placeholder.

If `engine metrics` answers `invalid_token`, iron-proxy is not injecting. Usual causes,
in order: the vault item is not `QWALO_ENGINE_TOKEN` with a `credential` field; the
engine host in `overlays/qwalo/tools/engine/pyproject.toml` no longer matches
`QWALO_ENGINE_URL` (one line, both places); `OP_VAULT` does not name `prod-qwalo-ere`.

If it answers `wrong_scope`, the `account_scope` and the token slot disagree — fix the
deploy, do not widen the token.

## Step 8 — turn Slack on

In the Slack app: Event Subscriptions → Request URL →
`https://ere.qwalo.raava.dev/api/webhooks/slack`, save until it shows a green check,
then enable. Mention the bot in a channel it is in:

> @Qwalo what is in the review queue today?

Expect a reply that names queue depth and the `external_id`s with the reason each waits.
Then ask it to send a follow-up to a prospect. The correct answer is a refusal that says
it has no send capability. If it tries anything else, stop and treat it as a defect, not
a UX nit.

## Step 9 — after go-live

- Watch `kubectl -n centaur-ere logs deploy/centaur-centaur-slackbotv2` for the
  first day. A stale `centaur-infra-env` Slack secret is the known cause of a silent
  outage: the Slackbot stays `Running` while Slack answers `401 invalid_auth`.
- A leaked engine token is an incident, not a rotation: the holder can write labels for
  that `account_scope` (step 4). Drop the slot from `SERVICE_TOKENS`, re-issue, replace
  the 1Password item, and review the engine's request rows for that caller.
- Roll back an application with `helm -n centaur-ere rollback centaur-ere`. Postgres has
  the client's data: never delete the namespace or its PVC to undo a bad deploy. The
  engine's D1 backups and bookmarks belong to the engine release process, not this one.
- Adding the next client is steps 1–8 with a different slug. Nothing in this overlay is
  edited per client except the engine host line when a client's engine host differs.

## Step 10 — what is not true yet

Ship this overlay knowing these are open. They are listed in `README.md` and
`FAILURE_MODES.md` F8 with source evidence:

1. **The engine read API is not merged, and that is Gate A.** `GET /v1/leads`,
   `/v1/leads/:id`, `/v1/review-queue`, `/v1/metrics` come from t-0027. This tool was
   written against `src/v1/routes.ts` for the one merged route and the t-0027 contract
   for the rest. Re-check names and fields when it lands.
2. **The persona does not reach a sandbox.** `persona_id` carries no prompt on the Rust
   control plane, and nothing writes `AGENTS_BASE.md`. The agent runs base instructions.
3. **The overlay system prompt does not reach a sandbox.** It is rendered into a
   ConfigMap mounted on api-rs only.
4. **A client release registers zero workflows, by hiding the base tree.** The chart has
   no workflow subdirectory knob, so `apiRs.extraEnv.WORKFLOW_DIRS` points at the empty
   `overlays/qwalo/workflows`. Hiding is a runtime behaviour, so step 7 reads the value
   back from the container. FAILURE_MODES F10.
5. **The engine token is not verb-scoped, and that is Gate B.** Step 4 explains the
   exposure, including that iron-proxy injects on any method to the engine host.
6. **Centaur holds no lead or seller data. Decided 2026-10-21, and enforced only as far
   as an overlay can reach.** The tool is a stateless read and the release registers no
   workflow that writes, which `lead_lookup_writes_nothing_to_a_database` and
   `overlay_ships_no_database_writer` prove. What they do not prove is that nothing is
   stored: the control plane's transcript writer keeps `session_messages` and
   `session_events` in plain text with no redaction or retention knob, so text the agent
   quotes into a reply is stored with the conversation. Closing that is a platform
   change or a persona rule, and the persona is the owners' work in progress.
7. **CI does not cover this overlay.** The E2E gate is
   `python3 overlays/qwalo/e2e/run_fake_engine.py`, and
   `overlays/qwalo/e2e/run-on-proxmox.sh` runs it on the lab guest and commits the
   artifact and the rendered release. Run it before you deploy.
8. **No tracing is configured, and that is a decision, not an absence of thought.** The
   platform would forward harness spans to any OTLP endpoint named in `apiRs.extraEnv`,
   and those spans contain prompts and tool output: the seller's text. A Qwalo release
   names no endpoint and leaves `networkPolicy.otlpEgress.enabled` false. Turning on
   Langfuse for a client is an owner decision about that client's data. FAILURE_MODES
   F12.
