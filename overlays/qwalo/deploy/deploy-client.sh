#!/usr/bin/env bash
# Deploy one Centaur release for one Qwalo client.
#
#   overlays/qwalo/deploy/deploy-client.sh <client>
#   overlays/qwalo/deploy/deploy-client.sh --check <client>
#   overlays/qwalo/deploy/deploy-client.sh --manifest <client> [file]
#   overlays/qwalo/deploy/deploy-client.sh --values <client> <file>
#   overlays/qwalo/deploy/deploy-client.sh --vault-scope <client>
#
# One client = one namespace (centaur-<client>) = one Slack app = one engine
# account_scope. The Helm release is named `centaur` in every namespace, because
# contrib/scripts/bootstrap-k8s-secrets.sh bakes the Postgres host as
# centaur-centaur-postgres. Isolation is the namespace, not the release name.
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
#   op://prod-qwalo-<client>/OPENAI_API_KEY/credential       -> iron-proxy only
#   op://prod-qwalo-<client>/QWALO_ENGINE_TOKEN/credential   -> iron-proxy only
#
# The last one is never copied by this script into a Kubernetes Secret. iron-proxy
# resolves QWALO_ENGINE_TOKEN itself against op://$OP_VAULT/QWALO_ENGINE_TOKEN/credential
# and stamps the bearer on outbound requests to the engine host, so the sandbox
# holds no token and no token variable at all.
#
# --check renders and validates, then stops: no cluster, no secret read, no Slack.
# --manifest prints the rendered Slack app manifest (it carries no credentials), or
#   writes it to [file] when one is given.
# --values writes the rendered Helm values file, the same bytes a deploy would apply.
#   Nothing but config is in it; secrets are read at apply time, never rendered here.
# --vault-scope runs only the 1Password scope gate: refuses a token that can read a
#   second prod-qwalo-* vault, and refuses to guess when it cannot list vaults.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_DIR="$(dirname "$DEPLOY_DIR")"
REPO_DIR="$(cd "$OVERLAY_DIR/../.." && pwd)"
TEMPLATE="$DEPLOY_DIR/values.client.yaml.tmpl"
SYSTEM_PROMPT="$OVERLAY_DIR/services/sandbox/SYSTEM_PROMPT.md"
MANIFEST_TEMPLATE="$DEPLOY_DIR/slack-app-manifest.json.tmpl"

MODE=deploy
CLIENT=""
# --manifest <client> [file]: the optional destination is captured here, in the
# loop, because the loop shifts every argument it reads.
OUT_FILE=""
# Set only after a human confirmed the vault scope in the 1Password UI.
VAULT_SCOPE_ATTESTED=0

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --manifest) MODE=manifest; shift ;;
    --values) MODE=values; shift ;;
    --vault-scope) MODE=vaultscope; shift ;;
    --vault-scope-verified) VAULT_SCOPE_ATTESTED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    *) if [[ -z "$CLIENT" ]]; then CLIENT="$1"; elif [[ -z "$OUT_FILE" ]]; then OUT_FILE="$1"; else echo "too many arguments" >&2; exit 2; fi; shift ;;
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
# The Helm release is named `centaur` in every client namespace. Release names are
# namespace-scoped, so per-client isolation comes from the namespace, not the name.
# The name is not free: contrib/scripts/bootstrap-k8s-secrets.sh:183 bakes
# DATABASE_URL as ...@centaur-centaur-postgres:5432/ai_v2, and that host is
# <release>-<chart>-postgres. Call it `centaur-ere` and the API is born pointing at
# a host that does not exist (FAILURE_MODES.md F10).
RELEASE="centaur"
DOMAIN="${QWALO_DOMAIN:-qwalo.raava.dev}"
HOST="${CLIENT}.${DOMAIN}"
ENGINE_URL="${QWALO_ENGINE_URL:-https://qwalo.raava.dev}"
# No override. The account_scope IS the client, so a stale export cannot point one
# client's release at another client's leads (FAILURE_MODES.md F1).
ENGINE_SCOPE="$CLIENT"
# The console that answers the link the tool stamps onto a lead answer. One console
# serves every scope today; a client with its own console overrides this.
CONSOLE_URL="${QWALO_CONSOLE_URL:-https://qwalo.raava.dev}"
IMAGE_TAG="${QWALO_IMAGE_TAG:-}"
TOOL_REF="${QWALO_TOOL_REF:-}"
CHART_DIR="${QWALO_CHART_DIR:-$REPO_DIR/contrib/chart}"
VAULT="prod-qwalo-${CLIENT}"

# Item names, canonical field `credential`. The engine token name is fixed by
# iron-proxy's lookup (op://$OP_VAULT/<SECRET_NAME>/credential), not by taste.
VAULT_ITEMS=(SLACK_BOT_TOKEN SLACK_SIGNING_SECRET SLACKBOT_API_KEY OPENAI_API_KEY QWALO_ENGINE_TOKEN)

