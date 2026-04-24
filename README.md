# web_search_mcp

A local MCP server exposing a unified `search_web` tool that queries configured
search backends, normalizes results into a shared schema, and returns an
evidence-friendly payload for a downstream LLM agent.

## Status

- **Phase 1 — shipped.** MCP skeleton + SearXNG adapter operational.
- **Phase 2 — current.** Adds the Brave Search adapter, multi-provider
  parallel orchestration, URL canonicalization, canonical-URL dedupe with
  provenance merging, light reranking (overlap / trusted-domain / recency
  bonuses), and populated warnings (`degraded` / `partial_failure` plus
  low-diversity heuristics).
- Phase 3 adds the Exa adapter and mode-based routing. Phase 4 adds
  `fetch_url` and `search_health`.

## Why multi-provider search?

A single search engine is a single retrieval bias, a single rate-limit
ceiling, and a single point of failure. An LLM agent reasoning over
search results cannot tell — from one provider alone — whether a URL is
genuinely authoritative or just happens to rank well in that engine's
particular algorithm. This MCP queries multiple independent providers
in parallel and fuses their results so the agent gets a richer, more
trustworthy signal.

### What each provider contributes

| Provider   | What it brings                                                                                                | Phase |
| ---------- | ------------------------------------------------------------------------------------------------------------- | ----- |
| **SearXNG**| Self-hosted meta-search; aggregates Google, Bing, DuckDuckGo, Qwant, Startpage, Wikipedia, and many more through a single endpoint. Free, private, and provides keyword-search breadth. | 1     |
| **Brave**  | Independent web index (not a Google re-ranker). Official API with stable response contract. Fast, commercial-grade quality. | 2     |
| **Exa**    | Semantic / embedding-based retrieval — finds conceptually similar content that keyword search misses.         | 3     |

Each provider has a different retrieval bias. Keyword engines surface
pages that match query terms; Exa's semantic search surfaces pages
that match query *meaning*; meta-search catches the long tail of
niche sites. Together they approximate a much broader view of the web
than any one alone.

### How this helps a downstream LLM agent

The fusion layer turns per-provider raw results into a payload an agent
can reason over directly:

- **Provenance on every result.** Each result carries a `providers`
  array listing which engines surfaced it, plus a `provider_overlap`
  count. The agent can distinguish "cross-confirmed by two independent
  providers" from "surfaced by one provider only."
- **Confidence as a quantitative priority signal.** The `confidence`
  field combines rank, overlap, domain trust, and recency into a
  single `0.0–1.0` score. An agent deciding which URLs to deeply
  investigate (via a scraper, for example) can thresh at, say, `0.5`
  and skip weaker hits — no prompt engineering needed.
- **Graceful degradation the agent can reason about.** `search_status`
  reports `ok` / `degraded` / `partial_failure` / `failed` with a
  plain-English `warnings` array when things go sideways. An agent
  observing `partial_failure` knows the result set is incomplete and
  can choose to retry, broaden the query, or surface the gap to its
  user instead of silently presenting a thin answer as complete.
- **URL canonicalization across providers.** `utm_*` / `fbclid` /
  `gclid` / `ref` tracking params are stripped before dedupe, so two
  providers returning the same page with different tracking tags are
  correctly recognized as one result (not double-counted as "two
  independent confirmations").
- **Bias reduction through overlap scoring.** A result returned by two
  independent providers ranks above results returned by only one, even
  if any single provider ranked the solo result higher. This is the
  simplest possible form of cross-source triangulation — good enough
  to consistently promote canonical sources over SEO-optimized noise.
- **Normalized schema across providers.** Brave's `age` / `page_age`,
  SearXNG's `publishedDate`, and (future) Exa's metadata all map into
  the same `published_date` field. The agent writes one parser, not
  three.
- **Transparent, tunable scoring.** All weights live in one file
  (`tools/search_web.py`) with inline comments. No black-box reranker
  — if an operator decides recency matters more, it's a one-line edit.

### What this MCP is NOT

- **Not an AI search engine.** No LLM calls happen inside this server.
  It fuses raw provider output and returns structured JSON — the
  reasoning stays with the calling agent.
- **Not a content fetcher.** `search_web` returns URLs + snippets.
  Extracting full page content is left to the caller (e.g., a
  downstream `fetch_url` MCP, Firecrawl, Playwright). This separation
  is deliberate: search and fetch have very different failure modes
  and rate limits, and coupling them would hurt both.
- **Not a replacement for RAG over curated corpora.** When you know
  exactly which documents matter, a vector DB over that corpus beats
  open-web search. This MCP is for the case where the agent doesn't
  know what it doesn't know and needs to discover relevant URLs on
  the open web first.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)
