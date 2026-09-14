#!/usr/bin/env python
"""Launcher for MCP clients (Claude Desktop, Claude Code).

Equivalent to the `fraud-mcp` console script, but it puts `src/` on `sys.path`
itself instead of relying on the editable install. That indirection is not
cosmetic: uv was observed to disable this project's editable `.pth` entry after
a source edit, so the console script would fail with `ModuleNotFoundError` until
the next `uv sync --reinstall-package`. An MCP server that intermittently fails
to start is worse than one with a slightly unusual entry point, and a client
config that keeps working across edits is worth more here than purity.

    uv run python scripts/serve.py stdio
    uv run python scripts/serve.py http --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fraud_mcp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
