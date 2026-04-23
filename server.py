"""FastMCP stdio server for web_search_mcp — Phase 1.

Registers exactly one tool (`search_web`). `fetch_url` and
`search_health` are reserved for Phase 4 and are not registered here.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from providers.searxng import SearxngProvider
from tools.search_web import run_search_web
from utils.config import load_config
from utils.logging import configure_logging, get_logger

configure_logging()
log = get_logger("web_search_mcp.server")

_config = load_config()
_provider = SearxngProvider(
    base_url=_config.searxng_base_url,
    timeout_seconds=_config.search_timeout_seconds,
)

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
        mode: One of "balanced" | "recall" | "precision". Phase 1 routes
            all modes to SearXNG.

    Returns:
        A normalized MCP response:
          {
            "query": str,
            "search_status": "ok" | "degraded" | "failed",
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
        provider=_provider,
    )


def main() -> None:
    log.info(
        "starting web_search_mcp stdio server "
        "(searxng_base_url=%s, timeout=%.1fs, default_max=%d)",
        _config.searxng_base_url,
        _config.search_timeout_seconds,
        _config.default_max_results,
    )
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
