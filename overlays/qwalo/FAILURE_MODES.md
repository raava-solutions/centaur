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
   write path in `client.py`: the single HTTP helper is `self._get(path, params)`
   and it is the only place a request is built. That controls the **tool path**.
2. The token is never in the sandbox. It is declared `mode = "inject"` for the
   engine host, so iron-proxy adds `Authorization: Bearer <token>` in flight, and
   inject mode seeds **no environment variable at all** (`placeholder_env()` covers
   replace-mode secrets only). `QWALO_ENGINE_TOKEN` is simply absent in a sandbox,
   and the tool sends no `Authorization` header of its own.
3. Prompt rules restate it (persona `PROMPT.md`, `services/sandbox/SYSTEM_PROMPT.md`)
   as defence in depth, never as the control.

**The hole, stated plainly.** iron-proxy matches a **host**, not a method or a
path: `HttpSecretMode::Inject` carries `hosts` plus the header and formatter, and
the compiled fragment's `rules` carry `{host}` only
(`services/api-rs/crates/centaur-iron-proxy/src/fragment.rs`). So the proxy stamps
the engine bearer on **any** request from that sandbox to that host, including a
hand-written `curl -X POST .../v1/labels`. The read-only tool surface protects the
tool path; it does not protect the sandbox.

Nothing in this overlay makes the token read-only, because the engine cannot mint a
read-only token: `TokenSlot` in `src/v1/auth.ts` carries `caller`, `sha256`, and
`account_scopes`, and no capability field. **A client must not go live on this
token.** `RELEASE.md` step 5 is a hard gate, not advice. The engine change asked
for (tracked as a bead in the engine repo): a per-slot read capability, such as
`"methods": ["GET"]`, enforced in `requireService` before the route runs, answered
403 `method_not_allowed`.

Until that exists, the honest claim is: *a sandbox cannot leak a credential it does
not hold, and its only shipped tool cannot write* — not *the token is read-only*.

**Test.** `cli_exposes_no_write_verbs`, `no_write_request_reaches_the_engine`,
`sandbox_shaped_env_sends_no_bearer`. `mutation_proves_the_placeholder_guard_bites`
records that deleting the placeholder guard puts the placeholder on the wire, so the
sibling case is not vacuous. No test here can prove the proxy's verb behaviour; that
is why the gate is a deploy gate.

## F3 — the agent attempts a seller send

**Damage.** An SMS goes to a prospect from the client's SmarterContact number without
review. This is the one failure that costs the client a customer relationship.

**Controls.**

1. The release points the sandbox tool tree at this overlay, not at the base
   `tools/` directory: `toolServer.subdir: overlays/qwalo/tools`, `extraSources:
   []`. That distinction is the whole control. The base tree ships
   `tools/comms/telegram` with a working `send_message`, plus discord and twitter,
   so mounting it would have made "this agent cannot message anyone" false on its
   face. With the overlay tree mounted, `engine` is the only CLI shim a sandbox
   gets. No `sc-connector` tool, no SmarterContact credential, no send endpoint.
2. The engine itself has no send route at all (`src/v1/routes.ts`: qualify, verdict,
   labels) — a read token cannot become a send through it.
3. NetworkPolicies already deny sandbox egress except to the API, the proxy, DNS and
   the configured egress list; SmarterContact is never added for a Qwalo release.
4. `PROMPT.md` forbids it and tells the agent to say it cannot, so the answer a human
   sees is honest rather than a silent tool error.

**Test.** `only_the_engine_tool_ships` (the overlay ships one tool) and
`the_sandbox_gets_only_the_overlay_tool_tree`, which reads the rendered values and
re-proves that the base telegram tool still can send, so the assertion is about a
live threat rather than an assumption.

## F4 — a missing, expired, or placeholder engine token

**Damage.** Silent wrong answer: the agent reads "no leads" from an error body and
tells the owner the queue is empty.

**Controls.**

1. No engine URL, or no `account_scope`, → the tool refuses the call locally with
   `engine_url_missing` / `account_scope_missing` and exit code 1, without sending a
   request. A release that cannot prove which client it is reading for reads nothing.
2. The sandbox shape (no token variable, inject mode) and a leftover placeholder
   value both mean the tool sends no `Authorization` header; the engine's 401 comes
   back as `invalid_token`, never as an empty result.
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
`sandbox_shaped_env_sends_no_bearer`, `placeholder_token_is_never_sent`,
`invalid_token_is_reported`, `engine_503_is_reported`, `engine_404_is_reported`,
`engine_unreachable_is_reported`.

