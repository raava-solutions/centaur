# Raava Internal Slack Deployment Runbook

This runbook is for standing up the Raava internal Centaur Slack product from
the overlay without pushing Raava-specific work to upstream Centaur.

## Boundary

- Upstream base repo: `paradigmxyz/centaur`
- Raava product overlay: `overlays/raava-internal`
- Deploy values: `overlays/raava-internal/deploy/values.raava-local.yaml`
- Slack app manifest:
  `overlays/raava-internal/deploy/slack-app-manifest.json`
- Cloudflare tunnel manifest:
  `overlays/raava-internal/deploy/cloudflared-centaur.yaml`

If `git remote -v` shows `origin` as `github.com/paradigmxyz/centaur.git`, do
not push this branch to `origin`. Publish via a Raava-owned overlay repo or fork.

## Runtime Topology

The current Raava dogfood target is local Docker images running in a local Kind
cluster, not GCP:

- Kubernetes context: `kind-raava-centaur`
- Kind cluster: `raava-centaur`
- Namespace: `centaur`
- Public URL: `https://centaur.raava.dev` through the in-cluster Cloudflare
  tunnel

Build images locally, load them into Kind, and deploy with Helm. GCP is not part
of this path unless a future production plan explicitly changes the topology.

## Local Verification

From the repo root:

```bash
cd services/api
uv run pytest \
  tests/test_raava_internal_overlay.py \
  tests/test_raava_internal_slack_routing.py \
  tests/test_raava_internal_delegation.py
uv run ruff check ../../overlays/raava-internal \
  tests/test_raava_internal_overlay.py \
  tests/test_raava_internal_slack_routing.py \
  tests/test_raava_internal_delegation.py
cd ../..
bun test services/slackbot/test/emulate/slack-e2e.test.ts
docker build -t raava-centaur-overlay:local overlays/raava-internal
docker build -t raava-gcloud-cloud-run-proxy:local \
  -f overlays/raava-internal/deploy/gbrain-cloudrun-proxy.Dockerfile .
```

Expected result: API overlay tests pass, Ruff passes, Slack emulator tests pass,
and the overlay/proxy images build.

## Required Live Inputs

A real Slack deployment requires:

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACKBOT_API_KEY`
- `DATABASE_URL` or the chart-managed Postgres secret
- Kubernetes context plus Helm
- Active Cloudflare tunnel for `centaur.raava.dev`

Do not invent placeholder production secrets. The Slackbot health route can run
without them, but real Slack events require the app signing secret and bot token.

## 1Password Connect Secret Path

Use this path when local dogfood should match production secret handling.
Application and tool credentials stay in 1Password, Centaur tools receive
placeholders, and iron-proxy resolves the real values through 1Password Connect
only for declared upstream hosts.

The local production-shaped values overlay is:

```text
overlays/raava-internal/deploy/values.raava-local-connect.yaml
```

It layers after `values.raava-local.yaml` and enables:

- `ironProxy.secretSource=onepassword-connect`
- the in-cluster `onepassword-connect` Deployment and Service
- API egress through `centaur-api-proxy`, so API-hosted tools get credential
  injection
- `centaur-onepassword-connect-credentials` as the Connect credentials Secret
- `OP_CONNECT_TOKEN` and `OP_VAULT` from `centaur-infra-env`

Create or reuse a shared vault for Centaur tool credentials. For Raava local
dogfood, use the `Raava` vault. Do not use Personal, Private, or Employee
vaults for Connect access.

Create the Connect server credentials file outside the repo:

```bash
CONNECT_DIR="$HOME/.config/centaur/onepassword-connect/raava-centaur-local"
mkdir -p "$CONNECT_DIR"
chmod 700 "$HOME/.config/centaur" "$HOME/.config/centaur/onepassword-connect" "$CONNECT_DIR"

cd "$CONNECT_DIR"
umask 077
op connect server create "Raava Centaur Local" --vaults "Raava" --force
chmod 600 1password-credentials.json
```

Create a least-privilege read token for Centaur's local secret reads:

```bash
OP_CONNECT_TOKEN="$(op connect token create \
  "raava-centaur-local-read" \
  --server "Raava Centaur Local" \
  --vault "Raava,r")"
