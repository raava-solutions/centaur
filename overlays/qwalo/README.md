# Qwalo Centaur overlay — one release per client

> **Scaffolding, not a shipping release.** The engine's read API is not merged, the
> persona and overlay system prompt do not reach a sandbox pod on the Rust control
> plane, and the engine cannot yet mint a read-only token. Read
> [`deploy/RELEASE.md`](deploy/RELEASE.md) step 5 before anyone talks about going
> live. What is here is the shape of the release, gated and tested, waiting on those
> three things.

The agent surface for Qwalo's lead-qualification product. Each client gets its own
Centaur release: its own namespace, its own Slack app, its own engine token scoped to
one `account_scope`. No Marketplace, no shared router, no shared database. ERE
(workspace `elevaterepartners`) is the first client and the worked example in
[`deploy/RELEASE.md`](deploy/RELEASE.md).

What the agent can do: read leads, read a verdict, read the review queue, read
metrics, and explain them. What it cannot do: talk to SmarterContact, send anything to
a seller, write a verdict or a label, or see another client. The controls are in code
and topology, not in the prompt — [`FAILURE_MODES.md`](FAILURE_MODES.md) is the ledger,
and `e2e/run_fake_engine.py` proves most of it.

One naming fact up front, because it surprises everyone: the Kubernetes **namespace**
is per client (`centaur-ere`) and the **Helm release name is not** — it is `centaur` in
every namespace. Release names are namespace-scoped, so isolation does not need a
different name, and `centaur-infra-env` bakes the database host as
`centaur-centaur-postgres`. FAILURE_MODES F9.

## Decisions for the first client

Taken by the owner on 2026-10-20, recorded so nothing in this overlay re-decides them:

| Decision | Value |
|---|---|
| Client slug and namespace | `ere`, `centaur-ere` |
| Host | `ere.qwalo.raava.dev` |
| 1Password vault | `prod-qwalo-ere`, exists, empty until the engine issues a token |
| Seller text | May be shown to that client's reviewers, in that client's Slack and Centaur release only. Never across clients, never into a trace sink (F7, F12) |
| Seller text and Postgres | Open question for the owners, not decided here: persist, or in-turn only. Today the control plane stores `session_messages` and `session_events` with no redaction knob, so whatever is shown is also stored |
| Service account `sa-prod-qwalo-ere` | A human creates it in the 1Password web UI; no script and no agent creates it |

## Layout

| Path | What it is |
|---|---|
| `FAILURE_MODES.md` | Threats, controls, and the E2E case that proves each control |
| `tools/engine/` | The only tool: GET-only client for the engine `/v1` API, plus the `engine` CLI shim |
| `tools/personas/qwalo/` | Persona id and prompt: read-only qualification reporter |
| `services/sandbox/SYSTEM_PROMPT.md` | Overlay sandbox prompt (`overlay.systemPrompt`) |
| `deploy/values.client.yaml.tmpl` | Per-client values; placeholders only, never secrets |
| `deploy/deploy-client.sh` | `--check` renders and validates; `--values` writes the values a deploy applies; `--manifest` writes the Slack app manifest; `--vault-scope` runs the 1Password scope gate alone; bare run deploys one client |
| `deploy/slack-app-manifest.json.tmpl` | Slack app manifest, one app per client workspace |
| `deploy/RELEASE.md` | ERE end to end, including the steps only a human may take |
| `e2e/run_fake_engine.py` | The E2E gate: the tool against a fake engine, the real chart render, artifact recorded |
| `e2e/run-on-proxmox.sh` | Runs that gate on the lab guest and commits the artifact and the rendered release back here |
| `e2e/reports/fake-engine.json` | Committed artifact of a green run |
| `e2e/reports/client-release.rendered.yaml` | The 18 resources `helm template` produces for `ere`, from the shipping script's own values |
| `workflows/` | Empty on purpose: the directory a client release points `WORKFLOW_DIRS` at (FAILURE_MODES F10) |
| `Dockerfile` | Overlay image (`COPY . /overlay`), the same shape as the raava overlay |

## Engine contract

`tools/engine/client.py` speaks the read surface of the engine `/v1` API:

| Command | Request |
|---|---|
| `engine leads` | `GET /v1/leads` |
| `engine lead <id>` | `GET /v1/leads/:id` |
| `engine verdict <id>` | `GET /v1/leads/:external_id/verdict` |
| `engine queue` | `GET /v1/review-queue` |
| `engine metrics` | `GET /v1/metrics` |

Every request carries `account_scope` from `QWALO_ACCOUNT_SCOPE` and never from the
caller. Auth is a bearer service token; the engine's own error codes
(`invalid_token`, `wrong_scope`, `lead_not_found`, `service_auth_not_configured`)
pass through into the tool's reply so the agent can name the failure.

In a sandbox there is **no token variable at all**. The secret is iron-proxy
inject-mode, and inject mode seeds no placeholder env var (`placeholder_env()`
covers replace-mode secrets only), so the tool sends no `Authorization` header and
iron-proxy adds the real bearer on the way out. The tool still refuses to send a
value that equals its own name, which is the shape a replace-mode or hand-set
placeholder has.

