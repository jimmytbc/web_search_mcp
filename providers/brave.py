"""Brave Search provider adapter.

Calls GET {base_url}/res/v1/web/search with the X-Subscription-Token
header and maps each entry in `web.results[]` to a RawSearchResult
preserving provider ordering via raw_rank.

Per the Phase 2 contract:
  - `count`  = min(max_results, 20)
  - `country`, `search_lang` are only sent when the matching env var
    is set — no location defaults in the adapter itself.
  - `safesearch` always goes through (default "moderate" via config).
  - `freshness` is never sent. Recency is handled as a soft ranking
    bonus in fusion/rank.py, not as a hard API-side filter.
"""

from __future__ import annotations

from typing import Optional

import httpx

from models.search_result import RawSearchResult
from utils.logging import get_logger

log = get_logger(__name__)


class BraveProvider:
    name = "brave"

    def __init__(
        self,
        api_base: str,
        api_key: str,
        timeout_seconds: float,
        safesearch: str,
        default_country: Optional[str] = None,
        default_search_lang: Optional[str] = None,
        max_count_ceiling: int = 20,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._safesearch = safesearch
        self._default_country = default_country
        self._default_search_lang = default_search_lang
        self._max_count_ceiling = max_count_ceiling

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[list[RawSearchResult], list[str]]:
        url = f"{self._api_base}/res/v1/web/search"
        params: dict[str, str | int] = {
            "q": query,
            "count": min(max_results, self._max_count_ceiling),
            "safesearch": self._safesearch,
        }
        if self._default_country:
            params["country"] = self._default_country
        if self._default_search_lang:
            params["search_lang"] = self._default_search_lang

        headers = {
            "X-Subscription-Token": self._api_key,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

        warnings: list[str] = []
        log.info("brave.search query=%r max_results=%d", query, max_results)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params, headers=headers)

        if resp.status_code != 200:
            raise BraveError(
                f"Brave returned HTTP {resp.status_code} for query {query!r}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise BraveError(f"Brave returned non-JSON response: {e}") from e

        web = payload.get("web") or {}
        raw = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw, list):
            # A valid response with no web section (e.g., a query that
            # returns only news/video) is treated as zero-results, not
            # an error. Surface as a soft warning.
            warnings.append("Brave returned no web.results section for this query")
            return [], warnings

        results: list[RawSearchResult] = []
        for idx, entry in enumerate(raw[:max_results]):
            if not isinstance(entry, dict):
                continue
            link = entry.get("url") or ""
            if not link:
                continue
            title = entry.get("title") or ""
            description = entry.get("description") or ""
            # Brave exposes `age` (short form like "2 days ago") and, on
            # some responses, a more precise `page_age` ISO-8601 string.
            # Prefer the structured one when present.
            published = entry.get("page_age") or entry.get("age") or None
            results.append(
                RawSearchResult(
                    provider=self.name,
                    raw_rank=idx,
                    title=title,
                    url=link,
                    snippet=description,
                    published_date=published,
                    extra={
                        "profile": entry.get("profile"),
                        "age": entry.get("age"),
                        "page_age": entry.get("page_age"),
                    },
                )
            )

        log.info(
            "brave.search returned=%d warnings=%d", len(results), len(warnings)
        )
        return results, warnings


class BraveError(RuntimeError):
    pass
