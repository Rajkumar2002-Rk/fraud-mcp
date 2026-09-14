"""Protocol-level tests.

`test_tools.py` covers the handlers; this file covers what an agent actually
sees over MCP - the advertised schemas, the tool descriptions, and the error
contract after the SDK's own validation layer has had its say.

The schema assertions are deliberately picky. Descriptions are the product here:
they are the surface most of the FINDINGS.md fixes landed on, and a regression
in them is a real regression in agent behaviour.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from fraud_mcp.server import server

EXPECTED_TOOLS = {"get_transactions", "evaluate_fraud_rules", "lookup_device_history", "flag_case"}


async def call(session: ClientSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, args)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@asynccontextmanager
async def mcp_session():
    """Connect an in-process MCP client to the real server.

    Deliberately a context manager rather than a pytest fixture: anyio cancel
    scopes must be entered and exited in the same task, and an async-generator
    fixture splits setup and teardown across tasks, which trips anyio's
    same-task assertion.
    """
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def test_server_advertises_exactly_the_four_tools():
    async with mcp_session() as session:
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == EXPECTED_TOOLS


async def test_server_instructions_state_the_call_order():
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
        instructions = init.instructions or ""
        assert "evaluate_fraud_rules" in instructions
        assert instructions.index("evaluate_fraud_rules") < instructions.index("flag_case")


async def test_every_tool_documents_every_parameter():
    async with mcp_session() as session:
        for tool in (await session.list_tools()).tools:
            assert tool.description, f"{tool.name} has no description"
            for param, spec in tool.input_schema.get("properties", {}).items():
                assert spec.get("description"), f"{tool.name}.{param} has no description"


async def test_identifier_parameters_are_pattern_constrained():
    async with mcp_session() as session:
        schemas = {t.name: t.input_schema for t in (await session.list_tools()).tools}
        assert schemas["get_transactions"]["properties"]["account_id"]["pattern"] == r"^ACC-\d{4}$"
        assert schemas["lookup_device_history"]["properties"]["device_id"]["pattern"]
        days = schemas["get_transactions"]["properties"]["days"]
        assert days["minimum"] == 1 and days["maximum"] == 365


async def test_flag_case_severity_is_an_enum():
    async with mcp_session() as session:
        schemas = {t.name: t.input_schema for t in (await session.list_tools()).tools}
        severity = schemas["flag_case"]["properties"]["severity"]
        assert set(severity["enum"]) == {"low", "medium", "high", "critical"}


async def test_read_only_tools_are_annotated_as_such():
    async with mcp_session() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert tools["get_transactions"].annotations.read_only_hint is True
        assert tools["flag_case"].annotations.read_only_hint is False


async def test_happy_path_over_the_wire():
    async with mcp_session() as session:
        payload = await call(session, "evaluate_fraud_rules", {"account_id": "ACC-1021"})
        assert payload["ok"] is True
        assert "STRUCTURING" in payload["verdict"]["fired_rule_ids"]
        assert payload["provenance"]["rules_version"]


async def test_handler_level_error_is_a_structured_envelope():
    async with mcp_session() as session:
        payload = await call(session, "evaluate_fraud_rules", {"account_id": "ACC-9999"})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNKNOWN_ACCOUNT"
        assert payload["error"]["remediation"]


async def test_schema_level_rejection_is_also_a_structured_envelope():
    """Guards the middleware: the SDK's own validation must not leak raw pydantic text."""
    async with mcp_session() as session:
        payload = await call(session, "get_transactions", {"account_id": "ACC-1013", "days": 9999})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        assert payload["error"]["remediation"]
        assert payload["error"]["details"]["tool"] == "get_transactions"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_transactions", {"account_id": "not-an-account", "days": 30}),
        ("get_transactions", {"account_id": "ACC-1013"}),
        ("lookup_device_history", {"device_id": "ACC-1013"}),
        ("flag_case", {"account_id": "ACC-1013", "reason": "too short", "severity": "urgent"}),
    ],
)
async def test_bad_arguments_never_surface_as_raw_errors(name, args):
    async with mcp_session() as session:
        payload = await call(session, name, args)
        assert payload["ok"] is False
        assert payload["error"]["code"]
        assert payload["error"]["remediation"]
        assert "Traceback" not in json.dumps(payload)


async def test_empty_window_returns_guidance_over_the_wire():
    async with mcp_session() as session:
        payload = await call(session, "get_transactions", {"account_id": "ACC-1009", "days": 30})
        assert payload["ok"] is True
        assert payload["transaction_count"] == 0
        assert payload["empty_result_guidance"]["do_not_conclude"]


async def test_rules_tool_is_discoverable_by_the_vocabulary_of_the_task():
    """Guards the rename from Desktop Run 1.

    Some clients search for tools by keyword before loading their schemas, so a
    tool has to win a search before its description can influence anything. The
    old name, `check_velocity_rules`, described one of six rules and would not
    plausibly match a query about laundering or takeover. The name and
    description must carry the vocabulary an investigator would actually use.
    """
    async with mcp_session() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert "evaluate_fraud_rules" in tools
        assert "check_velocity_rules" not in tools

        haystack = (tools["evaluate_fraud_rules"].description or "").lower()
        for term in ("structuring", "laundering", "takeover", "velocity",
                     "cloned card", "shared device", "escalating"):
            assert term in haystack, f"{term!r} missing from the rules tool description"