Source of truth: `src/v1/routes.ts` and `src/v1/auth.ts` in
`raava-solutions/qwaelo-ai-engine`. Of those five routes only
`GET /v1/leads/:external_id/verdict` is merged today. The other four are thread
t-0027's work; when its `docs/v1-api.md` lands, re-read `client.py` and
`e2e/run_fake_engine.py` against it and fix any drift in path names, pagination
(`limit` / `cursor` / `status`), and response field names. Until then the fake engine
speaks the shapes asserted here, not the shapes t-0027 will ship, and this overlay is
**not** ready to deploy against production.

## Tracing (where Langfuse plugs in)

The raava overlay has no Langfuse hook and neither does the base platform, so this
overlay ships no tracing code. The plug-in point already exists and it is OTLP:

1. `SANDBOX_OTLP_PASSTHROUGH_ENV_KEYS` in
   `services/api-rs/crates/centaur-api-server/src/args.rs` forwards
   `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`,
   `OTEL_EXPORTER_OTLP_HEADERS`, and `OTEL_RESOURCE_ATTRIBUTES` from api-rs into every
   sandbox.
2. `services/sandbox/codex-app-wrapper.py` reads them and writes codex's `[otel]`
   `trace_exporter = { otlp-http = { ... } }` block, so harness turns export spans.
3. Langfuse ingests OTLP over HTTP (`/api/public/otel/v1/traces`), so a Qwalo release
   points those same variables at Langfuse with its public/secret key as the basic-auth
   header value: `apiRs.extraEnv` in the values file, plus
   `networkPolicy.otlpEgress.{enabled,namespace,port}` for the collector's namespace.

Engine-side traces (t-0025) belong to the engine; nothing here is needed for them. If
Langfuse becomes a standing requirement, the change is four values entries and one
NetworkPolicy flag — not an overlay hook.

That is exactly why it is gated: [`FAILURE_MODES.md`](FAILURE_MODES.md) F12 records that
those four entries would stream every prompt and tool result, seller text included, to
another tenant's UI. A Qwalo release configures no endpoint and no OTLP egress today, and
enabling one for a client is an owner decision about that client's data, not an
integration task.

## Inert today — read before claiming a deploy works

Inherited from the Rust control plane and recorded with source evidence in
`overlays/raava-internal/deploy/READINESS.md`. These ship correctly and do nothing yet:

- **The persona is inert.** `tools/personas/qwalo/PROMPT.md` is not delivered:
  `persona_id` is an opaque label and nothing writes `AGENTS_BASE.md`. A Qwalo release
  today runs the base agent with base instructions, not this persona.
- **The overlay system prompt is inert.** `overlay.systemPrompt` renders a ConfigMap
  mounted only on api-rs (`contrib/chart/templates/apirs.yaml:133` sets
  `CENTAUR_OVERLAY_DIR` on api-rs only). No sandbox pod receives it. The `--set-file`
  in `deploy-client.sh` is there so the day the chart mounts it, nothing else changes.
- **Overlay workflows are inert.** `WORKFLOW_DIRS` ignores
  `toolServer.extraSources[].subdir`. Qwalo ships no workflows; do not add one until
  that is fixed.

What **is** live: the `engine` tool and its CLI shim (`[project.scripts]` is what
`services/sandbox/install_tool_shims.py` installs), iron-proxy bearer injection for the
declared host, per-client namespace, database, Slack app, and the engine's own
**account** scope enforcement.

Say the read-only claim precisely, because the loose version is false:

- The **tool** cannot write. It has five read verbs and one GET helper.
- The **token** is not read-only. The engine scopes it by `account_scope` and never by
  verb (`TokenSlot` in `src/v1/auth.ts`), and iron-proxy stamps it on **any** request
  from the sandbox to that host, method and path unchecked. A `curl -X POST` from
  inside the sandbox to `/v1/labels` would be authenticated.
- What stops an agent today is the absence of a write path in its tools plus the
  absence of a SmarterContact credential — not a read-only credential. `RELEASE.md`
  step 5 makes the read-only token a precondition for go-live, and FAILURE_MODES F2
  states the hole in full.

## E2E

```bash
python3 overlays/qwalo/e2e/run_fake_engine.py
```

Runs the `engine` CLI through `uvx --from overlays/qwalo/tools/engine` against a fake
engine that speaks the real auth and error contract, and writes
`e2e/reports/fake-engine.json`. Nothing is deployed and no credential is read.

```bash
overlays/qwalo/e2e/run-on-proxmox.sh              # default: engineer@centaur-k3s
```

The committed artifact is produced on the Proxmox lab guest, not on a laptop: the
runner syncs the overlay, the chart, and one base tool, runs the suite, renders the
release with `helm template`, and copies both back. `client-release.rendered.yaml` is
that render, committed, so a reviewer can read what the release actually is: the
Postgres Service the baked DSN names, `TOOL_DIRS` narrowed to the overlay tree, one
host, `CODEX_AUTH_MODE=api_key`, the admitted webhook namespace, and no secret material.

The suite is deterministic in the strong sense: no timestamp, no machine path, no
ephemeral port, no errno string. A run on macOS and a run on the Linux guest produce
the same bytes today, and a diff means behaviour moved rather than the host changed.
Where a case cannot observe behaviour it says so: the webhook delivery, the read-back
of `WORKFLOW_DIRS` from a running container, and the read-only token all need a cluster
or a Slack app, and `RELEASE.md` is where a human proves them.
