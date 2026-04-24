"""Ad-hoc CLI to run search_web outside Claude Desktop.

Usage:
    uv run python scripts/query.py "<query>" [max_results] [mode]

Prints the raw JSON response to stdout. Reads config from .env (via
utils.config) or the shell environment, exactly like the MCP server.
Handy for smoke tests, fusion-pipeline debugging, and dumping the
raw fused output when the Claude Desktop UI is inconvenient.
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
from tools.search_web import run_search_web  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.logging import configure_logging  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    query = sys.argv[1]
    max_results = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    mode = sys.argv[3] if len(sys.argv) > 3 else "balanced"

    configure_logging()
    config = load_config()
    providers = build_providers(config)
    response = asyncio.run(
        run_search_web(
            query=query,
            max_results=max_results,
            mode=mode,
            config=config,
            providers=providers,
        )
    )
    print(json.dumps(response, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
