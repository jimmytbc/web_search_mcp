"""Phase 0 probe for Phase 3 of web_search_mcp.

Runs four assertions in order. Each prints a PASS/FAIL line.
Exits non-zero if any assertion fails (excluding the warn-only path
in assertion (i) when EXA_API_KEY is unset). Per rule R4, this must
be run before writing any phase code, and the implementer must stop
and surface the specific failure if any assertion fails.

Note on env loading: this probe does NOT read .env (matches the
Phase 2 probe convention — probes test bare external contracts).
To run assertion (ii) live, export EXA_API_KEY in the shell before
invoking the probe, e.g.:

    export EXA_API_KEY=...
    uv run python probes/phase-3-probe.py

Assertions:
  (i)   EXA_API_KEY presence check. Warn-only when missing so the
        probe can still validate the in-process parts.
  (ii)  POST to {EXA_API_BASE}/search with `type: "auto"` and
        `contents: {"highlights": true}` returns parseable JSON whose
        `results[]` carry title and url. Skipped with a note if (i)
        warned.
  (iii) Mode routing — `_select_providers_for_mode` returns the
        correct subset for each of `balanced` / `recall` / `precision`
        given a synthetic enabled-list with all three providers.
        Pure in-process, no network.
  (iv)  Fusion provenance — an Exa-only RawSearchResult flowing
        through normalize → dedupe → rank yields a NormalizedResult
        with `providers == ["exa"]` and `provider_overlap == 1`.
        Pure in-process.
"""

from __future__ import annotations

import os
import sys
import traceback

EXA_API_BASE = os.environ.get("EXA_API_BASE", "https://api.exa.ai")
EXA_PROBE_QUERY = "python"
SEARCH_TIMEOUT_SECONDS = float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "10"))


def _pass(label: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"PASS  {label}{suffix}")


def _fail(label: str, detail: str) -> None:
    print(f"FAIL  {label} — {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"WARN  {label} — {detail}")


def assertion_i_exa_key_present() -> tuple[bool, bool]:
    """Returns (passed, key_present)."""
    label = "(i) EXA_API_KEY env var presence"
    key = os.environ.get("EXA_API_KEY") or None
    if not key:
        _warn(
            label,
            "EXA_API_KEY not set; assertion (ii) will be skipped. "
            "This is allowed — modes that require Exa will degrade "
            "gracefully and the in-process assertions still run.",
        )
        return True, False
    redacted = f"{key[:4]}…{key[-4:]}" if len(key) >= 8 else "set"
    _pass(label, f"EXA_API_KEY present ({redacted})")
    return True, True


def assertion_ii_exa_json(key_present: bool) -> bool:
    label = "(ii) Exa /search returns JSON with results[] shape"
    if not key_present:
        _warn(label, "skipped (EXA_API_KEY not set)")
        return True

    try:
        import httpx
    except Exception as e:
        _fail(label, f"httpx import failed: {e!r}")
        return False

    url = f"{EXA_API_BASE}/search"
    headers = {
        "x-api-key": os.environ["EXA_API_KEY"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = {
        "query": EXA_PROBE_QUERY,
        "numResults": 3,
        "type": "auto",
        "contents": {"highlights": True},
    }
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

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        _fail(label, f"'results' missing, not a list, or empty; top-level keys={list(payload)}")
        return False

    first = results[0]
    required = ("title", "url")
    missing = [f for f in required if f not in first]
    if missing:
        _fail(label, f"first result missing fields: {missing}; keys={list(first)}")
        return False

    _pass(label, f"got {len(results)} results; first has {required}")
    return True


def assertion_iii_mode_routing() -> bool:
    label = "(iii) _select_providers_for_mode returns correct subset per mode"
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from tools.search_web import _select_providers_for_mode  # noqa: WPS433
    except Exception as e:
        _fail(label, f"import failed: {e!r}")
        return False

    class _Fake:
        def __init__(self, name: str) -> None:
            self.name = name

    available = [_Fake("serper"), _Fake("brave"), _Fake("exa")]
    expected: dict[str, list[str]] = {
        "balanced": ["serper", "brave", "exa"],
        "recall": ["serper", "exa"],
        "precision": ["brave", "exa"],
    }

    failures: list[str] = []
    for mode, want in expected.items():
        try:
            selected, warnings = _select_providers_for_mode(mode, available)
        except Exception as e:
            failures.append(f"mode={mode} raised {e!r}")
            continue
        got = [p.name for p in selected]
        if got != want:
            failures.append(f"mode={mode} got {got}, expected {want}")
        if warnings:
            failures.append(
                f"mode={mode} unexpected warnings with full enabled set: {warnings}"
            )

    if failures:
        for f in failures:
            print(f"      · {f}")
        _fail(label, f"{len(failures)} routing case(s) failed")
        return False

    _pass(label, "balanced/recall/precision all map to expected provider subsets")
    return True


def assertion_iv_exa_fusion_provenance() -> bool:
    label = "(iv) Exa-only result carries providers=['exa'] and provider_overlap=1"
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

    raw = RawSearchResult(
        provider="exa",
        raw_rank=0,
        title="Python (programming language)",
        url="https://en.wikipedia.org/wiki/Python_(programming_language)",
        snippet="Python is a high-level programming language ...",
        published_date=None,
        extra={"score": 0.42, "id": "exa-id", "author": None},
    )

    try:
        normalized = [normalize(raw)]
        deduped = dedupe_by_canonical_url(normalized)
        ranked = rank_results(deduped, recency_window_days=30)
    except Exception as e:
        _fail(label, f"fusion pipeline raised: {e!r}\n{traceback.format_exc()}")
        return False

    if len(ranked) != 1:
        _fail(label, f"expected 1 result after fusion, got {len(ranked)}")
        return False

    out = ranked[0]
    if list(out.providers) != ["exa"]:
        _fail(label, f"providers={out.providers!r}, expected ['exa']")
        return False
    if out.provider_overlap != 1:
        _fail(label, f"provider_overlap={out.provider_overlap}, expected 1")
        return False

    _pass(
        label,
        f"providers={out.providers}, provider_overlap={out.provider_overlap}, "
        f"rank_score={out.rank_score:.3f}",
    )
    return True


def main() -> int:
    print("=" * 64)
    print("Phase 3 probe — web_search_mcp")
    print(f"EXA_API_BASE           = {EXA_API_BASE}")
    print(f"SEARCH_TIMEOUT_SECONDS = {SEARCH_TIMEOUT_SECONDS}")
    print("=" * 64)

    i_pass, key_present = assertion_i_exa_key_present()
    ii_pass = assertion_ii_exa_json(key_present)
    iii_pass = assertion_iii_mode_routing()
    iv_pass = assertion_iv_exa_fusion_provenance()

    results = [i_pass, ii_pass, iii_pass, iv_pass]
    passed = sum(1 for r in results if r)
    total = len(results)

    print("-" * 64)
    if passed == total:
        print(f"SUMMARY: {passed}/{total} assertions passed. Phase 0 gate OPEN.")
        if not key_present:
            print("NOTE: EXA_API_KEY was not set — assertion (ii) was skipped.")
        return 0
    print(f"SUMMARY: {passed}/{total} assertions passed. Phase 0 gate CLOSED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
