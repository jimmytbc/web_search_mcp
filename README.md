# web_search_mcp

A local MCP server exposing a unified `search_web` tool that queries configured
search backends, normalizes results into a shared schema, and returns an
evidence-friendly payload for a downstream LLM agent — plus `fetch_url`
(static page fetch + main-content extraction) and `search_health`
(provider connectivity / auth / mode-availability report).

## Status

- **Phase 1 - shipped.** MCP skeleton + SearXNG adapter operational.
- **Phase 2 - shipped.** Adds the Brave Search adapter, multi-provider
  parallel orchestration, URL canonicalization, canonical-URL dedupe with
  provenance merging, light reranking (overlap / trusted-domain / recency
  bonuses), and populated warnings (`degraded` / `partial_failure` plus
  low-diversity heuristics).
- **Phase 3 - shipped.** Adds the Exa semantic-search adapter and
  activates mode-based routing (`balanced` / `recall` / `precision`
  each select a different provider subset).
- **Phase 3.1 - shipped.** Retires SearXNG and swaps in Serper.dev as
  the breadth provider. See [Why Phase 3.1?](#why-phase-31-searxng--serperdev)
  below.
- **Phase 4 - shipped.** Adds the `fetch_url` tool (static-HTML fetch
  with trafilatura main-content extraction, robots.txt respect, and
  private-address blocking) and the `search_health` tool (per-provider
  connectivity + auth-validity probes and mode-availability summary).
  Completes the original 4-phase roadmap.

## Why Phase 3.1: SearXNG → Serper.dev

**What changed:**
- Removed the SearXNG provider, `SEARXNG_BASE_URL` env var, and the
  SearXNG `settings.yml` operator runbook.
- Added a Serper.dev provider (`POST https://google.serper.dev/search`,
  `X-API-KEY` auth) gated on the new `SERPER_API_KEY` env var.
- Serper slots into SearXNG's previous routing positions:
  `balanced` and `recall` now call Serper instead of SearXNG;
  `precision` is unchanged.
- Hardened the cross-provider dedupe merge with field-level precedence
  for `published_date`: a provider that returns `None` can never
  overwrite a non-null date from another provider, and when multiple
  providers return ISO-8601 timestamps the earliest wins (original
  publish date rather than a later re-crawl).

**Why the swap:**

SearXNG was a self-hosted meta-search aggregating ~50 upstream
engines, which gave it excellent keyword breadth on paper. In
practice it proved unreliable as the breadth provider for this MCP:

- **Operational fragility.** The local SearXNG container had to be
  running for `balanced` and `recall` modes to function. Restarts,
  config drift, and port conflicts on `localhost:8888` turned every
  fresh dev environment into a setup chore.
- **Constant `degraded` status from upstream engine flakiness.**
  Several of SearXNG's bundled engines (`duckduckgo`, `karmasearch`,
  the `brave.*` scrapers) routinely flagged as unresponsive,
  surfacing in the MCP's `warnings` array on essentially every
  query. The result was a `search_status: "degraded"` baseline that
  swamped real degradation signals.
- **Maintenance burden.** Avoiding the above required hand-editing
  SearXNG's `settings.yml` to disable problem engines, restarting
  the container, and re-doing the same edits whenever the SearXNG
  image was updated. None of that maintenance was actually about
  the MCP itself.

Serper.dev was selected as the replacement because it provides
keyword-search breadth via the Google SERP through a stable,
hosted JSON API — no local runtime to babysit, no upstream engine
flakiness to manage, and a contract simple enough to mirror the
existing Brave/Exa adapter pattern exactly.

**Accepted trade-offs:**

- Serper is a paid hosted API, not free + self-hosted. Cost and
  rate-limit management are now operator concerns (the MCP does
  not enforce in-process budgeting).
