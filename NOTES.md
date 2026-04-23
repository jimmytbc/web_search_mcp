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