```

Install the Connect credentials and token into Kubernetes without printing
secret values:

```bash
kubectl -n centaur create secret generic centaur-onepassword-connect-credentials \
  --from-file=1password-credentials.json="$CONNECT_DIR/1password-credentials.json" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n centaur patch secret centaur-infra-env --type merge \
  -p "$(printf '{"data":{"OP_CONNECT_TOKEN":"%s","OP_VAULT":"%s"}}' \
    "$(printf '%s' "$OP_CONNECT_TOKEN" | base64 | tr -d '\n')" \
    "$(printf '%s' "Raava" | base64 | tr -d '\n')")"

unset OP_CONNECT_TOKEN
```

Store tool credentials as 1Password items in the `Raava` vault. The item name
must match the tool secret name and the secret value must live in the
`credential` field.

| Item title | Field | Unlocks | Required for first Raava dogfood |
|------------|-------|---------|----------------------------------|
| `EXA_API_KEY` | `credential` | Raw `websearch search` retrieval. | Yes |
| `OPENROUTER_API_KEY` | `credential` | Synthesized `websearch` and `deep_research` through OpenRouter. | Yes |
| `FIRECRAWL_API_KEY` | `credential` | `firecrawl search` and `firecrawl scrape`. | Yes |
| `ANTHROPIC_API_KEY` | `credential` | Optional Anthropic compatibility for websearch synthesis. | No |
| `SUPERMEMORY_API_KEY` | `credential` | `supermemory recall`, `write`, and `status`. | Yes |
| `RAAVA_GBRAIN_OAUTH` | `credential` JSON with `client_id` and `client_secret` | Hosted GCP gbrain MCP access through the proxy-minted client-credentials bearer. | Yes |
| `RAAVA_GBRAIN_API_KEY` | `credential` | Legacy/static hosted gbrain bearer fallback. | No |

Non-secret model selection stays in deployment config, not 1Password:
`OPENROUTER_MODEL=deepseek/deepseek-chat` and
`WEBSEARCH_SYNTHESIS_PROVIDER=auto` are the local Raava defaults.

Deploy the Connect-backed local stack:

```bash
helm upgrade --install centaur contrib/chart \
  -n centaur --create-namespace \
  -f contrib/chart/values.dev.yaml \
  -f overlays/raava-internal/deploy/values.raava-local.yaml \
  -f overlays/raava-internal/deploy/values.raava-local-connect.yaml

kubectl rollout status -n centaur deploy/onepassword-connect --timeout=180s
kubectl rollout status -n centaur deploy/centaur-api-proxy --timeout=180s
kubectl rollout status -n centaur deploy/centaur-centaur-api --timeout=180s
```

Verify Connect from the allowed path, the API proxy:

```bash
kubectl -n centaur exec deploy/centaur-api-proxy -- sh -lc '
  curl -fsS -H "Authorization: Bearer ${OP_CONNECT_TOKEN}" \
    http://onepassword-connect:8080/v1/vaults | jq "[.[] | {name}]"
'
```

Verify real tool secret injection with Exa-backed raw websearch:

```bash
kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/tools/websearch/search \
    -d "{\"query\":\"1Password Connect Helm chart\",\"num_results\":2,\"synthesize\":false,\"timeout_seconds\":20}" | jq
'
```

Expected result: `result.results` contains Exa search results and no
`INVALID_API_KEY` error.

Verify OpenRouter-backed synthesis without requiring Anthropic:

```bash
kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/tools/websearch/search \
    -d "{\"query\":\"1Password Connect Helm chart\",\"num_results\":2,\"synthesize\":true,\"timeout_seconds\":45}" | jq ".result.meta"
'
```

Expected result: `synthesis_provider` is `openrouter` and no
`ANTHROPIC_API_KEY not set` error appears.

Verify Firecrawl discovery and a bounded search:

```bash
kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    http://localhost:8000/tools/firecrawl | jq ".methods[].name"
'

kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/tools/firecrawl/search \
    -d "{\"query\":\"Firecrawl scrape API\",\"limit\":2,\"timeout_seconds\":20}" | jq
'
```

Expected result: the tool exposes `search` and `scrape`, and
`result.results` contains Firecrawl search rows.

Verify Supermemory bridge discovery and a harmless write/recall:

```bash
kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    http://localhost:8000/tools/supermemory | jq ".methods[].name"
'

kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/tools/supermemory/write \
    -d "{\"content\":\"Centaur local Supermemory smoke $(date -u +%FT%TZ)\",\"container_tag\":\"raava-internal\"}" | jq
