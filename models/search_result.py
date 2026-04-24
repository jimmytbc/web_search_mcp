"""Search result models.

Two shapes:
  - RawSearchResult: per-provider intermediate, carries raw_rank so
    normalization and fusion can preserve provider ordering.
  - NormalizedResult: the per-result shape in the MCP output contract
    (handoff §11). Downstream phases extend the field values; they do
    not change the output shape.

Phase 2 addition: `rank_score` is an internal float used during
fusion/ranking. It is deliberately omitted from `to_dict()` so the
output contract stays exactly as published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawSearchResult:
    provider: str
    raw_rank: int
    title: str
    url: str
    snippet: str
    published_date: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class NormalizedResult:
    title: str
    url: str
    snippet: str
    domain: str
    providers: list[str]
    provider_overlap: int
    published_date: Optional[str]
    content_type: str
    confidence: float
    # Internal: fusion-time ranking score. Not serialized in the MCP
    # output — the payload carries `confidence` only. Kept on the model
    # so rank.py can write it and the handler can sort on it without an
    # extra parallel list.
    rank_score: Optional[float] = None
    # Internal: per-provider ranks keyed by provider name. Used by the
    # ranker as a fallback base score when a provider does not supply
    # its own score. Not serialized.
    raw_ranks: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": self.domain,
            "providers": list(self.providers),
            "provider_overlap": self.provider_overlap,
            "published_date": self.published_date,
            "content_type": self.content_type,
            "confidence": self.confidence,
        }
