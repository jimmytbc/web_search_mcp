# NOTES

Observations and "while I'm here" candidates surfaced during Phase 1
implementation, to be decided by the product owner before acting on
them.

- `fusion/normalize.py` uses a short static `_DOMAIN_CATEGORY` dict
  for `content_type` classification. Handoff §15 calls for this to
  be pluggable/configurable later. Candidate for a real classifier
  or external config file in Phase 2+.
- SearXNG sometimes returns per-result `publishedDate` for news-
  tagged engines. Phase 1 keeps `published_date` null per the phase
  prompt; lifting this is a small follow-up if Phase 2 wants it.

## Phase 2 — items surfaced during implementation

- `fusion/rank.py::TRUSTED_EXACT_DOMAINS` uses strict exact-string
  match per the spec. This means `en.wikipedia.org` does NOT match
  `wikipedia.org`. In practice, Wikipedia still ranks well via the
  overlap + base-score mechanics, but if the product owner wants
  subdomains of listed exact domains treated as trusted, a small
  edit to `is_trusted_domain` (or adding `en.wikipedia.org`
  explicitly to the set) will do it.
- Phase 1 had `fusion/normalize.py` hard-coding `published_date=None`;
  Phase 2 flips it to read `raw.published_date` so Brave's `page_age`
  / `age` flows through. SearXNG path still lands with None because
  the SearXNG adapter wasn't changed; revisit if we want dated
  recency signals from SearXNG-tagged news engines too.
- Low-diversity heuristics (>70% single domain, >90% single provider)
  are spec-locked thresholds; pulled out as module constants in
  `tools/search_web.py` (`_DOMAIN_DIVERSITY_THRESHOLD`,
  `_PROVIDER_DOMINANCE_THRESHOLD`) so they're easy to tune if
  operators find them too noisy or too quiet.
- Small-N edge case: on a 1-result response the thresholds trigger
  trivially (1/1 = 100%). The warning string is literally correct
  ("low source diversity: example.com accounts for 1 of 1 results")
  but carries little information at that sample size. Kept literal
  per the spec; consider gating with `min_results_for_diversity=3`
  if the product owner finds the small-N output noisy.
- Probe (iv) is a correctness gate on `fusion/canonicalize.py` rather
  than a pure external-precondition check. Keeping it means we
  implemented canonicalize.py first, then reran the probe green
  before proceeding. Noting here in case future phases want to keep
  probes strictly about external contracts.

## Phase 3 — items surfaced during implementation

- **Live Brave HTTP 422s during Claude Desktop testing (2026-05-10) —
  root cause: stale `BRAVE_API_KEY` in `claude_desktop_config.json`
  overriding the valid key in `.env`.** Diagnostic timeline:
  initial hypothesis was transient Brave-side outage (CLI
  reproductions returned HTTP 200), then briefly query-shape
  (year-token "2025" pattern — also wrong). The body-capture commit
  (`c264085`) revealed Brave's actual response on the next batch:
  `"detail": "The provided subscription token is invalid.",
  "meta": {"component": "authentication"}`. A direct CLI curl with
  the key from `.env` then returned HTTP 200, isolating the gap to
  the Claude-Desktop-spawned process specifically. The
  `claude_desktop_config.json` `env` block contained an older key
  that took precedence over `.env` (Python's `load_dotenv()` defers
  to existing env vars by default). Operator removed the stale key
  from the Desktop config; MCP now falls back to `.env` and Brave
  returned `status=ok` on the post-restart `precision`-mode test.
  Lessons: (1) the body-capture change is the load-bearing
  improvement here — without it the misdiagnosis would have
  persisted; (2) the README already documents that Desktop env
  overrides `.env`, but the *failure mode* (stale Desktop key
  silently winning over fresh `.env` key) is worth flagging if
  this trap recurs for other operators. Same body-capture change
  applied to `providers/exa.py` for symmetry.
- Exa's joined-highlights snippet can be very long when Exa returns
  multiple long highlights for content-heavy pages (CoinGecko,
  CoinDesk on a "bitcoin price" query returned snippets in the 2-3KB
  range). Spec is satisfied (` " ... ".join(highlights) `) but
  downstream agents working under context pressure may want a
  shorter snippet. Candidate follow-ups: cap snippet length at e.g.
  500 chars, or trim per-highlight. No code change made — flagging
  for product-owner triage.

## Phase 5 parking list (recorded at Phase 4 ship, 2026-06-12)

Out-of-scope items deliberately deferred from Phase 4 (per phase-4.md
TASK 11 and decisions ratified by the product owner 2026-06-12):

- **JS rendering for fetch_url** (env or per-call opt-in; Playwright-
  class dependency footprint was rejected for v1 — D1).
- **Batch `urls[]` fetch** — re-introduces `partial_failure` to the
  status vocabulary (D4).
- **Per-provider success/failure counters** in search_web plus the
  Serper-only-hit-count diagnostic from the Phase 3.1 §7 observation
  (D11 — search_web stays untouched).
- **Content-type classifier coupling** — fetch_url returns the raw
  HTTP Content-Type header; sharing search_web's domain classifier
  was rejected for v1 (D13).
- **search_health probe cooldown / result caching** — v1 probes on
  every invocation by design (D9).
- **User-Agent string derived from package metadata** — currently a
  config default ("web_search_mcp/0.4"), bumped manually alongside
  the project version.
- **DNS-rebinding TOCTOU residual risk in fetch_url** (ratified D-a):
  the URL-policy guard resolves and validates every A/AAAA record,
  but httpx re-resolves at connect time, so a rebinding DNS server
  could still steer the connect to a private address. Full fix = IP
  pinning (connect to the validated IP with Host/SNI override).
  Accepted for v1; revisit if the MCP ever runs on a network with
  sensitive internal services.
- **Port policy for fetch_url** (ratified D-c): arbitrary ports on
  public hosts are accepted in v1; a port allowlist is a Phase 5
  candidate.
- **Per-provider auth-failure code maps in search_health** (ratified):
  v1 applies the blanket 401/403/422 → auth-failure mapping across
  all providers (422 is Brave-specific lore). Refine per provider if
  a misclassification is ever observed.
