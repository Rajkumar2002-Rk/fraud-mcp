#!/usr/bin/env python
"""Drive the MCP server through end-to-end investigation scenarios.

This is a *scripted* client, not an agent: every tool call is fixed, so the run
is reproducible and can assert on the results. It exists to prove the server
behaves correctly over a real MCP connection - including the failure paths that
a happy-path demo would never touch.

The agent-driven counterpart (where a model chooses the calls, and gets them
wrong) is what FINDINGS.md documents.

Usage:
    uv run python scripts/run_scenarios.py          # human-readable transcript
    uv run python scripts/run_scenarios.py --json   # machine-readable results
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp import ClientSession  # noqa: E402
from mcp.client._memory import InMemoryTransport  # noqa: E402

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    else ("", "", "", "", "", "")
)


@dataclass
class Step:
    """One tool call plus the assertions that must hold on its response."""

    tool: str
    arguments: dict[str, Any]
    why: str
    expect: list[tuple[str, Any]] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    premise: str
    teaches: str
    steps: list[Step]


def dig(payload: Any, path: str) -> Any:
    """Read a dotted path like 'verdict.fired_rule_ids' out of a response."""
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.get(part) if isinstance(current, dict) else None
        if current is None:
            return None
    return current


SCENARIOS: list[Scenario] = [
    Scenario(
        name="velocity-burst",
        premise="A customer reports charges they do not recognise on ACC-1007.",
        teaches="The ordinary happy path: verdict first, then evidence, then a case.",
        steps=[
            Step("evaluate_fraud_rules", {"account_id": "ACC-1007"},
                 "Get the authoritative verdict before looking at anything else.",
                 [("ok", True), ("verdict.fired_rule_ids.0", "VELOCITY_BURST"),
                  ("verdict.highest_severity_fired", "high")]),
            Step("get_transactions", {"account_id": "ACC-1007", "days": 7},
                 "Pull the transactions the fired rule cites.",
                 [("ok", True)]),
            Step("flag_case",
                 {"account_id": "ACC-1007",
                  "reason": ("VELOCITY_BURST fired: 7 transactions in 4.7 minutes against a "
                             "threshold of 5 in 10, escalating from 1.00 to 1890.00 USD - the "
                             "signature of card testing followed by a cash-out."),
                  "severity": "high", "rule_ids": ["VELOCITY_BURST", "AMOUNT_SPIKE"]},
                 "Record the outcome, citing the rules that fired.",
                 [("ok", True), ("audit.supported_by_engine", True)]),
        ],
    ),
    Scenario(
        name="account-takeover-pivot",
        premise="ACC-1013 shows a large purchase from an unfamiliar country.",
        teaches="Pivoting from account to device to find the other victims.",
        steps=[
            Step("evaluate_fraud_rules", {"account_id": "ACC-1013"},
                 "Verdict first.",
                 [("ok", True), ("verdict.highest_severity_fired", "critical")]),
            Step("lookup_device_history", {"device_id": "DEV-ATO-01"},
                 "Pivot on the device named in the SHARED_DEVICE evidence.",
                 [("ok", True), ("distinct_accounts_with_events", 3),
                  ("password_reset_count", 3)]),
            Step("evaluate_fraud_rules", {"account_id": "ACC-1014"},
                 "Check a co-located account surfaced by the device lookup.",
                 [("ok", True)]),
        ],
    ),
    Scenario(
        name="structuring",
        premise="Compliance asks whether ACC-1021 is splitting payments.",
        teaches="A CRITICAL finding whose evidence is a set of individually boring rows.",
        steps=[
            Step("evaluate_fraud_rules", {"account_id": "ACC-1021"},
                 "The rule, not the model, decides that this pattern is structuring.",
                 [("ok", True), ("verdict.highest_severity_fired", "critical")]),
            Step("get_transactions", {"account_id": "ACC-1021", "days": 5},
                 "Confirm the individual amounts sit just under the threshold.",
                 [("ok", True)]),
        ],
    ),
    Scenario(
        name="true-negative",
        premise="Routine review of ACC-1002, which has no planted pattern.",
        teaches="A clean account must come back clean - no rule may fire on noise.",
        steps=[
            Step("evaluate_fraud_rules", {"account_id": "ACC-1002"},
                 "Expect an empty fired list and a null severity.",
                 [("ok", True), ("verdict.rules_fired", 0),
                  ("verdict.highest_severity_fired", None)]),
        ],
    ),
    Scenario(
        name="empty-window-trap",
        premise="ACC-1009 is reviewed with a 30-day window. It is dormant.",
        teaches=("The trap. Zero rows is not an all-clear, and four rules return SKIPPED "
                 "rather than a pass. Both facts are stated in the payload."),
        steps=[
            Step("get_transactions", {"account_id": "ACC-1009", "days": 30},
                 "An empty result that must not be read as 'clean'.",
                 [("ok", True), ("transaction_count", 0),
                  ("empty_result_guidance.total_transactions_all_time", 9)]),
            Step("evaluate_fraud_rules", {"account_id": "ACC-1009"},
                 "Rules that cannot be evaluated report SKIPPED, not NOT_FIRED.",
                 [("ok", True), ("verdict.rules_fired", 0),
                  ("verdict.skipped_rule_ids.0", "AMOUNT_SPIKE")]),
            Step("get_transactions", {"account_id": "ACC-1009", "days": 365},
                 "Widening the window, as the guidance instructs, finds the history.",
                 [("ok", True)]),
        ],
    ),
    Scenario(
        name="error-handling",
        premise="The client sends malformed and out-of-range arguments.",
        teaches="Every failure is a structured envelope with a remediation, never a traceback.",
        steps=[
            Step("evaluate_fraud_rules", {"account_id": "ACC-9999"},
                 "Unknown but well-formed account id.",
                 [("ok", False), ("error.code", "UNKNOWN_ACCOUNT")]),
            Step("get_transactions", {"account_id": "ACC-1013", "days": 9999},
                 "Out-of-range window, rejected by the schema and normalised by middleware.",
                 [("ok", False), ("error.code", "INVALID_ARGUMENT")]),
            Step("lookup_device_history", {"device_id": "ACC-1013"},
                 "An account id passed where a device id belongs - a real agent mistake.",
                 [("ok", False), ("error.code", "INVALID_ARGUMENT")]),
            Step("flag_case", {"account_id": "ACC-1002",
                               "reason": "Looks suspicious to me on general principles.",
                               "severity": "critical"},
                 "Escalating a clean account with no cited rules: recorded, but audited.",
                 [("ok", True), ("audit.supported_by_engine", False)]),
        ],
    ),
]


async def call(session: ClientSession, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def run(as_json: bool) -> int:
    from fraud_mcp.server import server

    failures = 0
    report: list[dict[str, Any]] = []

    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for scenario in SCENARIOS:
                if not as_json:
                    print(f"\n{BOLD}=== {scenario.name} ==={RESET}")
                    print(f"{DIM}premise: {scenario.premise}{RESET}")
                    print(f"{DIM}teaches: {scenario.teaches}{RESET}")
                steps_out = []
                for step in scenario.steps:
                    payload = await call(session, step.tool, step.arguments)
                    checks = []
                    for path, expected in step.expect:
                        actual = dig(payload, path)
                        passed = actual == expected
                        failures += not passed
                        checks.append({"path": path, "expected": expected,
                                       "actual": actual, "passed": passed})
                    if not as_json:
                        args = json.dumps(step.arguments)
                        print(f"\n  {BOLD}{step.tool}{RESET}({DIM}{args}{RESET})")
                        print(f"  {DIM}why: {step.why}{RESET}")
                        print(f"  {summarise(step.tool, payload)}")
                        for c in checks:
                            mark = f"{GREEN}PASS{RESET}" if c["passed"] else f"{RED}FAIL{RESET}"
                            print(f"    [{mark}] {c['path']} == {c['expected']!r}"
                                  + ("" if c["passed"] else f"  (got {c['actual']!r})"))
                    steps_out.append({"tool": step.tool, "arguments": step.arguments,
                                      "why": step.why, "checks": checks, "response": payload})
                report.append({"scenario": scenario.name, "premise": scenario.premise,
                               "teaches": scenario.teaches, "steps": steps_out})

    if as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        total = sum(len(s.expect) for sc in SCENARIOS for s in sc.steps)
        colour = GREEN if not failures else RED
        print(f"\n{colour}{BOLD}{total - failures}/{total} assertions passed"
              f"{RESET} across {len(SCENARIOS)} scenarios.")
    return 1 if failures else 0


def summarise(tool: str, payload: dict[str, Any]) -> str:
    """One-line human summary of a response, so the transcript stays readable."""
    if not payload.get("ok", True):
        err = payload["error"]
        return f"{RED}error{RESET} {err['code']}: {err['message']}"
    if tool == "evaluate_fraud_rules":
        v = payload["verdict"]
        fired = ", ".join(v["fired_rule_ids"]) or "none"
        skipped = ", ".join(v["skipped_rule_ids"])
        out = f"fired: {YELLOW}{fired}{RESET} | severity: {v['highest_severity_fired']}"
        return out + (f" | {DIM}skipped: {skipped}{RESET}" if skipped else "")
    if tool == "get_transactions":
        s = payload["summary"]
        base = (f"{payload['transaction_count']} txns, {s['total_amount']:.2f} USD, "
                f"countries: {','.join(s['countries']) or '-'}")
        return base + (f" {YELLOW}[empty-result guidance attached]{RESET}"
                       if "empty_result_guidance" in payload else "")
    if tool == "lookup_device_history":
        return (f"{payload['distinct_accounts_with_events']} accounts, "
                f"{payload['event_count']} events, "
                f"{payload['failed_login_count']} failed logins, "
                f"{payload['password_reset_count']} password resets")
    if tool == "flag_case":
        warn = payload["audit"]["warnings"]
        return (f"{payload['case_id']} severity={payload['severity']} "
                + (f"{GREEN}engine-supported{RESET}" if payload["audit"]["supported_by_engine"]
                   else f"{YELLOW}UNSUPPORTED: {len(warn)} warning(s){RESET}"))
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()

    from fraud_mcp import db as dbmod
    from fraud_mcp.seed import build_database

    if not dbmod.db_path().exists():
        build_database()
    return asyncio.run(run(args.json))


if __name__ == "__main__":
    raise SystemExit(main())
