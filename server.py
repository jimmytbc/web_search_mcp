"""FastMCP stdio server for web_search_mcp.

Registers three tools: `search_web` (Phases 1-3.1), plus `fetch_url`
and `search_health` (Phase 4).

Provider set is assembled via `build_providers`, which enables Brave,
Exa, and Serper when their respective API keys are set. Any subset
can be omitted; the server runs on whatever is available.
Mode-based routing in `tools/search_web.py` filters that set per call.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from providers import build_providers
from tools.fetch_url import run_fetch_url
from tools.search_health import run_search_health
from tools.search_web import run_search_web
from utils.config import load_config
from utils.logging import configure_logging, get_logger

configure_logging()
log = get_logger("web_search_mcp.server")

_config = load_config()
_providers = build_providers(_config)

mcp = FastMCP(name="web_search_mcp")


@mcp.tool
async def search_web(
    query: str,
    max_results: int = 5,
    mode: str = "balanced",
) -> dict:
    """Query configured web-search providers and return normalized results.

    Args:
        query: Search query string. Required.
        max_results: Maximum results to return (1–10, default 5).
        mode: One of "balanced" | "recall" | "precision". Routing matrix:
            balanced  → [serper, brave, exa]
            recall    → [serper, exa]
            precision → [brave, exa]
            Disabled providers in a mode's subset are skipped with a
            descriptive warning. If the subset is empty, the call
            returns search_status="failed" without contacting any
            provider.

    Returns:
        A normalized MCP response:
          {
            "query": str,
            "search_status": "ok" | "degraded" | "partial_failure" | "failed",
            "providers_used": list[str],
            "warnings": list[str],
            "results": [
              {
                "title": str, "url": str, "snippet": str, "domain": str,
                "providers": list[str], "provider_overlap": int,
                "published_date": str | null, "content_type": str,
                "confidence": float
              }, ...
            ]
          }
    """
    return await run_search_web(
        query=query,
        max_results=max_results,
        mode=mode,
        config=_config,
        providers=_providers,
    )


@mcp.tool
async def fetch_url(url: str) -> dict:
    """Fetch a single URL and extract its main text content.

    Static-HTML fetch only (no JavaScript rendering). Respects
    robots.txt by default, blocks private/local addresses by default,
    follows at most 5 redirects, and caps the body at
    FETCH_URL_MAX_BODY_BYTES.

    Args:
        url: Absolute http(s) URL to fetch. Required.

    Returns:
        {
          "url": str,            # echoes the input URL
          "status": "ok" | "degraded" | "failed",
          "content_type": str,   # HTTP Content-Type header ("" if none)
          "title": str,
          "text": str,           # extracted main text ("" on failure)
          "metadata": {          # absent fields are omitted
            "published_date"?: str, "author"?: str, "site_name"?: str
          },
          "warnings": list[str]
        }
        "degraded" means the page was fetched but yielded thin or
        non-HTML content (e.g. JavaScript-rendered pages).
    """
    return await run_fetch_url(url, config=_config)


@mcp.tool
async def search_health() -> dict:
    """Report provider connectivity, auth validity, and mode availability.

    Live mode probes each enabled provider with one minimal search
    call (this costs one billable API call per enabled provider).
    Set SEARCH_HEALTH_DRY_RUN=true to skip live probes and report
    enablement/mode availability only.

    Returns:
        {
          "status": "ok" | "degraded" | "failed",
          "providers": [
            {"name": str, "enabled": bool, "reachable": bool | null,
             "auth_ok": bool | null, "last_status": str,
             "warnings": list[str]}, ...
          ],
          "modes": {
            "balanced" | "recall" | "precision":
              {"available": bool, "reason"?: str}
          }
        }
    """
    return await run_search_health(config=_config, providers=_providers)


def main() -> None:
    log.info(
        "starting web_search_mcp stdio server "
        "(timeout=%.1fs, default_max=%d, "
        "brave_enabled=%s, exa_enabled=%s, serper_enabled=%s, "
        "recency_window_days=%d)",
        _config.search_timeout_seconds,
        _config.default_max_results,
        _config.brave_enabled,
        _config.exa_enabled,
        _config.serper_enabled,
        _config.recency_window_days,
    )
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
