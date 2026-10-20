#!/usr/bin/env bash
# Deploy one Centaur release for one Qwalo client.
#
#   overlays/qwalo/deploy/deploy-client.sh <client>
#   overlays/qwalo/deploy/deploy-client.sh --check <client>
#   overlays/qwalo/deploy/deploy-client.sh --manifest <client> [file]
#
# One client = one namespace (centaur-<client>) = one Helm release = one Slack app
# = one engine account_scope. Nothing is shared between clients except the cluster
# and, when it is installed once, the cluster-scoped agent-sandbox controller.
#
# Required environment for a real deploy (release identity is pinned, never latest):
#
#   QWALO_IMAGE_TAG    image tag for api-rs, slackbotv2, the sandbox
#   QWALO_TOOL_REF     git ref the tool tree is cloned from
#   OP_SERVICE_ACCOUNT_TOKEN   a service account scoped to THIS client's vault only
#
# Secrets are never arguments, values, or files in the repo. They are read at apply
# time from one vault named after the client, with the canonical item/field shape
# the base platform's 1Password source expects (item = secret name, field =
# credential):
#
#   op://prod-qwalo-<client>/SLACK_BOT_TOKEN/credential     -> SLACK_BOT_TOKEN
#   op://prod-qwalo-<client>/SLACK_SIGNING_SECRET/credential -> SLACK_SIGNING_SECRET
#   op://prod-qwalo-<client>/SLACKBOT_API_KEY/credential     -> SLACKBOT_API_KEY
#   op://prod-qwalo-<client>/OPENAI_API_KEY/credential       -> OPENAI_API_KEY
#   op://prod-qwalo-<client>/QWALO_ENGINE_TOKEN/credential   -> iron-proxy only
#
# The last one is never copied by this script into a Kubernetes Secret. iron-proxy
# resolves QWALO_ENGINE_TOKEN itself against op://$OP_VAULT/QWALO_ENGINE_TOKEN/credential
# and stamps the bearer on outbound requests to the engine host, so the sandbox
# holds no token and no token variable at all.
#
# --check renders and validates, then stops: no cluster, no secret read, no Slack.
# --manifest prints the rendered Slack app manifest (it carries no secrets) so a
# human never re-renders it by hand with sed.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(dirname "$DEPLOY_DIR")"
REPO_DIR="$(cd "$OVERLAY_DIR/../.." && pwd)"
TEMPLATE="$DEPLOY_DIR/values.client.yaml.tmpl"
SYSTEM_PROMPT="$OVERLAY_DIR/services/sandbox/SYSTEM_PROMPT.md"
MANIFEST_TEMPLATE="$DEPLOY_DIR/slack-app-manifest.json.tmpl"

MODE=deploy
CLIENT=""

usage() {
  sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --manifest) MODE=manifest; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    *) [[ -n "$CLIENT" ]] && { echo "one client at a time" >&2; exit 2; }; CLIENT="$1"; shift ;;
  esac
done

[[ -n "$CLIENT" ]] || { echo "usage: deploy-client.sh [--check|--manifest] <client>" >&2; exit 2; }
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
# No override. The account_scope IS the client, so a stale export cannot point one
# client's release at another client's leads (FAILURE_MODES.md F1).
ENGINE_SCOPE="$CLIENT"
IMAGE_REGISTRY="${QWALO_IMAGE_REGISTRY:-}"
IMAGE_TAG="${QWALO_IMAGE_TAG:-}"
TOOL_REF="${QWALO_TOOL_REF:-}"
CHART_DIR="${QWALO_CHART_DIR:-$REPO_DIR/contrib/chart}"
VAULT="prod-qwalo-${CLIENT}"

# Item names, canonical field `credential`. The engine token name is fixed by
# iron-proxy's lookup (op://$OP_VAULT/<SECRET_NAME>/credential), not by taste.
VAULT_ITEMS=(SLACK_BOT_TOKEN SLACK_SIGNING_SECRET SLACKBOT_API_KEY OPENAI_API_KEY QWALO_ENGINE_TOKEN)

fail() { echo "FATAL: $*" >&2; exit 1; }

for f in "$TEMPLATE" "$SYSTEM_PROMPT" "$MANIFEST_TEMPLATE"; do
  [[ -r "$f" ]] || fail "missing $f"
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

# Pinned identity, checked before anything else: an unpinned release is a release
# that changes when someone else pushes.
check_pinned() {
  [[ -n "$IMAGE_TAG" ]] || fail "QWALO_IMAGE_TAG is required; a client release names the build it runs"
  [[ -n "$TOOL_REF" ]] || fail "QWALO_TOOL_REF is required; a client release names the tool tree it runs"
  [[ "$IMAGE_TAG" != "latest" ]] || fail "QWALO_IMAGE_TAG must be a pinned tag, not latest"
  [[ "$TOOL_REF" != "main" && "$TOOL_REF" != "master" ]] || fail "QWALO_TOOL_REF must be a pinned ref (a tag or a sha), not a moving branch"
}

render

if grep -qE '<<[A-Z_]+>>' "$VALUES" || grep -qE '<<[A-Z_]+>>' "$MANIFEST"; then
  fail "unsubstituted placeholder left in the rendered values or manifest"
fi

