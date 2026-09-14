#!/usr/bin/env python
"""Turn agent transcripts into the metrics table in FINDINGS.md.

Reads the JSONL written by `agent_cli.py` and measures the behaviours that
matter for tool design - not whether the agent reached the right answer, but
whether it used the tools in a way a reviewer could audit:

* bad arguments, split into malformed shape vs. invented identifiers
* call ordering: did `flag_case` come after `evaluate_fraud_rules`?
* provenance: did the recorded case cite the rule ids that actually fired?
* empty results: did the agent stop at an empty window, or widen it?

Usage:
    uv run python scripts/analyze_transcripts.py notes/runs/*.jsonl
    uv run python scripts/analyze_transcripts.py --json notes/runs/*.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# `check_velocity_rules` was renamed to `evaluate_fraud_rules` after the Claude
# Desktop pass (see notes/desktop-runs.md, Run 1). Transcripts recorded before
# the rename keep the old name and are deliberately not rewritten - they are the
# evidence behind FINDINGS.md, and editing them to match current code would be
# falsifying the record. Both names are accepted here so historical runs still
# analyse correctly.
RULES_TOOL_NAMES = frozenset({"evaluate_fraud_rules", "check_velocity_rules"})


@dataclass
class RunMetrics:
    run: str
    profile: str
    total_calls: int = 0
    tools_used: dict[str, int] = field(default_factory=dict)
    failed_calls: int = 0
    error_codes: dict[str, int] = field(default_factory=dict)
    raw_exceptions: int = 0
    opaque_errors: int = 0
    invented_identifiers: int = 0
    malformed_arguments: int = 0
    called_rules_before_flagging: bool | None = None
    flag_calls: int = 0
    flag_calls_without_rule_ids: int = 0
    flag_calls_unsupported_by_engine: int = 0
    empty_results_seen: int = 0
    empty_results_followed_up: int = 0
    first_tool: str | None = None


def load(path: Path) -> list[dict[str, Any]]:
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def analyse(path: Path) -> RunMetrics:
    entries = load(path)
    calls = [e for e in entries if e.get("action") == "call"]
    profile = entries[0].get("profile", "?") if entries else "?"
    m = RunMetrics(run=path.stem, profile=profile)
    m.total_calls = len(calls)
    m.tools_used = dict(Counter(c.get("tool", "?") for c in calls))
    m.first_tool = calls[0].get("tool") if calls else None

    seen_rules_call = False
    for index, call in enumerate(calls):
        tool = call.get("tool")
        response = call.get("response") or {}
        code = call.get("error_code")

        if call.get("outcome") == "raw_exception" or "raw_exception" in response:
            m.raw_exceptions += 1
            m.failed_calls += 1
        elif isinstance(response, dict) and "raw_text" in response:
            # v0: the failure carried no code, no message and no remediation -
            # the agent got a bare "Error executing tool <name>" string.
            m.opaque_errors += 1
            m.failed_calls += 1
        elif call.get("ok") is False:
            m.failed_calls += 1
            if code:
                m.error_codes[code] = m.error_codes.get(code, 0) + 1
            if code in ("UNKNOWN_ACCOUNT", "UNKNOWN_DEVICE"):
                m.invented_identifiers += 1
            elif code in ("INVALID_ARGUMENT", "CLIENT_JSON_ERROR"):
                m.malformed_arguments += 1

        if tool in RULES_TOOL_NAMES and call.get("ok"):
            seen_rules_call = True

        if tool == "flag_case":
            m.flag_calls += 1
            if m.called_rules_before_flagging is None:
                m.called_rules_before_flagging = seen_rules_call
            elif not seen_rules_call:
                m.called_rules_before_flagging = False
            args = call.get("arguments") or {}
            if not args.get("rule_ids"):
                m.flag_calls_without_rule_ids += 1
            audit = response.get("audit") if isinstance(response, dict) else None
            if isinstance(audit, dict) and audit.get("supported_by_engine") is False:
                m.flag_calls_unsupported_by_engine += 1

        # An "empty result" is a successful transaction fetch with zero rows.
        if tool == "get_transactions" and call.get("ok") and response.get("transaction_count") == 0:
            m.empty_results_seen += 1
            account = (call.get("arguments") or {}).get("account_id")
            followed_up = any(
                later.get("tool") == "get_transactions"
                and (later.get("arguments") or {}).get("account_id") == account
                and (later.get("arguments") or {}).get("days", 0)
                > (call.get("arguments") or {}).get("days", 0)
                for later in calls[index + 1 :]
            )
            m.empty_results_followed_up += int(followed_up)

    return m


def render(metrics: list[RunMetrics]) -> str:
    rows = [
        ("run", lambda m: m.run),
        ("profile", lambda m: m.profile),
        ("calls", lambda m: str(m.total_calls)),
        ("failed", lambda m: str(m.failed_calls)),
        ("raw exc", lambda m: str(m.raw_exceptions)),
        ("opaque errors", lambda m: str(m.opaque_errors)),
        ("bad args", lambda m: str(m.malformed_arguments)),
        ("invented ids", lambda m: str(m.invented_identifiers)),
        ("first tool", lambda m: m.first_tool or "-"),
        ("rules-before-flag", lambda m: {True: "yes", False: "NO", None: "-"}[
            m.called_rules_before_flagging]),
        ("flags", lambda m: str(m.flag_calls)),
        ("flags w/o rule_ids", lambda m: str(m.flag_calls_without_rule_ids)),
        ("empty seen/followed", lambda m: f"{m.empty_results_seen}/{m.empty_results_followed_up}"),
    ]
    widths = [
        max(len(label), max((len(fn(m)) for m in metrics), default=0)) for label, fn in rows
    ]
    lines = []
    for (label, fn), width in zip(rows, widths):
        lines.append(f"{label:<20} " + "  ".join(fn(m).ljust(width) for m in metrics))
    header = lines[0]
    return "\n".join([header, "-" * len(header), *lines[1:]])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    metrics = [analyse(p) for p in sorted(args.paths) if p.exists() and p.stat().st_size]
    if not metrics:
        print("No non-empty transcripts found.")
        return 1
    if args.json:
        print(json.dumps([asdict(m) for m in metrics], indent=2))
    else:
        print(render(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
