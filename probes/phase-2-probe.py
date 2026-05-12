"""Phase 0 probe for Phase 2 of web_search_mcp.

Runs four assertions in order. Each prints a PASS/FAIL line.
Exits non-zero if any assertion fails (excluding the warn-only path
in assertion i when BRAVE_API_KEY is unset). Per rule R4, this must
be run before writing any phase code, and the implementer must stop
and surface the specific failure if any assertion fails.

Assertions:
  (i)   BRAVE_API_KEY presence check. Warn-only when missing so the
        probe can still validate the Phase 1 parts of the stack.
  (ii)  GET to Brave /res/v1/web/search with the key and a known-good
        query returns JSON with web.results[] carrying title, url,
        description. Skipped with a note if (i) warned.
  (iii) asyncio.gather with per-coroutine timeout returns the fast
        marker and drops the slow coroutine — proves the parallel
        orchestration + timeout pattern we'll use in the search_web
        handler.
  (iv)  fusion.canonicalize.canonicalize_url collapses tracking-param
        variants (utm_source, fbclid, gclid, ref), lowercases scheme
        and host, and strips trailing slash except root.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

BRAVE_API_BASE = os.environ.get("BRAVE_API_BASE", "https://api.search.brave.com")
BRAVE_PROBE_QUERY = "python"
SEARCH_TIMEOUT_SECONDS = float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "10"))
# Keep the slow coroutine's sleep > SEARCH_TIMEOUT_SECONDS but cap its
# absolute upper bound so the probe itself stays quick.
_SLOW_SLEEP = min(SEARCH_TIMEOUT_SECONDS + 2.0, 12.0)


def _pass(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} — {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"WARN  {label} — {detail}")


def assertion_i_brave_key_present() -> tuple[bool, bool]:
    """Returns (passed, key_present)."""
    label = "(i) BRAVE_API_KEY env var presence"
    key = os.environ.get("BRAVE_API_KEY") or None
    if not key:
        _warn(
            label,
            "BRAVE_API_KEY not set; assertion (ii) will be skipped. "
            "This is allowed — modes that require Brave will degrade "
            "gracefully and the in-process assertions still run.",
        )
        return True, False
    redacted = f"{key[:4]}…{key[-4:]}" if len(key) >= 8 else "set"
    _pass(label, f"BRAVE_API_KEY present ({redacted})")
    return True, True


def assertion_ii_brave_json(key_present: bool) -> bool:
    label = "(ii) Brave /res/v1/web/search returns JSON with web.results[] shape"
    if not key_present:
        _warn(label, "skipped (BRAVE_API_KEY not set)")
        return True

    try:
        import httpx
    except Exception as e:
        _fail(label, f"httpx import failed: {e!r}")
        return False

    url = f"{BRAVE_API_BASE}/res/v1/web/search"
    headers = {
        "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    params = {"q": BRAVE_PROBE_QUERY, "count": 3}
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=SEARCH_TIMEOUT_SECONDS)
    except Exception as e:
        _fail(label, f"GET {url} raised: {e!r}")
        return False

    if resp.status_code != 200:
        _fail(label, f"HTTP {resp.status_code} from {url} body={resp.text[:200]!r}")
        return False

    try:
        payload = resp.json()
    except Exception as e:
        _fail(label, f"response not JSON: {e!r}")
        return False

    web = payload.get("web")
    if not isinstance(web, dict):
        _fail(label, f"'web' key missing or not an object; top-level keys={list(payload)}")
        return False
    results = web.get("results")
    if not isinstance(results, list) or not results:
        _fail(label, f"'web.results' missing, not a list, or empty; web keys={list(web)}")
        return False

    first = results[0]
    required = ("title", "url", "description")
    missing = [f for f in required if f not in first]
    if missing:
        _fail(label, f"first result missing fields: {missing}; keys={list(first)}")
        return False

    _pass(label, f"got {len(results)} results; first has {required}")
    return True


def assertion_iii_gather_timeout() -> bool:
    label = "(iii) asyncio.gather with per-coroutine timeout drops slow coroutine"

    async def fast() -> str:
        await asyncio.sleep(0.1)
        return "fast-marker"

    async def slow() -> str:
        await asyncio.sleep(_SLOW_SLEEP)
        return "slow-marker"

    async def bounded(coro):
        try:
            return await asyncio.wait_for(coro, timeout=SEARCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return asyncio.TimeoutError()

    async def run() -> tuple[list, float]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        outs = await asyncio.gather(
            bounded(fast()),
            bounded(slow()),
            return_exceptions=True,
        )
        elapsed = loop.time() - start
        return outs, elapsed

    try:
        outs, elapsed = asyncio.run(run())
    except Exception as e:
        _fail(label, f"asyncio.run raised: {e!r}\n{traceback.format_exc()}")
        return False

    # The fast coroutine must return its marker; the slow must surface as
    # a TimeoutError sentinel. Elapsed must be within the timeout window
    # (with a small overhead allowance).
    if outs[0] != "fast-marker":
        _fail(label, f"fast coroutine did not return marker; got {outs[0]!r}")
        return False
    if not isinstance(outs[1], asyncio.TimeoutError):
        _fail(label, f"slow coroutine did not time out; got {outs[1]!r}")
        return False
    upper = SEARCH_TIMEOUT_SECONDS + 2.0
    if elapsed > upper:
        _fail(label, f"gather elapsed {elapsed:.2f}s exceeds window {upper:.2f}s")
        return False

    _pass(
        label,
        f"fast returned marker, slow timed out, elapsed={elapsed:.2f}s "
        f"(window={SEARCH_TIMEOUT_SECONDS:.1f}s)",
    )
    return True


def assertion_iv_canonicalize() -> bool:
    label = "(iv) fusion.canonicalize.canonicalize_url collapses tracking variants"
    try:
        # Ensure the repo root is importable even when the probe is
        # invoked as `python probes/phase-2-probe.py`.
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from fusion.canonicalize import canonicalize_url  # noqa: WPS433
    except Exception as e:
        _fail(
            label,
            f"import failed: {e!r} — fusion/canonicalize.py must exist "
            "before this assertion can pass",
        )
        return False

    clean = "https://example.com/path"
    cases = [
        # Tracking-param strips
        ("https://example.com/path?utm_source=x", clean),
        ("https://example.com/path?fbclid=abc", clean),
        ("https://example.com/path?gclid=xyz", clean),
        ("https://example.com/path?ref=partner", clean),
        ("https://example.com/path?utm_source=x&utm_medium=y&fbclid=z", clean),
        # Scheme / host lowercasing
        ("HTTPS://Example.COM/path", clean),
        # Trailing slash removed except root
        ("https://example.com/path/", clean),
        ("https://example.com/", "https://example.com/"),
        # Non-tracking params survive
        (
            "https://example.com/path?q=hello&utm_source=x",
            "https://example.com/path?q=hello",
        ),
    ]

    failures: list[str] = []
    for raw, expected in cases:
        try:
            got = canonicalize_url(raw)
        except Exception as e:
            failures.append(f"{raw!r} raised {e!r}")
            continue
        if got != expected:
            failures.append(f"{raw!r} -> {got!r}, expected {expected!r}")

    if failures:
        for f in failures:
            print(f"      · {f}")
        _fail(label, f"{len(failures)}/{len(cases)} canonicalize cases failed")
        return False

    _pass(label, f"{len(cases)} canonicalize cases match expected output")
    return True


def main() -> int:
    print("=" * 64)
    print("Phase 2 probe — web_search_mcp")
    print(f"BRAVE_API_BASE         = {BRAVE_API_BASE}")
    print(f"SEARCH_TIMEOUT_SECONDS = {SEARCH_TIMEOUT_SECONDS}")
    print("=" * 64)

    i_pass, key_present = assertion_i_brave_key_present()
    ii_pass = assertion_ii_brave_json(key_present)
    iii_pass = assertion_iii_gather_timeout()
    iv_pass = assertion_iv_canonicalize()

    results = [i_pass, ii_pass, iii_pass, iv_pass]
    passed = sum(1 for r in results if r)
    total = len(results)

    print("-" * 64)
    if passed == total:
        print(f"SUMMARY: {passed}/{total} assertions passed. Phase 0 gate OPEN.")
        if not key_present:
            print("NOTE: BRAVE_API_KEY was not set — assertion (ii) was skipped.")
        return 0
    print(f"SUMMARY: {passed}/{total} assertions passed. Phase 0 gate CLOSED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
