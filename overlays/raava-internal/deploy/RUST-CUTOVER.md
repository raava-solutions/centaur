# Rust production cutover — centaur-k3s (Proxmox VM 110)

Status: prepared. Target: replace the legacy Python control plane
(chart `centaur-0.1.41`, images `centaur-api:latest` + `centaur-slackbot:latest`)
with the Rust stack (chart `0.1.139`, images `centaur-api-rs` /
`centaur-slackbotv2` / `centaur-console` / `centaur-iron-proxy` /
`centaur-agent`) from upstream `paradigmxyz/centaur` main + the Raava overlay.

Rollback: one `helm rollback` + two secret/manifest restores. See the last
section. Do not start the cutover until the pre-flight checks below pass.

## Decisions already made

- Base is latest upstream `main` (93c43ed8). The prepared fork branch
  (`raava/feat/raava-overlay-on-rs`) is 599 upstream commits behind. Do not
  deploy stale code; the overlay ports instead.
- Fresh database `ai_v3` for the Rust stack on the same ParadeDB instance.
  The Python database `ai_v2` stays untouched as the read-only archive. The
  Rust API migrations create the Rust schema; the Slack ETL tables ship in the
  same migrations, so `slackbotv2` finds its tables there.
- Images are side-loaded: build on the VM, `k3s ctr images import`, pinned
  tags, `pullPolicy: IfNotPresent`. No registry dependency. This follows the
  documented manual release flow (`deploy/RELEASE.md`).
- The chart prunes the Python `api`/`slackbot`/`token-broker` deployments on
  upgrade. That IS the cutover. The console replaces the token-broker.
- Sandbox `controller: pod`; the standalone agent-sandbox controller stays
  installed and unused (`agentSandbox.enabled: false`).
- Codex keeps `codexAuthMode: access_token`. Console-worker owns the OAuth
  refresh loop; the broker credential must be re-seeded after cutover
  (post-cutover step 5).
- Company-context via RBE is NOT part of this cutover. The Python API carried
  the RBE client; the Rust stack has no equivalent yet. Port it as a
  fast-follow. Nothing else on this path depends on it.

## Preconditions (all must pass before step 1)

1. `GITHUB_TOKEN` exists in the `centaur-infra-env` secret: a fine-grained
   read-only PAT (contents:read) on `raava-solutions/centaur`. Repo-cache
   clones the private fork with it. Without it, sandboxes get no tools and no
   personas.
2. DB dump exists: `pg_dump ai_v2` copied off the VM (this Mac + `/root/backups`).
3. Images for the pinned tag imported into k3s containerd (5 images).
4. `helm get values` + `helm history` snapshot saved (rollback inputs).
5. Saved copy of the current cloudflared manifest (rollback input).

## Cutover steps (run on the VM unless stated)

1. `kubectl -n centaur edit secret centaur-infra-env`: change the `DATABASE_URL`
   key to `postgresql://tempo:<POSTGRES_PASSWORD>@centaur-centaur-postgres:5432/ai_v3`.
2. `helm upgrade --install centaur contrib/chart -n centaur \
     -f contrib/chart/values.yaml \
     -f overlays/raava-internal/deploy/values.raava-proxmox.yaml \
     --set-file overlay.systemPrompt=overlays/raava-internal/services/sandbox/SYSTEM_PROMPT.md`
3. `kubectl -n centaur rollout status deploy/centaur-centaur-api-rs \
     deploy/centaur-centaur-slackbotv2 deploy/centaur-centaur-console-web \
     --timeout=600s`
4. Update + apply the cloudflared manifest
   (`overlays/raava-internal/deploy/cloudflared-centaur.yaml` — already
   points at `centaur-centaur-slackbotv2:3001`):
   `kubectl apply -f overlays/raava-internal/deploy/cloudflared-centaur.yaml`
   then `kubectl -n centaur rollout restart deploy/centaur-cloudflared`.
5. Seed the Codex OAuth broker credential in the fresh console DB. Use
   `centaur-perms broker create --foreign-id openai-codex \
     --token-endpoint https://auth.openai.com/oauth/token \
     --client-id <id> --refresh-token <token>` with values from the
   `centaur-codex-auth` secret (`auth.json`) / 1Password. Then send one Slack
   message and confirm the agent runs.
6. Verify, in order:
   - `kubectl -n centaur get pods` — all Running, no CreateContainerConfigError.
   - `curl http://localhost:8080/healthz` against the api-rs pod (exec).
   - Post a Slack message in a test thread, expect a PONG reply.
   - `centaur-tools list` inside a sandbox shows base + Raava tools.
   - Default persona is chief on a fresh thread.
7. Delete stale pods left from the Python era (sandbox pods in `Unknown` for
   19+ days, old Error/Completed pods). Nothing else: the Python
   deployments/services were already pruned by helm.
8. Record state: helm revision, image tags, `ai_v3` URL, in the runbook log
   section below + the brain.

## Post-cutover fast-follows (file tickets, do not block)

- Port RBE company-context to the Rust stack (tool or api integration).
- Phase 1–3 of `deploy/RELEASE.md`: GHCR images under `raava-solutions`,
  deploy workflow with a runner, retire the side-load.
- Sandbox image right-size (current image is ~10 GB; texlive dominates).
- Remove the legacy `raava_gbrain` tool once RBE replaces its callers.
- Reclaim containerd/docker space from Python-era images after a soak period.

## Cutover log

- (fill in during execution: dates, revisions, tags, outcomes)
