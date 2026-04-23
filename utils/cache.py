"""Session-lifetime in-memory cache for normalized search responses.

Key: (query, mode, max_results). Value: the full normalized MCP
response dict. TTL = session — the dict is cleared when the process
exits. Cache lookups happen before provider calls; writes happen
after a successful response is assembled.
"""

from __future__ import annotations

from typing import Optional

CacheKey = tuple[str, str, int]

_store: dict[CacheKey, dict] = {}


def make_key(query: str, mode: str, max_results: int) -> CacheKey:
    return (query, mode, max_results)


def get(key: CacheKey) -> Optional[dict]:
    return _store.get(key)


def set(key: CacheKey, value: dict) -> None:
    _store[key] = value


def clear() -> None:
    _store.clear()


def size() -> int:
    return len(_store)