# A values file is diffed, pasted into Slack, and printed by CI. It carries
# config only. Any secret-named YAML key here is the bug F6 describes.
if grep -nE '^[^#]*[A-Z_]*(BOT_TOKEN|SIGNING_SECRET|API_KEY|ENGINE_TOKEN|PASSWORD)[A-Z_]*[[:space:]]*:' "$VALUES" "$MANIFEST"; then
  fail "a secret-named key appeared in the rendered values or manifest"
fi

if [[ "$MODE" == "manifest" ]]; then
  # The manifest carries no credentials, so it is safe to print and pipe.
  if [[ $# -gt 0 && -n "${1:-}" ]]; then
    cp "$MANIFEST" "$1"; echo "wrote $1" >&2
  else
    cat "$MANIFEST"
  fi
  exit 0
fi

if [[ "$MODE" == "check" ]]; then
  check_pinned
  echo "client       : $CLIENT"
  echo "namespace    : $NAMESPACE"
  echo "helm release : $RELEASE"
  echo "image tag    : ${IMAGE_REGISTRY:-<in-cluster>}/${IMAGE_TAG}"
  echo "tool ref     : ${TOOL_REF}"
  echo "tools shipped: overlays/qwalo/tools only (engine). Base tools/ tree excluded."
  echo "slack host   : https://${HOST}"
  echo "account_scope: ${ENGINE_SCOPE} (no override)"
  echo "engine token : iron-proxy inject; op://${VAULT}/QWALO_ENGINE_TOKEN/credential"
  echo "harness auth : codex api_key; op://${VAULT}/OPENAI_API_KEY/credential"
  echo "vault items  : ${VAULT_ITEMS[*]} (field credential)"
  echo "values       : rendered clean ($(wc -l < "$VALUES" | tr -d ' ') lines, deleted on exit)"
  exit 0
fi

# ---- 1. tools and vault scope ---------------------------------------------------
require() { command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"; }
require op; require kubectl; require helm
[[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] || fail "OP_SERVICE_ACCOUNT_TOKEN is required"
check_pinned

# One service account per client vault, scoped to that vault alone. When the token
# can read a second prod-qwalo-* vault, one client's deploy could fund another
# client's release, so stop here rather than discover it later.
if other_vaults=$(op vault list --format json 2>/dev/null | grep -o "prod-qwalo-[a-z0-9-]*" | grep -v "prod-qwalo-${CLIENT}\$" | sort -u); then
  if [[ -n "$other_vaults" ]]; then
    fail "this 1Password token can also read: $(echo "$other_vaults" | tr '\n' ' ')— use a service account scoped to ${VAULT} only"
  fi
else
  echo "WARN: could not list vaults with this token; confirm by hand that it is scoped to ${VAULT}" >&2
fi

# ---- 2. secrets -----------------------------------------------------------------
# Existence checks only: a read into /dev/null proves the item and field exist
# without putting a credential in this shell's history, a log line, or a terminal.
for item in "${VAULT_ITEMS[@]}"; do
  op read "op://${VAULT}/${item}/credential" >/dev/null 2>&1 \
    || fail "cannot read op://${VAULT}/${item}/credential — create it before deploying (RELEASE.md step 4)"
done

export SLACK_BOT_TOKEN="$(op read "op://${VAULT}/SLACK_BOT_TOKEN/credential")"
export SLACK_SIGNING_SECRET="$(op read "op://${VAULT}/SLACK_SIGNING_SECRET/credential")"
export SLACKBOT_API_KEY="$(op read "op://${VAULT}/SLACKBOT_API_KEY/credential")"
export OPENAI_API_KEY="$(op read "op://${VAULT}/OPENAI_API_KEY/credential")"
# iron-proxy resolves the engine bearer from this vault. The sandbox never sees it.
export OP_VAULT="$VAULT"
trap 'unset SLACK_BOT_TOKEN SLACK_SIGNING_SECRET SLACKBOT_API_KEY OPENAI_API_KEY; rm -rf "$WORK"' EXIT

# ---- 3. namespace + infra secret ------------------------------------------------
# bootstrap-k8s-secrets.sh owns centaur-infra-env (the Secret the chart reads with
# secretManager.existingSecretName) plus the iron-proxy CA, in one namespace.
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
"$REPO_DIR/contrib/scripts/bootstrap-k8s-secrets.sh" --namespace "$NAMESPACE"

# ---- 4. apply -------------------------------------------------------------------
[[ -d "$CHART_DIR" ]] || fail "chart not found at $CHART_DIR"
helm upgrade --install "$RELEASE" "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  -f "$CHART_DIR/values.yaml" \
  -f "$VALUES" \
  --set-file "overlay.systemPrompt=$SYSTEM_PROMPT"

echo "Deployed $RELEASE in $NAMESPACE on ${IMAGE_TAG} (tools ${TOOL_REF})."
echo "Deployment names come from <release>-<chart>-<component>:"
echo "  kubectl -n $NAMESPACE get deploy"
echo "  kubectl -n $NAMESPACE rollout status deploy/\$(kubectl -n $NAMESPACE get deploy -o name | sed 's|deployment/||' | tr '\n' ' ')"
echo "Then run the probes in RELEASE.md step 7, and only then set the Slack event URL:"
echo "  https://${HOST}/api/webhooks/slack   # Slack app -> Event Subscriptions -> Request URL"
