"""Cross-provider dedupe keyed by canonicalized URL.

Per handoff §10.3, the canonical URL is the only dedupe signal in
Phase 2. Secondary heuristics (same-domain + near-title matching)
are deferred.

Behavior:
  - Canonicalize each result's URL.
  - Group results sharing the same canonical URL.
  - Collapse each group into one NormalizedResult: the first result's
    URL stands in for the group (preserving input order for the
    group's position in the output), providers merges into a deduped
    union, provider_overlap is the count of distinct providers, and
    best-available title/snippet/published_date are chosen by picking
    the first non-empty/non-null value in input order.
  - Do not sort. Ranking owns ordering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from models.search_result import NormalizedResult

from fusion.canonicalize import canonicalize_url


def _first_non_empty(values: list[str]) -> str:
    for v in values:
        if v:
            return v
    return ""


def _parse_iso(value: str) -> Optional[datetime]:
    s = (value or "").strip()
    if not s:
        return None
    try:
        # fromisoformat accepts "Z" only from 3.11+; normalize defensively.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _merge_published_date(values: list) -> Optional[str]:
    """Field-level precedence for `published_date` across a dedupe group.

    Rules (Phase 3.1):
      - None is never preferred over a non-None value. This protects
        Serper's always-None against silently overwriting Brave/Exa's
        real dates when the same canonical URL surfaces from both.
      - When two or more providers return non-None ISO-8601 timestamps
        that differ, prefer the EARLIEST — i.e., the original publish
        date rather than a later re-crawl timestamp.
      - When no value is ISO-parseable but at least one is non-None,
        fall back to the first non-None in input order (preserves the
        pre-fix behavior for non-ISO strings such as Brave's `age`).
    """
    non_null = [v for v in values if v is not None]
    if not non_null:
        return None
    parsed: list[tuple[datetime, str]] = []
    for v in non_null:
        if not isinstance(v, str):
            continue
        dt = _parse_iso(v)
        if dt is not None:
            parsed.append((dt, v))
    if parsed:
        parsed.sort(key=lambda t: t[0])
        return parsed[0][1]
    return non_null[0]


def dedupe_by_canonical_url(
    results: list[NormalizedResult],
) -> list[NormalizedResult]:
    groups: dict[str, list[NormalizedResult]] = {}
    order: list[str] = []

    for r in results:
        key = canonicalize_url(r.url)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    merged: list[NormalizedResult] = []
    for key in order:
        group = groups[key]
        head = group[0]
        if len(group) == 1:
            merged.append(head)
            continue

        providers_union: list[str] = []
        seen: set[str] = set()
        for member in group:
            for p in member.providers:
                if p not in seen:
                    seen.add(p)
                    providers_union.append(p)

        raw_ranks: dict = {}
        for member in group:
            # Later-seen entries do not overwrite earlier ones. This
            # preserves the "first provider's rank" for downstream
            # base-score fallback.
            for prov, rnk in member.raw_ranks.items():
                raw_ranks.setdefault(prov, rnk)

        merged.append(
            NormalizedResult(
                title=_first_non_empty([m.title for m in group]),
                url=head.url,
                snippet=_first_non_empty([m.snippet for m in group]),
                domain=head.domain,
                providers=providers_union,
                provider_overlap=len(seen),
                published_date=_merge_published_date([m.published_date for m in group]),
                content_type=head.content_type,
                confidence=head.confidence,
                rank_score=None,
                raw_ranks=raw_ranks,
            )
        )

    return merged
