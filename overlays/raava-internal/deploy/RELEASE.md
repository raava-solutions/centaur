# Centaur release process — Raava production (Proxmox k3s)

Status: **the tag does not reach production today.** This file states what is true,
what blocks automation, and the sequence to close the gap.

## What is verified

`publish-images.yml` fires on push to `main` and on any `v*` tag. It builds four
images — `centaur-api-rs`, `centaur-slackbotv2`, `centaur-agent`,
`centaur-iron-proxy` — for amd64 and arm64, and pushes them to GHCR. Then it stops.
`release-chart.yml` publishes the chart when `contrib/chart/**` changes on `main`.
Neither workflow touches a cluster. No deploy workflow exists on `main` at all;
`.github/workflows/` holds only `ci.yml`, `docs.yml`, `pr-audit.yml`,
`publish-images.yml`, and `release-chart.yml`.

Two things break the tag-to-production path:

1. **Namespace.** `publish-images.yml` line 31 hard-codes
   `IMAGE_NAMESPACE: paradigmxyz/centaur`. Those are upstream's packages. A Raava
   build cannot push to them, and Raava's production must not pull from them,
   because an upstream tag would then be a production deploy of unreviewed code.
2. **Image naming.** Production runs bare-name images that CI never produces:
   `centaur-api-slack-hermes-f8663481`, `centaur-slackbot-...`. They were built
   locally, exported with `docker save`, copied to the VM, and imported with
   `k3s ctr images import`, under `pullPolicy: IfNotPresent`. A CI tag build is
   therefore not pullable by the cluster even today.

## Manual release (works now)

1. Land the change on `raava/main`.
2. Tag and push: `git tag v0.x.y && git push raava v0.x.y`. Confirm the four image
   builds went green.
3. Build the four images for `linux/amd64` and export them.
4. Copy the tarballs to the VM and import them into k3s containerd:
   `k3s ctr images import <tarball>`.
5. Bump `x-release.tag` in `values.raava-proxmox.yaml`. One line.
6. Apply: `helm upgrade --install centaur contrib/chart -n centaur -f
   contrib/chart/values.yaml -f overlays/raava-internal/deploy/values.raava-proxmox.yaml`
7. Verify: `kubectl -n centaur rollout status deploy/centaur-api-rs
   deploy/centaur-slackbotv2`, then post one message in Slack and confirm a reply.

Step 4 is the step that should not survive. Everything else is a normal release.

## Closing the gap

**Phase 1 — Raava-owned images.** Override `IMAGE_NAMESPACE` to
`raava-solutions/centaur` on the fork (make it a `vars.` lookup so upstream stays
default) and create the four packages with a fine-grained PAT that has
`write:packages`. Set the packages public, or ship an `imagePullSecret` and
reference it from the chart's `imagePullSecrets`. This alone makes a CI tag build
meaningful.

**Phase 2 — one deploy step.** Register a self-hosted GitHub Actions runner on the
k3s VM and add `deploy-production.yml`: trigger on `workflow_dispatch` plus `v*`
tags, guard it with a GitHub deployment environment so it needs manual approval,
then run the Helm apply from step 6 and `kubectl rollout status`. A runner on the
box avoids exposing the k3s API server and avoids SSH keys in repository secrets.
Alternative if you prefer GitOps: install Flux and let it track the GHCR tags. That
is a larger change and it changes how you intervene during an incident.

**Phase 3 — retire the side-load.** Switch `x-release.registry` to
`ghcr.io/raava-solutions`, keep `pullPolicy: IfNotPresent` with immutable semver
tags, and delete the local-build steps from
`overlays/raava-internal/deploy/runbook.md`. Pin semver tags, never `latest`, so a
re-deploy is reproducible.

Do Phase 1 before Phase 2. A deploy workflow that pulls from upstream's namespace
automates the wrong thing.

## Production facts worth knowing while you release

- Production lives on the `centaur-k3s` VM (Proxmox node `master`, VM 110,
  10.100.0.9). The Postgres PVC lives on spinning disk, so a pod restart is slow and
  a `local-path` PVC does not survive a volume move. Back it up before anything that
  could recreate it.
- Node is `k3s`-versioned; `kubectl` runs as root on the VM and needs `--insecure`
  against the self-signed API server.
- The `agent-sandbox` controller is installed standalone in
  `agent-sandbox-system`, cluster-scoped. `agentSandbox.enabled: false` in the
  overlay values, deliberately. A duplicate controller fights over the same CRs.
- Slack secrets rotate. A stale `centaur-secrets` Secret is the most recent known
  cause of a silent outage: the Slackbot stayed `Running` while the Socket Mode
  endpoint returned `401 invalid_auth`. Check that first when Slack stops replying.
