# Overlay readiness on the Rust control plane

Gap ledger from the rebase onto `raava/main`. Every status below was read out of
source on this branch, with the file and line noted. Do not treat a "works" row as
verified on a cluster — nothing here has run on Proxmox yet.

## Headline

Python tools and Python workflows are still supported. They are not supported the
old way. Execution moved into the sandbox as CLI shims, and workflow handlers run
under a host that fabricates the old `api.workflow_engine` module. The blockers are
small and specific, not architectural.

## Ledger

| Component | Status | Why | Needs |
|---|---|---|---|
| `tools/{raava_gbrain,raava_outreach,outreach_send,raava_supermemory}` | needs work | Secret blocks parse fine, but no `[project.scripts]` → no CLI shim is installed → not callable | Add `[project.scripts]` + `cli.py` per upstream |
| `tools/personas/*` (10) | dead | `persona_id` is an opaque label. No `.rs` file reads `PROMPT.md`, and nothing writes `AGENTS_BASE.md` | Upstream feature, or deliver via `sandbox.extraEnv` |
| `workflows/gtm_outreach_daily.py`, `_raava_roles.py`, `_gtm_report_blocks.py` | works | `workflow_host.py` synthesizes `api.workflow_engine.WorkflowContext` | Deployment fix (row below) |
| `workflows/raava_delegate.py` | dead | imports `ControlPlaneError` from `api.runtime_control`, absent from the compat shim | Import guard |
| `workflows/slack_thread_turn.py` | dead | imports `api.workflows.*`, which no longer exists. RFC 0003 makes this path native api-rs | Delete |
| overlay workflow discovery | dead | `apirs.yaml:12,16` builds `WORKFLOW_DIRS` as `<repo>/workflows` and ignores `toolServer.extraSources[].subdir` | Chart change |
| `services/sandbox/SYSTEM_PROMPT.md` | dead | `overlay.systemPrompt` builds a ConfigMap that no template mounts into a sandbox | Mount + env |
| `.agents/skills/raava-centaur` | dead | copy logic exists at `entrypoint.sh:265-276`, gated on `CENTAUR_OVERLAY_DIR`, which no sandbox receives | Same fix as above |
| `conftest.py` | works | `sys.path` injection is valid | — |

## Evidence

**Tools.** `tool_discovery.rs:113-134` produces only an iron-proxy secret fragment —
it never imports Python. `services/sandbox/install_tool_shims.py:200-201` iterates
`project.scripts`; with no scripts there is no shim, and the tool is unreachable
even though `TOOL_DIRS` includes the overlay (`apirs.yaml:13-17`). 55 of 76 upstream
tools declare `[project.scripts]`; ours declare none. `[tool.centaur].module` is
still honoured (`install_tool_shims.py:209-211`), so `module = "client.py"` stays
correct. `centaur_sdk/tool_sdk.py:47 secret()` is intact.

**Personas.** `persona_id` flows `types.rs:11` → `routes.rs:203` →
`session-runtime/src/lib.rs:202,236` → persisted at `session-core:146`, and
`PersonaConflict` (`session-sqlx:102-108`) becomes HTTP 409 (`error.rs:58`). It
carries no prompt. `entrypoint.sh:294` claims the API writes `AGENTS_BASE.md`; on
this branch nothing does. Our directory layout is byte-identical to upstream's
`tools/personas/eng/`, so this is an upstream gap, not a Raava mistake.

**Workflows.** The format is still Python. `workflow_host.py` ships in the api-rs
image (`services/api-rs/Dockerfile:35-37`) and as `/usr/local/bin/workflow-host`
(`services/sandbox/Dockerfile:207`), launched by api-rs through
`centaur_workflows::WorkflowHostSandboxRuntime` (`args.rs:30`, `:563-571`). It
fabricates `api.workflow_engine` (`:199-213`) and gives `api.runtime_control` only
`canonical_json` and `decode_jsonb` (`:215-218`). Import probe under the real compat
layer:

