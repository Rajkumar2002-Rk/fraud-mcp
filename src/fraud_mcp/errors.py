"""Structured error envelope shared by every tool handler.

Design rule: a tool NEVER raises out to the MCP layer. Every failure becomes a
JSON object the model can read, reason about, and recover from. An exception
crossing the transport boundary gives the model a stack trace, which it tends to
either ignore or hallucinate around; a typed error code with a `remediation`
string gives it something actionable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, machine-readable failure taxonomy.

    These strings are part of the tool contract. Renaming one is a breaking
    change for any agent prompt that branches on them.
    """

    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    UNKNOWN_DEVICE = "UNKNOWN_DEVICE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolError(Exception):
    """Raised inside a handler, caught at the boundary, serialised as an envelope.

    Never escapes `fraud_mcp.tools`.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        remediation: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.details = details or {}

    def to_envelope(self) -> dict[str, Any]:
        return error_envelope(
            self.code,
            self.message,
            remediation=self.remediation,
            details=self.details,
        )


def error_envelope(
    code: ErrorCode,
    message: str,
    *,
    remediation: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical error response.

    `remediation` is mandatory on purpose: an error the agent cannot act on is a
    dead end that usually ends in the agent inventing an answer instead.
    """
    return {
        "ok": False,
        "error": {
            "code": str(code),
            "message": message,
            "remediation": remediation,
            "details": details or {},
        },
    }
