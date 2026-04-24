"""URL canonicalization for cross-provider dedupe.

Scope is intentionally minimal per handoff §10.2:
  - lowercase scheme and host
  - strip a fixed set of tracking parameters
  - remove trailing slash from path except when the path is exactly "/"
  - preserve everything else (non-tracking query params, fragments,
    port, user-info, scheme, hostname case for stuff we don't touch)

We do NOT normalize http vs https and we do NOT fold `www.` or `m.`
hostnames, per the handoff. Secondary heuristics (same-domain +
near-title matching) are deferred.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Exact query-param names to strip. `utm_*` is handled by prefix match
# below; these are the non-prefixed trackers we want to remove too.
_EXACT_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "ref",
        "mc_cid",
        "mc_eid",
    }
)

_TRACKING_PREFIXES = ("utm_",)


def _is_tracking(name: str) -> bool:
    if name in _EXACT_TRACKING_PARAMS:
        return True
    for prefix in _TRACKING_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)

    scheme = parts.scheme.lower()
    # `netloc` may include user-info and port; only the hostname portion
    # is case-insensitive per RFC 3986, but lowercasing the whole netloc
    # is the pragmatic behavior operators expect for dedupe and matches
    # handoff §10.2 wording.
    netloc = parts.netloc.lower()

    path = parts.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/") or "/"

    # Preserve repeated keys and ordering of non-tracking params.
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    query = urlencode(query_pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, parts.fragment))
