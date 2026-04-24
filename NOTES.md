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
