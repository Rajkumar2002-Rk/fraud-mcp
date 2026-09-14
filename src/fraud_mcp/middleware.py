"""Middleware that guarantees the "structured errors, never raw" contract.

The handlers in `tools.py` validate their own arguments and return an error
envelope. But the SDK validates arguments against the JSON Schema *before* the
handler runs, so a genuinely out-of-range argument never reaches our code - the
client gets a raw pydantic validation string instead:

    Error executing tool get_transactions: 1 validation error for ...
    days: Input should be less than or equal to 365 ...

That is a worse experience for an agent than our own envelope: no error code, no
remediation, and a wall of framework noise that models tend to either ignore or
apologise at. Rather than loosening the schema - the constraints are genuinely
useful, since well-behaved clients repair arguments against them - this
middleware catches any errored `tools/call` result and rewrites it into the same
envelope shape the handlers produce.

Net effect: one error contract, whether the failure was caught by the schema or
by the handler.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ErrorCode

REMEDIATION = (
    "The arguments did not match this tool's schema. Re-read the tool's input schema, "
    "paying attention to the type, pattern, and allowed range of each field, then call it "
    "again with corrected arguments. Identifiers must be copied verbatim from a previous "
    "tool response - do not invent, reformat, or guess them."
)


def _envelope(tool_name: str, detail: str, arguments: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": str(ErrorCode.INVALID_ARGUMENT),
            "message": f"Arguments rejected by the schema for tool {tool_name!r}.",
            "remediation": REMEDIATION,
            "details": {
                "tool": tool_name,
                "schema_validation_detail": detail,
                "received_arguments": arguments,
            },
        },
    }


async def structured_error_middleware(ctx, call_next):
    """Rewrite schema-rejection results into the project's error envelope."""
    result = await call_next(ctx)

    if ctx.method != "tools/call" or not isinstance(result, dict):
        return result
    if not result.get("isError"):
        return result
    # A handler that returned its own envelope is already in the right shape and
    # is not marked isError; only framework-level rejections land here.
    params = ctx.params if isinstance(ctx.params, dict) else {}
    tool_name = str(params.get("name", "unknown"))
    arguments = params.get("arguments")

    detail = " ".join(
        block.get("text", "")
        for block in result.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()

    envelope = _envelope(tool_name, detail, arguments)
    return {
        "content": [{"type": "text", "text": json.dumps(envelope, indent=2)}],
        "structuredContent": envelope,
        "isError": False,
    }