'

kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/tools/supermemory/recall \
    -d "{\"query\":\"Centaur local Supermemory smoke\",\"container_tag\":\"raava-internal\",\"limit\":3}" | jq
'
```

Expected result: the tool exposes `write`, `recall`, and `status`; write
returns a Supermemory document id/status; recall returns matching results after
indexing completes.

Verify the sandbox CLI bridge does not need provider keys:

```bash
kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/agent/spawn \
    -d "{\"thread_key\":\"bridge-smoke-$(date +%s)\"}" | jq
'
```

After a sandbox is assigned, exec into the sandbox pod and run:

```bash
centaur-tool-bridge tools | jq 'keys'
centaur-tool-bridge discover supermemory | jq '.methods[].name'
env | grep -E 'SUPERMEMORY_API_KEY|OPENROUTER_API_KEY|FIRECRAWL_API_KEY|EXA_API_KEY' && exit 1 || true
```

Expected result: tools are visible through the bridge and raw provider keys are
not present in the sandbox environment.

For local Kind, `RAAVA_GBRAIN_BASE_URL` points at an API-pod localhost Cloud
Run proxy sidecar. This avoids exposing raw hosted gbrain OAuth material to the
tool process while still querying the hosted GCP service. Create the proxy key
Secret from the local service-account key, then patch the API deployment:

```bash
kubectl -n centaur create secret generic raava-gbrain-cloudrun-proxy-key \
  --from-file=key.json="$HOME/.config/gcloud/raava-brain-gbrain-sync-key.json" \
  --dry-run=client -o yaml | kubectl apply -f -

GBRAIN_OAUTH_JSON="$(
  gcloud secrets versions access latest \
    --project raava-481318 \
    --secret raava-brain-gbrain-oauth-local-zay-agents
)"
patch_file="$(mktemp)"
jq -n --arg v "$GBRAIN_OAUTH_JSON" \
  '{stringData:{RAAVA_GBRAIN_OAUTH_JSON:$v}}' > "$patch_file"
unset GBRAIN_OAUTH_JSON
kubectl -n centaur patch secret centaur-infra-env --type merge \
  --patch-file "$patch_file"
rm "$patch_file"

kubectl -n centaur patch deploy centaur-centaur-api --type strategic -p '{
  "spec": {
    "template": {
      "spec": {
        "volumes": [
          {
            "name": "raava-gbrain-cloudrun-proxy-key",
            "secret": {"secretName": "raava-gbrain-cloudrun-proxy-key"}
          }
        ],
        "containers": [
          {
            "name": "api",
            "env": [
              {"name": "RAAVA_GBRAIN_BASE_URL", "value": "http://127.0.0.1:8087"}
            ]
          },
          {
            "name": "gbrain-cloudrun-proxy",
            "image": "raava-gcloud-cloud-run-proxy:local",
            "imagePullPolicy": "IfNotPresent",
            "command": ["/usr/bin/cloud-run-proxy"],
            "args": [
              "-host",
              "raava-brain-gbrain-lmbn6fkciq-ue.a.run.app",
              "-bind",
              "127.0.0.1:8087"
            ],
            "env": [
              {"name": "GOOGLE_APPLICATION_CREDENTIALS", "value": "/var/secrets/google/key.json"}
            ],
            "volumeMounts": [
              {"name": "raava-gbrain-cloudrun-proxy-key", "mountPath": "/var/secrets/google", "readOnly": true}
            ]
          }
        ]
      }
    }
  }
}'
```

Direct-hosted deployments can instead set `RAAVA_GBRAIN_BASE_URL` to
`https://raava-brain-gbrain-lmbn6fkciq-ue.a.run.app` when the
`RAAVA_GBRAIN_OAUTH` item is available to iron-proxy. Verify hosted gbrain
grounding:

```bash
kubectl -n centaur exec deploy/centaur-centaur-api -- sh -lc '
  curl -sS -X POST \
    -H "Authorization: Bearer ${LOCAL_DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    http://localhost:8000/tools/raava_gbrain/search_decisions \
    -d "{\"query\":\"active Raava function leads\",\"limit\":3}" | jq
'
```

Expected result: hosted responses show `source: hosted-gbrain`. If hosted
gbrain is unavailable or OAuth is not configured, the result should clearly show
the local-baseline source rather than inventing unsupported facts.

## Local Kind Bootstrap

