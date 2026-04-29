"""Environment-backed configuration.

Phase 1 read three variables; Phase 2 adds Brave credentials and
behavioral knobs, plus a RECENCY_WINDOW_DAYS setting used by the
ranking layer. `.env` is loaded via python-dotenv at import time so
operators can keep local secrets out of the shell profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Load .env once at process start. Silent no-op if the file is absent.
load_dotenv()

DEFAULT_SEARXNG_BASE_URL = "http://localhost:8888"
DEFAULT_SEARCH_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_UPPER_BOUND = 10
DEFAULT_BRAVE_API_BASE = "https://api.search.brave.com"
DEFAULT_BRAVE_SAFESEARCH = "moderate"
DEFAULT_RECENCY_WINDOW_DAYS = 30
BRAVE_MAX_RESULTS_CEILING = 20


@dataclass(frozen=True)
class Config:
    searxng_base_url: str
    search_timeout_seconds: float
    default_max_results: int
    brave_api_base: str
    brave_api_key: Optional[str]
    brave_default_country: Optional[str]
    brave_default_search_lang: Optional[str]
    brave_safesearch: str
    recency_window_days: int
    max_results_upper_bound: int = MAX_RESULTS_UPPER_BOUND
    tavily_api_key: Optional[str] = None
    brave_max_results_ceiling: int = BRAVE_MAX_RESULTS_CEILING

    @property
    def brave_enabled(self) -> bool:
        return bool(self.brave_api_key)

    @property
    def tavily_enabled(self) -> bool:
        return bool(self.tavily_api_key)


def _get_str(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


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
    brave_api_base = os.environ.get("BRAVE_API_BASE", DEFAULT_BRAVE_API_BASE).rstrip("/")
    return Config(
        searxng_base_url=base_url,
        search_timeout_seconds=_get_float("SEARCH_TIMEOUT_SECONDS", DEFAULT_SEARCH_TIMEOUT_SECONDS),
        default_max_results=_get_int("DEFAULT_MAX_RESULTS", DEFAULT_MAX_RESULTS),
        brave_api_base=brave_api_base,
        brave_api_key=_get_str("BRAVE_API_KEY"),
        brave_default_country=_get_str("BRAVE_DEFAULT_COUNTRY"),
        brave_default_search_lang=_get_str("BRAVE_DEFAULT_SEARCH_LANG"),
        brave_safesearch=(_get_str("BRAVE_SAFESEARCH") or DEFAULT_BRAVE_SAFESEARCH),
        recency_window_days=_get_int("RECENCY_WINDOW_DAYS", DEFAULT_RECENCY_WINDOW_DAYS),
        tavily_api_key=_get_str("TAVILY_API_KEY"),
    )