- A running SearXNG instance with JSON output enabled (default
  `http://localhost:8888`). Test with:
  ```
  curl 'http://localhost:8888/search?format=json&q=python' | head
  ```
- Optional: a Brave Search API key. If omitted, the server runs
  SearXNG-only and logs a one-line notice at startup.

## Setup

```
uv sync
cp .env.example .env   # optional — add your BRAVE_API_KEY here if you have one
```

> **Important:** put `.env` at the **repo root** (next to `pyproject.toml`),
> not inside `.venv/`. `.venv/` is regenerated by `uv sync` and any files
> placed there will be lost. `load_dotenv()` only walks up from the
> working directory, so a `.env` nested inside `.venv/` is never found.

### Recommended SearXNG configuration

SearXNG is a meta-search that aggregates ~50 upstream engines. A few of
those engines interact badly with this MCP and should be disabled to
avoid permanent `degraded` status on every query:

| Engine in SearXNG   | Why to disable                                                                                            |
| ------------------- | --------------------------------------------------------------------------------------------------------- |
| `brave`             | Scrapes `brave.com` HTML. Brave aggressively blocks scrapers and this MCP already calls Brave's API directly — fully redundant. |
| `brave.images` / `.videos` / `.news` | Same issue; same redundancy.                                                               |
| `karmasearch` (all variants) | Upstream meta-search with frequent availability issues; tends to time out and flood the warnings array. |

**How to disable** — edit SearXNG's `settings.yml`. For each engine
listed above, add `disabled: true` as the last line of its block:

```yaml
engines:
  # ...
  - name: brave
    engine: brave
    shortcut: br
    categories: [general, web]
    brave_category: search
    disabled: true        # <-- add this line

  - name: karmasearch
    engine: karmasearch
    categories: [general, web]
    search_type: web
    shortcut: ka
    disabled: true        # <-- add this line

  - name: karmasearch videos
    engine: karmasearch
    categories: [general, web]
    search_type: videos
    shortcut: kav
    disabled: true        # <-- add this line

  # Optional but recommended — disable the remaining brave.*/karmasearch.*
  # sub-engines for the same reasons.
```

**Finding `settings.yml`:**

- **Local install:** typically `/etc/searxng/settings.yml`.
- **Docker, bind-mounted config:** run
  `docker inspect <container-id> --format '{{ range .Mounts }}{{ .Type }}  {{ .Source }} → {{ .Destination }}{{println}}{{ end }}'`
  and look for the line pointing at `/etc/searxng`. Edit the file at
  the printed `Source` path.
- **Docker, no bind mount:** copy the default out, edit, mount it back:
  ```bash
  docker cp <container-id>:/etc/searxng/settings.yml ./settings.yml
  # edit ./settings.yml, then update your docker-compose.yml / run command
  # to mount it back to /etc/searxng/settings.yml
  ```

Restart the SearXNG container after editing:

```bash
docker restart <container-id>
```

Then verify with:

```bash
uv run python scripts/query.py "openai"
```

A clean run returns `"search_status": "ok"` and `"warnings": []`.
If specific engines still appear in warnings, add them to your
`settings.yml` disable list the same way.

### Environment variables