- Serper returns publication dates as relative strings ("3 days
  ago") rather than ISO-8601. The adapter sets `published_date`
  to `null` rather than parsing them, so the recency ranking bonus
  does not fire for Serper-only results. The dedupe precedence fix
  above ensures this never costs cross-provider results their real
  dates.

## Why multi-provider search?

A single search engine is a single retrieval bias, a single rate-limit
ceiling, and a single point of failure. An LLM agent reasoning over
search results cannot tell - from one provider alone - whether a URL is
genuinely authoritative or just happens to rank well in that engine's
particular algorithm. This MCP queries multiple independent providers
in parallel and fuses their results so the agent gets a richer, more
trustworthy signal.

### What each provider contributes

| Provider   | What it brings                                                                                                | Phase |
| ---------- | ------------------------------------------------------------------------------------------------------------- | ----- |
| **Brave**  | Independent web index (not a Google re-ranker). Official API with stable response contract. Fast, commercial-grade quality. | 2     |
| **Exa**    | Semantic / embedding-based retrieval - finds conceptually similar content that keyword search misses.         | 3     |
| **Serper** | Google SERP via a stable JSON API. Provides keyword-search breadth from the world's largest crawl. Returns publication dates as relative strings ("3 days ago") rather than ISO-8601. This adapter sets `published_date = None` for Serper results. Recency-based ranking signals do not apply to Serper-only results. | 3.1   |

Each provider has a different retrieval bias. Keyword engines (Brave,
Serper) surface pages that match query terms; Exa's semantic search
surfaces pages that match query *meaning*. Together they approximate
a much broader view of the web than any one alone.

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
  and skip weaker hits - no prompt engineering needed.
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
  simplest possible form of cross-source triangulation - good enough
  to consistently promote canonical sources over SEO-optimized noise.
- **Normalized schema across providers.** Brave's `age` / `page_age`
  and Exa's `publishedDate` both map into the same `published_date`
  field. Serper returns relative strings ("3 days ago") and is not
  parsed, so Serper results carry `published_date = null`. The agent
  writes one parser regardless of which provider surfaced a result.
- **Transparent, tunable scoring.** All weights live in one file
  (`tools/search_web.py`) with inline comments. No black-box reranker
  - if an operator decides recency matters more, it's a one-line edit.

### What this MCP is NOT

- **Not an AI search engine.** No LLM calls happen inside this server.
  It fuses raw provider output and returns structured JSON - the
  reasoning stays with the calling agent.
- **Not a JS renderer or a crawler.** `fetch_url` performs a static
  HTML fetch of one URL per call - no JavaScript rendering, no
  link-following, no site crawling. JavaScript-rendered SPAs come back
  `degraded` with a thin-text warning. Private, loopback, link-local,
  and carrier-NAT addresses are blocked by default, and robots.txt is
  respected by default. Search and fetch stay deliberately decoupled:
  they have very different failure modes and rate limits, and
  `fetch_url` never auto-fetches `search_web` results.
- **Not a replacement for RAG over curated corpora.** When you know
  exactly which documents matter, a vector DB over that corpus beats
  open-web search. This MCP is for the case where the agent doesn't
  know what it doesn't know and needs to discover relevant URLs on
  the open web first.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)