For a local Kind smoke without 1Password, create the required chart secrets
directly. These values are placeholders and are not valid for live Slack:

```bash
kubectl create namespace centaur --dry-run=client -o yaml | kubectl apply -f -
openssl genrsa -out /tmp/centaur-firewall-ca.key 4096
openssl req -x509 -new -nodes -key /tmp/centaur-firewall-ca.key -sha256 -days 3650 \
  -subj '/CN=centaur iron-proxy CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign' \
  -out /tmp/centaur-firewall-ca.crt
kubectl -n centaur create secret generic centaur-firewall-ca \
  --from-file=ca-cert.pem=/tmp/centaur-firewall-ca.crt \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n centaur create secret generic centaur-firewall-ca-key \
  --from-file=ca-cert.pem=/tmp/centaur-firewall-ca.crt \
  --from-file=ca-key.pem=/tmp/centaur-firewall-ca.key \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n centaur create secret generic centaur-infra-env \
  --from-literal=POSTGRES_PASSWORD=tempo_dev_change_me \
  --from-literal=DATABASE_URL='postgresql://tempo:tempo_dev_change_me@centaur-centaur-postgres:5432/ai_v2' \
  --from-literal=SLACK_BOT_TOKEN='xoxb-local-placeholder' \
  --from-literal=SLACK_SIGNING_SECRET='local-signing-secret' \
  --from-literal=SLACKBOT_API_KEY='local-slackbot-key' \
  --from-literal=SANDBOX_SIGNING_KEY='local-sandbox-signing-key' \
  --from-literal=IRON_MANAGEMENT_API_KEY="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/centaur-firewall-ca.key /tmp/centaur-firewall-ca.crt
```

For Codex, use OAuth from the host Codex login. Do not rely on the baked
placeholder API key in the sandbox image:

```bash
test -s "$HOME/.codex/auth.json"
kubectl -n centaur create secret generic centaur-codex-auth \
  --from-file=auth.json="$HOME/.codex/auth.json" \
  --dry-run=client -o yaml | kubectl apply -f -
```

`values.raava-local.yaml` wires this secret through `sandbox.codexAuth`. When
that value is set, sandbox pods must not receive `OPENAI_API_KEY`; that
placeholder forces Codex into API-key mode and bypasses the mounted OAuth file.

## Deploy With Helm

Build the base images and overlay image:

```bash
just build
docker build -t raava-centaur-overlay:local overlays/raava-internal
kind load docker-image centaur-api:latest --name raava-centaur
kind load docker-image centaur-slackbot:latest --name raava-centaur
kind load docker-image centaur-agent:latest --name raava-centaur
kind load docker-image centaur-iron-proxy:latest --name raava-centaur
kind load docker-image raava-centaur-overlay:local --name raava-centaur
```

Bootstrap Kubernetes secrets from the shell environment:

```bash
export SLACK_BOT_TOKEN=...
export SLACK_SIGNING_SECRET=...
export SLACKBOT_API_KEY=...
just bootstrap-secrets
```

Deploy the chart with base dev values plus the Raava overlay values:

```bash
helm dependency update contrib/chart
helm upgrade --install centaur contrib/chart \
  -n centaur --create-namespace \
  -f contrib/chart/values.dev.yaml \
  -f overlays/raava-internal/deploy/values.raava-local.yaml
```

Verify overlay discovery:

```bash
kubectl exec -n centaur deploy/centaur-centaur-api -- \
  sh -lc 'echo "$TOOL_DIRS"; echo "$WORKFLOW_DIRS"; ls -la /app/overlay/org'
kubectl logs -n centaur deploy/centaur-centaur-slackbot --tail=100
```

Run the end-to-end local smoke:

```bash
just smoke
```

Expected result: the JSON response has `"result_text": "PONG"`. If it is empty,
check the active sandbox:

```bash
pod=$(kubectl -n centaur get pods -l centaur.ai/managed=true -o jsonpath='{.items[0].metadata.name}')
kubectl -n centaur exec "$pod" -c sandbox -- sh -lc '
  codex login status
  if env | grep -q "^OPENAI_API_KEY="; then
    echo "openai_api_key_env:present"
  else
    echo "openai_api_key_env:absent"
  fi
'
```

Expected result: `codex login status` reports ChatGPT, and
`openai_api_key_env:absent` confirms the placeholder env var is not overriding
OAuth.

