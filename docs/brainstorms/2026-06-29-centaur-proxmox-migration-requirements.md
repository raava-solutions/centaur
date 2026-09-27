# Centaur → Proxmox Migration — Requirements

**Date:** 2026-06-29
**Status:** Requirements settled; ready for planning (`ce-plan`) and devops-lead execution.
**Owner seat:** `devops-lead` executes infra; Claude orchestrates + reviews.

## Outcome

Centaur runs as standalone, always-on infrastructure on the Proxmox cluster, fully off the Mac Studio. It is reachable and driveable through Slack, with reliable uptime, and has **no runtime dependency** on `raava-outreach` or the Mac.

## Settled decisions

1. **Centaur is standalone.** Remove the `raava-outreach` `:8770` HTTP coupling from Centaur's runtime. Centaur does not call the Routine plane and does not reach back to the Mac. Any Routine→Centaur exchange happens through the Slack landing zone only.
2. **Scope: the Centaur k8s platform** (`centaur-api`, `centaur-slackbot`, `centaur-iron-proxy`, `centaur-agent` sandbox + Postgres). `raava-outreach` stays where it is on the Mac and is out of scope.
3. **Runtime form: k3s on a single VM**, deployed via the existing Helm chart (`contrib/chart/`). VM, not LXC — k3s/containerd in LXC is fragile; a VM is the supported path. This matches Centaur's current single-Kind-node shape and its own `mac-mini-setup.mdx` "k3s on a small always-on host" guidance.
4. **Reliable uptime via Proxmox HA.** The k3s VM runs under Proxmox's HA manager so it auto-restarts on the surviving node if one dies. This delivers "stays up" without building a multi-node k8s control plane. (Full multi-node k3s HA is deferred — only if Proxmox-level HA proves insufficient.)
5. **Slack is the interface.** No dependency on being at the Mac.
6. **State survives reboots.** In-cluster Postgres on a persistent volume backed by the VM disk; nightly `pg_dump` backup off-box. No more ephemeral Kind-style state loss.
7. **Secrets unchanged in mechanism.** Reuse `OP_SERVICE_ACCOUNT_TOKEN` + `OP_VAULT` → `just bootstrap-secrets` → k8s Secrets. Set the token on the Proxmox host/cluster; no new secret store.

## Success criteria

- Centaur is deployed on the Proxmox k3s VM and reachable through Slack with the Mac Studio powered off.
- A VM/node reboot recovers Centaur automatically (Proxmox HA restart) with state intact.
- No code path in Centaur references the `raava-outreach` `:8770` bridge or `host.docker.internal`.
- Secrets resolve from 1Password on the cluster with no manual per-boot steps.

## Outside this scope

- Migrating `raava-outreach` (the Routine plane) off the Mac — separate effort.
- Re-architecting the Routine↔Centaur report exchange beyond "decouple the direct HTTP call" — if Centaur needs outreach reports, the Slack landing zone is the seam; designing that intake is its own task.
- Full multi-node k3s HA.

## Resolved facts (verified 2026-06-29 — repo + tailnet)

1. **Slack ingress = HTTP Events API over a Cloudflare tunnel.** Slackbot verifies request signatures (`services/slackbot/src/index.ts`); no Socket Mode. The tunnel `overlays/raava-internal/deploy/cloudflared-centaur.yaml` routes `centaur.raava.dev` → `centaur-centaur-slackbot:3001`, all outbound (TCP 443 + UDP 7844). **No inbound port on the Proxmox host** — the cloudflared deploy comes along unchanged.
2. **Cluster topology confirmed:** `pve-01` (100.104.80.71) + `pve-02` (100.64.219.49) up on the tailnet, Pi QDevice `raspberrypi` (100.64.146.44) active.
3. **State is already persisted, not ephemeral.** Postgres (ParadeDB `paradedb/paradedb:0.23.0-pg16`) uses a `volumeClaimTemplate` (20Gi RWO, configurable `storageClassName`) in `contrib/chart/templates/workloads.yaml`. **Open infra task (plan, not decision):** for HA restart on the other node the VM disk needs replicated/shared storage — ZFS replication between pve-01/pve-02 is the low-effort 2-node path.
4. **`:8770` decoupling is a config toggle, no platform breakage.** Single client `overlays/raava-internal/tools/raava_outreach/client.py` (default `host.docker.internal:8770`, override `RAAVA_OUTREACH_BASE_URL`), consumed only by `workflows/gtm_outreach_daily.py`. Removing it neutralizes the dependency; the GTM report-intake redesign (Slack landing zone) is an explicit follow-up, **out of this migration's scope**.

## Sizing & prerequisites (verified)

- **VM sizing:** ~4–6 GiB RAM, ~1 vCPU, ≥20 GiB disk for the Postgres PVC (plus headroom if the sandbox warm pool is later enabled; `warmPoolEnabled: false` by default). Per-sandbox request is 100m CPU / 512Mi.
- **Chart deps to stand up:** `agent-sandbox` controller `v0.4.6`, 1Password Connect `2.4.1`, ParadeDB Postgres.
- **token-broker (codex/OAuth harness auth):** set `tokenBroker.enabled=true`, `ironProxy.secretSource=onepassword-connect`, and a **writable** `OP_CONNECT_TOKEN` (read-only blocks token issuance). Reference pattern: `overlays/raava-internal/deploy/values.raava-local-connect.yaml`.
- **Secrets bootstrap prereqs:** provide `OP_SERVICE_ACCOUNT_TOKEN`, `OP_VAULT`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACKBOT_API_KEY`; `just bootstrap-secrets` generates `POSTGRES_PASSWORD`/`DATABASE_URL`/`IRON_MANAGEMENT_API_KEY`/`SANDBOX_SIGNING_KEY`/`IRON_BROKER_TOKEN` + firewall CA.

## Dependencies / assumptions

- Proxmox cluster is reachable on the tailnet and has spare capacity for a small always-on VM (control plane + Postgres + a few sandbox pods).
- Centaur has no GPU/local-LLM need (verified) — agents call external LLM APIs over HTTPS.
- 1Password vault items remain accessible from the Proxmox network via the service-account token.

## Reference

- Canonical definition: the `/centaur` skill (`~/.claude/skills/centaur/SKILL.md`).
- Repo: `~/centaur` — Helm chart `contrib/chart/`, deploy docs `docs/pages/deploying-in-production.mdx`, `docs/pages/mac-mini-setup.mdx`.
