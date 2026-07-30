# Follow-ups - out-of-scope items for operator triage

Per `~/.claude/CLAUDE.md` §5.3. Opened 2026-07-30 during the MCP-spec
transport migration (`transport="sse"` → `"http"` in `server.py`; client
endpoint is now `/mcp`).

## Open

### F-1 - Native MCP 2026-07-28 waits on fastmcp 4.0 stable

- 2026-07-30: bumped to `fastmcp>=3.4.5,<4` under operator authorisation
  (upper bound added - 4.x is a breaking line). fastmcp 3.x hard-pins
  `mcp<2.0`, so protocol ceiling stays 2025-11-25. The 2026-07-28-native
  line is fastmcp 4.x (requires mcp>=2), currently **beta only**
  (`4.0.0b1`, 2026-07-28). Bump to 4.x when a stable release lands and has
  been vetted. Deprecation-clean and safe within the 12-month window
  meanwhile.

### F-2 - venv drift vs uv.lock ✅ resolved 2026-07-30

- Explained: `fastmcp` is a metapackage over `fastmcp-slim` (the earlier
  fastmcp_slim presence was normal, not corruption). `uv lock && uv sync`
  during the 3.4.5 bump reconciled the environment with the lockfile.

### F-3 - Auth + TLS for the exposed HTTP endpoint

- `docker-compose.yml` publishes `0.0.0.0:8000` with no auth and no TLS, and
  README instructs clients to pass `--allow-http`. Decide mechanism at
  Stage 2 (bearer middleware, reverse proxy/TLS termination, or
  private-network-only).

### F-4 - "Session" cache docstrings are misleading under HTTP

- `utils/cache.py`, `utils/fetch_cache.py`, `tools/fetch_url.py` describe
  TTL caches as "session"-scoped, but scope is process lifetime: under the
  remote deployment all clients share one cache. Harmless today; rename or
  isolate per-session if multi-tenant use is ever intended.
