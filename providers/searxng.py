"""SearXNG provider adapter.

Calls GET {base_url}/search?format=json&q={query} and maps each entry
to a RawSearchResult preserving provider ordering via raw_rank.
Upstream degradation (unresponsive engines) is surfaced as warnings,
never as exceptions.
"""

from __future__ import annotations

import httpx

from models.search_result import RawSearchResult
from utils.logging import get_logger

log = get_logger(__name__)


class SearxngProvider:
    name = "searxng"

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[list[RawSearchResult], list[str]]:
        url = f"{self._base_url}/search"
        params = {"format": "json", "q": query}
        warnings: list[str] = []

        log.info("searxng.search query=%r max_results=%d", query, max_results)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params)

        if resp.status_code != 200:
            raise SearxngError(
                f"SearXNG returned HTTP {resp.status_code} for query {query!r}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise SearxngError(f"SearXNG returned non-JSON response: {e}") from e

        raw = payload.get("results")
        if not isinstance(raw, list):
            raise SearxngError("SearXNG response missing 'results' array")

        unresponsive = payload.get("unresponsive_engines") or []
        if unresponsive:
            names = _format_unresponsive(unresponsive)
            warnings.append(
                f"SearXNG reported unresponsive engines: {names}"
            )

        results: list[RawSearchResult] = []
        for idx, entry in enumerate(raw[:max_results]):
            title = entry.get("title") or ""
            link = entry.get("url") or ""
            content = entry.get("content") or ""
            if not link:
                continue
            results.append(
                RawSearchResult(
                    provider=self.name,
                    raw_rank=idx,
                    title=title,
                    url=link,
                    snippet=content,
                    published_date=entry.get("publishedDate") or entry.get("published_date"),
                    extra={"engine": entry.get("engine"), "engines": entry.get("engines")},
                )
            )

        log.info(
            "searxng.search returned=%d warnings=%d", len(results), len(warnings)
        )
        return results, warnings


class SearxngError(RuntimeError):
    pass


def _format_unresponsive(entries: list) -> str:
    # SearXNG typically reports [[engine_name, reason], ...] but formats
    # vary by version. Stringify defensively for a human-readable warning.
    parts: list[str] = []
    for e in entries:
        if isinstance(e, (list, tuple)) and e:
            parts.append(str(e[0]))
        else:
            parts.append(str(e))
    return ", ".join(parts) if parts else "unknown"
