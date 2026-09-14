"""Schema profiles: the naive first draft (`v0`) versus the hardened one (`v1`).

Set `FRAUD_MCP_PROFILE=v0` to serve the tools as they were originally written -
terse one-line descriptions, no schema constraints beyond types, no guidance
blocks on empty results, no interpretation contract, no audit of submitted
cases, and exceptions allowed to escape as raw errors.

This exists so the agent-misuse findings in FINDINGS.md are reproducible rather
than retrospective. The experiment was run against `v0`, the failures were
observed, and the fixes became `v1` - the default. Anyone can re-run the
ablation:

    FRAUD_MCP_PROFILE=v0 uv run python scripts/agent_cli.py list-tools

The underlying rules engine is identical in both profiles. Only the *interface*
changes - which is precisely the claim being tested: that most agent misuse of a
tool is a schema and description problem, not a model problem.
"""

from __future__ import annotations

import os


def profile() -> str:
    return os.environ.get("FRAUD_MCP_PROFILE", "v1").lower()


def is_v0() -> bool:
    return profile() == "v0"