| Variable                    | Required | Default                     | Purpose / valid values                                                                                     |
| --------------------------- | -------- | --------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `SEARXNG_BASE_URL`          | yes      | `http://localhost:8888`     | SearXNG base URL; JSON output must be enabled.                                                             |
| `SEARCH_TIMEOUT_SECONDS`    | no       | `10`                        | Per-request HTTP timeout (seconds). Any positive number.                                                   |
| `DEFAULT_MAX_RESULTS`       | no       | `5`                         | Default `max_results` if the caller omits it. Clamped to `1..10`.                                          |
| `BRAVE_API_KEY`             | no       | *(unset)*                   | Brave Search subscription token. Unset = Brave disabled; SearXNG-only mode.                                |
| `BRAVE_API_BASE`            | no       | `https://api.search.brave.com` | Brave API base URL. Override only for non-default endpoints.                                            |
| `BRAVE_DEFAULT_COUNTRY`     | no       | *(unset)*                   | Two-letter ISO country code. Examples: `US`, `GB`, `SG`, `DE`. Unset = no localization.                    |
| `BRAVE_DEFAULT_SEARCH_LANG` | no       | *(unset)*                   | Two-letter language code. Examples: `en`, `es`, `fr`, `ja`. Unset = no language hint.                      |
| `BRAVE_SAFESEARCH`          | no       | `moderate`                  | Brave SafeSearch level. Valid: `off` / `moderate` / `strict`.                                              |
| `RECENCY_WINDOW_DAYS`       | no       | `30`                        | Days from today inside which a dated result earns a ranking recency bonus. Any positive integer.           |

### Registering a Brave API key

Either path works — pick one. The server loads `.env` via `python-dotenv` at
startup, so values set via the Claude Desktop config `env` block override
anything in `.env` for that launch.

**Option A — local `.env` file (recommended for development).**
Copy `.env.example` → `.env` and fill in `BRAVE_API_KEY`. The `.env` file is
in `.gitignore` and will not be committed.

**Option B — Claude Desktop config `env` block.** Put the key in the
`env` block of `claude_desktop_config.json` (see the sample below). Values
here travel with the Claude Desktop profile rather than the repo checkout.

## Run the Phase 2 probe

The probe is a standing diagnostic. Run it before expecting the server to
work, and rerun it after any provider / contract change to isolate whether
an issue is on the provider side or our side.

```
uv run python probes/phase-2-probe.py
```

Assertions, in order:

1. `BRAVE_API_KEY` env presence. Warn-only if missing — the probe continues
   and skips assertion 2.
2. GET to Brave `/res/v1/web/search` returns JSON with the expected
   `web.results[]` shape. Skipped if assertion 1 warned.
3. `asyncio.gather` with a per-coroutine timeout returns the fast coroutine
   and drops the slow one — proves the parallel-orchestration pattern.
4. `fusion.canonicalize.canonicalize_url` collapses `utm_*` / `fbclid` /
   `gclid` / `ref` variants, lowercases scheme+host, and strips trailing
   slashes (except root).

Expected summary:

```
SUMMARY: 4/4 assertions passed. Phase 0 gate OPEN.
```

The Phase 1 probe (`probes/phase-1-probe.py`) remains in the tree as a
standing SearXNG/FastMCP diagnostic.

## Run the server

```
uv run python server.py
```

