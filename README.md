# web_search_mcp

A local MCP server exposing a unified `search_web` tool that queries a configured
search backend, normalizes results into a shared schema, and returns an
evidence-friendly payload for a downstream LLM agent. Phase 1 ships the MCP
skeleton and the SearXNG adapter only; Brave, Exa, multi-provider fusion, and
the `fetch_url` / `search_health` tools land in later phases.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)
- A running SearXNG instance with JSON output enabled (default
  `http://localhost:8888`). Test with:
  ```
  curl 'http://localhost:8888/search?format=json&q=python' | head
  ```

## Setup

```
uv sync
cp .env.example .env   # optional — defaults match the confirmed setup
```

### Environment variables

| Variable                  | Default                   | Purpose                                         |
| ------------------------- | ------------------------- | ----------------------------------------------- |
| `SEARXNG_BASE_URL`        | `http://localhost:8888`   | SearXNG base URL; JSON output must be enabled. |
| `SEARCH_TIMEOUT_SECONDS`  | `10`                      | Per-request HTTP timeout for provider calls.    |
| `DEFAULT_MAX_RESULTS`     | `5`                       | Default max_results if the caller omits it.     |

## Run the Phase 0 probe

Run this before expecting the server to work. It verifies FastMCP is installed,
SearXNG is reachable and returns the expected JSON, and the in-process MCP
server lists the `search_web` tool.

```
uv run python probes/phase-1-probe.py
```

Expected summary:

```
SUMMARY: 3/3 assertions passed. Phase 0 gate OPEN.
```

## Run the server

```
uv run python server.py
```

The server speaks MCP over stdio. Logs are written to stderr — stdout is
reserved for the MCP protocol.

### Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "web_search_mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/web_search_mcp",
        "run",
        "python",
        "server.py"
      ],
      "env": {
        "SEARXNG_BASE_URL": "http://localhost:8888"
      }
    }
  }
}
```

Restart Claude Desktop after editing.

## `search_web` — example

**Request:**

```json
{
  "query": "bitcoin price",
  "max_results": 3,
  "mode": "balanced"
}
```

- `query` (string, required)
- `max_results` (int, optional, default 5, clamped to 1..10)
- `mode` (string, optional, default `"balanced"`, one of `"balanced" | "recall" | "precision"`).
  Phase 1 routes every mode to SearXNG only; the routing table arrives in Phase 2–3.

**Response shape:**

```json
{
  "query": "bitcoin price",
  "search_status": "ok",
  "providers_used": ["searxng"],
  "warnings": [],
  "results": [
    {
      "title": "Bitcoin Price Today: Live BTC Chart, Market Cap & News",
      "url": "https://coincheckup.com/coins/bitcoin",
      "snippet": "Bitcoin price today is $78,042, ...",
      "domain": "coincheckup.com",
      "providers": ["searxng"],
      "provider_overlap": 1,
      "published_date": null,
      "content_type": "unknown",
      "confidence": 1.0
    }
  ]
}
```

### `search_status` values (Phase 1)

- `"ok"` — SearXNG returned results and reported no upstream warnings.
- `"degraded"` — SearXNG returned results but reported unresponsive upstream
  engines (surfaced in `warnings` as a plain descriptive string).
- `"failed"` — SearXNG request raised, timed out, returned malformed JSON, or
  returned zero usable results. `results` is an empty list; `warnings` explains
  why. The server does not raise on provider failure.

`partial_failure` is reserved for multi-provider phases and does not appear in
Phase 1 output.

## Cache behavior

An in-memory dict keyed by `(query, mode, max_results)` stores the full
normalized response. TTL is session-only — the cache dies on server restart.
Repeat calls within the same session return from cache without hitting SearXNG;
this is visible in the server logs as `cache HIT`.

## Phase 1 limitations

Not implemented in Phase 1 (by design — these land in later phases):

- Brave provider adapter (Phase 2)
- Exa provider adapter (Phase 3)
- Multi-provider fusion, dedupe, URL canonicalization, reranking (Phase 2)
- Mode-based routing across providers (Phase 2–3)
- `fetch_url` tool (Phase 4)
- `search_health` tool (Phase 4)
- Persistent cache, on-disk TTL, authentication, HTTP/SSE transport

See `prompts/phase-1.md` for the full phase contract and `CLAUDE.md` for the
active rules of engagement.
