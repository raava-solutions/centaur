---
title: Codex startup fails when service_tier is set to default
date: 2026-06-14
category: runtime-errors
module: sandbox/codex-config
problem_type: runtime_error
component: tooling
symptoms:
  - "Slack and Codex sandbox startup failed during harness boot."
  - "Codex reported a configuration parse failure for `/home/agent/.codex/config.toml`."
  - "`service_tier = \"default\"` produced `unknown variant default, expected fast or flex`."
  - "Fresh sandbox verification succeeded only after omitting `service_tier`."
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - assistant
  - testing_framework
  - development_workflow
tags:
  - codex
  - config
  - service-tier
  - slack
  - sandbox
  - kind
  - regression-test
---

# Codex startup fails when service_tier is set to default

## Problem

Centaur's Codex harness needed medium reasoning effort while returning service
tier selection to Codex's default behavior. The first attempt encoded that as
`service_tier = "default"` in the committed Codex config, but Codex rejects that
literal during startup.

This broke Slack turns before the agent could answer because the sandbox copied
the invalid config into `/home/agent/.codex/config.toml` and Codex failed while
loading it.

## Symptoms

- Slack posted a Centaur reply with `failed to load configuration`.
- The error pointed at `/home/agent/.codex/config.toml:5:16`.
- Codex reported `unknown variant default, expected fast or flex`.
- The failed Slack thread kept an active assignment pinned to the bad sandbox
  until it was explicitly released.

## What Didn't Work

Setting the service tier to a literal default value was invalid:

```toml
service_tier = "default"
```

Codex only accepts explicit configured tiers for that field. In this version,
the valid configured values are `fast` and `flex`; default behavior is selected
by leaving the key absent.

The first rebuild/load smoke checked the copied TOML but did not force Codex to
parse the config. The Slack failure exposed that gap: file presence and TOML
shape are not enough when the harness owns stricter config enum validation.

## Solution

Keep the desired reasoning effort and remove the service tier key entirely:

```toml
model = "gpt-5.5"
model_reasoning_effort = "medium"
personality = "pragmatic"
model_verbosity = "low"
approval_policy = "never"
sandbox_mode = "danger-full-access"
```

Add a regression test that reads the committed harness config with `tomllib`
and locks the intended contract:

```python
config = tomllib.loads(CODEX_HARNESS_CONFIG.read_text())

assert config["model_reasoning_effort"] == "medium"
assert "service_tier" not in config
```

After the code change:

1. Rebuild `centaur-agent:latest` with `just build-one agent`.
2. Load the image into Kind with `kind load docker-image centaur-agent:latest --name raava-centaur`.
3. Release the failed Slack assignment so the next Slack turn does not reuse the
   bad sandbox.
4. Spawn a fresh sandbox and inspect `/home/agent/.codex/config.toml`.
5. Run `codex login status` inside the sandbox to prove Codex can load the
   config and reach the authenticated ChatGPT state.

## Why This Works

Centaur does not interpret every Codex config value; it copies the committed
harness config into the sandbox and starts the Codex harness. That means invalid
Codex enum values can pass Centaur's own startup path but still fail when Codex
parses its config.

Removing `service_tier` expresses default service-tier behavior in the way
Codex expects: absence of an explicit configured tier. The regression test
guards the committed config rather than only the entrypoint copy mechanics, so a
future edit cannot silently reintroduce the invalid value.

## Prevention

- Treat absent optional harness config keys as meaningful when the underlying
  harness uses absence to select defaults.
- For Codex config changes, verify both the copied config and a real Codex
  config-load path such as `codex login status`.
- After loading a rebuilt sandbox image into Kind, release stale thread
  assignments that were pinned to the previous image or bad config.
- Keep a regression test against the committed harness config for values whose
  validity is owned by the harness CLI, not Centaur.

## Related Issues

- Related learning:
  [`local-kind-codex-blank-responses-placeholder-api-key.md`](../integration-issues/local-kind-codex-blank-responses-placeholder-api-key.md)
  covers another Codex startup failure where a small auth/config precedence
  change selected the wrong startup path.
- GitHub issue search found no matching issues for `service_tier`,
  `unknown variant default expected fast or flex`, or related Centaur agent
  service-tier keywords.