## F5 — Slack events arrive before the release is ready

**Damage.** Slack retries a URL that 502s while pods roll, the manifest's request URL
still points at the previous client's host, or the first mentions of a client land on
a half-configured stack. Slack drops the app after repeated failures.

**Controls.**

1. The manifest names **this client's host only** (`<client>.<domain>`), and there is
   no shared router host for a retry to land on. The scopes are the reading
   minimum (`app_mentions:read`, `chat:write`, channel read/history, `users:read`)
   and the one subscribed event is `app_mention`: no channel firehose, and no
   `chat:write.public`, so the bot cannot post unbidden into a channel it was
   never invited to.
2. What actually guards the order is our own procedure plus isolation, not a Slack
   courtesy. `RELEASE.md` puts the URL last, after the rollout and the in-sandbox
   probes, and a human sets it by hand. Until it is saved, nothing is delivered
   anywhere. Each client's URL names its own host, so a URL saved for one client
   can only reach that client's slackbotv2. Slack's URL verification (the
   `challenge` POST it expects back) makes an unreachable host fail visibly at save
   time; that is an assumption to observe at step 8, not the control.
3. `deploy-client.sh` refuses to render or apply when the client name is not a DNS
   slug, when the image tag or tool ref is unpinned, or when a required 1Password
   reference is unreadable, so a half-typed client cannot produce a half-deploy.
4. Each client has its own host (`<client>.qwalo.raava.dev`) and its own namespace, so
   a late-arriving retry for one client can never be served by another client's
   Slackbot.

**Test.** `manifest_names_only_this_client_host` (including `bot_events == ["app_mention"]`),
`values_template_has_no_secret_values`, `deploy_script_rejects_bad_client`,
`deploy_script_gates_the_apply`, `no_double_scheme_url_anywhere`.

## F6 — secrets in values files

**Damage.** A bot token, signing secret, or engine token committed to
`raava-solutions/centaur` is live in every clone of a repo that is not private
forever. Also: the same secret leaking from client A's values file into client B's.

**Controls.**

1. `deploy/values.client.yaml.tmpl` contains no secret material at all — only
   `<<CLIENT>>`, `<<NAMESPACE>>`, `<<HOST>>`, `<<ENGINE_URL>>`, `<<ENGINE_SCOPE>>`,
   `<<REGISTRY>>`, `<<TAG>>`, `<<TOOL_REF>>` placeholders and non-secret config.
2. `deploy-client.sh` reads each per-client secret from
   `op://prod-qwalo-<client>/<SECRET_NAME>/credential` and writes the three Slack
   ones and the model key into the `centaur-infra-env` Kubernetes Secret in that
   client's namespace, the same consumption path the base chart uses
   (`secretManager.existingSecretName`). The item name is the secret name and the
   field is `credential` because that is the shape the platform itself resolves:
   `centaur-iron-control/src/registry.rs` builds `op://{vault}/{name}/credential`.
   An item named in prose, or a field named `password`, never resolves.
3. The rendered values file is written under a `mktemp -d` directory with a `trap`
   cleanup; it is never written into the repo or a durable path.
4. The script `unset`s the four exported secrets on exit and never echoes secret
   values; it prints reference paths only.
5. The engine token is a 1Password item, not a `values` key, because a values file is
   diffed, pasted into Slack, and shown in CI logs. `RELEASE.md` repeats the rule.
6. The engine token never becomes a Kubernetes Secret at all: it is an iron-proxy
   inject-mode secret read from the client's own vault, so there is no copy in the
   cluster and no environment variable in a sandbox.
7. One 1Password service account per client vault, scoped to that vault. The script
   lists what its token can see and refuses to run when the same token reaches
   another `prod-qwalo-*` vault, because that token could fund another client's
   release.

**Test.** `values_template_has_no_secret_values`, `deploy_script_gates_the_apply`,
`engine_secret_ref_matches_the_proxy_lookup`,
`deploy_script_refuses_a_token_that_sees_other_vaults`.

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

The same section names what **is** live, and one item there is a control, not a
convenience: `toolServer.subdir: overlays/qwalo/tools` is what keeps the base
`tools/` tree, with its messaging plugins, out of the sandbox.

**Test.** `inert_pieces_are_declared` — the runner fails if README.md stops naming the
inert pieces, so the caveat cannot rot into a lie.
