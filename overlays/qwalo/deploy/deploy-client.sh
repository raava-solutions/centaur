#!/usr/bin/env bash
# Deploy one Centaur release for one Qwalo client.
#
#   overlays/qwalo/deploy/deploy-client.sh <client>
#   overlays/qwalo/deploy/deploy-client.sh --check <client>
#
# One client = one namespace (centaur-<client>) = one Helm release = one Slack app
# = one engine account_scope. Nothing is shared between clients except the cluster
# and, when it is installed once, the cluster-scoped agent-sandbox controller.
#
# Secrets are never arguments, values, or files in the repo. They are read from
# 1Password at apply time:
#
#   op://prod-qwalo-<client>/slack bot token/password        -> SLACK_BOT_TOKEN
#   op://prod-qwalo-<client>/slack signing secret/password   -> SLACK_SIGNING_SECRET
#   op://prod-qwalo-<client>/slackbot api key/password       -> SLACKBOT_API_KEY
#   op://prod-qwalo-<client>/engine v1 token/password        -> resolved by iron-proxy
#
# The engine token is deliberately never copied into a Kubernetes Secret: the tool
# declares it as an iron-proxy inject-mode secret, so iron-proxy reads it from the
# vault named by OP_VAULT (prod-qwalo-<client> here) and adds the bearer header in
# flight. The sandbox and this script both go without it.
#
# --check renders and validates the values file, then stops. It touches no cluster,
# no 1Password item, and no Slack app; it is what the E2E runner executes.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(dirname "$DEPLOY_DIR")"
REPO_DIR="$(cd "$OVERLAY_DIR/../.." && pwd)"
TEMPLATE="$DEPLOY_DIR/values.client.yaml.tmpl"
SYSTEM_PROMPT="$OVERLAY_DIR/services/sandbox/SYSTEM_PROMPT.md"
MANIFEST_TEMPLATE="$DEPLOY_DIR/slack-app-manifest.json.tmpl"

CHECK_ONLY=0
CLIENT=""

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    *) [[ -n "$CLIENT" ]] && { echo "one client at a time" >&2; exit 2; }; CLIENT="$1"; shift ;;
  esac
done

[[ -n "$CLIENT" ]] || { echo "usage: deploy-client.sh [--check] <client>" >&2; exit 2; }
# A client name is a namespace name, a hostname label, and a 1Password vault name.
# Anything else is a typo about to reach a cluster.
if [[ ! "$CLIENT" =~ ^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$ ]]; then
  echo "client must be a lowercase DNS slug (a-z, 0-9, -): got '$CLIENT'" >&2
  exit 2
fi

NAMESPACE="centaur-${CLIENT}"
RELEASE="centaur-${CLIENT}"
DOMAIN="${QWALO_DOMAIN:-qwalo.raava.dev}"
HOST="${CLIENT}.${DOMAIN}"
ENGINE_URL="${QWALO_ENGINE_URL:-https://qwalo.raava.dev}"
ENGINE_SCOPE="${QWALO_ENGINE_SCOPE:-$CLIENT}"
IMAGE_REGISTRY="${QWALO_IMAGE_REGISTRY:-}"
IMAGE_TAG="${QWALO_IMAGE_TAG:-latest}"
TOOL_REF="${QWALO_TOOL_REF:-main}"
CHART_DIR="${QWALO_CHART_DIR:-$REPO_DIR/contrib/chart}"
VAULT="prod-qwalo-${CLIENT}"

