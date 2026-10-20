# Qwalo overlay — failure modes

Written before the code. Each row names the damage, the control that stops it, where
the control lives, and the E2E case that proves it. Cases are `e2e/run_fake_engine.py`
case names; their evidence is in `e2e/reports/fake-engine.json`.

The threat model is simple: the agent is an untrusted program. Lead text it reads is
untrusted data. Everything it may do is decided by what the tool exposes and by which
token the engine accepts, not by the prompt.

## F1 — one client's release reaches another client's leads

**Damage.** ERE's agent answers a question with Elevate's (or any other client's)
leads, names, and replies. Cross-client disclosure; unrecoverable trust damage.

**Controls.**

1. Per-client namespace, per-client release, per-client Slack app. `deploy-client.sh`
   renders a values file whose release name and namespace are both `centaur-<client>`;
   two clients never share a control plane, a database, or a warm-pool sandbox.
2. Per-client engine token, scoped to one `account_scope` server-side. The engine
   answers 403 `wrong_scope` for any scope the token does not hold, and 404 for
   resources outside it (engine `src/v1/auth.ts`, `scopeOf()` in `src/v1/routes.ts`).
3. The tool cannot choose a scope. `QWALO_ACCOUNT_SCOPE` is read from the deployment
   environment and injected into every request; there is no `--account-scope` flag and
   no `account_scope` parameter on any method. An agent that names another client in
   prose changes nothing.
4. The release refuses to start when `QWALO_ACCOUNT_SCOPE` is absent or differs from
   the client it deploys (`deploy-client.sh` gate), so a copy-paste of a values file
   cannot serve ERE data under the `acme` name.

**Test.** `scope_is_forced_by_env`, `cross_scope_request_is_refused`,
`fixtures_carry_no_real_contact_data`.

## F2 — an engine token with write capability reaches an agent

**Damage.** The agent (or a prompt injected through a lead's message text) POSTs a
qualification, writes a human label, or settles a verdict. The engine's audit trail
becomes untrustworthy and a model starts deciding on customers.

**Controls.**

1. The tool exposes GET methods only. There is no `qualify`, `label`, or any other
   write path in `client.py`, so nothing in the CLI can emit a non-GET request: the
   single HTTP helper is `self._get(path, params)` and `httpx` is called only there.
2. `pyproject.toml` declares no write-capable secret, and the token that is declared
   never exists inside the sandbox (F2.3). The real backstop is therefore that a
   sandbox has no credential to write with: **engine service tokens are scoped by
   `account_scope` only, never by verb** (`TokenSlot` in `src/v1/auth.ts` carries
   `caller`, `sha256`, `account_scopes`). A leaked agent token can call
   `POST /v1/qualify` and `POST /v1/labels` for its own scopes. `RELEASE.md` treats
   that as the incident it is, and asks the engine team for read-scoped slots.
3. In production the sandbox never holds the token. The secret is declared
   `mode = "inject"` for the engine host, so iron-proxy adds
   `Authorization: Bearer <token>` in flight; the in-sandbox value is the placeholder
   string `QWALO_ENGINE_TOKEN` (`placeholder_env()` in
   `services/api-rs/crates/centaur-iron-proxy/src/fragment.rs`).
4. Prompt rules restate it (persona `PROMPT.md`, `services/sandbox/SYSTEM_PROMPT.md`)
   as defence in depth, never as the control.

**Test.** `cli_exposes_no_write_verbs`, `no_write_request_reaches_the_engine`,
`placeholder_token_is_never_sent`. The mutation note in `e2e/reports/fake-engine.json`
records that deleting the placeholder guard makes the tool send the placeholder, so the
case is not vacuous.

## F3 — the agent attempts a seller send

**Damage.** An SMS goes to a prospect from the client's SmarterContact number without
review. This is the one failure that costs the client a customer relationship.

**Controls.**

1. The overlay ships exactly one tool: `engine`. No `sc-connector` tool, no SmarterContact
   credential, no send endpoint. The agent cannot send because the capability is absent.
2. The engine itself has no send route at all (`src/v1/routes.ts`: qualify, verdict,
   labels) — a read token cannot become a send through it.
3. NetworkPolicies already deny sandbox egress except to the API, the proxy, DNS and
   the configured egress list; SmarterContact is never added for a Qwalo release.
4. `PROMPT.md` forbids it and tells the agent to say it cannot, so the answer a human
   sees is honest rather than a silent tool error.

**Test.** `only_the_engine_tool_ships` (fails if any tool other than `engine` appears
under `overlays/qwalo/tools/`, personas excepted).

## F4 — a missing, expired, or placeholder engine token

**Damage.** Silent wrong answer: the agent reads "no leads" from an error body and
tells the owner the queue is empty.

**Controls.**

1. No engine URL, or no `account_scope`, → the tool refuses the call locally with
   `engine_url_missing` / `account_scope_missing` and exit code 1, without sending a
   request. A release that cannot prove which client it is reading for reads nothing.
2. Placeholder-only (proxy did not inject, or the engine host does not match the
   declared secret host) → the tool does not send `Bearer QWALO_ENGINE_TOKEN`; the
   engine's 401 is reported as `invalid_token`.
3. Engine misconfigured (`SERVICE_TOKENS` unset → 503 `service_auth_not_configured`),
   expired or wrong token (401 `invalid_token`), wrong scope (403 `wrong_scope`), no
   such lead (404 `lead_not_found`), any other 4xx/5xx (`engine_http_error`), an
   unreachable host (`engine_unreachable`), and a non-JSON body
   (`engine_bad_response`) are each mapped to a named code in `_http_error` and
   `_get`. The engine's own code is carried through so the agent reports the real
   reason.
4. Every failure path exits non-zero, so a turn that would treat a tool failure as an
   empty result still shows a failing command in the transcript.
5. Human-facing: `RELEASE.md` step 7 probes the token from a running sandbox before
   anyone asks the bot a question.

**Test.** `missing_engine_url_fails_closed`, `missing_account_scope_fails_closed`,
`placeholder_token_is_never_sent`, `invalid_token_is_reported`,
`engine_503_is_reported`, `engine_404_is_reported`, `engine_unreachable_is_reported`.

## F5 — Slack events arrive before the release is ready

**Damage.** Slack retries a URL that 502s while pods roll, the manifest's request URL
still points at the previous client's host, or the first mentions of a client land on
a half-configured stack. Slack drops the app after repeated failures.

**Controls.**

1. The manifest names **this client's host only** (`<client>.<domain>`), and there is
   no shared router host for a retry to land on. The scopes are the reading minimum:
   `app_mentions:read`, `chat:write`, channel/group history and read, `users:read`.
   `chat:write.public` is deliberately absent, so the bot cannot post unbidden into a
   channel it was never invited to.
2. The manifest's `request_url` points at a host that does not answer until this
   release is live. Slack will not enable event subscriptions against an unverifiable
   URL, so an app created before the deploy cannot half-work: mentions simply do not
   arrive until step 8.
3. `RELEASE.md` puts the URL last, after `rollout status` is green and the
   engine-token probe passed.
4. `deploy-client.sh` refuses to render or apply when the client name is not a DNS
   slug, or when a required 1Password reference is unreadable, so a half-typed client
   cannot produce a half-deploy.
5. Each client has its own host (`<client>.qwalo.raava.dev`) and its own namespace, so
   a late-arriving retry for one client can never be served by another client's
   Slackbot.

**Test.** `manifest_names_only_this_client_host`,
`values_template_has_no_secret_values`, `deploy_script_rejects_bad_client`,
`deploy_script_gates_the_apply`.

## F6 — secrets in values files

**Damage.** A bot token, signing secret, or engine token committed to
`raava-solutions/centaur` is live in every clone of a repo that is not private
forever. Also: the same secret leaking from client A's values file into client B's.

**Controls.**

1. `deploy/values.client.yaml.tmpl` contains no secret material at all — only
   `<<CLIENT>>`, `<<HOSTNAME>>`, `<<ENGINE_HOST>>` placeholders and non-secret config.
2. `deploy-client.sh` reads the three per-client secrets from
   `op://prod-qwalo-<client>/...` with the 1Password CLI at apply time and writes them
   into the `centaur-infra-env` Kubernetes Secret in that client's namespace, the same
   consumption path the base chart uses (`secretManager.existingSecretName`).
3. The rendered values file is written under a `mktemp -d` directory with a `trap`
   cleanup; it is never written into the repo or a durable path.
4. The script `unset`s `OP_SERVICE_ACCOUNT_TOKEN`/`OP_CONNECT_TOKEN` after use and
   never echoes secret values; it prints reference paths only.
5. The engine token is a 1Password item, not a `values` key, because a values file is
   diffed, pasted into Slack, and shown in CI logs. `RELEASE.md` repeats the rule.
6. The engine token never becomes a Kubernetes Secret at all: it is an iron-proxy
   inject-mode secret read from the client's own vault, so there is no copy in the
   cluster to leak.

**Test.** `values_template_has_no_secret_values`, `deploy_script_reads_op_refs_only`,
`deploy_script_gates_the_apply`.

## F7 — lead text (PII) leaves the client's boundary

Not on the required list; it is the reason the read-only rule exists.

**Damage.** Seller phone numbers and message text from client A appear in another
tenant's logs, in a Slack message, or in a prompt for a model the client never
approved.

**Controls.**

1. The tool returns engine responses unchanged and adds no logging of lead bodies.
2. The engine's v1 surface already stores no seller PII and sanitises rationales
   (`src/v1/routes.ts` header, `sanitizeReason`), so a read of `/v1/leads` cannot pull
   more than the engine is willing to show.
3. Anything the agent reads lands in the client's own Slack workspace only — one app,
   one workspace, one namespace per client.
4. The E2E fixtures are synthetic ids with no contact data, so the repo, its tests,
   and the committed artifact cannot become where a client's leads leak.
5. Open question for the owner, recorded here, not decided in code: whether lead text
   may be retained in Centaur's Postgres for a client release. The base platform
   stores transcripts and execution events. `RELEASE.md` flags it as a human decision
   before go-live.

**Test.** `fixtures_carry_no_real_contact_data`.

## F8 — the release is inert and green anyway

**Damage.** The persona prompt and the sandbox system prompt are shipped, tested, and
never reach a sandbox. The overlay looks deployed and behaves like the base agent.

**Why it is real.** `deploy/READINESS.md` in the raava overlay records that on the Rust
control plane `persona_id` carries no prompt, and `overlay.systemPrompt` renders a
ConfigMap mounted only on api-rs (`contrib/chart/templates/apirs.yaml:133`,
`CENTAUR_OVERLAY_DIR` on api-rs only; no sandbox template consumes it). This overlay
inherits both gaps. `README.md` states which parts are live today. Do not claim the
persona or the Qwalo system prompt is active until the chart mounts
`CENTAUR_OVERLAY_DIR`/`AGENTS_OVERLAY.md` into sandbox pods.

**Test.** `inert_pieces_are_declared` — the runner fails if README.md stops naming the
inert pieces, so the caveat cannot rot into a lie.
