# Raava Internal Slack Deployment Runbook

This runbook is for standing up the Raava internal Centaur Slack product from
the overlay without pushing Raava-specific work to upstream Centaur.

## Boundary

- Upstream base repo: `paradigmxyz/centaur`
- Raava product overlay: `overlays/raava-internal`
- Deploy values: `overlays/raava-internal/deploy/values.raava-local.yaml`
- Slack app manifest:
  `overlays/raava-internal/deploy/slack-app-manifest.json`

If `git remote -v` shows `origin` as `github.com/paradigmxyz/centaur.git`, do
not push this branch to `origin`. Publish via a Raava-owned overlay repo or fork.

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
```

Expected result: API overlay tests pass, Ruff passes, Slack emulator tests pass,
and the overlay image builds.

## Required Live Inputs

A real Slack deployment requires:

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACKBOT_API_KEY`
- `DATABASE_URL` or the chart-managed Postgres secret
- Kubernetes context plus Helm
- Active Cloudflare tunnel for `iwp-centaur.raava.dev`

Do not invent placeholder production secrets. The Slackbot health route can run
without them, but real Slack events require the app signing secret and bot token.

## Local Kind Bootstrap

For a local Kind smoke without 1Password, create the required chart secrets
directly. These values are placeholders and are not valid for live Slack:

```bash
kubectl create namespace centaur --dry-run=client -o yaml | kubectl apply -f -
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -subj '/CN=centaur-local-firewall' \
  -keyout /tmp/centaur-firewall-ca.key \
  -out /tmp/centaur-firewall-ca.crt
kubectl -n centaur create secret generic centaur-firewall-ca \
  --from-file=ca-cert.pem=/tmp/centaur-firewall-ca.crt \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n centaur create secret generic centaur-firewall-ca-key \
  --from-file=ca-key.pem=/tmp/centaur-firewall-ca.key \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n centaur create secret generic centaur-infra-env \
  --from-literal=POSTGRES_PASSWORD=tempo_dev_change_me \
  --from-literal=DATABASE_URL='postgresql://tempo:tempo_dev_change_me@centaur-centaur-postgres:5432/ai_v2' \
  --from-literal=SLACK_BOT_TOKEN='xoxb-local-placeholder' \
  --from-literal=SLACK_SIGNING_SECRET='local-signing-secret' \
  --from-literal=SLACKBOT_API_KEY='local-slackbot-key' \
  --from-literal=SANDBOX_SIGNING_KEY='local-sandbox-signing-key' \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/centaur-firewall-ca.key /tmp/centaur-firewall-ca.crt
```

## Deploy With Helm

Build the base images and overlay image:

```bash
just build
docker build -t raava-centaur-overlay:local overlays/raava-internal
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
https://iwp-centaur.raava.dev/api/webhooks/slack
```

If Slack receives Cloudflare 1033/530, the DNS route exists but the named
tunnel has no active connector. Start the existing tunnel after the Slackbot is
reachable locally or in-cluster:

```bash
cloudflared tunnel info iwp-centaur
cloudflared tunnel run iwp-centaur
```

Then verify:

```bash
curl -fsS https://iwp-centaur.raava.dev/health
```

## Slack App

Apply or verify the manifest in
`overlays/raava-internal/deploy/slack-app-manifest.json`.

After the app is installed in the Raava workspace:

1. Mention `@Centaur` in a channel mapped by
   `RAAVA_CENTAUR_CHANNEL_DEFAULTS`.
2. Verify the workflow is `slack_thread_turn`.
3. Verify metadata includes the resolved function lead.
4. Try an explicit lead selector such as `--vivian`.
5. Try a private specialist selector such as `--hana`; the answer should be
   owned by the relevant function lead.
