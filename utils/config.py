"""Environment-backed configuration.

Brave, Exa, and Serper each enable when their respective API key is
set. RECENCY_WINDOW_DAYS tunes the ranker's recency bonus. `.env` is
loaded via python-dotenv at import time so operators can keep local
secrets out of the shell profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Load .env once at process start. Silent no-op if the file is absent.
load_dotenv()

DEFAULT_SEARCH_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_UPPER_BOUND = 10
DEFAULT_BRAVE_API_BASE = "https://api.search.brave.com"
DEFAULT_BRAVE_SAFESEARCH = "moderate"
DEFAULT_RECENCY_WINDOW_DAYS = 30
BRAVE_MAX_RESULTS_CEILING = 20
DEFAULT_EXA_API_BASE = "https://api.exa.ai"
DEFAULT_EXA_NUM_RESULTS_CEILING = 10
DEFAULT_SERPER_API_BASE = "https://google.serper.dev"
DEFAULT_SERPER_NUM_RESULTS_CEILING = 10


@dataclass(frozen=True)
class Config:
    search_timeout_seconds: float
    default_max_results: int
    brave_api_base: str
    brave_api_key: Optional[str]
    brave_default_country: Optional[str]
    brave_default_search_lang: Optional[str]
    brave_safesearch: str
    recency_window_days: int
    exa_api_base: str
    exa_api_key: Optional[str]
    serper_api_base: str
    serper_api_key: Optional[str]
    max_results_upper_bound: int = MAX_RESULTS_UPPER_BOUND
    brave_max_results_ceiling: int = BRAVE_MAX_RESULTS_CEILING
    exa_num_results_ceiling: int = DEFAULT_EXA_NUM_RESULTS_CEILING
    serper_num_results_ceiling: int = DEFAULT_SERPER_NUM_RESULTS_CEILING

    @property
    def brave_enabled(self) -> bool:
        return bool(self.brave_api_key)

    @property
    def exa_enabled(self) -> bool:
        return bool(self.exa_api_key)

    @property
    def serper_enabled(self) -> bool:
        return bool(self.serper_api_key)


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
    brave_api_base = os.environ.get("BRAVE_API_BASE", DEFAULT_BRAVE_API_BASE).rstrip("/")
    exa_api_base = os.environ.get("EXA_API_BASE", DEFAULT_EXA_API_BASE).rstrip("/")
    serper_api_base = os.environ.get("SERPER_API_BASE", DEFAULT_SERPER_API_BASE).rstrip("/")
    return Config(
        search_timeout_seconds=_get_float("SEARCH_TIMEOUT_SECONDS", DEFAULT_SEARCH_TIMEOUT_SECONDS),
        default_max_results=_get_int("DEFAULT_MAX_RESULTS", DEFAULT_MAX_RESULTS),
        brave_api_base=brave_api_base,
        brave_api_key=_get_str("BRAVE_API_KEY"),
        brave_default_country=_get_str("BRAVE_DEFAULT_COUNTRY"),
        brave_default_search_lang=_get_str("BRAVE_DEFAULT_SEARCH_LANG"),
        brave_safesearch=(_get_str("BRAVE_SAFESEARCH") or DEFAULT_BRAVE_SAFESEARCH),
        recency_window_days=_get_int("RECENCY_WINDOW_DAYS", DEFAULT_RECENCY_WINDOW_DAYS),
        exa_api_base=exa_api_base,
        exa_api_key=_get_str("EXA_API_KEY"),
        exa_num_results_ceiling=_get_int("EXA_NUM_RESULTS_CEILING", DEFAULT_EXA_NUM_RESULTS_CEILING),
        serper_api_base=serper_api_base,
        serper_api_key=_get_str("SERPER_API_KEY"),
        serper_num_results_ceiling=_get_int(
            "SERPER_NUM_RESULTS_CEILING", DEFAULT_SERPER_NUM_RESULTS_CEILING
        ),
    )
