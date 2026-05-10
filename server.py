"""FastMCP stdio server for web_search_mcp.

Registers exactly one tool (`search_web`). `fetch_url` and
`search_health` are reserved for Phase 4 and are not registered here.

Provider set is assembled via `build_providers`, which enables Brave
when `BRAVE_API_KEY` is set and Exa when `EXA_API_KEY` is set. Either
or both can be omitted; the server runs on whatever subset is
available (SearXNG is always enabled as a local runtime dependency).
Mode-based routing in `tools/search_web.py` filters that set per call.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from providers import build_providers
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
            balanced  → [searxng, brave, exa]
            recall    → [searxng, exa]
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


def main() -> None:
    log.info(
        "starting web_search_mcp stdio server "
        "(searxng_base_url=%s, timeout=%.1fs, default_max=%d, "
        "brave_enabled=%s, recency_window_days=%d)",
        _config.searxng_base_url,
        _config.search_timeout_seconds,
        _config.default_max_results,
        _config.brave_enabled,
        _config.recency_window_days,
    )
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
