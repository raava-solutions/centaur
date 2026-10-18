# Approver-gated Proxmox root SSH — runbook and E2E proof

Feature: sandboxes of Slack threads whose ROOT author is in
`apiRs.privilegedAccess.approverUserIds` (currently zay, `U08TZCX8Q2E`) get
root SSH credentials to pve-01..pve-05 installed on every sandbox bind, plus
TCP/22 egress to exactly the five nodes' tailnet addresses. Every other
sandbox is actively cleared of both. Evaluation uses the session's immutable
`original_requester` snapshot (INSERT-time), never the mutable per-message
metadata.

Trust chain: Slack thread-root author → slackbotv2 create metadata
(`slack_thread_author_user_id` / `slack_thread_is_reply`) → api-rs
`original_requester` column (written once on INSERT, never merged over) →
`original_requester_is_approver` (fail closed) → per-bind reconcile →
exec-stdin install of key/known_hosts/ssh_config + per-sandbox additive
egress NetworkPolicy. Destructive SSH commands remain approval-gated in the
persona/system prompts (`approval request`, zay's in-thread decision).

## Ways the gate could fail, and how each is covered

1. **Requester change mid-thread.** `metadata` is merged over with the
   current poster on every create/reuse; a gate reading it would flip when a
   non-approver posts into zay's thread, or grant when zay posts into someone
   else's. Prevented by the INSERT-only `original_requester` column.
   E2E step 5: after a non-approver posts a turn in zay's gated thread, SSH
   still works (thread-level scope) and the durable row still shows zay while
   `metadata` shows the new poster.

2. **Approver posting into a non-approver's thread.** zay mentioning the bot
   in a thread someone else started would grant a sandbox that thread's other
   posters can also drive. Prevented by resolving the thread-ROOT author
   (conversations.replies) and failing closed when a reply-created session
   cannot resolve it (`slack_thread_is_reply` with no author denies).
   E2E step 6: zay mentions the bot in a non-approver's thread → no key, no
   egress, SSH refused.

3. **Warm-pool spare leakage.** A pre-created warm spare must never carry
   credentials, and an approver binding a spare must get them at claim time.
   Prevented by reconciling on every bind (spares are created keyless; the
   claim path installs). E2E step 2: with warm pool size 1, verify the
   unclaimed spare pod has no `*-privileged-access` NetworkPolicy; zay's
   first turn claims it and SSH works immediately.

4. **Approver-list drift.** Removing zay from the list must revoke existing
   sessions, not just future ones. Prevented because the reconcile runs on
   every bind against the CURRENT config and clears non-gated sandboxes.
   E2E step 8: redeploy with an empty approver list; a new turn in zay's
   existing thread loses the key and the netpol.

5. **Install failure failing open.** A missing/empty Secret or exec/netpol
   error on a gated bind would otherwise hand the session a half-installed
   grant. Prevented fail-closed: install errors fail the turn; only clear
   (removal) is best-effort. E2E step 7: point the Secret name at a missing
   Secret; zay's turn must FAIL with the privileged-access error, not run
   without SSH.

6. **Partial configuration half-enabling.** Approvers set but destinations
   missing (or any subset) must not enable a broken grant. Prevented in both
   the chart (all three must be set) and api-rs (partial config logs a
   warning and stays off). E2E step 0 includes the rendered-manifest check
   (done in development: env + Secret volume render only when fully
   configured).

7. **Egress escape.** The grant must not become "port 22 to the whole
   tailnet" or a key usable from anywhere. Prevented by /32-per-node egress
   entries, TCP/22 only, and the `from=` source pin in each node's
   authorized_keys entry (installed per node — the files are NODE-LOCAL).
   E2E steps 3–4: SSH to a non-listed tailnet host and TCP to a non-22 port
   on a node are both refused from the gated sandbox; the key is refused from
   devbox (outside the k3s egress).

8. **Key material exposure.** Key bytes must never appear in logs, pod specs,
   env, or ConfigMaps. Prevention: exec-stdin only (no command-line args),
   SHA-256 fingerprint logging only, Secret mounted 0400 into api-rs alone.
   E2E step 2: inspect api-rs logs and the sandbox pod spec for key bytes;
   confirm in-sandbox files are mode 0600 and absent from non-approver
   sandboxes.

9. **Sandbox replacement dropping the grant (or leaking the old netpol).**
   Capability changes replace a session's sandbox mid-thread. Prevented
   because the reconcile runs at the end of every `ensure_session_sandbox`,
   and sandbox stop deletes the privileged-access policy. E2E step 5's
   second half: force a sandbox replacement (restricted-capability turn) and
   confirm SSH still works in the new sandbox and the old pod's netpol is
   gone.

## Out-of-band setup (requires operator approval — none of this is in Helm)

1. **Key** (never in this repo): `ssh-keygen -t ed25519 -f centaur-proxmox-root
   -C centaur-sandbox@raava` (no passphrase), stored in 1Password
   Engineering as `CENTAUR_PROXMOX_ROOT_SSH` (private key + public key).
2. **known_hosts**: `ssh-keyscan -t ed25519 <node-tailnet-ip>` for all five
   nodes, fingerprints cross-checked against known-good host keys, entries
   written per tailnet IP.
3. **ssh_config**: `Host pve-0N` blocks, `HostName <tailnet IP>`, `User root`,
   `IdentityFile ~/.ssh/centaur-privileged/id_ed25519`,
   `UserKnownHostsFile ~/.ssh/centaur-privileged/known_hosts`,
   `IdentitiesOnly yes`, `StrictHostKeyChecking yes`.
4. **Kubernetes Secret** `centaur-privileged-access` (namespace `centaur`)
   with keys `id_ed25519`, `known_hosts`, `ssh_config`:
   `kubectl -n centaur create secret generic centaur-privileged-access
   --from-file=id_ed25519=... --from-file=known_hosts=...
   --from-file=ssh_config=...`.
5. **Node authorized_keys** (EACH of the five nodes; `/etc/pve/priv/` is NOT
   shared): append one additive line to `/root/.ssh/authorized_keys`:
   `from="<node-observed-source-ips>",restrict ssh-ed25519 AAAA…
   centaur-sandbox@raava`. The source IP is observed in E2E step 1 (node
   auth logs) and pinned afterwards; until then install WITHOUT `from=` is
   acceptable only during the sealed E2E window.
6. **Rollout**: build + side-load `centaur-api-rs`, `centaur-slackbotv2`,
   `centaur-agent` (image now ships openssh-client) from this branch;
   `helm upgrade` with the overlay; api-rs restarts and picks up the env.

## E2E protocol (live cluster; run after setup, record every command + result)

- **Step 0** — Config review: `helm template ... --show-only templates/apirs.yaml`
  shows the four `SESSION_PRIVILEGED_ACCESS_*` env vars and the Secret volume;
  api-rs logs `privileged_access` configuration once at startup (no secret
  material).
- **Step 1 — Approver grant**: zay starts a NEW top-level thread and asks for
  `ssh root@pve-0N uptime` on all five nodes. All five succeed. Verify: api-rs
  logged `sandbox_privileged_access_installed`; the session pod has a
  `*-privileged-access` NetworkPolicy with the five /32s on TCP/22; in-sandbox
  files are 0600. THEN capture the source IP each node saw in its auth log —
  this becomes the `from=` pin (setup item 5).
- **Step 2 — Warm spare keyless + non-approver denial**: before any gated
  thread claims it, the warm spare pod has no privileged-access netpol and no
  key. A non-approver (second account) starts a thread asking for the same
  SSH: the agent reports privileged access unavailable; verify no key files
  and no privileged-access netpol on that session's pod.
- **Step 3 — Egress scope**: from zay's gated sandbox, port 22 to a
  non-listed tailnet host is refused, and a non-22 port on a node
  (e.g. 8006) is refused.
- **Step 4 — Key refused outside**: from devbox, attempt SSH to a node with
  the same private key: the `from=` pin rejects it (node auth log shows the
  refused attempt).
- **Step 5 — Immutability + rebind**: a non-approver posts a turn in zay's
  thread asking for SSH again: it still works (thread-level grant) and the
  durable row (`original_requester`) still shows zay while `metadata` shows
  the other poster. Then force a sandbox replacement (a
  restricted-capability turn replaces the pod): SSH still works in the new
  sandbox; the old pod's privileged-access netpol is deleted.
- **Step 6 — Hijack denial**: a non-approver starts a thread; zay replies in
  that thread mentioning the bot: no key, no netpol, SSH refused; the
  snapshot records the thread-root author (or `slack_thread_is_reply` with an
  unresolvable author, which also denies).
- **Step 7 — Install failure fails closed**: temporarily point
  `credentialsSecretName` at a missing Secret and redeploy; zay's next turn
  FAILS with the privileged-access install error (fail closed). Restore.
- **Step 8 — Revocation**: set `approverUserIds: []` and redeploy; a new turn
  in zay's existing thread has no key and its netpol is cleared. Restore
  zay's ID and confirm a fresh thread regains access.

Each step's evidence (commands, outputs, log lines, netpol dumps) goes into
the project report. Steps 7–8 are invasive (api-rs restarts): run them only
in the approved maintenance window.