# ---- 1. render -------------------------------------------------------------------
for f in "$TEMPLATE" "$SYSTEM_PROMPT" "$MANIFEST_TEMPLATE"; do
  [[ -r "$f" ]] || { echo "FATAL: missing $f" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
VALUES="$WORK/values.${CLIENT}.yaml"
MANIFEST="$WORK/slack-app-manifest.${CLIENT}.json"

render() {
  sed -e "s|<<CLIENT>>|${CLIENT}|g" \
      -e "s|<<NAMESPACE>>|${NAMESPACE}|g" \
      -e "s|<<HOST>>|${HOST}|g" \
      -e "s|<<ENGINE_URL>>|${ENGINE_URL}|g" \
      -e "s|<<ENGINE_SCOPE>>|${ENGINE_SCOPE}|g" \
      -e "s|<<REGISTRY>>|${IMAGE_REGISTRY}|g" \
      -e "s|<<TAG>>|${IMAGE_TAG}|g" \
      -e "s|<<TOOL_REF>>|${TOOL_REF}|g" \
      "$TEMPLATE" > "$VALUES"
  sed -e "s|<<CLIENT>>|${CLIENT}|g" -e "s|<<HOST>>|${HOST}|g" \
      "$MANIFEST_TEMPLATE" > "$MANIFEST"
}
render

fail() { echo "FATAL: $*" >&2; exit 1; }

if grep -qE '<<[A-Z_]+>>' "$VALUES" || grep -qE '<<[A-Z_]+>>' "$MANIFEST"; then
  fail "unsubstituted placeholder left in the rendered values or manifest"
fi

# A values file is diffed, pasted into Slack, and printed by CI. It carries
# config only. Any secret-named YAML key here is the bug F6 describes.
if grep -nE '^[^#]*[A-Z_]*(BOT_TOKEN|SIGNING_SECRET|API_KEY|ENGINE_TOKEN|PASSWORD)[A-Z_]*[[:space:]]*:' "$VALUES" "$MANIFEST"; then
  fail "a secret-named key appeared in the rendered values or manifest"
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "client      : $CLIENT"
  echo "namespace   : $NAMESPACE"
  echo "helm release: $RELEASE"
  echo "slack host  : https://${HOST}"
  echo "engine      : $ENGINE_URL scope=$ENGINE_SCOPE (token: iron-proxy inject, vault $VAULT)"
  echo "values      : rendered clean ($(wc -l < "$VALUES" | tr -d ' ') lines, deleted on exit)"
  echo "manifest    : rendered clean, event request_url left empty until the release is live"
  exit 0
fi

# ---- 2. secrets ------------------------------------------------------------------
require() { command -v "$1" >/dev/null 2>&1 || { echo "FATAL: required command not found: $1" >&2; exit 1; }; }
require op; require kubectl; require helm
[[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] || { echo "FATAL: OP_SERVICE_ACCOUNT_TOKEN is required" >&2; exit 1; }

# Existence checks only: a read into /dev/null proves the item is there without
# putting a credential in this shell's history, a log line, or a terminal.
for item in "slack bot token" "slack signing secret" "slackbot api key" "engine v1 token"; do
  op read "op://${VAULT}/${item}/password" >/dev/null 2>&1 \
    || fail "cannot read op://${VAULT}/${item}/password — create it before deploying (RELEASE.md step 3)"
done

export SLACK_BOT_TOKEN="$(op read "op://${VAULT}/slack bot token/password")"
export SLACK_SIGNING_SECRET="$(op read "op://${VAULT}/slack signing secret/password")"
export SLACKBOT_API_KEY="$(op read "op://${VAULT}/slackbot api key/password")"
# iron-proxy resolves QWALO_ENGINE_TOKEN from this vault.
export OP_VAULT="$VAULT"
trap 'unset SLACK_BOT_TOKEN SLACK_SIGNING_SECRET SLACKBOT_API_KEY; rm -rf "$WORK"' EXIT

# ---- 3. namespace + infra secret -------------------------------------------------
# bootstrap-k8s-secrets.sh owns centaur-infra-env (the Secret the chart reads with
# secretManager.existingSecretName) plus the iron-proxy CA, in one namespace.
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
"$REPO_DIR/contrib/scripts/bootstrap-k8s-secrets.sh" --namespace "$NAMESPACE"

# ---- 4. apply --------------------------------------------------------------------
[[ -d "$CHART_DIR" ]] || fail "chart not found at $CHART_DIR"
helm upgrade --install "$RELEASE" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  -f "$CHART_DIR/values.yaml" \
  -f "$VALUES" \
  --set-file "overlay.systemPrompt=$SYSTEM_PROMPT"

echo "Deployed $RELEASE in $NAMESPACE. Watch it, then set the Slack event URL:"
echo "  kubectl -n $NAMESPACE rollout status deploy/${RELEASE}-centaur-api-rs deploy/${RELEASE}-centaur-slackbotv2"  # names: <release>-<chart>-<component>
echo "  https://://${HOST}/api/webhooks/slack   # Slack app → Event Subscriptions → Request URL"
