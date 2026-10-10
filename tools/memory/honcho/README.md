# Honcho Plugin

Honcho-backed peer memory for Centaur agents: durable user identity and
metamemory across conversations.

## Configuration

- `HONCHO_BASE_URL` — Honcho API base URL (default `http://localhost:8000`).
  In the Raava deployment this points at the self-hosted instance over the
  tailnet and is passed into sandboxes out of band.
- `HONCHO_WORKSPACE` — Honcho workspace id (default `centaur`).
- `HONCHO_API_KEY` — bearer token. Optional on the server side (Honcho's
  `AUTH_USE_AUTH` may be off); the CLI always sends a value and iron-proxy
  injects the real key for the declared host, so enabling auth later is a
  vault change only.

## Conventions

- Slack requester peer id: `slack:<slack-user-id>` (stored sanitized as
  `slack-U0123ABC` — Honcho ids only allow `[a-zA-Z0-9_-]`).
- Agent persona peer id: `agent:<persona>` (e.g. `agent:chief`).
- Session id: the centaur thread key (e.g.
  `slack:C0TEST12345:1791670752.873056`, stored sanitized).
- All ids passed to this CLI are sanitized deterministically; the sanitized
  form is what Honcho stores.

## Tools

### `health` / `capability` / `workspace-ensure` / `queue-status`

Connectivity, config, workspace bootstrap, and deriver queue depth.

### `peer-create` / `peer-get`

Create (idempotent) or look up a peer.

### `session-create` / `session-get` / `session-add-peer`

Create (idempotent) or look up a session; attach peers.

### `message-add` / `messages-list`

Append peer-attributed messages to a session; list them back.

### `peer-context` / `peer-representation` / `peer-chat`

Metamemory reads: what Honcho knows about a peer. `peer-chat --query` answers
natural-language questions ("what do I know about this user?").

### `session-context`

Session-scoped context (messages, summary, peer representations).
