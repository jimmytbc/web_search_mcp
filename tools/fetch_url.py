"""The `fetch_url` tool handler — static fetch + main-content extraction.

Phase 4 behavior (phase-4.md TASK 5; ratified hardenings 2026-06-12):
  1.  Cache check (canonical-URL key, utils/fetch_cache.py).
  2.  URL-policy guard: http/https only; unless FETCH_URL_ALLOW_PRIVATE,
      every resolved address must clear the private/local blocklist.
      Resolve-once via socket.getaddrinfo() (AF_UNSPEC); ALL returned
      A/AAAA records are checked — if any is blocked the request is
      rejected before a connection is attempted (fail-closed). Checks:
      ipaddress boolean predicates (is_loopback, is_private,
      is_link_local, is_reserved, is_multicast, is_unspecified) plus
      CGNAT 100.64.0.0/10 and IPv4-mapped-IPv6 (unwrapped via
      .ipv4_mapped and re-checked as IPv4).
  3.  DNS-rebinding defence (patch-dns-rebind.md, 2026-06-13): the
      authoritative IP from step 2 is pinned for the lifetime of the
      connection via a socket.getaddrinfo patch (_pin_dns) around each
      sync httpx request. The original hostname is preserved for the
      Host header and TLS SNI. Same pattern applied to every redirect
      hop and to the robots.txt fetch. Implemented as sync httpx.Client
      in asyncio.to_thread (known-good fallback per the patch brief;
      async httpx does not use the stdlib getaddrinfo path).
  4.  robots.txt check (FETCH_URL_RESPECT_ROBOTS): fetched via sync
      httpx with the same guard + pin, parsed via
      urllib.robotparser .parse() (never .read(), which would fetch
      unguarded). Parsed results are cached per (scheme, host, port)
      for the session. 4xx/5xx/unreachable/missing robots → allowed
      (logged at INFO). Robots is re-checked when a redirect hop
      crosses to a different (scheme, host, port).
  5.  Manual redirects: follow_redirects=False, max 5 hops; resolve-
      check-pin re-runs on every hop target.
  6.  Body cap on the DECOMPRESSED stream (iter_bytes), truncating with
      a warning at FETCH_URL_MAX_BODY_BYTES.
  7.  Extraction via trafilatura (single-pass bare_extraction with
      metadata; trafilatura 2.1.0 verified at install time per R2).
      Non-HTML Content-Type skips extraction with a content-aware
      degraded warning (ratified D-d).
  8.  Thin-text threshold → "degraded" with a count-bearing warning.
  9.  Envelope: {url, status, content_type, title, text, metadata,
      warnings}; status ∈ ok | degraded | failed. `url` echoes the
      caller's input verbatim; `content_type` is the last received
      response's Content-Type header ("" when no response was
      received).
  10. Cache write (ok and degraded only).

The whole network section runs under one monotonic deadline of
FETCH_URL_TIMEOUT_SECONDS — robots fetch, every redirect hop, and the
body read all draw from the same budget.

Error model: never raises to the MCP layer — every failure becomes
status="failed" plus a plain descriptive warning (no stack traces, no
exception class names).
"""

from __future__ import annotations

import asyncio
import contextlib
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


@contextlib.contextmanager
def _pin_dns(hostname: str, pinned_ip: str):
    """Patch socket.getaddrinfo for hostname to always return pinned_ip.

    Only used with sync httpx (httpx.Client) — async httpx uses the
    event-loop resolver, not the stdlib socket function directly.
    Not thread-safe for concurrent calls to the same hostname; acceptable
    because fetch_url is single-URL, operator-paced (patch brief rationale).
    Restores the original function unconditionally in finally.
    """
    real = socket.getaddrinfo
    ip_obj = ipaddress.ip_address(pinned_ip)
    is_v6 = isinstance(ip_obj, ipaddress.IPv6Address)
    af = socket.AF_INET6 if is_v6 else socket.AF_INET

    def _patched(host, port, family=0, type=0, proto=0, flags=0):
        if host == hostname:
            p = port if isinstance(port, int) else 0
            sa = (pinned_ip, p, 0, 0) if is_v6 else (pinned_ip, p)
            return [(af, socket.SOCK_STREAM, 6, "", sa)]
        return real(host, port, family, type, proto, flags)

    socket.getaddrinfo = _patched
    try:
        yield
    finally:
        socket.getaddrinfo = real


