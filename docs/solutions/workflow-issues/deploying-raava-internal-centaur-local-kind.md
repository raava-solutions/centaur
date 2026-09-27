---
title: Deploying the Raava-internal Centaur (local Kind, not a deploy box)
date: 2026-06-25
category: docs/solutions/workflow-issues
module: overlays/raava-internal/deploy
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - Rolling a code change into the already-running Raava-internal Centaur
  - Deciding which deploy command to run against the live Slack stack
  - Reasoning about where Centaur runs (local Kind vs a remote deploy box / GCP)
tags: [deployment, kind, helm, raava-internal, cloudflared, slackbot]
related_components: [slackbot, api, iron-proxy, cloudflared]
---

# Deploying the Raava-internal Centaur (local Kind, not a deploy box)

## Context

The Raava-internal Centaur that answers in Slack does **not** run on a remote deploy box or GCP. It runs as local Docker images inside a local Kind cluster on this Mac, exposed to Slack through an in-cluster Cloudflare tunnel. The base repo's `AGENTS.md` mentions a production deploy box, which is misleading for the Raava dogfood target and caused a near-miss when deciding how to ship a slackbot change to the live app.

Two separate gotchas make a naive deploy dangerous:
1. Plain `just deploy` (and `just up`) drops the Raava overlay values, which would tear down the cloudflared tunnel, channel defaults, and Codex OAuth wiring that make the live Slack app work.
2. Images are tagged `:latest` with `imagePullPolicy: IfNotPresent`, so a rebuilt image is *not* picked up by a running pod without an explicit `kind load` + `rollout restart`.

(session history) Prior sessions independently confirmed the local-Kind topology: the `raava_outreach` tool was rewritten from a host-subprocess to HTTP precisely because "a Kind pod can't reach" host binaries, reaching host services over `host.docker.internal` instead.

## Guidance

### Runtime topology (authoritative)

- Kubernetes context: `kind-raava-centaur`
- Kind cluster: `raava-centaur`
- Namespace: `centaur`
- Public URL: `https://centaur.raava.dev` via the in-cluster Cloudflare tunnel (`iwp-centaur`)
- Slack events URL: `https://centaur.raava.dev/api/webhooks/slack` (Slack app `A0B9VET4RKP`, workspace `zapgroup-workspace`)
- CI (`.github/workflows/publish-images.yml`) only builds and publishes images to GHCR. It does **not** deploy. Deployment is always a local, manual step on this machine.

The canonical full stand-up procedure lives in `overlays/raava-internal/deploy/runbook.md`. The fast path below is for the common case: rolling one service's code change into an already-running stack.

### Safe image-only redeploy of a single service

```bash
# 1. Rebuild just the one service image (api | iron-proxy | slackbot | agent)
just build-one slackbot

# 2. Load the rebuilt :latest image into the Kind cluster (deploy does NOT do this)
kind load docker-image centaur-slackbot:latest --name raava-centaur

# 3. Restart the deployment so the pod picks up the newly loaded image
kubectl rollout restart -n centaur deploy/centaur-centaur-slackbot
kubectl rollout status  -n centaur deploy/centaur-centaur-slackbot --timeout=180s

# 4. Verify the full Slack -> API -> sandbox pipeline end to end
just smoke   # expect "result_text": "PONG"
```

Deployment names follow `centaur-centaur-<service>` (e.g. `centaur-centaur-slackbot`, `centaur-centaur-api`). Image names follow `centaur-<service>:latest`.

### When a full Helm deploy is required, keep the overlay

If chart values, secrets, or topology changed (not just code), run Helm with the Raava overlay layered on top of the dev profile:

```bash
helm dependency update contrib/chart
helm upgrade --install centaur contrib/chart \
  -n centaur --create-namespace \
  -f contrib/chart/values.dev.yaml \
  -f overlays/raava-internal/deploy/values.raava-local.yaml
```

## Why This Matters

`just deploy` runs `helm upgrade --install centaur contrib/chart -n centaur -f contrib/chart/values.dev.yaml` plus an optional `EXTRA_VALUES`-only overlay. With no `EXTRA_VALUES` set it omits `overlays/raava-internal/deploy/values.raava-local.yaml` entirely, so running it (or `just up`, which calls `just deploy`) against the live stack would silently revert the deployment to the upstream dev profile: no cloudflared tunnel, no Raava channel defaults, no Codex OAuth wiring. The Slack endpoint at `centaur.raava.dev` would stop working.

The image-pickup gotcha is just as silent: because every service ships as `:latest` with `imagePullPolicy: IfNotPresent`, a `just build-one` followed by only a `rollout restart` reuses the stale image already on the node. The `kind load docker-image ... --name raava-centaur` step is what actually places the new bytes where the kubelet will find them. Skipping it makes the deploy look successful while running old code.

## When to Apply

- Shipping a verified code change to the running Raava-internal Centaur: use the image-only redeploy.
- Any operation against the live stack: never use bare `just deploy`/`just up`; always include the Raava overlay values file (or set `EXTRA_VALUES`).
- Discussing "my Centaur" / the live Slack app: assume this local-Kind topology, not a remote box, unless explicitly stated otherwise.

## Examples

Shipping the no-mention slackbot change to the live app (2026-06-25):

```bash
just build-one slackbot
kind load docker-image centaur-slackbot:latest --name raava-centaur
kubectl rollout restart -n centaur deploy/centaur-centaur-slackbot
just smoke   # -> "result_text": "PONG"
```

Rollback is the same pattern against the previously-good image, or `kubectl rollout undo -n centaur deploy/centaur-centaur-slackbot`.

Dangerous vs safe:

```bash
just deploy                              # DROPS the Raava overlay -> breaks the live tunnel
just build-one slackbot && \
  kubectl rollout restart ...            # reuses STALE image -> runs old code (no kind load)
```

## Related

- `overlays/raava-internal/deploy/runbook.md` — authoritative full stand-up (secrets, 1Password Connect, cloudflared, Slack app)
- `docs/solutions/integration-issues/local-kind-codex-blank-responses-placeholder-api-key.md` — same local-Kind/Raava context, Codex auth angle
- `docs/solutions/runtime-errors/codex-service-tier-default-breaks-startup.md` — reuses the `just build-one` + `kind load` redeploy fragment