```
OK    _raava_roles / _gtm_report_blocks / gtm_outreach_daily
FAIL  raava_delegate     ImportError: cannot import name 'ControlPlaneError' from 'api.runtime_control'
FAIL  slack_thread_turn  ModuleNotFoundError: No module named 'api.workflows'
```

`ctx.call_tool` routes to the same CLI shims (`workflow_host.py:84-87,165`), so a
workflow can only call a tool that has `[project.scripts]`.

Reproduce the probe yourself:

```bash
python3 - <<'PY'
import importlib.util, sys, pathlib
sys.path.insert(0, "services/workflow-python")
import workflow_host as wh
wh.install_api_compat_module()                 # fabricates api.workflow_engine
for name in ("_raava_roles", "_gtm_report_blocks", "gtm_outreach_daily",
             "raava_delegate", "slack_thread_turn"):
    p = pathlib.Path("overlays/raava-internal/workflows/%s.py" % name)
    spec = importlib.util.spec_from_file_location("wf_" + name, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf_" + name] = m               # required: dataclasses reads cls.__module__
    try:
        spec.loader.exec_module(m)
        print("OK   ", name)
    except Exception as e:
        print("FAIL", name, type(e).__name__, e)
PY
```

Omitting the `sys.modules` line makes `_raava_roles` fail with
`AttributeError: 'NoneType' object has no attribute '__dict__'`. That is a harness
artifact, not a defect: `@dataclass` with `from __future__ import annotations` looks
up `sys.modules[cls.__module__]` on Python 3.14.

**Sandbox prompt and skills.** `overlay-configmap.yaml:9-10` writes the ConfigMap.
Grepping every template: it is referenced only as a pod checksum (`apirs.yaml:99`)
and as env on **api-rs** (`apirs.yaml:133-135`). No sandbox volumeMount.
`SESSION_SANDBOX_PASSTHROUGH_ENV` defaults empty (`args.rs:544-548`), so
`CENTAUR_OVERLAY_DIR` never crosses into the sandbox, so `entrypoint.sh:289-291` and
`:265-276` both no-op.

## Highest-leverage fix, in order

1. Set `SESSION_SANDBOX_PASSTHROUGH_ENV=CENTAUR_OVERLAY_DIR` and mount the overlay
   volume into sandbox pods. This unblocks the prompt overlay and the skill overlay
   together.
2. Add `[project.scripts]` and a `cli.py` to each overlay tool. This is what makes
   them callable at all.
3. Honour `subdir` when composing `WORKFLOW_DIRS`, so overlay workflows are
   discovered.
4. Delete `slack_thread_turn.py` and guard `raava_delegate.py`.

Steps 1 and 3 are chart and api-rs changes, so they are upstream contributions.
Step 2 is ours and blocks nothing upstream.

## Upstream bugs found while reading

These are not Raava problems. Worth reporting; both are silent.

- **`call` is broken against api-rs for every subcommand.** `services/sandbox/call.sh`
  POSTs `/agent/spawn`, `/agent/message`, `/agent/execute`, `/agent/runtime`,
  `/agent/status`, `/tools/<tool>/<method>` (`:121,146,172,191,205,223,225,254-256`).
  The api-rs router (`routes.rs:84-109`, one flat `Router::new()`, no `nest`/`merge`)
  serves none of them. Upstream's own baked `services/sandbox/SYSTEM_PROMPT.md:11,52`
  still promises `call agent runtime`.
- **Persona prompts are never delivered.** See the personas row. `AGENT_PERSONA` is
  documented at `docs/pages/configuration.mdx:163` and read at `call.sh:141`, but
  nothing sets it.

Our `SYSTEM_PROMPT.md` is already correct for the new contract: it tells the agent
to use `websearch search` and `firecrawl scrape` as CLI shims and never mentions
`call`.

## About the green test suite

`python3 -m pytest overlays/raava-internal -q` passes 65 tests. That is not evidence
the overlay works. The suite never imports `slack_thread_turn.py` or
`raava_delegate.py`, and no test exercises tool-callability, prompt delivery, or
workflow discovery — the four things that are actually broken. Green here means the
pure functions are still pure.
