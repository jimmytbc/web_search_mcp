"""The `search_health` tool handler — provider connectivity / auth /
mode-availability report.

Phase 4 behavior (phase-4.md TASK 6; ratified decisions 2026-06-12):
  - Live mode (default): one minimal probe search per ENABLED provider
    ("ping", max_results=1), in parallel via asyncio.gather with a
    per-provider soft timeout of SEARCH_TIMEOUT_SECONDS — the same
    pattern as tools/search_web.py. Calling provider.search() is
    sanctioned contact; the provider modules themselves are frozen.
  - Dry-run mode (SEARCH_HEALTH_DRY_RUN=true): no live calls;
    reachable/auth_ok stay null and last_status="not probed".
  - Classification (ratified D-g): the HTTP status code is recovered
    from the provider error message's "returned HTTP <code>" token
    (providers/brave.py, exa.py, serper.py — frozen contract this
    module is deliberately coupled to; see NOTES.md):
      HTTP 401/403/422  -> reachable=true,  auth_ok=false
      HTTP 429          -> reachable=true,  auth_ok=true  (key worked;
                           quota is a different failure) + warning
      other HTTP / non-JSON -> reachable=true,  auth_ok=null + warning
      timeout            -> reachable=false, auth_ok=null
      network error      -> reachable=false, auth_ok=null
      success            -> reachable=true,  auth_ok=true
    The blanket 401/403/422 -> auth-failure mapping across providers is
    Brave-derived lore applied uniformly (brief TASK 6); noted in
    NOTES.md as a Phase 5 per-provider refinement candidate.
  - `reachable` and `auth_ok` are nullable booleans by contract.
  - Mode availability (ratified D-h): available := the intersection of
    _MODE_ROUTING[mode] with the enabled providers is non-empty —
    derived from config only, identically in live and dry-run; probe
    outcomes never change mode availability. A reason string naming
    the missing providers and env vars is emitted whenever the mode's
    wanted set is incompletely enabled.
  - Top-level status:
      live:    failed   = no enabled provider is usable
                          (usable := reachable and auth_ok is not false);
               ok       = every enabled provider probed reachable+auth_ok;
               degraded = otherwise. Zero enabled providers = failed.
      dry-run (ratified D-i): failed iff zero providers enabled;
               degraded iff any mode unavailable; ok otherwise.
  - last_status vocabulary: "HTTP <code>" | "timeout" |
    "network error" | "not probed".
  - No result caching and no probe cooldown in v1 (operator-invoked,
    not agent-looped; cooldown is a NOTES.md Phase 5 candidate). Every
    live invocation costs one billable search call per enabled
    provider (Exa's adapter additionally requests contents/highlights
    — frozen adapter contract).
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import httpx

from providers.base import SearchProvider
from providers.brave import BraveError
from providers.exa import ExaError
from providers.serper import SerperError
from tools.search_web import _MODE_ROUTING, _PROVIDER_REQUIRED_ENV
from utils.config import Config
from utils.logging import get_logger

log = get_logger(__name__)

# Fixed probe query — referenced by probes/phase-4-probe.py.
PROBE_QUERY = "ping"
_PROBE_MAX_RESULTS = 1

_PROVIDER_ERRORS = (BraveError, ExaError, SerperError)
_HTTP_CODE_RE = re.compile(r"returned HTTP (\d{3})")
_AUTH_FAILURE_CODES = {401, 403, 422}

# Canonical provider order for report entries (matches the
# build_providers registration order in providers/__init__.py).
_PROVIDER_ORDER = ("brave", "exa", "serper")


def _enabled_map(config: Config) -> dict[str, bool]:
    return {
        "brave": config.brave_enabled,
        "exa": config.exa_enabled,
        "serper": config.serper_enabled,
    }


def _mode_availability(config: Config) -> dict[str, dict]:
    """Mode availability from config/enabled flags only (ratified D-h)."""
    enabled = {name for name, on in _enabled_map(config).items() if on}
    modes: dict[str, dict] = {}
    for mode, wanted in _MODE_ROUTING.items():
        present = wanted & enabled
        missing = sorted(wanted - enabled)
        entry: dict = {"available": bool(present)}
        if missing:
            clauses = []
            for name in missing:
                env_var = _PROVIDER_REQUIRED_ENV.get(name)
                env_clause = f" ({env_var} not set)" if env_var else ""
                clauses.append(f"{name} not enabled{env_clause}")
            entry["reason"] = "; ".join(clauses)
        modes[mode] = entry
    return modes


def _classify_provider_error(name: str, error: Exception) -> tuple[bool, Optional[bool], str, list[str]]:
    """Map a provider exception to (reachable, auth_ok, last_status, warnings)."""
    match = _HTTP_CODE_RE.search(str(error))
    if match:
        code = int(match.group(1))
        last_status = f"HTTP {code}"
        if code in _AUTH_FAILURE_CODES:
            return True, False, last_status, [
                f"{name} authentication failed (HTTP {code})"
            ]
        if code == 429:
            return True, True, last_status, [f"{name} rate limited (HTTP 429)"]
        return True, None, last_status, [f"{name} returned HTTP {code}"]
    # Provider error without an HTTP token (e.g. non-JSON response body).
    return True, None, "network error", [f"{name} probe failed: {error}"]


async def _probe_provider(
    provider: SearchProvider,
    timeout: float,
) -> dict:
    """Probe one provider. Self-contained — never raises."""
    name = provider.name
    warnings: list[str] = []
    try:
        await asyncio.wait_for(
            provider.search(PROBE_QUERY, _PROBE_MAX_RESULTS),
            timeout=timeout,
        )
        reachable: Optional[bool] = True
        auth_ok: Optional[bool] = True
        last_status = "HTTP 200"
    except asyncio.TimeoutError:
        reachable, auth_ok, last_status = False, None, "timeout"
        warnings.append(f"{name} timed out after {timeout:.1f}s")
    except _PROVIDER_ERRORS as e:
        reachable, auth_ok, last_status, warnings = _classify_provider_error(name, e)
    except httpx.TransportError as e:
        reachable, auth_ok, last_status = False, None, "network error"
        warnings.append(f"{name} unreachable: {e}")
    except Exception as e:  # backstop — classify as undetermined
        reachable, auth_ok, last_status = True, None, "network error"
        warnings.append(f"{name} probe failed: {e}")

    return {
        "name": name,
        "enabled": True,
        "reachable": reachable,
        "auth_ok": auth_ok,
        "last_status": last_status,
        "warnings": warnings,
    }


def _disabled_entry(name: str) -> dict:
    env_var = _PROVIDER_REQUIRED_ENV.get(name)
    env_clause = f" ({env_var} not set)" if env_var else ""
    return {
        "name": name,
        "enabled": False,
        "reachable": None,
        "auth_ok": None,
        "last_status": "not probed",
        "warnings": [f"{name} not enabled{env_clause}"],
    }


def _not_probed_entry(name: str, enabled: bool) -> dict:
    if not enabled:
        return _disabled_entry(name)
    return {
        "name": name,
        "enabled": True,
        "reachable": None,
        "auth_ok": None,
        "last_status": "not probed",
        "warnings": [],
    }


def _top_level_status_live(entries: list[dict]) -> str:
    enabled_entries = [e for e in entries if e["enabled"]]
    if not enabled_entries:
        return "failed"
    usable = [
        e for e in enabled_entries
        if e["reachable"] is True and e["auth_ok"] is not False
    ]
    if not usable:
        return "failed"
    if all(e["reachable"] is True and e["auth_ok"] is True for e in enabled_entries):
        return "ok"
    return "degraded"


def _top_level_status_dry_run(config: Config, modes: dict[str, dict]) -> str:
    if not any(_enabled_map(config).values()):
        return "failed"
    if any(not entry["available"] for entry in modes.values()):
        return "degraded"
    return "ok"


async def run_search_health(
    config: Config,
    providers: list[SearchProvider],
) -> dict:
    enabled = _enabled_map(config)
    modes = _mode_availability(config)

    if config.search_health_dry_run:
        entries = [_not_probed_entry(name, enabled[name]) for name in _PROVIDER_ORDER]
        status = _top_level_status_dry_run(config, modes)
        log.info("search_health dry-run status=%s", status)
        return {"status": status, "providers": entries, "modes": modes}

    by_name = {p.name: p for p in providers}
    probe_targets = [by_name[name] for name in _PROVIDER_ORDER if name in by_name]
    probed = await asyncio.gather(
        *[_probe_provider(p, config.search_timeout_seconds) for p in probe_targets],
    )
    probed_by_name = {entry["name"]: entry for entry in probed}

    entries = []
    for name in _PROVIDER_ORDER:
        if name in probed_by_name:
            entries.append(probed_by_name[name])
        else:
            entries.append(_disabled_entry(name))

    status = _top_level_status_live(entries)
    log.info(
        "search_health done status=%s providers=%s",
        status,
        {e["name"]: e["last_status"] for e in entries},
    )
    return {"status": status, "providers": entries, "modes": modes}
