"""Phase 0 probe — current provider-contract state.

The file name is retained for git-history continuity, but its contents
track the *current* code's contracts, not the historical Phase 1
deliverables. The breadth provider it exercises is now Serper.dev.

Runs five assertions in order. Each prints a PASS/FAIL line. Exits
non-zero if any assertion fails (excluding warn-only paths). Per rule
R4, this must be run before any phase implementation code is written,
and the implementer must stop and surface the specific failure if any
assertion fails.

Assertions:
  (i)   FastMCP installs and exposes a tool-registration API.
  (ii)  SERPER_API_KEY presence check. Warn-only when missing so the
        in-process assertions still run.
  (iii) POST to {SERPER_API_BASE}/search with the key and a known-good
        query returns JSON whose `organic[]` entries carry title /
        link / snippet / position. Skipped with a note if (ii) warned.
  (iv)  A minimal FastMCP stdio server registers a placeholder
        search_web tool and exposes it via the MCP list-tools
        protocol call (verified in-process, not via Claude Desktop).
  (v)   Sort stability — a synthetic result set mixing a Serper-style
        item (`published_date=None`) and a Brave/Exa-style item
        (`published_date=ISO-8601`) flows through dedupe + rank + sort
        without raising `TypeError`, and equal-score ties preserve
        input order (Python list.sort stability under None handling).
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

SERPER_API_BASE = os.environ.get("SERPER_API_BASE", "https://google.serper.dev")
SERPER_PROBE_QUERY = "latest AI news"
SEARCH_TIMEOUT_SECONDS = float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "10"))


def _pass(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} — {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"WARN  {label} — {detail}")


def assertion_i_fastmcp_tool_registration_api() -> bool:
    label = "(i) FastMCP installs and exposes a tool-registration API"
    try:
        import fastmcp
        from fastmcp import FastMCP
    except Exception as e:
        _fail(label, f"import failed: {e!r}")
        return False

    required = {"tool", "add_tool", "list_tools", "run_stdio_async"}
    missing = [m for m in required if not hasattr(FastMCP, m)]
    if missing:
        _fail(label, f"FastMCP missing attrs: {missing}")
        return False

    version = getattr(fastmcp, "__version__", "unknown")
    _pass(label, f"fastmcp=={version}, registration API present")
    return True


def assertion_ii_serper_key_present() -> tuple[bool, bool]:
    """Returns (passed, key_present)."""
    label = "(ii) SERPER_API_KEY env var presence"
    key = os.environ.get("SERPER_API_KEY") or None
    if not key:
        _warn(
            label,
            "SERPER_API_KEY not set; assertion (iii) will be skipped. "
            "This is allowed — modes that require Serper will degrade "
            "gracefully and the in-process assertions still run.",
        )
        return True, False
    redacted = f"{key[:4]}…{key[-4:]}" if len(key) >= 8 else "set"
    _pass(label, f"SERPER_API_KEY present ({redacted})")
    return True, True


def assertion_iii_serper_json(key_present: bool) -> bool:
    label = "(iii) Serper /search returns JSON with organic[] shape"
    if not key_present:
        _warn(label, "skipped (SERPER_API_KEY not set)")
        return True

    try:
        import httpx
    except Exception as e:
        _fail(label, f"httpx import failed: {e!r}")
        return False

    url = f"{SERPER_API_BASE}/search"
    headers = {
        "X-API-KEY": os.environ["SERPER_API_KEY"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = {"q": SERPER_PROBE_QUERY, "num": 3}
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=SEARCH_TIMEOUT_SECONDS)
    except Exception as e:
        _fail(label, f"POST {url} raised: {e!r}")
        return False

    if resp.status_code != 200:
        _fail(label, f"HTTP {resp.status_code} from {url} body={resp.text[:200]!r}")
        return False

    try:
        payload = resp.json()
    except Exception as e:
        _fail(label, f"response not JSON: {e!r}")
        return False

    organic = payload.get("organic")
    if not isinstance(organic, list) or not organic:
        _fail(
            label,
            f"'organic' missing, not a list, or empty; top-level keys={list(payload)}",
        )
        return False

    first = organic[0]
    required = ("title", "link", "snippet", "position")
    missing = [f for f in required if f not in first]
    if missing:
        _fail(label, f"first organic result missing fields: {missing}; keys={list(first)}")
        return False

    _pass(label, f"got {len(organic)} results; first has {required}")
    return True


def assertion_iv_stdio_server_lists_search_web() -> bool:
    label = "(iv) FastMCP stdio server exposes search_web via list-tools"
    try:
        from fastmcp import Client, FastMCP
    except Exception as e:
        _fail(label, f"import failed: {e!r}")
        return False

    async def run() -> list[str]:
        srv = FastMCP(name="phase-1-probe")

        @srv.tool
        def search_web(query: str, max_results: int = 5, mode: str = "balanced") -> dict:
            """Placeholder search_web for probe."""
            return {"query": query, "results": []}

        async with Client(srv) as client:
            tools = await client.list_tools()
            return [t.name for t in tools]

    try:
        names = asyncio.run(run())
    except Exception as e:
        _fail(label, f"in-process client call failed: {e!r}\n{traceback.format_exc()}")
        return False

    if "search_web" not in names:
        _fail(label, f"search_web not in listed tools: {names}")
        return False

    _pass(label, f"listed tools: {names}")
    return True


def assertion_v_sort_stability_with_mixed_dates() -> bool:
    label = (
        "(v) Sort stability — mixed None / ISO published_date flows "
        "through dedupe + rank without TypeError; ties preserve input order"
    )
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from fusion.dedupe import dedupe_by_canonical_url  # noqa: WPS433
        from fusion.normalize import normalize  # noqa: WPS433
        from fusion.rank import rank_results  # noqa: WPS433
        from models.search_result import RawSearchResult  # noqa: WPS433
    except Exception as e:
        _fail(label, f"import failed: {e!r}")
        return False

    # Two distinct URLs so they survive dedupe as separate results.
    # Same raw_rank and no trusted-domain / overlap signals so the
    # base rank score ties at 1.0 unless recency kicks in. The Brave
    # ISO date is far in the past so it does NOT earn the recency
    # bonus — both items therefore tie and stable sort must preserve
    # input order.
    serper_raw = RawSearchResult(
        provider="serper",
        raw_rank=0,
        title="Latest AI news roundup",
        url="https://example.com/serper-ai",
        snippet="A summary of recent AI news.",
        published_date=None,  # Serper sets published_date to None.
        extra={"position": 1, "date": "3 days ago"},
    )
    brave_raw = RawSearchResult(
        provider="brave",
        raw_rank=0,
        title="A look at AI history",
        url="https://example.com/brave-ai-history",
        snippet="Historic context for current AI developments.",
        published_date="2010-01-01T00:00:00Z",  # Old enough to skip recency.
        extra={},
    )

    try:
        normalized = [normalize(serper_raw), normalize(brave_raw)]
        deduped = dedupe_by_canonical_url(normalized)
        ranked = rank_results(deduped, recency_window_days=30)
    except TypeError as e:
        _fail(label, f"TypeError in dedupe/rank pipeline: {e!r}\n{traceback.format_exc()}")
        return False
    except Exception as e:
        _fail(label, f"pipeline raised: {e!r}\n{traceback.format_exc()}")
        return False

    if len(ranked) != 2:
        _fail(label, f"expected 2 results after dedupe+rank, got {len(ranked)}")
        return False

    # Both items should have equal rank_score (no recency / trusted /
    # overlap factors in play); stable sort must preserve input order.
    if ranked[0].rank_score != ranked[1].rank_score:
        _fail(
            label,
            f"expected tied rank_scores, got "
            f"{ranked[0].rank_score!r} vs {ranked[1].rank_score!r} — "
            "stability test premise broken",
        )
        return False
    if ranked[0].url != serper_raw.url:
        _fail(
            label,
            f"stable sort did not preserve input order: "
            f"first url={ranked[0].url!r}, expected {serper_raw.url!r}",
        )
        return False

    _pass(
        label,
        f"both items survived, tied rank_score={ranked[0].rank_score:.3f}, "
        f"input order preserved",
    )
    return True


def main() -> int:
    print("=" * 64)
    print("Phase 0 probe — current provider-contract state")
    print(f"SERPER_API_BASE        = {SERPER_API_BASE}")
    print(f"SEARCH_TIMEOUT_SECONDS = {SEARCH_TIMEOUT_SECONDS}")
    print("=" * 64)

    i_pass = assertion_i_fastmcp_tool_registration_api()
    ii_pass, key_present = assertion_ii_serper_key_present()
    iii_pass = assertion_iii_serper_json(key_present)
    iv_pass = assertion_iv_stdio_server_lists_search_web()
    v_pass = assertion_v_sort_stability_with_mixed_dates()

    results = [i_pass, ii_pass, iii_pass, iv_pass, v_pass]
    passed = sum(1 for r in results if r)
    total = len(results)

    print("-" * 64)
    if passed == total:
        print(f"SUMMARY: {passed}/{total} assertions passed. Phase 0 gate OPEN.")
        if not key_present:
            print("NOTE: SERPER_API_KEY was not set — assertion (iii) was skipped.")
        return 0
    print(f"SUMMARY: {passed}/{total} assertions passed. Phase 0 gate CLOSED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