def _resolve_check_pin(host: str, allow_private: bool) -> Optional[str]:
    """Resolve host, check ALL returned IPs, return first passing IP string.

    Returns None when allow_private=True (no SSRF check, no pin needed).
    Raises ValueError(warning_message) if any IP is blocked or resolution
    fails (fail-closed). Checks every IP returned by getaddrinfo — a
    multi-homed host with any blocked IP is rejected in full.
    """
    if allow_private:
        # SSRF guard is explicitly disabled; skip DNS and pinning entirely.
        return None

    blocked_warning = f"Fetch blocked: {host} resolves to a private or local address"

    # IP literal — no DNS needed; check and return directly.
    try:
        literal = ipaddress.ip_address(host)
        if _ip_is_blocked(literal):
            raise ValueError(blocked_warning)
        return str(literal)
    except ValueError as e:
        if "Fetch blocked" in str(e):
            raise
        # Not a valid IP literal — proceed to DNS resolution.

    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        raise ValueError(blocked_warning)  # fail closed on resolution error

    if not infos:
        raise ValueError(blocked_warning)

    first_ip: Optional[str] = None
    for info in infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _ip_is_blocked(ip_obj):
            raise ValueError(blocked_warning)  # any blocked IP → reject all
        if first_ip is None:
            first_ip = ip_str

    if first_ip is None:
        raise ValueError(blocked_warning)

    return first_ip


def _sync_fetch_robots(
    scheme: str,
    host: str,
    port: int,
    client: httpx.Client,
    config: Config,
    deadline: float,
    initial_pinned_ip: Optional[str],
) -> Optional[RobotFileParser]:
    """Fetch and parse robots.txt synchronously with per-hop IP pinning.

    Returns None on any failure — caller treats None as allow-all.
    Raises _FetchDeadlineExceeded if the deadline expires mid-fetch.
    """
    netloc = host if port in (80, 443) else f"{host}:{port}"
    current = f"{scheme}://{netloc}/robots.txt"
    current_host = host
    current_pinned_ip = initial_pinned_ip

    for _ in range(_MAX_REDIRECT_HOPS + 1):
        parts = urlsplit(current)
        hop_host = parts.hostname or ""
        if not hop_host:
            return None

        # Re-resolve and re-check on origin change.
        if hop_host != current_host:
            try:
                current_pinned_ip = _resolve_check_pin(
                    hop_host, config.fetch_url_allow_private
                )
            except ValueError as e:
                log.info("robots.txt redirect blocked (%s) — treating as allowed", e)
                return None
            current_host = hop_host

        try:
            pin_ctx = (
                _pin_dns(hop_host, current_pinned_ip)
                if current_pinned_ip is not None
                else contextlib.nullcontext()
            )
            with pin_ctx:
                resp = client.get(
                    current,
                    headers={"User-Agent": config.fetch_url_user_agent},
                    follow_redirects=False,
                    timeout=_remaining(deadline),
                )
        except _FetchDeadlineExceeded:
            raise
        except Exception as e:
            log.info(
                "robots.txt unreachable for %s (%s) — treating as allowed", current, e
            )
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
                current,
                resp.status_code,
            )
            return None

        parser = RobotFileParser()
        # .parse() on decoded lines only — never .read()/.set_url(),
        # which would fetch through urllib unguarded.
        parser.parse(resp.text.splitlines())
        return parser

    log.info(
        "robots.txt for %s://%s exceeded redirect cap — treating as allowed",
        scheme,
        host,
    )
    return None


