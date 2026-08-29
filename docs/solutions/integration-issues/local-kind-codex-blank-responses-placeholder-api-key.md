---
title: Local Kind Codex sandboxes return blank output with placeholder API key
date: 2026-06-12
category: integration-issues
module: sandbox/codex-auth
problem_type: integration_issue
component: authentication
symptoms:
  - "Raava Centaur local Kind sandboxes reached Codex but returned blank output."
  - "just smoke completed transport calls but failed to produce result_text containing PONG."
  - "Sandbox Codex ran in placeholder OPENAI_API_KEY mode instead of host OAuth mode."
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - assistant
  - tooling
  - development_workflow
tags:
  - codex
  - oauth
  - openai-api-key
  - centaur-codex-auth
  - kind
  - sandbox
  - raava-local
  - smoke-test
---

# Local Kind Codex sandboxes return blank output with placeholder API key

## Problem

Raava Centaur local Docker/Kind could spawn a sandbox and deliver the user turn
to Codex, but the assistant output was blank. The API-to-sandbox transport was
working; the failure was inside the sandbox auth posture.

The sandbox had `OPENAI_API_KEY=OPENAI_API_KEY`, which made Codex use API-key
mode and bypass the desired host ChatGPT OAuth state.

## Symptoms

- `just smoke` reached `/agent/spawn`, `/agent/message`, and `/agent/execute`,
  but `result_text` was empty instead of `PONG`.
- `codex login status` inside the sandbox did not report ChatGPT login.
- `OPENAI_API_KEY` was present inside the sandbox even though Raava local Kind
  was intended to use mounted host OAuth.

The expected healthy state is documented in
[`overlays/raava-internal/deploy/runbook.md`](../../../overlays/raava-internal/deploy/runbook.md):
`result_text` contains `PONG`, `codex login status` reports ChatGPT, and
`OPENAI_API_KEY` is absent from the sandbox environment.

## What Didn't Work

Treating this as a Slack or API transport issue was the wrong layer. The
Centaur API already writes turns to the sandbox through stdin and reads result
events from stdout. The broken state happened after transport, when Codex
selected the wrong credential mode.

The baked placeholder auth path also did not solve the local dogfood topology.
The sandbox image can include a default auth placeholder, and production
deployments can use API-key placeholders behind the credential proxy, but local
Raava Kind wanted host OAuth mounted into the sandbox.

Keeping the generic `OPENAI_API_KEY` placeholder alongside mounted OAuth caused
the wrong precedence: Codex preferred API-key mode before the OAuth auth file
could be used.

## Solution

Commit `19a6b6b0f06bcd09c1c4c4f3ad328c8dd913551d` wires host OAuth into the
sandbox and disables the conflicting OpenAI placeholder when OAuth is
configured.

The Raava local overlay opts into a Kubernetes secret:

```yaml
sandbox:
  codexAuth:
    existingSecretName: centaur-codex-auth
    secretKey: auth.json
```

The Helm chart passes that secret name and key to the API pod as
`KUBERNETES_CODEX_AUTH_SECRET_NAME` and `KUBERNETES_CODEX_AUTH_SECRET_KEY`.
When `KUBERNETES_CODEX_AUTH_SECRET_NAME` is present, the sandbox environment
builder omits the `OPENAI_API_KEY` placeholder.

The sandbox pod mounts the `centaur-codex-auth` secret read-only, and the
sandbox entrypoint copies the mounted OAuth file into Codex's real auth path
before the harness starts:

```bash
cp "$HOME_DIR/.centaur-codex-auth/auth.json" "$HOME_DIR/.codex/auth.json"
chmod 600 "$HOME_DIR/.codex/auth.json"
```

## Why This Works

Codex selects its credential mode at startup. If `OPENAI_API_KEY` is present,
the entrypoint's API-key login path can override the intended OAuth mode. If the
mounted `auth.json` is installed first and the placeholder API key is absent,
Codex starts from the host ChatGPT OAuth state.

The fix preserves the production placeholder model for proxy-backed API-key
deployments while making OAuth-backed local Codex sandboxes explicit and
opt-in.

## Prevention

- Keep the Raava local bootstrap step that creates `centaur-codex-auth` from
  the host `$HOME/.codex/auth.json`.
- Keep the `just smoke` auth checks: result text contains `PONG`,
  `codex login status` reports ChatGPT, and `OPENAI_API_KEY` is absent.
- Keep regression coverage that confirms `KUBERNETES_CODEX_AUTH_SECRET_NAME`
  removes the OpenAI placeholder from Codex sandbox env.
- When adding another auth mode, verify both config presence and active sandbox
  env; a mounted auth file is not enough if a higher-precedence env var remains.

## Related Issues

- Primary fix: `19a6b6b0f06bcd09c1c4c4f3ad328c8dd913551d`
- Related prior auth infrastructure: `a5e4d3c1`, `feat(token-broker): brokered Codex OAuth end-to-end (#200)`
- Closest upstream issue family: `paradigmxyz/centaur#141`, which also concerns
  Codex API-key mode versus token-backed auth, but uses a different proposed
  mechanism.
