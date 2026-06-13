"""Phase 0 probe for Phase 4 of web_search_mcp.

Runs seven assertions in order. Each prints a PASS/FAIL/WARN/RED line.
Exits non-zero if any assertion FAILs. Per rule R4 this probe is
written before any phase code (TDD gating, ratified by the product
owner 2026-06-12): assertions that exercise Phase 4 deliverables
report RED (expected-red) while the target module or dependency does
not exist yet, and must read PASS on the final acceptance run.

Run profiles:
  - pre-implementation: (vi) must PASS once trafilatura is installed;
    (i)-(v), (vii), (viii) may read RED (module not yet implemented).
    Any FAIL is an unexpected failure — stop and surface.
  - final gate: all eight must read PASS (plus WARN-only skips where
    the live network is unavailable).

Note on env loading: this probe does NOT read .env (matches the
phase-2/3 probe convention — probes test bare external contracts).
Assertion (ii) fetches a live URL; no API keys are required by any
assertion. Override the live target with PHASE4_PROBE_FETCH_URL.

Assertions:
  (i)   fetch_url AND search_health visible via FastMCP list-tools
        (in-process, warn-only — registration is enforced by FastMCP).
  (ii)  Live fetch_url against a known-good static article URL returns
        status="ok" with non-empty text (live; warn-only on network
        failure).
  (iii) search_health (dry-run config, synthetic) returns a payload
        matching the TASK 6 schema exactly: per-provider
        {name, enabled, reachable, auth_ok, last_status, warnings},
        top-level {status, providers, modes}. Offline, hard.
  (iv)  search_health reports recall mode unavailable when the
        synthetic config has no serper or exa key. Offline, hard.
  (v)   robots.txt disallow: with the per-host robots cache seeded
        from a disallow-all fixture, fetch_url returns status="failed"
        with the descriptive robots warning. Offline, hard.
  (vi)  trafilatura imports and extracts non-empty main text from an
        embedded static HTML fixture (R2 gate for the dependency).
        Offline, hard.
  (vii) fetch_url("http://127.0.0.1/") returns status="failed" with
        the private-address blocking warning, with zero network I/O.
        Offline, hard.
  (viii) DNS-rebind guard: fetch_url("http://localhost/") resolves the
        hostname via getaddrinfo, detects the loopback result, and
        returns status="failed" before any TCP connection is attempted.
        Offline, hard.
"""

from __future__ import annotations

import asyncio
import os
import sys

