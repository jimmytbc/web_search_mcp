"""Exa Search provider adapter.

Calls POST {base_url}/search with the x-api-key header and maps each
entry in `results[]` to a RawSearchResult preserving provider ordering
via raw_rank.

Per the Phase 3 contract:
  - body `{"query", "numResults": min(max_results, ceiling),
           "type": "auto", "contents": {"highlights": true}}`
  - `useAutoprompt` is NEVER sent (deprecated upstream).
  - `startPublishedDate` is NEVER sent — recency is handled as a soft
    ranking bonus in fusion/rank.py, not as a hard API-side filter.
  - snippet = `" ... ".join(highlights)` when highlights[] is present
    and non-empty; otherwise empty string. We do NOT fall back to the
    `text` field (can be enormous; would bloat the cache).
  - Exa's per-result `score` is stored in `extra` only and is NOT used
    as the ranking base — fusion/rank.py uses inverse-rank uniformly
    across all providers.
"""

from __future__ import annotations

import httpx

from models.search_result import RawSearchResult
from utils.logging import get_logger

log = get_logger(__name__)


class ExaProvider:
    name = "exa"

    def __init__(
        self,
        api_base: str,
        api_key: str,
        timeout_seconds: float,
        num_results_ceiling: int = 10,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._num_results_ceiling = num_results_ceiling

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[list[RawSearchResult], list[str]]:
        url = f"{self._api_base}/search"
        body = {
            "query": query,
            "numResults": min(max_results, self._num_results_ceiling),
            "type": "auto",
            "contents": {"highlights": True},
        }
        headers = {
            "x-api-key": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        warnings: list[str] = []
        log.info("exa.search query=%r max_results=%d", query, max_results)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=headers)

        if resp.status_code != 200:
            body = (resp.text or "").strip().replace("\n", " ")[:200]
            body_clause = f" body={body!r}" if body else ""
            raise ExaError(
                f"Exa returned HTTP {resp.status_code} for query {query!r}{body_clause}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            body = (resp.text or "").strip().replace("\n", " ")[:200]
            raise ExaError(
                f"Exa returned non-JSON response: {e} body={body!r}"
            ) from e

        raw = payload.get("results")
        if not isinstance(raw, list):
            warnings.append("Exa returned no results section for this query")
            return [], warnings

        results: list[RawSearchResult] = []
        for idx, entry in enumerate(raw[:max_results]):
            if not isinstance(entry, dict):
                continue
            link = entry.get("url") or ""
            if not link:
                continue
            title = entry.get("title") or ""
            highlights = entry.get("highlights")
            if isinstance(highlights, list) and highlights:
                snippet = " ... ".join(str(h) for h in highlights if h)
            else:
                snippet = ""
            published = entry.get("publishedDate") or None
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
                        "id": entry.get("id"),
                        "author": entry.get("author"),
                    },
                )
            )

        log.info(
            "exa.search returned=%d warnings=%d", len(results), len(warnings)
        )
        return results, warnings


class ExaError(RuntimeError):
    pass
