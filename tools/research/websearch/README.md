# Websearch Plugin

Web search and deep research tool backed by Exa retrieval plus configurable
analysis/synthesis. OpenRouter is the preferred Raava dogfood synthesis provider;
Anthropic remains available as an explicit compatibility provider.

## Secrets

Set these in root `.env` (preferred) or `tools/websearch/.env`:

- `EXA_API_KEY`
- `OPENROUTER_API_KEY` for synthesized answers through OpenRouter
- `OPENROUTER_MODEL` (default: `deepseek/deepseek-chat`)
- `WEBSEARCH_SYNTHESIS_PROVIDER` (`auto`, `openrouter`, or `anthropic`; default: `auto`)
- `ANTHROPIC_API_KEY` for Anthropic compatibility
- `DEEP_RESEARCH_MODEL` for Anthropic compatibility (default: `claude-opus-4-6`)

Raw Exa retrieval does not require a synthesis key when `synthesize=false`.

## Tools

### `search`

One-shot web search with normalized sources and a synthesized cited answer
(`answer_markdown`) by default.

Key defaults:

- `search_type="auto"`
- `num_results=10`
- synthesis enabled (`synthesize=true`)
- highlight-focused retrieval for token efficiency
- provider metadata is returned under `meta.synthesis_provider` and
  `meta.synthesis_model` when synthesis runs

### `deep_research`

Async multi-step research pipeline:

1. plan queries
2. run parallel Exa searches
3. review evidence + decide whether to continue
4. synthesize report
5. validate citations (`[source_id]`)

Key defaults:

- `max_iterations=1`
- `num_queries_per_iteration=4`
- `num_results_per_query=5`

## CLI

```bash
ai-v2 tools run websearch search "latest OpenAI and Anthropic model updates"
ai-v2 tools run websearch search "latest OpenAI and Anthropic model updates" --no-synthesize
ai-v2 tools run websearch deep-research "How should a fintech startup evaluate MPC vs HSM key management in 2026?"
```

Use `--pretty` for human-readable output in both commands.
