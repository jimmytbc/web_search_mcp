"""The `search_web` tool handler — Phase 2 fusion pipeline.

Phase 2 behavior:
  - `mode` is still pass-through: all three values route to every
    enabled provider. Mode-based routing arrives in Phase 3 with Exa.
  - Providers run in parallel via `asyncio.gather` with per-provider
    soft timeouts.
  - Results are normalized, canonicalized, deduped across providers,
    ranked, trimmed, and have `confidence` recomputed before output.
  - The final response shape is unchanged from Phase 1 (handoff §11);
    the difference is that `providers_used`, `provider_overlap`,
    `warnings`, and `confidence` are now meaningful across providers.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Literal, Optional

from fusion.dedupe import dedupe_by_canonical_url
from fusion.normalize import normalize
from fusion.rank import is_recent, is_trusted_domain, rank_results
from models.search_result import NormalizedResult, RawSearchResult
from providers.base import SearchProvider
from utils import cache
from utils.config import Config
from utils.logging import get_logger

log = get_logger(__name__)

Mode = Literal["balanced", "recall", "precision"]
_ALLOWED_MODES = {"balanced", "recall", "precision"}

# Low-diversity thresholds — spec-defined, centralized for clarity.
_DOMAIN_DIVERSITY_THRESHOLD = 0.70
_PROVIDER_DOMINANCE_THRESHOLD = 0.90

# Confidence weights are heuristic and tunable in this file only — no
# downstream consumer depends on the specific values. See handoff §14.
_CONF_OVERLAP_BOOST = 0.2
_CONF_TRUSTED_BOOST = 0.1
_CONF_RECENT_BOOST = 0.1


def _clamp_max_results(requested: int, default: int, upper_bound: int) -> int:
    if requested is None:
        return default
    if requested < 1:
        return 1
    if requested > upper_bound:
        return upper_bound
    return requested


def _normalize_mode(mode: str) -> Mode:
    if mode in _ALLOWED_MODES:
        return mode  # type: ignore[return-value]
    return "balanced"


async def _call_provider(
    provider: SearchProvider,
    query: str,
    max_results: int,
    timeout: float,
) -> tuple[str, Optional[list[RawSearchResult]], list[str]]:
    """Call a single provider with a soft timeout.

    Returns (provider_name, results_or_none, warnings). `results_or_none`
    is None only when the provider contributed nothing (timeout / error
    / zero-results). `warnings` is always descriptive — no stack traces
    or exception class names, per the output contract.
    """
    try:
        raws, warnings = await asyncio.wait_for(
            provider.search(query, max_results),
            timeout=timeout,
        )
        return provider.name, raws, warnings
    except asyncio.TimeoutError:
        log.warning("%s timed out after %.1fs", provider.name, timeout)
        return provider.name, None, [
            f"{provider.name} timed out after {timeout:.1f}s"
        ]
    except Exception as e:  # network / malformed JSON / provider error
        log.warning("%s request failed: %s", provider.name, e)
        return provider.name, None, [f"{provider.name} request failed: {e}"]


def _compute_confidence(
    position: int,
    result: NormalizedResult,
    recency_window_days: int,
) -> float:
    base = 1.0 / (position + 1)
    overlap_boost = _CONF_OVERLAP_BOOST if result.provider_overlap >= 2 else 0.0
    trusted_boost = _CONF_TRUSTED_BOOST if is_trusted_domain(result.domain) else 0.0
    recency_boost = (
        _CONF_RECENT_BOOST
        if is_recent(result.published_date, recency_window_days)
        else 0.0
    )
    return min(1.0, base + overlap_boost + trusted_boost + recency_boost)


def _detect_diversity_warnings(
    results: list[NormalizedResult],
    providers_called: list[str],
) -> list[str]:
    warnings: list[str] = []
    total = len(results)
    if total == 0:
        return warnings

    domain_counts = Counter(r.domain for r in results if r.domain)
    if domain_counts:
        top_domain, top_count = domain_counts.most_common(1)[0]
        if top_count / total > _DOMAIN_DIVERSITY_THRESHOLD:
            warnings.append(
                f"low source diversity: {top_domain} accounts for "
                f"{top_count} of {total} results"
            )

    if len(providers_called) >= 2:
        # A provider "dominates" when it is the SOLE source for most
        # results. Cross-confirmed hits (providers_overlap >= 2) are
        # evidence of good diversity, so they must not count toward
        # either provider's dominance tally.
        solo_counts: Counter = Counter()
        for r in results:
            if len(r.providers) == 1:
                solo_counts[r.providers[0]] += 1
        for provider, count in solo_counts.items():
            if count / total > _PROVIDER_DOMINANCE_THRESHOLD:
                warnings.append(
                    f"low provider diversity: {provider} was the sole "
                    f"source for {count} of {total} results despite "
                    "both providers being called"
                )
    return warnings


def _classify_status(
    providers_called: list[str],
    providers_failed: list[str],
    results: list[NormalizedResult],
    warnings: list[str],
) -> str:
    """State machine per the Phase 2 contract.

    - "failed": all providers failed, OR zero usable results after fusion.
    - "partial_failure": at least one provider failed AND at least one
      returned usable results.
    - "degraded": all called providers succeeded but warnings present.
    - "ok": all called providers succeeded and no warnings.
    """
    if not results:
        return "failed"
    if len(providers_failed) == len(providers_called):
        return "failed"
    if providers_failed:
        return "partial_failure"
    if warnings:
        return "degraded"
    return "ok"


async def run_search_web(
    query: str,
    max_results: int,
    mode: str,
    config: Config,
    providers: list[SearchProvider],
) -> dict:
    query = (query or "").strip()
    if not query:
        return {
            "query": query,
            "search_status": "failed",
            "providers_used": [],
            "warnings": ["query is required and must be a non-empty string"],
            "results": [],
        }

    effective_mode = _normalize_mode(mode)
    effective_max = _clamp_max_results(
        max_results, config.default_max_results, config.max_results_upper_bound
    )

    # Step 1 — cache check.
    key = cache.make_key(query, effective_mode, effective_max)
    cached = cache.get(key)
    if cached is not None:
        log.info(
            "cache HIT query=%r mode=%s max_results=%d",
            query,
            effective_mode,
            effective_max,
        )
        return cached

    log.info(
        "cache MISS query=%r mode=%s max_results=%d providers=%s",
        query,
        effective_mode,
        effective_max,
        [p.name for p in providers],
    )

    # Step 2 — parallel provider calls with per-provider soft timeout.
    timeout = config.search_timeout_seconds
    provider_outcomes = await asyncio.gather(
        *[_call_provider(p, query, effective_max, timeout) for p in providers],
        return_exceptions=False,
    )

    warnings: list[str] = []
    providers_called: list[str] = []
    providers_failed: list[str] = []
    providers_contributed: list[str] = []
    all_raws: list[RawSearchResult] = []

    for name, raws, prov_warnings in provider_outcomes:
        providers_called.append(name)
        warnings.extend(prov_warnings)
        if raws is None:
            providers_failed.append(name)
            continue
        if not raws:
            # Empty but not an error — still counts as a successful call.
            continue
        providers_contributed.append(name)
        all_raws.extend(raws)

    # Steps 3 & 4 — normalize + canonicalize. canonicalize_url is called
    # inside dedupe, so we do not mutate the per-result URL field.
    normalized = [normalize(r) for r in all_raws]

    # Step 5 — dedupe across providers, merging provenance.
    deduped = dedupe_by_canonical_url(normalized)

    # Step 6 & 7 — rank, sort (descending, stable), trim.
    ranked = rank_results(deduped, recency_window_days=config.recency_window_days)
    trimmed = ranked[:effective_max]

    # Step 8 — recompute confidence using the locked formula.
    for i, r in enumerate(trimmed):
        r.confidence = _compute_confidence(i, r, config.recency_window_days)

    # Low-diversity warning detection (spec task 12).
    diversity_warnings = _detect_diversity_warnings(trimmed, providers_called)
    warnings.extend(diversity_warnings)

    # Step 9 — assemble response.
    status = _classify_status(providers_called, providers_failed, trimmed, warnings)

    # providers_used = providers that actually contributed usable results
    # to the final (post-trim) output. Preserve original call order.
    contributors_in_output: set[str] = set()
    for r in trimmed:
        contributors_in_output.update(r.providers)
    providers_used = [p for p in providers_called if p in contributors_in_output]

    response = {
        "query": query,
        "search_status": status,
        "providers_used": providers_used,
        "warnings": warnings,
        "results": [r.to_dict() for r in trimmed],
    }

    # Step 10 — cache write.
    cache.set(key, response)

    log.info(
        "search_web done status=%s providers_used=%s results=%d warnings=%d",
        status,
        providers_used,
        len(trimmed),
        len(warnings),
    )
    # Step 11 — return.
    return response
