"""The `fetch_url` tool handler — static fetch + main-content extraction.

Phase 4 behavior (phase-4.md TASK 5; ratified hardenings 2026-06-12):
  1.  Cache check (canonical-URL key, utils/fetch_cache.py).
  2.  URL-policy guard: http/https only; unless FETCH_URL_ALLOW_PRIVATE,
      every resolved address must clear the private/local blocklist.
      The check uses ipaddress boolean predicates (is_loopback,
      is_private, is_link_local, is_reserved, is_multicast,
      is_unspecified) plus CGNAT 100.64.0.0/10 — a strict superset of
      the CIDR list enumerated in the brief — and rejects if ANY
      resolved A/AAAA record is blocked (fail-closed, including on
      resolution errors). IPv4-mapped IPv6 addresses are re-checked as
      their mapped IPv4. Residual DNS-rebinding TOCTOU (httpx
      re-resolves at connect time) is an accepted v1 risk — NOTES.md.
  3.  robots.txt check (FETCH_URL_RESPECT_ROBOTS): fetched via httpx
      with the same guard, parsed via urllib.robotparser .parse()
      (never .read(), which would fetch unguarded). Parsed results are
      cached per (scheme, host, port) for the session. 4xx/5xx/
      unreachable/missing robots → allowed (logged at INFO). Robots is
      re-checked when a redirect hop crosses to a different
      (scheme, host, port).
  4.  Manual redirects: follow_redirects=False, max 5 hops, the full
      step-2 guard re-runs on every absolute (urljoin-resolved) hop
      target.
  5.  Body cap on the DECOMPRESSED stream (aiter_bytes), truncating
      with a warning at FETCH_URL_MAX_BODY_BYTES.
  6.  Extraction via trafilatura (single-pass bare_extraction with
      metadata; trafilatura 2.1.0 verified at install time per R2).
      Non-HTML Content-Type skips extraction with a content-aware
      degraded warning (ratified D-d).
  7.  Thin-text threshold → "degraded" with a count-bearing warning.
  8.  Envelope: {url, status, content_type, title, text, metadata,
      warnings}; status ∈ ok | degraded | failed. `url` echoes the
      caller's input verbatim; `content_type` is the last received
      response's Content-Type header ("" when no response was
      received).
  9.  Cache write (ok and degraded only).

The whole network section runs under one monotonic deadline of
FETCH_URL_TIMEOUT_SECONDS — robots fetch, every redirect hop, and the
body read all draw from the same budget (a single httpx.Timeout
cannot cap a multi-request operation).

Error model: never raises to the MCP layer — every failure becomes
status="failed" plus a plain descriptive warning (no stack traces, no
exception class names).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from utils import fetch_cache
from utils.config import Config
from utils.logging import get_logger

log = get_logger(__name__)

# Tunable heuristic; no downstream code depends on the exact value
# (same convention as fusion/rank.py weights).
THIN_TEXT_THRESHOLD_CHARS = 200

_MAX_REDIRECT_HOPS = 5

# Content types we hand to trafilatura. Anything else short-circuits
# to "degraded" with a content-aware warning (ratified decision D-d).
# A missing Content-Type header is treated as HTML (optimistic).
_EXTRACTABLE_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

# CGNAT range — not covered by ipaddress.is_private (ratified D-b).
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# Per-host robots cache: (scheme, host, port) -> parsed RobotFileParser,
# or None meaning "no usable robots.txt — allow all". Module-level and
# session-lifetime by design (mirrors utils/cache.py's _store idiom);
# probes/phase-4-probe.py seeds this directly in assertion (v).
_robots_cache: dict[tuple[str, str, int], Optional[RobotFileParser]] = {}


class _FetchDeadlineExceeded(Exception):
    """Internal sentinel — converted to a failed envelope, never leaks."""


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _FetchDeadlineExceeded
    return remaining


def _effective_port(parts) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_is_blocked(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to all A/AAAA records. Raises on failure."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    addresses = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    return addresses


async def _check_url_policy(url: str, allow_private: bool) -> Optional[str]:
    """Validate scheme and resolved addresses. Returns a warning string
    on rejection, None when the URL is allowed."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return f"URL scheme not supported: {parts.scheme or '(none)'}"
    host = parts.hostname
    if not host:
        return "URL has no host"
    if allow_private:
        return None

    blocked_warning = f"Fetch blocked: {host} resolves to a private or local address"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return blocked_warning if _ip_is_blocked(literal) else None

    try:
        addresses = await _resolve_host(host)
    except Exception:
        # Fail closed: an unresolvable host gets the blocking-class
        # warning rather than an attempted fetch.
        return blocked_warning
    if not addresses or any(_ip_is_blocked(ip) for ip in addresses):
        return blocked_warning
    return None


