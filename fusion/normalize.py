"""Normalization of RawSearchResult -> NormalizedResult.

Phase 1 is single-provider (SearXNG only), so:
  - `providers` is always ["searxng"].
  - `provider_overlap` is always 1.
  - `published_date` is null (SearXNG does not reliably surface dates).
  - `content_type` is heuristically derived from the URL domain via a
    small static dict below. Pluggable/configurable in later phases
    per handoff §15.
  - `confidence` uses an inverse-rank formula: 1.0 / (raw_rank + 1),
    which yields 1.0 for rank 0, 0.5 for rank 1, and decays from there.
    Kept intentionally simple and transparent per handoff §14.
"""

from __future__ import annotations

from urllib.parse import urlparse

from models.search_result import NormalizedResult, RawSearchResult

# Domain / TLD heuristics for Phase 1. Minimal and pluggable later.
# Categories: official | news | market_data | reference | community | unknown
_SUFFIX_CATEGORY: dict[str, str] = {
    ".gov": "official",
    ".gov.uk": "official",
    ".mil": "official",
    ".edu": "reference",
    ".ac.uk": "reference",
}

_DOMAIN_CATEGORY: dict[str, str] = {
    # news
    "reuters.com": "news",
    "bloomberg.com": "news",
    "wsj.com": "news",
    "ft.com": "news",
    "nytimes.com": "news",
    "theguardian.com": "news",
    "bbc.com": "news",
    "bbc.co.uk": "news",
    "cnbc.com": "news",
    "apnews.com": "news",
    # market_data
    "coingecko.com": "market_data",
    "coinmarketcap.com": "market_data",
    "finance.yahoo.com": "market_data",
    "sg.finance.yahoo.com": "market_data",
    "uk.finance.yahoo.com": "market_data",
    "tradingview.com": "market_data",
    "binance.com": "market_data",
    "coinbase.com": "market_data",
    "kraken.com": "market_data",
    "coindesk.com": "market_data",
    "coincheckup.com": "market_data",
    "bitflyer.com": "market_data",
    # reference
    "wikipedia.org": "reference",
    "en.wikipedia.org": "reference",
    "arxiv.org": "reference",
    # community
    "reddit.com": "community",
    "stackoverflow.com": "community",
    "stackexchange.com": "community",
    "news.ycombinator.com": "community",
    "github.com": "community",
}


def extract_domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def classify_content_type(domain: str) -> str:
    if not domain:
        return "unknown"
    if domain in _DOMAIN_CATEGORY:
        return _DOMAIN_CATEGORY[domain]
    for suffix, category in _SUFFIX_CATEGORY.items():
        if domain.endswith(suffix):
            return category
    return "unknown"


def compute_confidence(raw_rank: int) -> float:
    # Inverse-rank: rank 0 -> 1.0, rank 1 -> 0.5, rank 2 -> 0.333, ...
    # Already in [0, 1]; no further normalization needed in Phase 1.
    return 1.0 / (raw_rank + 1)


def normalize(raw: RawSearchResult) -> NormalizedResult:
    domain = extract_domain(raw.url)
    # Phase 2: keep published_date from the raw result when the provider
    # supplies one (Brave exposes `age` / `page_age`; SearXNG typically
    # does not). Phase 1's SearXNG path still lands here with None.
    return NormalizedResult(
        title=raw.title,
        url=raw.url,
        snippet=raw.snippet,
        domain=domain,
        providers=[raw.provider],
        provider_overlap=1,
        published_date=raw.published_date,
        content_type=classify_content_type(domain),
        # Confidence here is a provisional per-provider value. The
        # search_web handler recomputes it after fusion using the
        # locked rank-based formula documented in the handoff.
        confidence=compute_confidence(raw.raw_rank),
        raw_ranks={raw.provider: raw.raw_rank},
    )


def normalize_all(raws: list[RawSearchResult]) -> list[NormalizedResult]:
    return [normalize(r) for r in raws]
