# Release a Qwalo client — ERE worked end to end

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

## Step 0 — what only a human may do

You need, and this release does not create or assume:

| Access | Used for |
|---|---|
| Slack workspace admin in the client's workspace (or a co-admin who installs the app) | create the app from the manifest, install it, set the event URL |
| Write access to the `prod-qwalo-<client>` 1Password vault | create the four items in step 3 |
| `op` CLI plus `OP_SERVICE_ACCOUNT_TOKEN` on the deploy machine | `deploy-client.sh` reads secrets at apply time |
| `kubectl` context for the cluster, `helm`, and `GITHUB_TOKEN` for the repo-cache clone of a private `raava-solutions/centaur` | the deploy itself |
| Wrangler access to `raava-solutions/qwaelo-ai-engine` | issue and register the engine token in step 4 |

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

Create the app with the Slack CLI (`slack app manifest validate /create`) or by
importing the JSON at <https://api.slack.com/apps?new_app=1>, in the **client's**
workspace. Install it and grant the scopes.

The manifest asks for `app_mentions:read`, `chat:write`, channel and group history and
read, and `users:read`. It does **not** ask for `chat:write.public`: the bot cannot
post into a channel it was not invited to. `reactions:write` is absent too — the
Raava internal overlay has it, a client-facing reader does not need it.

Event subscriptions cannot be enabled yet. Slack verifies `request_url` before it will
turn them on, and `https://<client>.qwalo.raava.dev` does not answer until step 6. That
failure is the safety property in `FAILURE_MODES.md` F5: an app created before the
deploy cannot receive mentions and cannot half-work.

## Step 3 — store the app's secrets in 1Password

Create vault `prod-qwalo-ere` (or ask the vault owner to), with one item per row, each
with a `password` field. The names are exact: `deploy-client.sh` refuses to deploy when
one of them cannot be read.

| Item | Field | What goes in it |
|---|---|---|
| `slack bot token` | password | the `xoxb-…` from step 2 |
| `slack signing secret` | password | the `Signing Secret` from Basic Information |
| `slackbot api key` | password | the Centaur API key this Slackbot authenticates with (`api_keys` row, `aiv2_*`) |
| `engine v1 token` | password | the bearer token from step 4 |

`deploy-client.sh` puts the first three into the `centaur-infra-env` Secret in the
client's namespace. The fourth is never copied anywhere by this script: iron-proxy reads
it from this vault at request time.

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
has no downtime. Store the plaintext in `prod-qwalo-ere/engine v1 token`.

Understand what this token is and is not:

- **It is scoped by account.** Requests for another `account_scope` get 403
  `wrong_scope`, and a lead outside the scope reads as 404. One client's token cannot
  enumerate another client's leads.
- **It is not scoped by verb.** `TokenSlot` in `src/v1/auth.ts` carries
  `caller`, `sha256`, and `account_scopes` only. A holder of this token **can** call
  `POST /v1/qualify` and `POST /v1/labels` for `ere`. The Centaur agent cannot, because
  its tool has no write path and the sandbox never holds the bearer — but a leaked
  token is a write-capable leak. Ask the engine team for read-scoped slots
  (`overlays/qwalo/README.md` → Tracing/F2 in `FAILURE_MODES.md`).

## Step 5 — confirm the engine answers for this scope before the client can see a page

The four read routes this overlay calls (`GET /v1/leads`, `/v1/leads/:id`,
`/v1/review-queue`, `/v1/metrics`) are thread t-0027's work and **are not merged as of
this PR**. Do not deploy a client release until they are on the production Worker. From
inside the engine's own E2E harness or with the smoke token:

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

6b. Deploy for real:

```bash
export OP_SERVICE_ACCOUNT_TOKEN=... GITHUB_TOKEN=...
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
kubectl -n centaur-ere rollout status deploy/centaur-ere-centaur-api-rs \
                                  deploy/centaur-ere-centaur-slackbotv2
kubectl -n centaur-ere get secret centaur-infra-env -o 'jsonpath={.data}' | tr ',' '\n'   # key names only, never values
```

Now prove the token works **from a sandbox**, which is the only place iron-proxy is in
the path. Exec into a running sandbox pod and run the tool as the agent runs it:

```bash
POD=$(kubectl -n centaur-ere get pods -l centaur-agent=true -o jsonpath='{.items[0].metadata.name}')
kubectl -n centaur-ere exec "$POD" -- sh -lc 'engine metrics'
kubectl -n centaur-ere exec "$POD" -- sh -lc 'engine leads --limit 5'
kubectl -n centaur-ere exec "$POD" -- sh -lc 'echo "$QWALO_ENGINE_TOKEN"'   # must print QWALO_ENGINE_TOKEN, the placeholder
```

Expected: `engine metrics` returns totals. `engine leads` returns the client's leads.
The token probe prints the placeholder string and nothing else. If `engine metrics`
answers `invalid_token`, iron-proxy is not injecting: the usual cause is that the engine
host in `overlays/qwalo/tools/engine/pyproject.toml` no longer matches
`QWALO_ENGINE_URL` (one line, both places), or the vault named `prod-qwalo-ere` is not
reachable with the token in `OP_VAULT`.

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

- Watch `kubectl -n centaur-ere logs deploy/centaur-ere-centaur-slackbotv2` for the
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

1. **The engine read API is not merged.** `GET /v1/leads`, `/v1/leads/:id`,
   `/v1/review-queue`, `/v1/metrics` come from t-0027. This tool was written against
   `src/v1/routes.ts` for the one merged route and the t-0027 contract for the rest.
   Re-check names and fields when it lands. Until then this release has nothing to read.
2. **The persona does not reach a sandbox.** `persona_id` carries no prompt on the Rust
   control plane, and nothing writes `AGENTS_BASE.md`. The agent runs base instructions.
3. **The overlay system prompt does not reach a sandbox.** It is rendered into a
   ConfigMap mounted on api-rs only.
4. **Overlay workflows are not discovered.** Qwalo ships none.
5. **The engine token is not verb-scoped.** Step 4 explains the exposure.
6. **A human decides whether lead text may live in Centaur's Postgres.** The base
   platform persists transcripts and execution events. Answer it before step 8, not
   after an owner asks.
7. **CI does not cover this overlay.** The E2E gate is
   `python3 overlays/qwalo/e2e/run_fake_engine.py`; run it before you deploy.
