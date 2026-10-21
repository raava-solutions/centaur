#!/usr/bin/env bash
# Run the Qwalo overlay proof on the Proxmox lab guest, not on a laptop.
#
#   overlays/qwalo/e2e/run-on-proxmox.sh [user@host]     default: engineer@centaur-k3s
#
# What it does, in order, over ssh:
#   1. sync the paths the suite reads: contrib/chart, overlays/qwalo, and one base
#      tool (tools/comms/telegram) whose existence is the point of failure mode F3
#   2. install uv into ~/.local/bin when it is absent (the suite builds the tool
#      with `uvx --from <dir> engine`, the same entrypoint the sandbox CLI shim uses)
#   3. run the E2E suite, including the real `helm template` render of the client
#      release, on Linux, in the cluster's own VM
#   4. copy the artifact and the rendered release back into the repo, so the proof
#      committed to the PR is the proof a lab machine produced
#
# Nothing is left running. The fake engine binds 127.0.0.1 for the length of the run
# and dies with the process. No cluster object is touched: helm template renders, it
# does not apply. No credential is read: the suite talks to a fake engine.
set -euo pipefail

TARGET="${1:-engineer@centaur-k3s}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
REMOTE_DIR="qwalo-proof"
REPORTS="overlays/qwalo/e2e/reports"

echo "== target: $TARGET"
ssh "$TARGET" 'uname -srm; python3 --version; helm version --short'

echo "== sync"
rsync -az --delete -e ssh "$REPO/contrib" "$REPO/overlays" "$TARGET:$REMOTE_DIR/"
rsync -az --delete -e ssh "$REPO/tools/comms" "$TARGET:$REMOTE_DIR/tools/"
ssh "$TARGET" 'command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'

echo "== suite (fake engine on 127.0.0.1, real helm render, no cluster writes)"
ssh -t "$TARGET" "cd $REMOTE_DIR && PATH=\$HOME/.local/bin:\$PATH python3 overlays/qwalo/e2e/run_fake_engine.py"

echo "== rendered release, from the shipping script's own values"
IDENT="$(ssh "$TARGET" "cd $REMOTE_DIR && python3 -c \"import json;d=json.load(open('overlays/qwalo/e2e/reports/fake-engine.json'))['release_identity'];print(d['image_tag'],d['tool_ref'])\"")"
read -r IMAGE_TAG TOOL_REF <<<"$IDENT"
echo "   identity under proof: $IMAGE_TAG / $TOOL_REF"
ssh "$TARGET" "cd $REMOTE_DIR && \
  QWALO_IMAGE_TAG=$IMAGE_TAG QWALO_TOOL_REF=$TOOL_REF \
  overlays/qwalo/deploy/deploy-client.sh --values ere values.ere.yaml && \
  helm template centaur contrib/chart \
    -f contrib/chart/values.yaml \
    -f values.ere.yaml \
    --set-file overlay.systemPrompt=overlays/qwalo/services/sandbox/SYSTEM_PROMPT.md \
    > rendered.yaml && \
  grep -c '^kind:' rendered.yaml"

mkdir -p "$REPO/$REPORTS"
scp -q "$TARGET:$REMOTE_DIR/overlays/qwalo/e2e/reports/fake-engine.json" "$REPO/$REPORTS/fake-engine.json"
scp -q "$TARGET:$REMOTE_DIR/rendered.yaml" "$REPO/$REPORTS/client-release.rendered.yaml"

echo "== committed proof"
cd "$REPO"
sha256sum "$REPORTS/fake-engine.json" "$REPORTS/client-release.rendered.yaml"
echo "run host: $TARGET"
echo "resources rendered: $(grep -c '^kind:' "$REPORTS/client-release.rendered.yaml")"
echo "repeat this proof with: overlays/qwalo/e2e/run-on-proxmox.sh $TARGET"