- At least one provider API key (`BRAVE_API_KEY`, `EXA_API_KEY`, or
  `SERPER_API_KEY`). If any is omitted, the server logs a one-line
  notice at startup and that provider is skipped. Modes whose subset
  has no enabled provider return `search_status: "failed"` without
  contacting any provider (see [Mode-routing matrix](#mode-routing-matrix)
  below).

## Setup

```
uv sync
cp .env.example .env   # add at least one provider API key
```

> **Important:** put `.env` at the **repo root** (next to `pyproject.toml`),
> not inside `.venv/`. `.venv/` is regenerated by `uv sync` and any files
> placed there will be lost. `load_dotenv()` only walks up from the
> working directory, so a `.env` nested inside `.venv/` is never found.

### Environment variables

| Variable                    | Required | Default                     | Purpose / valid values                                                                                     |
| --------------------------- | -------- | --------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `SEARCH_TIMEOUT_SECONDS`    | no       | `10`                        | Per-request HTTP timeout (seconds). Any positive number.                                                   |
| `DEFAULT_MAX_RESULTS`       | no       | `5`                         | Default `max_results` if the caller omits it. Clamped to `1..10`.                                          |
| `BRAVE_API_KEY`             | no       | *(unset)*                   | Brave Search subscription token. Unset = Brave disabled. Required for `balanced` and `precision` modes.    |
| `BRAVE_API_BASE`            | no       | `https://api.search.brave.com` | Brave API base URL. Override only for non-default endpoints.                                            |
| `BRAVE_DEFAULT_COUNTRY`     | no       | *(unset)*                   | Two-letter ISO country code. Examples: `US`, `GB`, `SG`, `DE`. Unset = no localization.                    |
| `BRAVE_DEFAULT_SEARCH_LANG` | no       | *(unset)*                   | Two-letter language code. Examples: `en`, `es`, `fr`, `ja`. Unset = no language hint.                      |
| `BRAVE_SAFESEARCH`          | no       | `moderate`                  | Brave SafeSearch level. Valid: `off` / `moderate` / `strict`.                                              |
| `EXA_API_KEY`               | no       | *(unset)*                   | Exa API key. Unset = Exa disabled. Required for `recall` and `precision` modes (see mode-routing matrix).  |
| `EXA_API_BASE`              | no       | `https://api.exa.ai`        | Exa API base URL. Override only for non-default endpoints.                                                 |
| `EXA_NUM_RESULTS_CEILING`   | no       | `10`                        | Cap on `numResults` sent to Exa per request. Tune down to reduce per-call cost. Any positive integer.      |
| `SERPER_API_KEY`            | no       | *(unset)*                   | Serper.dev API key. Unset = Serper disabled. Required for `balanced` and `recall` modes.                   |
| `SERPER_API_BASE`           | no       | `https://google.serper.dev` | Serper API base URL. Override only for non-default endpoints.                                              |
| `SERPER_NUM_RESULTS_CEILING`| no       | `10`                        | Cap on `num` sent to Serper per request. Tune down to reduce per-call cost. Any positive integer.          |
| `RECENCY_WINDOW_DAYS`       | no       | `30`                        | Days from today inside which a dated result earns a ranking recency bonus. Any positive integer.           |
| `FETCH_URL_TIMEOUT_SECONDS` | no       | `15`                        | Total wall-clock budget for one `fetch_url` call (robots fetch + redirects + body read combined). Any positive number. |
| `FETCH_URL_MAX_BODY_BYTES`  | no       | `2000000`                   | Cap on the decompressed response body. Larger bodies are truncated with a warning. Any positive integer.   |
| `FETCH_URL_USER_AGENT`      | no       | `web_search_mcp/0.4 (+fetch_url)` | User-Agent sent on `fetch_url` requests, including robots.txt fetches.                              |
| `FETCH_URL_RESPECT_ROBOTS`  | no       | `true`                      | Honor robots.txt (disallowed URLs return `failed` without fetching). Valid: `true` / `false`.              |
| `FETCH_URL_ALLOW_PRIVATE`   | no       | `false`                     | **Danger:** `true` disables all private/local address blocking, including across redirects. Trusted use only. |
| `SEARCH_HEALTH_DRY_RUN`     | no       | `false`                     | `true` = `search_health` skips live probes (free static view; `reachable`/`auth_ok` stay `null`).          |

### Registering provider API keys (Brave, Exa, Serper)

Either path works - pick one per key. The server loads `.env` via
`python-dotenv` at startup, so values set via the Claude Desktop config
`env` block override anything in `.env` for that launch.

**Option A - local `.env` file (recommended for development).**
Copy `.env.example` → `.env` and fill in `BRAVE_API_KEY`, `EXA_API_KEY`,
and/or `SERPER_API_KEY`. The `.env` file is in `.gitignore` and will not
be committed.

**Option B - Claude Desktop config `env` block.** Put the keys in the
`env` block of `claude_desktop_config.json` (see the sample below). Values
here travel with the Claude Desktop profile rather than the repo checkout.

## Run the server

### Local (stdio) — development default

```
uv run python server.py
```

The server speaks MCP over stdio. Logs go to stderr; stdout is reserved
for the MCP protocol.

### Remote (HTTP/SSE) — Docker deployment

`MCP_TRANSPORT=http` switches the server to an SSE endpoint on port 8000
(or `MCP_PORT=<n>`). This is the mode used when running on a remote machine.

```bash
# on the remote host
cp .env.example .env   # fill in API keys
docker compose up -d --build
```

The `docker-compose.yml` sets `MCP_TRANSPORT=http` and reads keys from `.env`.
API keys stay on the remote host; they are not needed on the client machine.

Two additional env vars for remote mode:

| Variable        | Default | Purpose |
|-----------------|---------|---------|
| `MCP_TRANSPORT` | `stdio` | Set to `http` for SSE/HTTP server mode. |
| `MCP_PORT`      | `8000`  | Port the HTTP server listens on. |

### Register with Claude Desktop

**Local (stdio)** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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
        "BRAVE_API_KEY": "your-brave-subscription-token",
        "EXA_API_KEY": "your-exa-api-key",
        "SERPER_API_KEY": "your-serper-api-key"
      }
    }
  }
}
```

**Remote (HTTP/SSE)** — point Claude Desktop at the running container via
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote). API keys are not
required on the client side.

```json
{
  "mcpServers": {
    "web_search_mcp": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://<host-ip>:8000/sse",
        "--allow-http"
      ]
    }
  }
}
```

`--allow-http` is required for plain-HTTP LAN addresses. Use HTTPS + remove
the flag if you terminate TLS in front of the container.

Restart Claude Desktop after editing.

## `search_web` - example

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
  `"balanced" | "recall" | "precision"`). Each mode routes to a
  different provider subset — see [Mode-routing matrix](#mode-routing-matrix).

### Mode-routing matrix

| Mode        | Providers called         | Intent                                                  |
| ----------- | ------------------------ | ------------------------------------------------------- |
| `balanced`  | `serper`, `brave`, `exa` | Default. All three for maximum cross-source confirmation. |
| `recall`    | `serper`, `exa`          | Keyword breadth + semantic discovery; drops the commercial keyword index. |
| `precision` | `brave`, `exa`           | Commercial-grade keyword + semantic; drops the high-breadth keyword index. |

Fusion behavior (canonicalization, dedupe, ranking, confidence) is
identical across all three modes — only the called provider set
differs. If a mode's subset names a provider that is not enabled
(missing API key), the pipeline continues with whatever intersection
is available and emits a descriptive warning naming the missing env
var. If the intersection is empty (e.g., `precision` mode with neither
`BRAVE_API_KEY` nor `EXA_API_KEY` set), the call returns
`search_status: "failed"` without contacting any provider.

**Response shape (multi-provider, healthy case):**

```json
{
  "query": "bitcoin price",
  "search_status": "ok",
  "providers_used": ["serper", "brave", "exa"],
  "warnings": [],
  "results": [
    {
      "title": "Bitcoin Price Today - CoinGecko",
      "url": "https://www.coingecko.com/en/coins/bitcoin",
      "snippet": "Bitcoin live price, market cap, and volume.",
      "domain": "coingecko.com",
      "providers": ["serper", "brave", "exa"],
      "provider_overlap": 3,
      "published_date": "2026-04-20T12:00:00Z",
      "content_type": "market_data",
      "confidence": 1.0
    },
    {
      "title": "Bitcoin - Wikipedia",
      "url": "https://en.wikipedia.org/wiki/Bitcoin",
      "snippet": "Bitcoin is a cryptocurrency …",
      "domain": "en.wikipedia.org",
      "providers": ["serper"],
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

- `"ok"` - all called providers succeeded; no warnings.
- `"degraded"` - all called providers succeeded, but at least one warning
  is present (e.g., low source/provider diversity).
- `"partial_failure"` - at least one provider failed (timeout / error /
  malformed JSON) and at least one other provider returned usable results.
- `"failed"` - every called provider failed, OR zero usable results after
  fusion.

`warnings` is a list of plain descriptive strings - never stack traces or
exception class names.

### Ranking

Each result earns a score used for sort order and feeds the
`confidence` field:

- **Base** - `1.0 / (raw_rank + 1)` using the best rank across providers
  that surfaced the URL.
- **Overlap bonus** - `+2` to rank / `+0.2` to confidence when at least
  two providers returned the same canonical URL.
- **Trusted bonus** - `+1` to rank / `+0.1` to confidence when the domain
  matches the trusted set in `fusion/rank.py` (starter list:
  `wikipedia.org`, `arxiv.org`, and `.gov` / `.edu` / `.gov.sg` /
  `.edu.sg` / `.gov.uk` / `.edu.au` suffixes). Edit in place.
- **Recency bonus** - `+1` / `+0.1` when `published_date` parses as ISO-8601
  and falls within `RECENCY_WINDOW_DAYS`.

Confidence caps at `1.0`. Weights are heuristic and live only in
`tools/search_web.py` - no downstream consumer depends on the specific
numbers.

### Low-diversity warnings

After sort+trim the handler checks two heuristics and emits descriptive
warnings if either triggers:

- **Single-domain dominance** - more than 70% of results share a domain.
- **Single-provider dominance** - when at least two providers were called
  but one was the sole source on more than 90% of the final results
  (cross-confirmed results don't count toward any provider's solo tally).

## Cache behavior

An in-memory dict keyed by `(query, mode, max_results)` stores the full
normalized response. TTL = session; the cache dies on server restart.
Repeat calls within the same session return from cache without hitting
any provider. Visible in the server logs as `cache HIT`.

`fetch_url` has its own session-lifetime cache (`utils/fetch_cache.py`),
keyed by canonical URL. Only `ok` and `degraded` outcomes are cached;
`failed` outcomes are retried on every call. Parsed robots.txt results
are also cached per host for the session.

## `fetch_url` - example

```
fetch_url(url="https://en.wikipedia.org/wiki/Python_(programming_language)")
```

Returns:

```json
{
  "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
  "status": "ok",
  "content_type": "text/html; charset=UTF-8",
  "title": "Python (programming language) - Wikipedia",
  "text": "Python is a high-level, general-purpose programming language...",
  "metadata": {"site_name": "Wikipedia"},
  "warnings": []
}
```

- `status` values: `ok` (fetched, meaningful text extracted),
  `degraded` (fetched, but the text is thin or the content type is not
  extractable HTML — typical for JavaScript-rendered SPAs, PDFs,
  images), `failed` (not fetched: non-2xx, timeout, blocked scheme or
  private address, robots.txt disallow, or redirect limit).
- Static HTML only — no JavaScript rendering (a Phase 5 candidate).
- One URL per call; the agent loops over multiple URLs.
- Redirects are followed manually (max 5 hops) with the private-address
  guard re-applied on every hop.
- `metadata` carries `published_date` / `author` / `site_name` only
  when the extractor found them — absent fields are omitted, never
  `null`.

## `search_health` - example

```
search_health()
```

Returns:

```json
{
  "status": "ok",
  "providers": [
    {"name": "brave",  "enabled": true, "reachable": true, "auth_ok": true, "last_status": "HTTP 200", "warnings": []},
    {"name": "exa",    "enabled": true, "reachable": true, "auth_ok": true, "last_status": "HTTP 200", "warnings": []},
    {"name": "serper", "enabled": true, "reachable": true, "auth_ok": true, "last_status": "HTTP 200", "warnings": []}
  ],
  "modes": {
    "balanced":  {"available": true},
    "recall":    {"available": true},
    "precision": {"available": true}
  }
}
```

- Live mode sends one minimal probe search (`"ping"`, one result) per
  **enabled** provider, in parallel. Each probe is a billable API call
  (Exa's probe additionally incurs its contents/highlights retrieval -
  that is the frozen adapter contract).
- `reachable` / `auth_ok` are nullable: `null` means "could not be
  determined" (timeouts, network errors, dry-run).
- Auth failures (HTTP 401/403/422 - the Brave-422 / Serper-403
  incident signatures) report `reachable: true, auth_ok: false`. This
  is the pre-flight check that catches stale-key/env-precedence traps
  before they surface as broken searches.
- `modes` availability is derived from which providers are enabled
  (a mode is available when at least one of its providers is enabled);
  probe outcomes never change mode availability.
- `SEARCH_HEALTH_DRY_RUN=true` skips all live calls and reports
  enablement + mode availability only - free and near-instant.

## Diagnostics

For ad-hoc queries against the full fusion pipeline without going through
Claude Desktop, a convenience script is provided:

```
uv run python scripts/query.py "your query here"
uv run python scripts/query.py "your query" 10 precision   # max_results + mode
```

Output is the same JSON the MCP would return. Uses `.env` for
configuration, so make sure `BRAVE_API_KEY`, `EXA_API_KEY`, and
`SERPER_API_KEY` are set at the repo root (not inside `.venv/`) if you
want full multi-provider output. Modes that require a missing key will
degrade with a warning.

If a downstream caller reports broken results, this script is the
fastest way to isolate the MCP's fused output from anything happening
client-side.

The Phase 4 tools have a sibling diagnostics script:

```
uv run python scripts/diag.py fetch https://example.org/article
uv run python scripts/diag.py health
```

`health` in live mode costs one billable search call per enabled
provider; set `SEARCH_HEALTH_DRY_RUN=true` for a free static view.

## Current limitations

- `fetch_url` is static HTML only — no JavaScript rendering. SPA pages
  return `degraded` with a thin-text warning (JS rendering is a
  Phase 5 candidate).
- `fetch_url` takes a single URL per call — no batch `urls[]` input.
- robots.txt is respected by default (`FETCH_URL_RESPECT_ROBOTS=false`
  to opt out); private/local addresses are blocked by default
  (`FETCH_URL_ALLOW_PRIVATE=true` to opt out — trusted use only).
- `search_health` live probes cost one API call per enabled provider
  on every invocation — no probe cooldown in v1.
- Persistent cache, on-disk TTL, authentication, HTTP/SSE transport.
- Secondary dedupe heuristics (same-domain + near-title). Canonical URL
  is the only dedupe signal.
- In-MCP cost or rate-limit logic for paid providers (Brave, Exa,
  Serper). Delegated to the operator.

Phase build contracts and per-session rules of engagement are kept as
local dev artifacts and are not published with the repo.