For a local smoke, the values file seeds `LOCAL_DEV_API_KEY` as
`aiv2_raava_local_dev_admin_key`. Verify a Raava workflow can be enqueued:

```bash
kubectl port-forward -n centaur svc/centaur-centaur-api 18000:8000
curl -fsS -X POST http://127.0.0.1:18000/workflows/runs \
  -H 'Authorization: Bearer aiv2_raava_local_dev_admin_key' \
  -H 'Content-Type: application/json' \
  -d '{
    "workflow_name": "raava_delegate",
    "trigger_key": "local-kind-smoke-raava-delegate",
    "eager_start": false,
    "input": {
      "manager_persona": "priya",
      "request": "Local Kind smoke for Raava internal Centaur overlay.",
      "specialists": [
        {"role": "qa", "brief": "Confirm local deployment risks."}
      ]
    }
  }' | jq
```

## Cloudflare Tunnel

Slack currently targets:

```text
https://centaur.raava.dev/api/webhooks/slack
```

DNS should route `centaur.raava.dev` to the `iwp-centaur` Cloudflare tunnel.
The current tunnel id is `de4cf7c8-e63d-4e99-98ee-bb001bd24695`.

Create the in-cluster tunnel credentials secret from the local Cloudflare
credentials file before applying the tunnel manifest:

```bash
cloudflared tunnel info iwp-centaur
cloudflared tunnel route dns --overwrite-dns iwp-centaur centaur.raava.dev
kubectl -n centaur create secret generic centaur-cloudflared-credentials \
  --from-file=credentials.json="$HOME/.cloudflared/de4cf7c8-e63d-4e99-98ee-bb001bd24695.json" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f overlays/raava-internal/deploy/cloudflared-centaur.yaml
kubectl rollout status -n centaur deploy/centaur-cloudflared --timeout=120s
```

Then verify:

```bash
curl -fsS https://centaur.raava.dev/health
```

## Slack App

Slack app:

- App ID: `A0B9VET4RKP`
- Workspace: `zapgroup-workspace`
- Team ID: `T08TZCX83UJ`
- Events URL: `https://centaur.raava.dev/api/webhooks/slack`

Apply or verify the manifest in
`overlays/raava-internal/deploy/slack-app-manifest.json`:

```bash
slack app link --team T08TZCX83UJ --app A0B9VET4RKP \
  --environment deployed --skip-update --force
slack manifest validate --team T08TZCX83UJ --source local --skip-update
slack app install --team T08TZCX83UJ --skip-update --force
slack app list --team T08TZCX83UJ --skip-update
```

The local Slack CLI project uses `.slack/hooks.json` to return the overlay
manifest. Keep that hook tolerant of extra Slack CLI hook arguments.

After installation, update Kubernetes with the Bot User OAuth Token and signing
secret, then restart the API and Slackbot:

```bash
kubectl -n centaur patch secret centaur-infra-env --type=json -p='[
  {"op":"replace","path":"/data/SLACK_BOT_TOKEN","value":"<base64-xoxb-token>"},
  {"op":"replace","path":"/data/SLACK_SIGNING_SECRET","value":"<base64-signing-secret>"}
]'
kubectl rollout restart -n centaur deploy/centaur-centaur-api deploy/centaur-centaur-slackbot
kubectl rollout status -n centaur deploy/centaur-centaur-api --timeout=180s
kubectl rollout status -n centaur deploy/centaur-centaur-slackbot --timeout=180s
```

Verify the public webhook path accepts Slack-signed traffic:

```bash
curl -fsS https://centaur.raava.dev/health
# Generate a Slack v0 HMAC signature with SLACK_SIGNING_SECRET, then POST a
# url_verification payload to /api/webhooks/slack and confirm the challenge is
# echoed back.
```

After the app is installed in the Raava workspace:

1. Mention `@Centaur` in a channel mapped by
   `RAAVA_CENTAUR_CHANNEL_DEFAULTS`.
2. Verify the workflow is `slack_thread_turn`.
3. Verify metadata includes the resolved function lead.
4. Try an explicit lead selector such as `--vivian`.
5. Try a private specialist selector such as `--hana`; the answer should be
   owned by the relevant function lead.

## Proxmox Migration — Phase A (k3s host VM)

