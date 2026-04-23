"""Environment-backed configuration for Phase 1.

Only three variables are read. Defaults match the Phase 1 runtime
assumptions (local SearXNG at :8888, 10 s timeout, 5 results).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SEARXNG_BASE_URL = "http://localhost:8888"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_UPPER_BOUND = 10


@dataclass(frozen=True)
class Config:
    searxng_base_url: str
    search_timeout_seconds: float
    default_max_results: int
    max_results_upper_bound: int = MAX_RESULTS_UPPER_BOUND


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config() -> Config:
    base_url = os.environ.get("SEARXNG_BASE_URL", DEFAULT_SEARXNG_BASE_URL).rstrip("/")
    return Config(
        searxng_base_url=base_url,
        search_timeout_seconds=_get_float("SEARCH_TIMEOUT_SECONDS", DEFAULT_SEARCH_TIMEOUT_SECONDS),
        default_max_results=_get_int("DEFAULT_MAX_RESULTS", DEFAULT_MAX_RESULTS),
    )
