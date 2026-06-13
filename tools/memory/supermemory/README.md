# Supermemory Plugin

Supermemory-backed memory for Centaur agents.

## Secrets

Set `SUPERMEMORY_API_KEY` in the Centaur secret source. The tool sends a
placeholder bearer token; iron-proxy resolves the real key for
`api.supermemory.ai`.

## Tools

### `write`

Store text in Supermemory. Use `container_tag` to isolate project, thread, or
deployment memory. The default container is `raava-centaur`.

### `recall`

Search Supermemory memories by natural-language query. The default limit is 5.

### `status`

Check a document ingestion status by Supermemory document id.