Foundation for moving Centaur off the Mac Studio Kind cluster onto the Proxmox
cluster (`pve-01`/`pve-02` + Pi QDevice). Covers plan units U1 (VM provision),
U2 (auto-restart — interim) and U3 (k3s + agent-sandbox). Plan:
`docs/plans/2026-06-29-001-feat-centaur-proxmox-migration-plan.md`. Phase A is
additive and reversible; it does NOT deploy Centaur (that is Phase B/U6).

### Degraded-HA decision (read first)

The plan's U2 calls for ZFS replication `pve-01`→`pve-02` so the VM disk is
present on the surviving node for a Proxmox HA restart. **Neither node has a ZFS
pool, a second disk, or shared storage** — both are a single 256 GB NVMe fully
consumed by the stock Proxmox LVM layout (`pvesm status` → only `local` + thin
`local-lvm`; `zpool list` → no pools). Provisioning therefore runs on
`local-lvm` (node-local) with **cross-node failover deferred** to a later
disk-hardening follow-up. The VM auto-restarts only on a **`pve-01` reboot**
(`onboot: 1`); a `pve-01` node loss does NOT relocate it to `pve-02`. An HA
resource is intentionally NOT added — with a node-local disk it would try to
relocate and wedge in an error state. R3's node-loss arm is explicitly unmet
until replicated/shared storage exists.

### Live inventory (verified 2026-06-29)

- Proxmox 9.2.2 on both nodes; cluster `homelab`; `pvecm status` quorate, Pi
  QDevice voting (total 3 / quorum 2 → survives one-node loss for *quorum*).
- `pve-01` tailnet `100.104.80.71` (LAN `10.200.160.152/25`, gw
  `10.200.160.129`, bridge `vmbr0`); `pve-02` tailnet `100.64.219.49`.
- Storage on each node: `local` (dir) + `local-lvm` (lvmthin, ~145 GB free). No
  ZFS, no Ceph/NFS.

### Provisioned resource (state left behind)

| Item | Value |
|------|-------|
| VM ID | `110` (avoided `100` — orphaned `pve-vm-100-disk-0` on pve-01) |
| Name / host | `centaur-k3s` |
| Node | `pve-01` |
| Spec | 8192 MiB RAM, 2 vCPU (1 socket), `cpu host` |
| Disk | `local-lvm:vm-110-disk-0`, 40 GiB, `discard=on` (node-local) |
| LAN IP | `10.200.160.160/25`, gw `10.200.160.129` (static via cloud-init) |
| Tailnet IP | `100.90.78.40` (hostname `centaur-k3s`) |
| OS | Debian 12 (bookworm), cloud-init |
| k3s | `v1.36.2+k3s1`, single control-plane node, traefik disabled |
| Docker | `29.6.1` (for Phase B image build → `k3s ctr images import`) |
| Auto-start | `onboot: 1` (host-reboot only; no HA resource) |

### U1 — Provision the VM (run on `pve-01` as root)

```bash
# Debian 12 genericcloud image (lazy clone+cloud-init path)
curl -4 -fSL -o /root/images/debian-12-genericcloud-amd64.qcow2 \
  https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2

# Stage the SSH pubkey to inject (one ed25519 line)
printf '%s\n' 'ssh-ed25519 AAAA... master@masters-Mac-Studio.local' > /root/centaur-k3s-zay.pub

qm create 110 --name centaur-k3s \
  --memory 8192 --cores 2 --sockets 1 --cpu host \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single \
  --ostype l26 --agent enabled=1
qm set 110 --scsi0 local-lvm:0,import-from=/root/images/debian-12-genericcloud-amd64.qcow2,discard=on
qm set 110 --ide2 local-lvm:cloudinit
qm set 110 --boot order=scsi0
qm set 110 --serial0 socket --vga serial0
qm disk resize 110 scsi0 40G
qm set 110 --ciuser zay --sshkeys /root/centaur-k3s-zay.pub \
  --ipconfig0 ip=10.200.160.160/25,gw=10.200.160.129 \
  --nameserver "1.1.1.1 8.8.8.8" --ciupgrade 0
qm set 110 --onboot 1            # U2 interim: auto-start on pve-01 boot
qm start 110
```

In-guest provisioning (SSH `zay@10.200.160.160`, ProxyJump via `root@pve-01`
until Tailscale is up; thereafter `zay@100.90.78.40` over the tailnet):

