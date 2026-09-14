#!/usr/bin/env python
"""A command-line bridge to the MCP server, for driving it with an LLM agent.

The agent under test cannot open an MCP socket, so this script stands in for a
client: `list-tools` prints the exact schemas and descriptions the server
advertises over MCP, and `call` performs a real `tools/call` and prints the real
response. Nothing is paraphrased or simplified in between.

Every call is appended to a JSONL transcript (`FRAUD_MCP_TRANSCRIPT`), which is
what the analysis in FINDINGS.md is built from - argument errors, call ordering,
and whether the agent looked at provenance are all recoverable from it.

Methodological caveat, stated plainly because it affects the findings: a native
MCP client validates arguments against the schema before dispatch and can repair
them, whereas this bridge sends whatever the agent typed. Argument-shape errors
are therefore somewhat over-represented here relative to a native client;
ordering, empty-result, and provenance failures are not affected.

    uv run python scripts/agent_cli.py list-tools
    uv run python scripts/agent_cli.py call evaluate_fraud_rules '{"account_id": "ACC-1013"}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp import ClientSession  # noqa: E402
from mcp.client._memory import InMemoryTransport  # noqa: E402


def log(entry: dict[str, Any]) -> None:
    target = os.environ.get("FRAUD_MCP_TRANSCRIPT")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry["profile"] = os.environ.get("FRAUD_MCP_PROFILE", "v1")
    entry["wall_time"] = time.time()
    with path.open("a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


async def do_list() -> int:
    from fraud_mcp.server import server

    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            out = {
                "server_instructions": init.instructions,
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                    }
                    for t in listed.tools
                ],
            }
            print(json.dumps(out, indent=2))
            log({"action": "list_tools", "tool_count": len(listed.tools)})
    return 0


async def do_call(tool: str, raw_args: str) -> int:
    from fraud_mcp.server import server

    try:
        arguments = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": {
            "code": "CLIENT_JSON_ERROR",
            "message": f"Arguments were not valid JSON: {exc}",
            "remediation": "Pass arguments as a single-quoted JSON object.",
        }}, indent=2))
        log({"action": "call", "tool": tool, "raw_arguments": raw_args,
             "outcome": "client_json_error"})
        return 2

    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                result = await session.call_tool(tool, arguments)
            except Exception as exc:  # noqa: BLE001 - surfaced verbatim on purpose
                payload = {"ok": False, "raw_exception": f"{type(exc).__name__}: {exc}"}
                print(json.dumps(payload, indent=2))
                log({"action": "call", "tool": tool, "arguments": arguments,
                     "outcome": "raw_exception", "response": payload})
                return 1

            if result.structured_content is not None:
                payload = result.structured_content
            else:
                text = result.content[0].text if result.content else ""
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"ok": False, "raw_text": text}

            print(json.dumps(payload, indent=2, default=str))
            log({
                "action": "call",
                "tool": tool,
                "arguments": arguments,
                "is_error": bool(result.is_error),
                "ok": payload.get("ok") if isinstance(payload, dict) else None,
                "error_code": (payload.get("error", {}) or {}).get("code")
                if isinstance(payload, dict) else None,
                "response": payload,
            })
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-tools", help="print the advertised tool schemas")
    call = sub.add_parser("call", help="invoke a tool")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()

    from fraud_mcp import db as dbmod
    from fraud_mcp.seed import build_database

    if not dbmod.db_path().exists():
        build_database()

    if args.command == "list-tools":
        return asyncio.run(do_list())
    return asyncio.run(do_call(args.tool, args.arguments))


if __name__ == "__main__":
    raise SystemExit(main())
