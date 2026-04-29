"""Tavily Search provider adapter.

Uses the tavily-python SDK (AsyncTavilyClient) and maps each result to
a RawSearchResult preserving provider ordering via raw_rank.

Tavily is enabled only when TAVILY_API_KEY is set. It participates as
an equal peer in the existing parallel-fusion pipeline alongside
SearXNG and Brave.
"""

from __future__ import annotations

from tavily import AsyncTavilyClient

from models.search_result import RawSearchResult
from utils.logging import get_logger

log = get_logger(__name__)


class TavilyProvider:
    name = "tavily"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
    ) -> None:
        self._client = AsyncTavilyClient(api_key=api_key)
        self._timeout = timeout_seconds

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[list[RawSearchResult], list[str]]:
        warnings: list[str] = []
        log.info("tavily.search query=%r max_results=%d", query, max_results)

        try:
            response = await self._client.search(
                query=query,
                max_results=max_results,
                search_depth="advanced",
            )
        except Exception as e:
            raise TavilyError(
                f"Tavily search failed for query {query!r}: {e}"
            ) from e

        raw = response.get("results")
        if not isinstance(raw, list):
            warnings.append("Tavily returned no results for this query")
            return [], warnings

        results: list[RawSearchResult] = []
        for idx, entry in enumerate(raw[:max_results]):
            if not isinstance(entry, dict):
                continue
            link = entry.get("url") or ""
            if not link:
                continue
            title = entry.get("title") or ""
            snippet = entry.get("content") or ""
            published = entry.get("published_date") or None
            results.append(
                RawSearchResult(
                    provider=self.name,
                    raw_rank=idx,
                    title=title,
                    url=link,
                    snippet=snippet,
                    published_date=published,
                    extra={
                        "score": entry.get("score"),
                    },
                )
            )

        log.info(
            "tavily.search returned=%d warnings=%d", len(results), len(warnings)
        )
        return results, warnings


class TavilyError(RuntimeError):
    pass