async def _fetch_robots_txt(
    scheme: str,
    host: str,
    port: int,
    client: httpx.AsyncClient,
    config: Config,
    deadline: float,
) -> Optional[RobotFileParser]:
    """Fetch and parse robots.txt for one origin. Returns None when no
    usable robots policy exists (treat as allow-all). The fetch obeys
    the same URL-policy guard and manual-redirect discipline as the
    main fetch, and draws from the same deadline budget."""
    netloc = host if port in (80, 443) else f"{host}:{port}"
    robots_url = f"{scheme}://{netloc}/robots.txt"
    current = robots_url
    for _hop in range(_MAX_REDIRECT_HOPS + 1):
        warning = await _check_url_policy(current, config.fetch_url_allow_private)
        if warning is not None:
            log.info("robots fetch for %s blocked by URL policy: %s", robots_url, warning)
            return None
        try:
            resp = await client.get(
                current,
                headers={"User-Agent": config.fetch_url_user_agent},
                timeout=_remaining(deadline),
            )
        except _FetchDeadlineExceeded:
            raise
        except Exception as e:
            log.info("robots.txt unreachable for %s (%s) — treating as allowed", robots_url, e)
            return None
        if resp.is_redirect:
            location = resp.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            log.info(
                "robots.txt for %s returned HTTP %d — treating as allowed",
                robots_url,
                resp.status_code,
            )
            return None
        parser = RobotFileParser()
        # .parse() on decoded lines only — never .read()/.set_url(),
        # which would fetch through urllib unguarded.
        parser.parse(resp.text.splitlines())
        return parser
    log.info("robots.txt for %s exceeded redirect cap — treating as allowed", robots_url)
    return None


async def _robots_allows(
    url: str,
    client: httpx.AsyncClient,
    config: Config,
    deadline: float,
) -> bool:
    parts = urlsplit(url)
    origin = (parts.scheme, parts.hostname or "", _effective_port(parts))
    if origin not in _robots_cache:
        _robots_cache[origin] = await _fetch_robots_txt(
            parts.scheme, parts.hostname or "", origin[2], client, config, deadline
        )
    parser = _robots_cache[origin]
    if parser is None:
        return True
    # Evaluate the actual per-hop URL, not the canonicalized form.
    return parser.can_fetch(config.fetch_url_user_agent, url)


