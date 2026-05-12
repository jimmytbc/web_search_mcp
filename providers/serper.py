"""Serper.dev Search provider adapter.

Calls POST {base_url}/search with the X-API-KEY header and maps each
entry in `organic[]` to a RawSearchResult preserving provider ordering
via Serper's own `position` field.

Per the Phase 3.1 contract:
  - body `{"q": query, "num": min(max_results, ceiling)}`
  - `/news` endpoint is reserved for a separate breaking-news project
    and is NEVER called from here.
  - `published_date` is always None for Serper results. Serper returns
    relative date strings like "3 days ago"; this adapter does not
    parse them. The recency ranking bonus therefore never fires for
    Serper-only results. Cross-provider results inherit a non-None
    `published_date` from Brave/Exa via the dedupe merge.
"""

from __future__ import annotations

import httpx

from models.search_result import RawSearchResult
from utils.logging import get_logger

log = get_logger(__name__)


class SerperProvider:
    name = "serper"

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
            "q": query,
            "num": min(max_results, self._num_results_ceiling),
        }
        headers = {
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        warnings: list[str] = []
        log.info("serper.search query=%r max_results=%d", query, max_results)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=headers)

        if resp.status_code != 200:
            body_text = (resp.text or "").strip().replace("\n", " ")[:200]
            body_clause = f" body={body_text!r}" if body_text else ""
            raise SerperError(
                f"Serper returned HTTP {resp.status_code} for query {query!r}{body_clause}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            body_text = (resp.text or "").strip().replace("\n", " ")[:200]
            raise SerperError(
                f"Serper returned non-JSON response: {e} body={body_text!r}"
            ) from e

        raw = payload.get("organic")
        if not isinstance(raw, list):
            warnings.append("Serper returned no organic results section for this query")
            return [], warnings

        results: list[RawSearchResult] = []
        for idx, entry in enumerate(raw[:max_results]):
            if not isinstance(entry, dict):
                continue
            link = entry.get("link") or ""
            if not link:
                continue
            title = entry.get("title") or ""
            snippet = entry.get("snippet") or ""
            # Serper provides a 1-based `position`. Fall back to enumeration
            # index when absent so raw_rank stays well-defined.
            position = entry.get("position")
            raw_rank = (position - 1) if isinstance(position, int) and position >= 1 else idx
            results.append(
                RawSearchResult(
                    provider=self.name,
                    raw_rank=raw_rank,
                    title=title,
                    url=link,
                    snippet=snippet,
                    # Relative date strings ("3 days ago") are not parsed.
                    # See module docstring.
                    published_date=None,
                    extra={
                        "position": position,
                        "date": entry.get("date"),
                    },
                )
            )

        log.info(
            "serper.search returned=%d warnings=%d", len(results), len(warnings)
        )
        return results, warnings


class SerperError(RuntimeError):
    pass