fail() { echo "FATAL: $*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"; }

# One service account per client vault, scoped to that vault alone. If the token can
# read a second prod-qwalo-* vault, one client's deploy could fund another client's
# release. Fails closed: no list, no deploy. A human who has checked the scope in
# the 1Password UI can attest with --vault-scope-verified.
check_vault_scope() {
  local json others
  json="$(op vault list --format json 2>/dev/null)" \
    || fail "cannot list vaults with this token. Confirm in the 1Password UI that the service account sees only ${VAULT}, then re-run with --vault-scope-verified"
  others="$(printf '%s' "$json" | grep -o 'prod-qwalo-[a-z0-9-]*' | grep -v "^prod-qwalo-${CLIENT}\$" | sort -u || true)"
  [[ -z "$others" ]] || fail "this 1Password token can also read: $(echo $others | tr '\n' ' ') — use a service account scoped to ${VAULT} only"
  echo "vault scope : ${VAULT} only"
}

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
      -e "s|<<CONSOLE_URL>>|${CONSOLE_URL}|g" \
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

# An empty substitution is not an empty string, it is a release with no identity:
# `tag: ""` renders fine and then helm pulls a mutable image. The Slack manifest is
# exempt — the app is created before any image exists (RELEASE.md step 2).
if grep -qE 'tag: ""' "$VALUES"; then
  fail "QWALO_IMAGE_TAG is empty; the rendered values would name no image"
fi
if grep -qE 'ref: ""' "$VALUES"; then
  fail "QWALO_TOOL_REF is empty; the rendered values would name no tool ref"
fi

# A values file is diffed, pasted into Slack, and printed by CI. It carries
# config only. Any secret-named YAML key here is the bug F6 describes.
if grep -nE '^[^#]*[A-Z_]*(BOT_TOKEN|SIGNING_SECRET|API_KEY|ENGINE_TOKEN|PASSWORD)[A-Z_]*[[:space:]]*:' "$VALUES" "$MANIFEST"; then
  fail "a secret-named key appeared in the rendered values or manifest"
fi

if [[ "$MODE" == "values" ]]; then
  # The rendered values file, written where a reviewer or a helm run can read it.
  # Without this the only copy lives in a temp directory that dies with the script,
  # and the proof of a release has to re-do the substitution by hand. That is a
  # second implementation of the render, and it drifts.
  [[ -n "$OUT_FILE" ]] || fail "--values <client> <file>: name the file to write"
  cp "$VALUES" "$OUT_FILE"
  echo "wrote $OUT_FILE" >&2
  exit 0
fi

if [[ "$MODE" == "manifest" ]]; then
  # The manifest carries no credentials, so it is safe to print and pipe.
  if [[ -n "$OUT_FILE" ]]; then
    cp "$MANIFEST" "$OUT_FILE"; echo "wrote $OUT_FILE" >&2
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
  echo "image tag    : ${IMAGE_TAG}"
  echo "tool ref     : ${TOOL_REF}"
  echo "tools shipped: overlays/qwalo/tools only (engine). Base tools/ tree excluded."
  echo "slack host   : https://${HOST}"
  echo "account_scope: ${ENGINE_SCOPE} (no override)"
  echo "console link : ${CONSOLE_URL}/#/review?id=<external_id>  (stamped by the tool)"
  echo "engine token : iron-proxy inject; op://${VAULT}/QWALO_ENGINE_TOKEN/credential"
  echo "harness auth : codex api_key; op://${VAULT}/OPENAI_API_KEY/credential"
  echo "vault items  : ${VAULT_ITEMS[*]} (field credential)"
  echo "values       : rendered clean ($(wc -l < "$VALUES" | tr -d ' ') lines, deleted on exit)"
  exit 0
fi

if [[ "$MODE" == "vaultscope" ]]; then
  require op
  [[ "$VAULT_SCOPE_ATTESTED" == "1" ]] && { echo "vault scope : attested by operator, not checked"; exit 0; }
  check_vault_scope
  exit 0
fi

# ---- 1. tools and vault scope ---------------------------------------------------
require op; require kubectl; require helm
[[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] || fail "OP_SERVICE_ACCOUNT_TOKEN is required"
check_pinned
[[ "$VAULT_SCOPE_ATTESTED" == "1" ]] || check_vault_scope

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
# iron-proxy resolves OPENAI_API_KEY and QWALO_ENGINE_TOKEN itself, from the vault
# named by OP_VAULT, and stamps them in flight. Neither is exported here: an export
# would put a credential in this process for no consumer, and the chart reads model
# auth from iron-proxy, not from centaur-infra-env.
export OP_VAULT="$VAULT"
trap 'unset SLACK_BOT_TOKEN SLACK_SIGNING_SECRET SLACKBOT_API_KEY; rm -rf "$WORK"' EXIT

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
