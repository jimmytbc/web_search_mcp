# Follow-ups - out-of-scope items for operator triage

Per `~/.claude/CLAUDE.md` §5.3. Opened 2026-07-30 during the MCP-spec
transport migration (`transport="sse"` → `"http"` in `server.py`; client
endpoint is now `/mcp`).

## Open

### F-1 - SDK pin blocks MCP 2026-07-28

- `pyproject.toml` floor-pins `fastmcp>=3.2.4` (locked 3.2.4, transitive
  `mcp` 1.27.0, protocol ceiling 2025-11-25). Bump deliberately once a
  2026-07-28-capable release is vetted (§7: dependency edits need explicit
  authorisation). The unbounded floor means a casual lock refresh can jump
  majors - consider an upper bound.

### F-2 - venv drift vs uv.lock

- The venv contains both `fastmcp-3.2.4` and `fastmcp_slim-3.3.1` with
  overlapping file records; installed tree does not match `uv.lock`.
  `uv sync --frozen` to restore before any dependency work. (Left untouched
  on 2026-07-30; smoke test ran against the drifted env and passed.)

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
