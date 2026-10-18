# Approver-gated Proxmox root SSH — runbook and E2E proof

Feature: sandboxes of Slack threads whose ROOT author is in
`apiRs.privilegedAccess.approverUserIds` (currently zay, `U08TZCX8Q2E`) get
root SSH credentials to pve-01..pve-05 installed on every sandbox bind, plus
TCP/22 egress to exactly the five nodes' cluster-LAN addresses. Every other
sandbox is actively cleared of both. Evaluation uses the session's immutable
`original_requester` snapshot (INSERT-time), never the mutable per-message
metadata.

Trust chain: Slack thread-root author → authenticated Slack ingress's explicit
`original_requester` create field (`slack_thread_author_user_id` /
`slack_thread_is_reply` plus bot/app markers) → api-rs
`original_requester` column (written once on INSERT, never merged over) →
`original_requester_is_approver` (fail closed) → per-bind reconcile →
exec-stdin install of key/known_hosts/ssh_config + per-sandbox additive
egress NetworkPolicy. Destructive SSH commands remain approval-gated in the
persona/system prompts (`approval request`, zay's in-thread decision).

Why the LAN and not the tailnet: Tailscale SSH is enabled on pve-01..pve-04
(`tailscale debug prefs` → `RunSSH: true`; pve-05 has it off). On those
nodes tailscaled answers port 22 on the tailnet IP and authorizes by tailnet
ACL, so authorized_keys and its `from=` pin would never be consulted. On
the LAN (`10.200.160.128/25`) plain sshd answers, and centaur-k3s
(`10.200.160.160`) SNATs sandbox pod egress to its LAN address, which is
the `from=` pin.

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
   E2E step 8: redeploy with an empty approver list; even though Helm omits
   the now-unused credential mount and api-rs feature env vars, the next bind
   still clears the key and the netpol.

5. **Install failure failing open.** A missing/empty Secret or exec/netpol
   error on a gated bind would otherwise hand the session a half-installed
   grant. The NetworkPolicy is created before any file as a durable
   payload-present marker; clear removes managed files first and deletes the
   policy last. A failed install therefore leaves a marker for the next clear,
   and no marker proves that there is nothing to remove. Install and clear
   errors both fail the turn closed. E2E step 7: point the Secret name at a missing
   Secret; zay's turn must FAIL with the privileged-access error, not run
   without SSH.

6. **Partial configuration half-enabling.** Approvers set but destinations
   missing (or any subset) must not enable a broken grant. Prevented in both
   the chart (all three must be set) and api-rs (partial config logs a
   warning and stays off). E2E step 0 includes the rendered-manifest check
   (done in development: env + Secret volume render only when fully
   configured).

7. **Egress escape.** The grant must not become "port 22 to the whole
   LAN" or a key usable from anywhere. Prevented by /32-per-node egress
   entries, TCP/22 only, and the `from="10.200.160.160"` source pin on the
   single shared authorized_keys line (pmxcfs).
   E2E steps 3–4: SSH to a non-listed LAN host and TCP to a non-22 port
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

1. **Key** (never in this repo): generated inside 1Password
   (`op item create --category 'SSH Key' --ssh-generate-key ed25519`) as
   Engineering/`CENTAUR_PROXMOX_ROOT_SSH`; fingerprint
   `SHA256:m/rtfyn66VBgKvoY1NXfFfH4W0TKmLpBek8HPNno13A`.
2. **known_hosts**: one `pve-0N,<LAN IP> ssh-ed25519 …` line per node, read
   from each node's `/etc/ssh/ssh_host_ed25519_key.pub` over an already
   authenticated root session (not `ssh-keyscan`).
3. **ssh_config**: `Host pve-0N` → `HostName` pve-01 `10.200.160.152`,
   pve-02 `.153`, pve-03 `.166`, pve-04 `.214`, pve-05 `.212`; shared
   block: `User root`, `IdentityFile ~/.ssh/centaur-privileged/id_ed25519`,
   `IdentitiesOnly yes`, `UserKnownHostsFile ~/.ssh/centaur-privileged/known_hosts`,
   `StrictHostKeyChecking yes`, `HostKeyAlias %n`, `ConnectTimeout 10`.
4. **Kubernetes Secret** `centaur-privileged-access` (namespace `centaur`)
   with keys `id_ed25519`, `known_hosts`, `ssh_config`:
   `kubectl -n centaur create secret generic centaur-privileged-access
   --from-file=id_ed25519=... --from-file=known_hosts=...
   --from-file=ssh_config=...`.
5. **Node authorized_keys**: `/root/.ssh/authorized_keys` is a symlink to
   the pmxcfs-shared `/etc/pve/priv/authorized_keys` on all five nodes
   (verify size + sha256 match on each first; add by hand on any node whose
   file is local). One additive line, appended once:
   `from="10.200.160.160",no-agent-forwarding,no-port-forwarding,no-X11-forwarding
   ssh-ed25519 AAAA… centaur-sandbox@raava t-0026`. E2E step 1 confirms the
   source the nodes actually log; if it differs, change the pin before the
   work counts as done.
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
  files are 0600. THEN confirm each node's auth log shows the login from
  `10.200.160.160` (the `from=` pin, setup item 5).
- **Step 2 — Warm spare keyless + non-approver denial**: before any gated
  thread claims it, the warm spare pod has no privileged-access netpol and no
  key. A non-approver (second account) starts a thread asking for the same
  SSH: the agent reports privileged access unavailable; verify no key files
  and no privileged-access netpol on that session's pod.
- **Step 3 — Egress scope**: from zay's gated sandbox, port 22 to a
  non-listed LAN host (e.g. devbox `10.200.160.226`) is refused, a node's
  tailnet IP on 22 is refused, and a non-22 port on a node
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
  unresolvable author, which also denies). Repeat with a bot/app-authored root
  whose message carries a Slack user ID: its `bot_id`, `app_id`, or non-plain
  `subtype` marker must still deny the grant.
- **Step 7 — Install failure fails closed**: temporarily point
  `credentialsSecretName` at a missing Secret and redeploy; zay's next turn
  FAILS with the privileged-access install error (fail closed). Restore.
- **Step 8 — Revocation**: set `approverUserIds: []` and redeploy; a new turn
  in zay's existing thread has no key and its netpol is cleared. Restore
  zay's ID and confirm a fresh thread regains access.

Each step's evidence (commands, outputs, log lines, netpol dumps) goes into
the project report. Steps 7–8 are invasive (api-rs restarts): run them only
in the approved maintenance window.
