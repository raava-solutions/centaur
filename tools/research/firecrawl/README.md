# Firecrawl Plugin

Firecrawl search and single-page scrape for research workflows.

## Secrets

Set `FIRECRAWL_API_KEY` in the environment or the Centaur 1Password-backed
secret source. The tool sends a placeholder bearer token; iron-proxy resolves
the real key for `api.firecrawl.dev`.

## Tools

### `search`

Search the web through Firecrawl and return normalized result rows plus the raw
Firecrawl response.

### `scrape`

Scrape one URL and return markdown by default. Crawl, map, browser interaction,
and long-running jobs are intentionally out of scope for this first tool.

