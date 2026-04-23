"""The `search_web` tool handler.

Phase 1 behavior:
  - All three `mode` values pass through to SearXNG only (routing
    lives in Phase 2–3).
  - Response cached by (query, mode, max_results) for the session.
  - SearXNG upstream-engine warnings -> search_status="degraded".
  - SearXNG failure or zero usable results -> search_status="failed"
    with a descriptive warning; handler does not raise (handoff §17).
"""

from __future__ import annotations

from typing import Literal

from fusion.normalize import normalize_all
from providers.searxng import SearxngError, SearxngProvider
from utils import cache
from utils.config import Config
from utils.logging import get_logger

log = get_logger(__name__)

Mode = Literal["balanced", "recall", "precision"]
_ALLOWED_MODES = {"balanced", "recall", "precision"}


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


async def run_search_web(
    query: str,
    max_results: int,
    mode: str,
    config: Config,
    provider: SearxngProvider,
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

    key = cache.make_key(query, effective_mode, effective_max)
    cached = cache.get(key)
    if cached is not None:
        log.info(
            "cache HIT query=%r mode=%s max_results=%d", query, effective_mode, effective_max
        )
        return cached

    log.info(
        "cache MISS query=%r mode=%s max_results=%d", query, effective_mode, effective_max
    )

    warnings: list[str] = []
    try:
        raws, provider_warnings = await provider.search(query, effective_max)
        warnings.extend(provider_warnings)
    except SearxngError as e:
        log.warning("searxng provider error: %s", e)
        return {
            "query": query,
            "search_status": "failed",
            "providers_used": [],
            "warnings": [f"searxng provider error: {e}"],
            "results": [],
        }
    except Exception as e:  # network/timeout/etc — per handoff §17 do not raise.
        log.warning("searxng request failed: %s", e)
        return {
            "query": query,
            "search_status": "failed",
            "providers_used": [],
            "warnings": [f"searxng request failed: {e}"],
            "results": [],
        }

    if not raws:
        return {
            "query": query,
            "search_status": "failed",
            "providers_used": [provider.name],
            "warnings": warnings + ["searxng returned zero usable results"],
            "results": [],
        }

    normalized = normalize_all(raws)

    status = "degraded" if warnings else "ok"
    response = {
        "query": query,
        "search_status": status,
        "providers_used": [provider.name],
        "warnings": warnings,
        "results": [r.to_dict() for r in normalized],
    }

    cache.set(key, response)
    return response
