"""Opt-in anonymous Parallel Search MCP adapter (Streamable HTTP)."""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlsplit

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from models.search_result import RawSearchResult

MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "web_search_mcp/0.1 (+https://github.com/jimmytbc/web_search_mcp)"
# This server has no conversation identifier. Conservatively reuse one ID for
# the whole process, including health probes, rather than resetting per request.
_SESSION_ID = uuid.uuid4().hex


class ParallelError(Exception):
    """A remote tool error or an invalid Parallel search response."""


class ParallelProvider:
    name = "parallel"

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    async def search(
        self, query: str, max_results: int,
    ) -> tuple[list[RawSearchResult], list[str]]:
        transport = StreamableHttpTransport(
            MCP_URL, headers={"User-Agent": USER_AGENT},
        )
        # Bound the handshake, tool call and cleanup as well as the HTTP reads.
        # Cancellation is deliberately allowed to propagate to the caller.
        async with asyncio.timeout(self._timeout):
            async with Client(
                transport, timeout=self._timeout, init_timeout=self._timeout,
            ) as client:
                response = await client.call_tool(
                    "web_search",
                    {"objective": query, "search_queries": [query],
                     "session_id": _SESSION_ID},
                    raise_on_error=False,
                )

        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        )
        if response.is_error:
            raise ParallelError(f"Parallel search failed: {text[:300]}")
        data = response.structured_content
        if data is None:
            try:
                data = json.loads(text)
            except (ValueError, TypeError) as exc:
                raise ParallelError("Parallel returned a non-JSON search response") from exc
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ParallelError("Parallel returned an invalid search response")

        warnings = [
            f"parallel: {warning['message']}"
            for warning in (data.get("warnings") or [])
            if isinstance(warning, dict) and isinstance(warning.get("message"), str)
        ]
        results: list[RawSearchResult] = []
        for rank, item in enumerate(data["results"]):
            if not isinstance(item, dict):
                warnings.append("parallel skipped a result with invalid fields")
                continue
            url = item.get("url")
            excerpts = item.get("excerpts")
            title, date = item.get("title"), item.get("publish_date")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid_url = parsed is not None and parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            except ValueError:
                valid_url = False
            if (not valid_url or not isinstance(excerpts, list)
                    or any(not isinstance(part, str) for part in excerpts)
                    or title is not None and not isinstance(title, str)
                    or date is not None and not isinstance(date, str)):
                warnings.append("parallel skipped a result with invalid fields")
                continue
            results.append(RawSearchResult(
                provider=self.name, raw_rank=rank, title=title or "", url=url,
                snippet=" ... ".join(excerpts), published_date=date,
            ))
            if len(results) >= max_results:
                break
        return results, warnings