def _sync_fetch_worker(
    url: str,
    config: Config,
    deadline: float,
    warnings: list[str],
) -> tuple[str, Optional[str], Optional[str]]:
    """Full fetch pipeline: resolve-check-pin + sync httpx.

    Runs in a thread via asyncio.to_thread(). Returns
    (content_type, decoded_html, failure_warning). Exactly one of
    decoded_html / failure_warning is non-None on return.
    Raises _FetchDeadlineExceeded if the deadline expires.
    Never raises any other exception — all failures become failure_warning.
    """
    current = url
    current_origin: Optional[tuple[str, str, int]] = None

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=config.fetch_url_timeout_seconds,
        ) as client:
            for hop in range(_MAX_REDIRECT_HOPS + 1):
                parts = urlsplit(current)
                host = parts.hostname or ""

                if parts.scheme not in {"http", "https"}:
                    return "", None, f"URL scheme not supported: {parts.scheme or '(none)'}"
                if not host:
                    return "", None, "URL has no host"

                # Resolve once, check all IPs, pin to first passing IP.
                try:
                    pinned_ip = _resolve_check_pin(host, config.fetch_url_allow_private)
                except ValueError as e:
                    log.info("fetch_url blocked url=%r: %s", current, e)
                    return "", None, str(e)

                # Robots check — re-check on origin change.
                origin = (parts.scheme, host, _effective_port(parts))
                if config.fetch_url_respect_robots and origin != current_origin:
                    if origin not in _robots_cache:
                        _robots_cache[origin] = _sync_fetch_robots(
                            parts.scheme,
                            host,
                            _effective_port(parts),
                            client,
                            config,
                            deadline,
                            pinned_ip,
                        )
                    parser = _robots_cache[origin]
                    if parser is not None and not parser.can_fetch(
                        config.fetch_url_user_agent, current
                    ):
                        return "", None, f"Fetch disallowed by robots.txt for {host}"
                    current_origin = origin

                # Fetch with pinned DNS.
                try:
                    pin_ctx = (
                        _pin_dns(host, pinned_ip)
                        if pinned_ip is not None
                        else contextlib.nullcontext()
                    )
                    with pin_ctx:
                        request = client.build_request(
                            "GET",
                            current,
                            headers={
                                "User-Agent": config.fetch_url_user_agent,
                                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                            },
                        )
                        resp = client.send(request, stream=True)
                except _FetchDeadlineExceeded:
                    raise
                except Exception as e:
                    return "", None, f"Fetch failed: {_plain(e)}"

                try:
                    content_type = resp.headers.get("content-type", "")

                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return (
                                content_type,
                                None,
                                "Fetch failed: redirect response without a Location header",
                            )
                        current = urljoin(current, location)
                        continue

                    if not (200 <= resp.status_code < 300):
                        return content_type, None, f"Fetch failed: HTTP {resp.status_code}"

                    # Body cap on the decompressed stream.
                    cap = config.fetch_url_max_body_bytes
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in resp.iter_bytes():
                        _remaining(deadline)
                        space = cap - received
                        if len(chunk) >= space:
                            chunks.append(chunk[:space])
                            received = cap
                            warnings.append(f"Body truncated at {cap} bytes")
                            break
                        chunks.append(chunk)
                        received += len(chunk)

                    body = b"".join(chunks)
                    html = _decode_body(body, resp)
                    return content_type, html, None

                finally:
                    resp.close()

        return "", None, f"Fetch failed: redirect limit exceeded ({_MAX_REDIRECT_HOPS} hops)"

    except _FetchDeadlineExceeded:
        raise
    except Exception as e:
        log.warning("fetch_url unexpected failure in sync worker url=%r: %s", url, e)
        return "", None, f"Fetch failed: {_plain(e)}"


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

    # Steps 2-6 — resolve-check-pin, robots, fetch, body cap (sync in thread).
    try:
        content_type, html, failure = await asyncio.to_thread(
            _sync_fetch_worker, url, config, deadline, warnings
        )
    except _FetchDeadlineExceeded:
        return _failed(
            url,
            warnings + [f"Fetch timed out after {config.fetch_url_timeout_seconds:.0f}s"],
        )
    except Exception as e:  # absolute backstop — never raise to MCP layer
        log.warning("fetch_url unexpected failure url=%r: %s", url, e)
        return _failed(url, warnings + [f"Fetch failed: {_plain(e)}"])

    if failure is not None:
        log.info("fetch_url failed url=%r: %s", url, failure)
        return _failed(url, warnings + [failure], content_type)

    # Step 7 — extraction (HTML-ish content only, ratified D-d).
    bare_type = content_type.split(";")[0].strip().lower()
    if bare_type and not any(bare_type == t for t in _EXTRACTABLE_CONTENT_TYPES):
        status = "degraded"
        text, title, metadata = "", "", {}
        warnings.append(f"Content-Type {bare_type} is not extractable HTML")
    else:
        text, title, metadata = _extract(html or "")
        # Step 8 — thin-text threshold.
        if len(text) < THIN_TEXT_THRESHOLD_CHARS:
            status = "degraded"
            warnings.append(
                f"Extracted text is thin ({len(text)} chars) — "
                "page may be JavaScript-rendered"
            )
        else:
            status = "ok"

    # Step 9 — envelope assembly.
    envelope = {
        "url": url,
        "status": status,
        "content_type": content_type,
        "title": title,
        "text": text,
        "metadata": metadata,
        "warnings": warnings,
    }

    # Step 10 — cache write (ok and degraded only; failed is never cached).
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
