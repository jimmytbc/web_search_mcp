"""Ad-hoc CLI to run fetch_url and search_health outside Claude Desktop.

Usage:
    uv run python scripts/diag.py fetch <url>
    uv run python scripts/diag.py health

Prints the raw JSON response to stdout. Reads config from .env (via
utils.config) or the shell environment, exactly like the MCP server —
the same code path as production (sibling of scripts/query.py, which
covers search_web). Note: `health` in live mode costs one billable
search call per enabled provider; set SEARCH_HEALTH_DRY_RUN=true for
a free static view.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Make the repo root importable regardless of CWD.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from providers import build_providers  # noqa: E402
from tools.fetch_url import run_fetch_url  # noqa: E402
from tools.search_health import run_search_health  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.logging import configure_logging  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"fetch", "health"}:
        print(__doc__, file=sys.stderr)
        return 2
    command = sys.argv[1]

    configure_logging()
    config = load_config()

    if command == "fetch":
        if len(sys.argv) < 3:
            print(__doc__, file=sys.stderr)
            return 2
        response = asyncio.run(run_fetch_url(sys.argv[2], config))
    else:
        providers = build_providers(config)
        response = asyncio.run(run_search_health(config, providers))

    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