The server speaks MCP over stdio. Logs go to stderr — stdout is reserved
for the MCP protocol.

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
        "SEARXNG_BASE_URL": "http://localhost:8888",
        "BRAVE_API_KEY": "your-brave-subscription-token"
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
  "max_results": 5,
  "mode": "balanced"
}
```

- `query` (string, required)
- `max_results` (int, optional, default 5, clamped to 1..10)
- `mode` (string, optional, default `"balanced"`, one of
  `"balanced" | "recall" | "precision"`). Phase 2 routes every mode to
  every enabled provider; mode-based routing arrives in Phase 3.

**Response shape (multi-provider, healthy case):**

```json
{
  "query": "bitcoin price",
  "search_status": "ok",
  "providers_used": ["searxng", "brave"],
  "warnings": [],
  "results": [
    {
      "title": "Bitcoin Price Today — CoinGecko",
      "url": "https://www.coingecko.com/en/coins/bitcoin",
      "snippet": "Bitcoin live price, market cap, and volume.",
      "domain": "coingecko.com",
      "providers": ["searxng", "brave"],
      "provider_overlap": 2,
      "published_date": "2026-04-20T12:00:00Z",
      "content_type": "market_data",
      "confidence": 1.0
    },
    {
      "title": "Bitcoin - Wikipedia",
      "url": "https://en.wikipedia.org/wiki/Bitcoin",
      "snippet": "Bitcoin is a cryptocurrency …",
      "domain": "en.wikipedia.org",
      "providers": ["searxng"],
      "provider_overlap": 1,
      "published_date": null,
      "content_type": "reference",
      "confidence": 0.5
    }
  ]
}
```

Multi-provider results with `provider_overlap: 2` are boosted in ranking
and in `confidence`. Single-provider results (`provider_overlap: 1`) still
appear in the top N when the fusion layer can't find a cross-provider
confirmation.

### `search_status` values

- `"ok"` — all called providers succeeded; no warnings.
- `"degraded"` — all called providers succeeded, but at least one warning
  is present (e.g., SearXNG unresponsive engines, low source/provider
  diversity).
- `"partial_failure"` — at least one provider failed (timeout / error /
  malformed JSON) and at least one other provider returned usable results.
- `"failed"` — every called provider failed, OR zero usable results after
  fusion.

`warnings` is a list of plain descriptive strings — never stack traces or
exception class names.

### Ranking

Each result earns a score used for sort order and feeds the
`confidence` field:

- **Base** — `1.0 / (raw_rank + 1)` using the best rank across providers
  that surfaced the URL.
- **Overlap bonus** — `+2` to rank / `+0.2` to confidence when at least
  two providers returned the same canonical URL.
- **Trusted bonus** — `+1` to rank / `+0.1` to confidence when the domain
  matches the trusted set in `fusion/rank.py` (starter list:
  `wikipedia.org`, `arxiv.org`, and `.gov` / `.edu` / `.gov.sg` /
  `.edu.sg` / `.gov.uk` / `.edu.au` suffixes). Edit in place.
- **Recency bonus** — `+1` / `+0.1` when `published_date` parses as ISO-8601
  and falls within `RECENCY_WINDOW_DAYS`.

Confidence caps at `1.0`. Weights are heuristic and live only in
`tools/search_web.py` — no downstream consumer depends on the specific
numbers.

### Low-diversity warnings

After sort+trim the handler checks two heuristics and emits descriptive
warnings if either triggers:

- **Single-domain dominance** — more than 70% of results share a domain.
- **Single-provider dominance** — when both providers were called but one
  was the sole source on more than 90% of the final results
  (cross-confirmed results don't count toward either provider's solo tally).

## Cache behavior

An in-memory dict keyed by `(query, mode, max_results)` stores the full
normalized response. TTL = session; the cache dies on server restart.
Repeat calls within the same session return from cache without hitting
any provider. Visible in the server logs as `cache HIT`.

## Diagnostics

Each phase ships a probe under `probes/`. They are retained permanently
and exist to isolate provider-contract issues from code issues:

```
uv run python probes/phase-1-probe.py   # FastMCP + SearXNG surface
uv run python probes/phase-2-probe.py   # Brave + parallel-gather + canonicalize
```

For ad-hoc queries against the full fusion pipeline without going through
Claude Desktop, a convenience script is provided:

```
uv run python scripts/query.py "your query here"
uv run python scripts/query.py "your query" 10 precision   # max_results + mode
```

Output is the same JSON the MCP would return. Uses `.env` for
configuration, so make sure `BRAVE_API_KEY` is set at the repo root
(not inside `.venv/`) if you want multi-provider output.

If a downstream caller reports broken results, run the probes first. If
the Phase 2 probe warns that `BRAVE_API_KEY` is unset but otherwise
passes, and the server still misbehaves, the issue is in the handler or
fusion layer — not the provider.

## Phase 2 limitations

Not implemented in Phase 2 (by design — these land in later phases):

- Exa provider adapter (Phase 3)
- Mode-based routing to different provider subsets (Phase 3). All three
  modes currently route to every enabled provider.
- `fetch_url` tool (Phase 4)
- `search_health` tool (Phase 4)
- Persistent cache, on-disk TTL, authentication, HTTP/SSE transport
- Secondary dedupe heuristics (same-domain + near-title). Canonical URL
  is the only dedupe signal.

Phase build contracts and per-session rules of engagement are kept as
local dev artifacts and are not published with the repo.