async def _fetch_with_redirects(
    url: str,
    client: httpx.AsyncClient,
    config: Config,
    deadline: float,
    warnings: list[str],
) -> tuple[Optional[httpx.Response], Optional[bytes], Optional[str]]:
    """Follow redirects manually with per-hop guard + robots re-check.

    Returns (final_response, body_bytes, failure_warning). Exactly one
    of body_bytes / failure_warning is non-None on return; the response
    is returned when one was received (even on failure) so the caller
    can surface its Content-Type.
    """
    current = url
    current_origin = None
    for hop in range(_MAX_REDIRECT_HOPS + 1):
        if hop > 0:
            warning = await _check_url_policy(current, config.fetch_url_allow_private)
            if warning is not None:
                return None, None, warning
        parts = urlsplit(current)
        origin = (parts.scheme, parts.hostname or "", _effective_port(parts))
        if config.fetch_url_respect_robots and origin != current_origin:
            if not await _robots_allows(current, client, config, deadline):
                return None, None, f"Fetch disallowed by robots.txt for {parts.hostname}"
            current_origin = origin

        try:
            request = client.build_request(
                "GET",
                current,
                headers={
                    "User-Agent": config.fetch_url_user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
            )
            resp = await client.send(request, stream=True, follow_redirects=False)
        except _FetchDeadlineExceeded:
            raise
        except Exception as e:
            return None, None, f"Fetch failed: {_plain(e)}"

        try:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    return resp, None, "Fetch failed: redirect response without a Location header"
                current = urljoin(current, location)
                continue
            if not (200 <= resp.status_code < 300):
                return resp, None, f"Fetch failed: HTTP {resp.status_code}"

            cap = config.fetch_url_max_body_bytes
            chunks: list[bytes] = []
            received = 0
            async for chunk in resp.aiter_bytes():
                _remaining(deadline)
                space = cap - received
                if len(chunk) >= space:
                    chunks.append(chunk[:space])
                    received = cap
                    warnings.append(f"Body truncated at {cap} bytes")
                    break
                chunks.append(chunk)
                received += len(chunk)
            return resp, b"".join(chunks), None
        finally:
            await resp.aclose()
    return None, None, f"Fetch failed: redirect limit exceeded ({_MAX_REDIRECT_HOPS} hops)"


def _plain(e: Exception) -> str:
    """Plain-string rendering of network errors — no class names."""
    text = str(e).strip()
    return text if text else "network error"


def _decode_body(body: bytes, response: Optional[httpx.Response]) -> str:
    charset = None
    if response is not None:
        charset = response.charset_encoding
    for encoding in filter(None, (charset, "utf-8")):
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("latin-1")


def _extract(html: str) -> tuple[str, str, dict]:
    """Run trafilatura; returns (text, title, metadata). Never raises."""
    try:
        doc = trafilatura.bare_extraction(html, with_metadata=True)
    except Exception as e:
        log.info("trafilatura extraction raised: %s", e)
        return "", "", {}
    if doc is None:
        return "", "", {}
    text = doc.text or ""
    title = doc.title or ""
    metadata: dict = {}
    # Omit absent fields entirely — the envelope contract emits no nulls.
    if doc.date:
        metadata["published_date"] = doc.date
    if doc.author:
        metadata["author"] = doc.author
    if doc.sitename:
        metadata["site_name"] = doc.sitename
    return text, title, metadata


def _failed(url: str, warnings: list[str], content_type: str = "") -> dict:
    return {
        "url": url,
        "status": "failed",
        "content_type": content_type,
        "title": "",
        "text": "",
        "metadata": {},
        "warnings": warnings,
    }


async def run_fetch_url(url: str, config: Config) -> dict:
    url = (url or "").strip()
    if not url:
        return _failed(url, ["url is required and must be a non-empty string"])

    # Step 1 — cache check.
    try:
        key = fetch_cache.make_key(url)
    except Exception:
        return _failed(url, [f"URL could not be parsed: {url!r}"])
    cached = fetch_cache.get(key)
    if cached is not None:
        log.info("fetch cache HIT url=%r", url)
        return cached

    deadline = time.monotonic() + config.fetch_url_timeout_seconds
    warnings: list[str] = []

    try:
        # Step 2 — URL-policy guard on the initial URL.
        policy_warning = await _check_url_policy(url, config.fetch_url_allow_private)
        if policy_warning is not None:
            log.info("fetch_url blocked url=%r: %s", url, policy_warning)
            return _failed(url, warnings + [policy_warning])

        # Steps 3-5 — robots, fetch with manual redirects, body cap.
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=config.fetch_url_timeout_seconds,
        ) as client:
            response, body, failure = await _fetch_with_redirects(
                url, client, config, deadline, warnings
            )
    except _FetchDeadlineExceeded:
        return _failed(
            url,
            warnings
            + [f"Fetch timed out after {config.fetch_url_timeout_seconds:.0f}s"],
        )
    except Exception as e:  # absolute backstop — never raise to MCP layer
        log.warning("fetch_url unexpected failure url=%r: %s", url, e)
        return _failed(url, warnings + [f"Fetch failed: {_plain(e)}"])

    content_type = ""
    if response is not None:
        content_type = response.headers.get("content-type", "")

    if failure is not None:
        log.info("fetch_url failed url=%r: %s", url, failure)
        return _failed(url, warnings + [failure], content_type)

    # Step 6 — extraction (HTML-ish content only, ratified D-d).
    bare_type = content_type.split(";")[0].strip().lower()
    if bare_type and not any(bare_type == t for t in _EXTRACTABLE_CONTENT_TYPES):
        status = "degraded"
        text, title, metadata = "", "", {}
        warnings.append(f"Content-Type {bare_type} is not extractable HTML")
    else:
        html = _decode_body(body or b"", response)
        text, title, metadata = _extract(html)
        # Step 7 — thin-text threshold.
        if len(text) < THIN_TEXT_THRESHOLD_CHARS:
            status = "degraded"
            warnings.append(
                f"Extracted text is thin ({len(text)} chars) — "
                "page may be JavaScript-rendered"
            )
        else:
            status = "ok"

    # Step 8 — envelope assembly.
    envelope = {
        "url": url,
        "status": status,
        "content_type": content_type,
        "title": title,
        "text": text,
        "metadata": metadata,
        "warnings": warnings,
    }

    # Step 9 — cache write (ok and degraded only; failed is never cached).
    fetch_cache.set(key, envelope)
    log.info(
        "fetch_url done url=%r status=%s content_type=%r text_len=%d warnings=%d",
        url,
        status,
        bare_type,
        len(text),
        len(warnings),
    )
    return envelope