```bash
sudo apt-get update
sudo apt-get install -y qemu-guest-agent ca-certificates curl gnupg
sudo systemctl enable --now qemu-guest-agent systemd-timesyncd
sudo timedatectl set-ntp true                 # clock-sensitive for OAuth/k8s certs
curl -fsSL https://get.docker.com | sudo sh   # Docker 29.6.1 (image build, Phase B)
sudo usermod -aG docker zay && sudo systemctl enable --now docker

# Join the tailnet headlessly with a one-time pre-auth key. DNS left on the
# VM's own resolvers (--accept-dns=false) so it does not depend on the Pi
# (QDevice + AdGuard). The key is single-use and revoked after provisioning.
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --authkey="<TS_PREAUTH_KEY_REDACTED>" \
  --hostname=centaur-k3s --accept-dns=false
```

**U1 verification (by live state):** `qm config 110` shows disk on
`local-lvm`; `tailscale status` lists `centaur-k3s 100.90.78.40`; direct
`ssh zay@100.90.78.40` works; a `sudo systemctl reboot` returns in ~30s with
`docker`/`tailscaled`/`qemu-guest-agent` all `active` and root fs 40 GiB.

### U2 — Auto-restart (interim; replication deferred)

Set by `qm set 110 --onboot 1` above. **No ZFS replication and no HA resource**
(see decision note). Verify: `qm config 110 | grep onboot` → `onboot: 1`;
`qm agent 110 ping` responds; `ha-manager status` shows nothing managed;
`zpool list` → no pools (substrate unchanged). Cross-node failover = deferred.

### U3 — k3s + agent-sandbox (run in-guest)

```bash
curl -sfL https://get.k3s.io | sudo sh -s - --disable traefik --write-kubeconfig-mode 644
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown zay:zay ~/.kube/config
kubectl get nodes          # Ready, v1.36.2+k3s1, runtime containerd (NOT Docker)

# agent-sandbox v0.4.6 controller — install the chart dep in isolation (no Centaur).
# The vendored subchart is contrib/chart/charts/agent-sandbox-0.1.0.tgz; tag from
# contrib/chart/values.yaml (agentSandbox.image.tag: v0.4.6).
# NOTE: the subchart templates its own namespace — do NOT pass --create-namespace
# (it conflicts). Pre-create the ns and set namespace.create=false.
kubectl create namespace agent-sandbox-system
helm install agent-sandbox /tmp/agent-sandbox-0.1.0.tgz -n agent-sandbox-system \
  --set image.tag=v0.4.6 --set namespace.create=false --wait
```

**U3 verification (by live state):**
- `kubectl get nodes` → `centaur-k3s Ready control-plane … containerd://2.3.2-k3s2`.
- local-path is the default StorageClass; a throwaway `local-path` PVC + pause
  pod binds (`PVC Bound`), then cleaned up.
- All four CRDs `Established=True`: `sandboxes.agents.x-k8s.io`,
  `sandboxclaims`/`sandboxtemplates`/`sandboxwarmpools.extensions.agents.x-k8s.io`.
- `agent-sandbox-controller` deploy `1/1 Ready`, image
  `registry.k8s.io/agent-sandbox/agent-sandbox-controller:v0.4.6` present in
  `k3s ctr images ls`; logs show it became leader and started the Sandbox worker.
- **Phase B note:** k3s containerd is separate from the Docker daemon — images
  built with `docker build` (U6) must be `docker save` → `sudo k3s ctr images
  import`'d; set `pullPolicy: IfNotPresent` for all five images.
- **Residual (minor):** the vendored agent-sandbox subchart's generated RBAC
  lacks `events:create`, so the controller logs an event-rejected warning. It is
  cosmetic (leader election + reconcile work); fix RBAC in the chart for Phase B.

### Rollback / teardown (fully reversible)

```bash
# In-guest: helm uninstall agent-sandbox -n agent-sandbox-system; /usr/local/bin/k3s-uninstall.sh
# On pve-01:
qm stop 110 && qm destroy 110 --purge      # removes vm-110-disk-0 + cloudinit
# Then revoke the Tailscale node + the one-time pre-auth key in the admin console.
```

### Deferred (NOT done in Phase A)

- Replicated/shared storage + Proxmox HA resource (R3 node-loss arm) — needs a
  storage decision (add disks / Ceph / NFS) from the owner.
- Phase B (secrets, 1Password Connect, token-broker, image build+import, Helm
  deploy) and Phase C (tunnel cutover, HA drill, Mac-off) — not started.