LIVE_FETCH_URL = os.environ.get(
    "PHASE4_PROBE_FETCH_URL",
    "https://en.wikipedia.org/wiki/Python_(programming_language)",
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Fixture for assertions (v) and (vi).
FIXTURE_HTML = (
    "<html><head><title>Probe Fixture Article</title></head><body><article>"
    + "<p>"
    + "This fixture sentence exists so trafilatura has a meaningful body "
    "of main content to extract during the phase four probe run. " * 8
    + "</p></article></body></html>"
)

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def _pass(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} — {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"WARN  {label} — {detail}")


def _red(label: str, detail: str) -> None:
    print(f"RED   {label} — expected-red: {detail}")


def _phase4_modules_missing() -> bool:
    try:
        import tools.fetch_url  # noqa: F401
        import tools.search_health  # noqa: F401
    except ImportError:
        return True
    return False


def _synthetic_config(**overrides):
    """Build a Config with all keys unset unless overridden."""
    from utils.config import Config

    base = dict(
        search_timeout_seconds=5.0,
        default_max_results=5,
        brave_api_base="https://api.search.brave.com",
        brave_api_key=None,
        brave_default_country=None,
        brave_default_search_lang=None,
        brave_safesearch="moderate",
        recency_window_days=30,
        exa_api_base="https://api.exa.ai",
        exa_api_key=None,
        serper_api_base="https://google.serper.dev",
        serper_api_key=None,
        fetch_url_timeout_seconds=5.0,
        fetch_url_max_body_bytes=2_000_000,
        fetch_url_user_agent="web_search_mcp-probe/0.4",
        fetch_url_respect_robots=True,
        fetch_url_allow_private=False,
        search_health_dry_run=True,
    )
    base.update(overrides)
    return Config(**base)


def assertion_i_list_tools(missing: bool) -> bool:
    label = "(i) fetch_url + search_health visible via FastMCP list-tools"
    if missing:
        _red(label, "tools/fetch_url.py / tools/search_health.py not yet implemented")
        return True
    try:
        from fastmcp import Client

        import server as srv_mod

        async def _list() -> list[str]:
            async with Client(srv_mod.mcp) as client:
                tools = await client.list_tools()
                return sorted(t.name for t in tools)

        names = asyncio.run(_list())
    except Exception as e:
        _warn(label, f"in-process list-tools raised: {e!r} (warn-only)")
        return True

    expected = {"fetch_url", "search_health", "search_web"}
    if not expected.issubset(set(names)):
        _warn(label, f"listed tools {names}, expected superset of {sorted(expected)} (warn-only)")
        return True
    _pass(label, f"listed tools: {names}")
    return True


def assertion_ii_live_fetch(missing: bool) -> bool:
    label = "(ii) live fetch_url returns ok + non-empty text"
    if missing:
        _red(label, "tools/fetch_url.py not yet implemented")
        return True
    try:
        from tools.fetch_url import run_fetch_url

        config = _synthetic_config(fetch_url_timeout_seconds=20.0)
        envelope = asyncio.run(run_fetch_url(LIVE_FETCH_URL, config))
    except Exception as e:
        _warn(label, f"live fetch raised: {e!r} (warn-only — network may be unavailable)")
        return True

    if envelope.get("status") != "ok" or not envelope.get("text"):
        _warn(
            label,
            f"status={envelope.get('status')!r} text_len={len(envelope.get('text') or '')} "
            f"warnings={envelope.get('warnings')} (warn-only — live target may have changed; "
            "override with PHASE4_PROBE_FETCH_URL)",
        )
        return True
    _pass(
        label,
        f"status=ok text_len={len(envelope['text'])} title={envelope.get('title')!r:.50}",
    )
    return True


def _dry_run_health_payload() -> dict:
    from tools.search_health import run_search_health

    config = _synthetic_config(brave_api_key="probe-synthetic-key")
    return asyncio.run(run_search_health(config, providers=[]))


def assertion_iii_health_schema(missing: bool, payload: dict | None) -> bool:
    label = "(iii) search_health payload matches TASK 6 schema"
    if missing:
        _red(label, "tools/search_health.py not yet implemented")
        return True
    if payload is None:
        _fail(label, "dry-run search_health raised — see assertion (iv) detail")
        return False

    top_keys = set(payload.keys())
    if top_keys != {"status", "providers", "modes"}:
        _fail(label, f"top-level keys {sorted(top_keys)}, expected ['modes','providers','status']")
        return False
    if payload["status"] not in {"ok", "degraded", "failed"}:
        _fail(label, f"status={payload['status']!r} not in ok|degraded|failed")
        return False
    providers = payload["providers"]
    if not isinstance(providers, list) or not providers:
        _fail(label, "providers is not a non-empty list")
        return False
    want_provider_keys = {"name", "enabled", "reachable", "auth_ok", "last_status", "warnings"}
    for entry in providers:
        if set(entry.keys()) != want_provider_keys:
            _fail(
                label,
                f"provider entry keys {sorted(entry.keys())}, expected {sorted(want_provider_keys)}",
            )
            return False
    modes = payload["modes"]
    if set(modes.keys()) != {"balanced", "recall", "precision"}:
        _fail(label, f"modes keys {sorted(modes.keys())}")
        return False
    for mode_name, mode_entry in modes.items():
        if "available" not in mode_entry or not isinstance(mode_entry["available"], bool):
            _fail(label, f"mode {mode_name} missing boolean 'available'")
            return False
    _pass(label, f"{len(providers)} provider entries; schema keys exact")
    return True


def assertion_iv_mode_unavailable(missing: bool, payload: dict | None) -> bool:
    label = "(iv) search_health reports recall unavailable without serper/exa keys"
    if missing:
        _red(label, "tools/search_health.py not yet implemented")
        return True
    if payload is None:
        _fail(label, "dry-run search_health raised")
        return False

    recall = payload["modes"]["recall"]
    if recall.get("available") is not False:
        _fail(label, f"recall.available={recall.get('available')!r}, expected False")
        return False
    reason = recall.get("reason", "")
    if "SERPER_API_KEY" not in reason and "EXA_API_KEY" not in reason:
        _fail(label, f"recall.reason={reason!r} does not name a missing env var")
        return False
    # precision (brave+exa) must remain available with the synthetic brave key.
    if payload["modes"]["precision"].get("available") is not True:
        _fail(label, "precision.available expected True with synthetic brave key")
        return False
    _pass(label, f"recall unavailable, reason={reason!r:.80}")
    return True


def assertion_v_robots_disallow(missing: bool) -> bool:
    label = "(v) robots.txt disallow returns failed + descriptive warning"
    if missing:
        _red(label, "tools/fetch_url.py not yet implemented")
        return True
    try:
        from urllib.robotparser import RobotFileParser

        import tools.fetch_url as fu

        rp = RobotFileParser()
        rp.parse(ROBOTS_DISALLOW_ALL.splitlines())
        fu._robots_cache.clear()
        fu._robots_cache[("https", "example.com", 443)] = rp

        # allow_private=True skips DNS resolution so this runs offline.
        config = _synthetic_config(fetch_url_allow_private=True)
        envelope = asyncio.run(fu.run_fetch_url("https://example.com/page", config))
    except Exception as e:
        _fail(label, f"raised: {e!r}")
        return False
    finally:
        try:
            fu._robots_cache.clear()
        except Exception:
            pass

    if envelope.get("status") != "failed":
        _fail(label, f"status={envelope.get('status')!r}, expected 'failed'")
        return False
    warnings = envelope.get("warnings") or []
    if not any("disallowed by robots.txt" in w and "example.com" in w for w in warnings):
        _fail(label, f"warnings={warnings!r} missing robots-disallow text")
        return False
    _pass(label, f"failed with warning {warnings[0]!r:.80}")
    return True


def assertion_vi_trafilatura() -> bool:
    label = "(vi) trafilatura extracts non-empty text from static HTML fixture"
    try:
        import trafilatura
    except Exception as e:
        _fail(label, f"import failed: {e!r} — R2 gate closed, stop and surface")
        return False
    try:
        text = trafilatura.extract(FIXTURE_HTML)
    except Exception as e:
        _fail(label, f"extract() raised: {e!r}")
        return False
    if not text or "fixture sentence" not in text:
        _fail(label, f"extract() returned {text!r:.80} — sentinel missing")
        return False
    _pass(label, f"version {trafilatura.__version__}, extracted {len(text)} chars")
    return True


def assertion_vii_private_block(missing: bool) -> bool:
    label = "(vii) http://127.0.0.1/ blocked as private with zero network"
    if missing:
        _red(label, "tools/fetch_url.py not yet implemented")
        return True
    try:
        import socket

        from tools.fetch_url import run_fetch_url

        real_create_connection = socket.create_connection

        def _no_network(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("network I/O attempted during private-block assertion")

        socket.create_connection = _no_network
        try:
            config = _synthetic_config()
            envelope = asyncio.run(run_fetch_url("http://127.0.0.1/", config))
        finally:
            socket.create_connection = real_create_connection
    except AssertionError as e:
        _fail(label, str(e))
        return False
    except Exception as e:
        _fail(label, f"raised: {e!r}")
        return False

    if envelope.get("status") != "failed":
        _fail(label, f"status={envelope.get('status')!r}, expected 'failed'")
        return False
    warnings = envelope.get("warnings") or []
    if not any("private or local address" in w and "127.0.0.1" in w for w in warnings):
        _fail(label, f"warnings={warnings!r} missing private-address text")
        return False
    _pass(label, f"failed with warning {warnings[0]!r:.80}")
    return True


def assertion_viii_dns_rebind_guard(missing: bool) -> bool:
    label = "(viii) DNS-rebind guard: hostname resolving to loopback blocked before connection"
    if missing:
        _red(label, "tools/fetch_url.py not yet patched")
        return True
    try:
        import socket

        from tools.fetch_url import run_fetch_url

        real_create_connection = socket.create_connection

        def _no_network(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("TCP connection attempted — DNS-rebinding guard failed to fire before connect")

        socket.create_connection = _no_network
        try:
            config = _synthetic_config()
            envelope = asyncio.run(run_fetch_url("http://localhost/", config))
        finally:
            socket.create_connection = real_create_connection
    except AssertionError as e:
        _fail(label, str(e))
        return False
    except Exception as e:
        _fail(label, f"raised: {e!r}")
        return False

    if envelope.get("status") != "failed":
        _fail(label, f"status={envelope.get('status')!r}, expected 'failed'")
        return False
    warnings = envelope.get("warnings") or []
    if not any("private or local address" in w and "localhost" in w for w in warnings):
        _fail(label, f"warnings={warnings!r} missing private-address text for localhost")
        return False
    _pass(label, f"failed with warning {warnings[0]!r:.80} — no TCP connection made")
    return True


def main() -> int:
    print("=" * 64)
    print("Phase 4 probe — web_search_mcp")
    print(f"LIVE_FETCH_URL = {LIVE_FETCH_URL}")
    print("=" * 64)

    missing = _phase4_modules_missing()
    if missing:
        print("NOTE: Phase 4 modules not yet present — pre-implementation profile.")

    health_payload: dict | None = None
    if not missing:
        try:
            health_payload = _dry_run_health_payload()
        except Exception as e:
            print(f"      · dry-run search_health raised: {e!r}")

    results = [
        assertion_i_list_tools(missing),
        assertion_ii_live_fetch(missing),
        assertion_iii_health_schema(missing, health_payload),
        assertion_iv_mode_unavailable(missing, health_payload),
        assertion_v_robots_disallow(missing),
        assertion_vi_trafilatura(),
        assertion_vii_private_block(missing),
        assertion_viii_dns_rebind_guard(missing),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)

    print("-" * 64)
    if passed == total:
        profile = "pre-implementation (RED allowed)" if missing else "final gate"
        print(f"SUMMARY: {passed}/{total} assertions clean [{profile}]. Phase 0 gate OPEN.")
        return 0
    failed_count = total - passed
    print(f"SUMMARY: {passed}/{total} assertions clean ({failed_count} failed). Phase 0 gate CLOSED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
