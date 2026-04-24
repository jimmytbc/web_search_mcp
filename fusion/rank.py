"""Light reranking over deduped NormalizedResults.

Formula per Phase 2 spec:
    base
      + 2 if provider_overlap >= 2
      + 1 if domain is trusted (exact match or trusted suffix)
      + 1 if published_date is non-null AND within
            RECENCY_WINDOW_DAYS days of today

`base` is the provider-supplied score when present, otherwise
`1.0 / (raw_rank + 1)` using the best (lowest) raw_rank across the
providers that surfaced this result.

Sort is stable and descending; ties preserve input order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from models.search_result import NormalizedResult
from utils.logging import get_logger

log = get_logger(__name__)

# Minimal starter list. Operators: edit in place to add/remove. Pluggable
# per handoff §15; no downstream module reads these constants elsewhere.
TRUSTED_EXACT_DOMAINS: set[str] = {"wikipedia.org", "arxiv.org"}
TRUSTED_SUFFIX_PATTERNS: set[str] = {
    ".gov",
    ".edu",
    ".gov.sg",
    ".edu.sg",
    ".gov.uk",
    ".edu.au",
}

_OVERLAP_BONUS = 2.0
_TRUSTED_BONUS = 1.0
_RECENT_BONUS = 1.0


def is_trusted_domain(domain: str) -> bool:
    if not domain:
        return False
    d = domain.lower()
    if d in TRUSTED_EXACT_DOMAINS:
        return True
    return any(d.endswith(suffix) for suffix in TRUSTED_SUFFIX_PATTERNS)


def _parse_published(value: str) -> Optional[datetime]:
    """Best-effort parse of the published_date field.

    Providers vary: Brave may send an ISO-8601 timestamp (`page_age`)
    or a short relative phrase (`age`). Returns None on anything we
    can't confidently interpret; the ranker then treats the result as
    non-recent. No hard failure, ever.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Try ISO-8601 first (handles "2026-04-10T12:00:00Z" and offsets).
    try:
        # fromisoformat accepts "Z" only from 3.11+; normalize defensively.
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_recent(
    published_date: Optional[str],
    recency_window_days: int,
    now: Optional[datetime] = None,
) -> bool:
    if not published_date:
        return False
    dt = _parse_published(published_date)
    if dt is None:
        return False
    reference = now or datetime.now(tz=timezone.utc)
    delta = reference - dt
    if delta.total_seconds() < 0:
        # Future-dated; treat as recent rather than penalize.
        return True
    return delta.days <= recency_window_days


def _base_score(result: NormalizedResult) -> float:
    # No provider supplies an explicit score in Phase 2, so fall back
    # to inverse-rank over the best raw_rank we saw across providers.
    if result.raw_ranks:
        best_rank = min(result.raw_ranks.values())
        return 1.0 / (best_rank + 1)
    return 0.0


def rank_results(
    results: list[NormalizedResult],
    recency_window_days: int,
) -> list[NormalizedResult]:
    now = datetime.now(tz=timezone.utc)
    scored = list(results)  # shallow copy; we mutate rank_score in place

    for r in scored:
        score = _base_score(r)
        if r.provider_overlap >= 2:
            score += _OVERLAP_BONUS
        if is_trusted_domain(r.domain):
            score += _TRUSTED_BONUS
        if is_recent(r.published_date, recency_window_days, now=now):
            score += _RECENT_BONUS
        r.rank_score = score

    # Python's list.sort is stable, so equal-score entries keep their
    # input order — which after dedupe is the first-surfaced order.
    scored.sort(key=lambda r: r.rank_score or 0.0, reverse=True)

    if log.isEnabledFor(20):  # INFO
        _log_rank_trace(scored, recency_window_days, now)

    return scored


def _log_rank_trace(
    results: Iterable[NormalizedResult],
    recency_window_days: int,
    now: datetime,
) -> None:
    for i, r in enumerate(results):
        log.info(
            "rank[%d] score=%.3f overlap=%d trusted=%s recent=%s "
            "providers=%s url=%s",
            i,
            r.rank_score or 0.0,
            r.provider_overlap,
            is_trusted_domain(r.domain),
            is_recent(r.published_date, recency_window_days, now=now),
            r.providers,
            r.url,
        )
