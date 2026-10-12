# Approval Plugin

Durable, one-shot operator approvals for agent actions. When an action needs
operator sign-off, the agent runs:

```
approval request --summary "qm destroy 410 --purge (VM 410 on pve-03)" \
  --action "qm destroy 410 --purge"
```

This creates a durable approval request at api-rs (Postgres `approval_requests`
table) bound to the hash of the exact action, posts a Block Kit card with
Approve/Deny buttons into the current Slack thread (delivered by slackbotv2),
and blocks until the decision lands. Exit code 0 = approved, 1 = denied,
2 = expired or local timeout. The JSON record (id, status, approver,
decided_at) prints on stdout.

Only Slack user IDs in the deployment's `APPROVER_SLACK_USER_IDS` allowlist
can decide; anyone else's click gets an ephemeral refusal and the request
stays pending. A typed chat message is NOT approval. Decisions are one-shot
and bound to the action hash: a changed action needs a new request.

## Configuration (sandbox env)

- `CENTAUR_API_URL` — api-rs base URL (in-cluster, plain HTTP). The client
  bypasses the egress proxy (`trust_env=False`): iron-proxy rejects plain-HTTP
  forwards.
- `CENTAUR_APPROVAL_API_KEY` — approval-scoped bearer token (api-rs
  `approval-agent` caller: create/read only, never decide). Passed into
  sandboxes via `SESSION_SANDBOX_PASSTHROUGH_ENV` from the infra secret.
- `CENTAUR_THREAD_KEY` — current thread; the request is anchored to it and the
  Slack card is posted there.

## Tools

- `request` — create + block until decision (see above).
- `status <approval-id>` — fetch the current state without waiting.
- `hash <action>` — print the stable action hash for a command string.
