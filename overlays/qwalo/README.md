# Qwalo Centaur overlay — one release per client

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

## Layout

| Path | What it is |
|---|---|
| `FAILURE_MODES.md` | Threats, controls, and the E2E case that proves each control |
| `tools/engine/` | The only tool: GET-only client for the engine `/v1` API, plus the `engine` CLI shim |
| `tools/personas/qwalo/` | Persona id and prompt: read-only qualification reporter |
| `services/sandbox/SYSTEM_PROMPT.md` | Overlay sandbox prompt (`overlay.systemPrompt`) |
| `deploy/values.client.yaml.tmpl` | Per-client values; placeholders only, never secrets |
| `deploy/deploy-client.sh` | `--check` renders and validates; bare run deploys one client |
| `deploy/slack-app-manifest.json.tmpl` | Slack app manifest, one app per client workspace |
| `deploy/RELEASE.md` | ERE end to end, including the steps only a human may take |
| `e2e/run_fake_engine.py` | The E2E gate: the tool against a fake engine, artifact recorded |
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
scope enforcement. The read-only guarantee holds because the tool has no write path and
the token has read scope — not because a prompt says so.

## E2E

```bash
python3 overlays/qwalo/e2e/run_fake_engine.py
```

Runs the `engine` CLI through `uvx --from overlays/qwalo/tools/engine` against a fake
engine that speaks the real auth and error contract, and writes
`e2e/reports/fake-engine.json`. Nothing is deployed and no credential is read. The
artifact is committed; a green run should change it.
