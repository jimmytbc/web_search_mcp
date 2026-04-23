"""Minimal adapter interface for search providers.

Each provider has a stable `name` and an async `search` method that
returns RawSearchResult items in the provider's native ordering.
Provider-level warnings (e.g., upstream-engine degradation) are
returned via the second tuple element so the caller can surface them
without a provider-specific exception hierarchy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from models.search_result import RawSearchResult


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> tuple[list[RawSearchResult], list[str]]:
        """Return (results, warnings) for the query.

        `warnings` is a list of plain descriptive strings suitable for
        the MCP `warnings` field; do not include stack traces or
        exception class names.
        """
        ...
