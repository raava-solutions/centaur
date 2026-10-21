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
`engine_unreachable_is_reported`, `page_size_cannot_crash_the_tool`.

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
`deploy_script_gates_the_apply`, `deploy_requires_pinned_release_identity`,
`manifest_mode_writes_the_file_it_promises`, `check_reports_the_deploy_facts`,
`no_double_scheme_url_anywhere`.

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
client's Slack, another client's database, a shared log, or a prompt for a model the
client never approved. Note what is *not* the damage any more: showing a client's own
seller text to that client's reviewers is the product (F7 control 5).

**Controls.**

1. The tool returns engine responses unchanged and adds no logging of lead bodies.
2. The engine's v1 surface already stores no seller PII and sanitises rationales
   (`src/v1/routes.ts` header, `sanitizeReason`), so a read of `/v1/leads` cannot pull
   more than the engine is willing to show.
3. Anything the agent reads lands in the client's own Slack workspace only — one app,
   one workspace, one namespace per client.
4. The E2E fixtures are synthetic ids with no contact data, so the repo, its tests,
   and the committed artifact cannot become where a client's leads leak.
5. Decided by the owner on 2026-10-21, replacing the 2026-10-20 rule for this client's
   own release: **a reviewer may see the seller's text messages.** A reviewer cannot
   judge what they are approving from a count, so the message text is in scope for the
   client's own release.
   - **In scope:** this client's Slack workspace, and this client's Centaur release.
     One app, one workspace, one namespace, one `account_scope` — F1, F3 and F5 are
     what keep that true.
   - **Not in scope:** any other client, any shared surface, and any third-party trace
     sink. See F12 for why a tracing endpoint is the easiest way to break this quietly.
   - **Open question, for the owners, not decided here:** does seller text *persist* in
     Centaur's Postgres, or live only inside the turn that read it? The facts the answer
     has to fit: the Rust control plane writes `session_messages` and `session_events`
     with no redaction or retention knob, so today anything the agent shows is also
     stored, and nothing in an overlay can make it otherwise. If the answer is "in-turn
     only", that is a platform ask, not an overlay setting.
      - **Nothing in this overlay decides what the agent may show.** The sandbox prompt
     carries no rule about seller text, and none is being added while the owners settle
     the overlay and the persona: which words a reviewer may see is their call, not a
     runbook's. No E2E case here claims to prove a posture.
   - The platform fact any answer has to survive: the Rust control plane writes
     `session_messages` and `session_events` in plain text with no redaction or
     retention knob, so anything shown in a turn is also stored, and no overlay setting
     changes that.
6. Nothing may pull conversation text in from outside the turn uninvited. The base
   repo's workflow tree ships `slack_backfill.py` and `slack_sync.py`, which call
   `upsert_messages` and backfill a whole channel's history into Postgres. Whether or
   not seller text may be *shown*, an unattended backfill of every conversation is a
   different act from answering a question, and a client release does not register it.
   See F10.

**Test.** `fixtures_carry_no_real_contact_data` (the repo and its artifact carry none),
F10 for the ingest path, F12 for the export path. Showing seller text inside one client
is a product decision rather than a control, and whether it persists in Postgres is an
open owner question; no case here claims to prove either.

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

## F9 — the client release boots with no database

**Damage.** Every sandbox in the namespace fails its first tool call, and the API
restarts in a loop, because the connection string names a host that was never created.

**Why it is real.** `contrib/scripts/bootstrap-k8s-secrets.sh:183` writes
`DATABASE_URL=postgresql://tempo:…@centaur-centaur-postgres:5432/ai_v2` into
`centaur-infra-env`. That host is `<release>-<chart>-postgres`, and it is baked at
bootstrap time, not resolved at deploy time. Naming the Helm release after the client
— the obvious thing to do for a per-client release — produces
`centaur-ere-centaur-postgres`, and nothing creates it.

**Controls.** `RELEASE="centaur"` in `deploy-client.sh`, in every namespace. Isolation
comes from the namespace, which is what a Helm release name is scoped to anyway. The
name is therefore load-bearing and cannot be chosen per client, and the E2E case reads
the baked host out of the bootstrap script and derives it from the release name the
script reports, so a rename fails the suite rather than the client.

`RELEASE.md` step 7 proves it on the real cluster: the decoded `DATABASE_URL` host and
`kubectl get svc` must agree.

**Test.** `release_name_keeps_the_database_host`, and the postgres-service assertion in
`values_template_renders_in_the_real_chart`.

## F10 — a client release registers the base workflow tree

**Damage.** A workflow nobody in this deployment asked for runs on a schedule and
writes a client's channel history into Postgres, while the owners are still deciding
whether seller text should persist at all. Nothing fails, no pod restarts, and the
retention is invisible until someone asks for it back.

