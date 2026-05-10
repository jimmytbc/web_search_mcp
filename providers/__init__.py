"""Provider registration.

One place where enabled providers are constructed from Config. The
`search_web` handler iterates the returned list — order here is the
authoritative provider ordering for logging and fallback behavior.
"""

from __future__ import annotations

from providers.base import SearchProvider
from providers.brave import BraveProvider
from providers.exa import ExaProvider
from providers.searxng import SearxngProvider
from utils.config import Config
from utils.logging import get_logger

log = get_logger(__name__)


def build_providers(config: Config) -> list[SearchProvider]:
    """Return the list of enabled providers in call order.

    SearXNG is always enabled (local runtime dependency). Brave is
    enabled only when BRAVE_API_KEY is set, Exa only when EXA_API_KEY
    is set; otherwise a single INFO log line is emitted per provider
    and that provider is skipped. The server can run on any non-empty
    subset.
    """
    providers: list[SearchProvider] = [
        SearxngProvider(
            base_url=config.searxng_base_url,
            timeout_seconds=config.search_timeout_seconds,
        ),
    ]

    if config.brave_enabled:
        providers.append(
            BraveProvider(
                api_base=config.brave_api_base,
                api_key=config.brave_api_key or "",
                timeout_seconds=config.search_timeout_seconds,
                safesearch=config.brave_safesearch,
                default_country=config.brave_default_country,
                default_search_lang=config.brave_default_search_lang,
                max_count_ceiling=config.brave_max_results_ceiling,
            )
        )
        log.info("Brave enabled (api_base=%s)", config.brave_api_base)
    else:
        log.info("Brave disabled: BRAVE_API_KEY not set.")

    if config.exa_enabled:
        providers.append(
            ExaProvider(
                api_base=config.exa_api_base,
                api_key=config.exa_api_key or "",
                timeout_seconds=config.search_timeout_seconds,
                num_results_ceiling=config.exa_num_results_ceiling,
            )
        )
        log.info("Exa enabled (api_base=%s)", config.exa_api_base)
    else:
        log.info("Exa disabled: EXA_API_KEY not set.")

    return providers


__all__ = ["build_providers"]
