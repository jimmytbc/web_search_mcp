"""Session-lifetime in-memory cache for fetch_url envelopes.

Key: the canonical URL (via fusion.canonicalize.canonicalize_url —
read-only import; this module never modifies canonicalization rules).
Value: the full fetch_url response envelope dict. TTL = session — the
dict is cleared when the process exits. Only "ok" and "degraded"
outcomes are cached; "failed" outcomes are never cached.

Cache keys are coupled to canonicalization rules — a future
canonicalization change re-keys this cache. That coupling is
deliberate and accepted (phase-4.md TASK 4).

Divergence from utils/cache.py: `get` returns a deep copy rather than
the stored object, so a caller mutating a returned envelope cannot
poison the cached copy for the rest of the session. Envelopes are
small, so the copy cost is negligible.
"""

from __future__ import annotations

import copy
from typing import Optional

from fusion.canonicalize import canonicalize_url

CacheKey = str

_store: dict[CacheKey, dict] = {}


def make_key(url: str) -> CacheKey:
    return canonicalize_url(url)


def get(key: CacheKey) -> Optional[dict]:
    value = _store.get(key)
    if value is None:
        return None
    return copy.deepcopy(value)


def set(key: CacheKey, value: dict) -> None:
    _store[key] = value


def clear() -> None:
    _store.clear()


def size() -> int:
    return len(_store)