**Why it is real.** `contrib/chart/templates/apirs.yaml:12` builds `WORKFLOW_DIRS` as
`<repoCache.hostPath>/<toolServer.repo>/workflows` with no subdirectory knob, so
`toolServer.subdir` narrows the tool tree and leaves the workflow tree alone. The base
tree holds eleven server-side workflows, including `slack_backfill.py` and
`slack_sync.py`, both of which call `upsert_messages`. `google_calendar_sync`,
`linear_sync`, `github_issue_triage` and `google_drive_sync` would register against
secrets a client vault does not hold, and read as broken integrations rather than
missing grants.

**Controls.**

1. `apiRs.extraEnv.WORKFLOW_DIRS` points at `overlays/qwalo/workflows`, an empty
   directory kept in the repo for exactly that purpose. `apirs.yaml` emits `extraEnv`
   after every built-in, and a container takes the last definition of a name, so the
   base path is hidden and the release registers zero workflows.
2. Hiding is a runtime property of the kubelet, not of the manifest, so `RELEASE.md`
   step 7 reads `WORKFLOW_DIRS` back out of the running container. One base path in
   that output is a live leak: stop traffic to the release.
3. A client workflow is added by putting one file in `overlays/qwalo/workflows`, which
   keeps the tenant boundary in the file layout instead of in a schedule.

**Test.** `values_template_renders_in_the_real_chart` asserts both entries are present,
in order, with the overlay path last, and records `workflow_dirs_seen` in the artifact.

## F11 — the release is Ready, healthy, and deaf

**Damage.** Slack posts events to the client's host and gets nothing back. Pods are
`Ready`, the Ingress exists, the rollout succeeded, and the only symptom is a bot that
never answers. Slack retries, then marks the request URL failing.

**Why it is real.** The chart is deny-by-default (`contrib/chart/templates/networkpolicy.yaml`)
and admits inbound webhook traffic only from `networkPolicy.ingressControllerNamespaces`,
whose chart default is `[kube-system]` (`contrib/chart/values.yaml:331`). Whatever
terminates TLS for `<client>.qwalo.raava.dev` may live in another namespace, and the
values template that forgets to say so still installs cleanly.

**Controls.**

1. The values template names `networkPolicy.ingressControllerNamespaces` explicitly, so
   the assumption is a line a reviewer reads rather than a chart default nobody chose.
2. `RELEASE.md` step 1 makes naming that namespace part of creating the network path,
   before any event is sent, and requires one real Slack event plus a log check before
   the release is trusted.
3. Public reach is a Cloudflare Tunnel, never Tailscale Funnel.

**Test.** `values_template_names_the_webhook_source_namespace`, and the admitted-namespace
assertion in `values_template_renders_in_the_real_chart`. The end to end fact — an event
delivered through the real tunnel — cannot be proven without a cluster and a Slack app,
and `RELEASE.md` step 1 is where a human proves it.

## F12 — seller text is exported to a trace sink

**Damage.** Every prompt, tool result, and harness turn from a client's release is
streamed to a third-party observability product, in clear text, under a different
tenant's account. Nothing in the release looks different. The client's seller
conversations are now a search box in someone else's UI, and it is the one exfiltration
path that survives every other control in this file, because it is switched on with four
values entries and one NetworkPolicy flag.

**Why it is real.** The platform already has the pipe. `SANDBOX_OTLP_PASSTHROUGH_ENV_KEYS`
(`services/api-rs/crates/centaur-api-server/src/args.rs`) forwards
`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, and `OTEL_RESOURCE_ATTRIBUTES` from api-rs into every
sandbox, and `services/sandbox/codex-app-wrapper.py` writes them into codex's
`[otel] trace_exporter` block. Langfuse ingests OTLP at `/api/public/otel/v1/traces`.
So `apiRs.extraEnv` plus `networkPolicy.otlpEgress.enabled = true` is a working
Langfuse integration, with no code anywhere and no test failing.

**Controls.**

1. A Qwalo release ships no tracing endpoint. The values template sets no `OTEL_*` key,
   and `networkPolicy.otlpEgress.enabled` stays at its chart default of `false`, so
   there is no destination and no route to one. The committed render contains no
   `OTEL` line at all, which is what the assertion in
   `values_template_renders_in_the_real_chart` checks.
2. `README.md` documents the plug-in point rather than pretending it does not exist.
   The reason it is written down is that a future integrator must find it and find this
   row, not discover it by turning it on inside a client's release.
3. If tracing is ever wanted for a client, it goes to a collector inside that client's
   own boundary, named in that client's values file, with the client's approval — spans
   carry prompts and tool output, which is the seller's text.

**Open, for the owners:** tracing and the seller-text decision point the same direction.
A trace sink is also the fastest way to answer "does it persist?" in the worst available
way. `RELEASE.md` step 10 lists this as not-configured, not as forbidden-forever.

**Test.** `values_template_renders_in_the_real_chart` (no `OTEL_*` in the rendered
release). The negative is the whole proof; there is nothing to observe at runtime
because nothing is configured.
