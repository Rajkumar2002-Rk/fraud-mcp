"""MCP server: a thin, typed wrapper over `fraud_mcp.tools`.

Everything an agent knows about these tools comes from the docstrings and
`Field` descriptions below. They are part of the product, not documentation
about it - most of the agent-misuse fixes recorded in FINDINGS.md are edits to
this file, not to the logic underneath.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import tools
from .profile import is_v0
from .middleware import structured_error_middleware

INSTRUCTIONS = """
Fraud investigation tools backed by a deterministic rules engine.

Recommended investigation order:
  1. evaluate_fraud_rules(account_id) - get the authoritative verdict first.
  2. get_transactions(account_id, days) - pull the evidence the rules cite.
  3. lookup_device_history(device_id) - pivot on any device named in the evidence.
  4. flag_case(...) - only after 1-3, citing the rule ids that fired.

The rules engine, not you, decides which rules fired. Your job is to interpret
the verdict, corroborate it against the transactions, and write it up with
citations. Never restate a verdict without its rule_id and evidence_txn_ids, and
never treat an empty result or a SKIPPED rule as evidence that an account is clean.
""".strip()

V0 = is_v0()

# --- parameter schemas -------------------------------------------------------
# v0 is the naive first draft: bare types and one-line descriptions. v1 is what
# the agent runs in FINDINGS.md produced. Swapping between them is the ablation.

if V0:
    AccountId = Annotated[str, Field(description="The account ID.")]
    Days = Annotated[int, Field(description="Number of days of history to fetch.")]
    DeviceId = Annotated[str, Field(description="The device ID.")]
    CaseReason = Annotated[str, Field(description="Why the case is being flagged.")]
    CaseSeverity = Annotated[str, Field(description="Severity of the case.")]
    CaseRuleIds = Annotated[list[str] | None, Field(description="Related rule IDs.")]
else:
    AccountId = Annotated[
        str,
        Field(
            description=(
                "Account identifier in the exact form 'ACC-####' (e.g. 'ACC-1013'). Copy it "
                "from a previous tool response or the user's message; do not invent or "
                "reformat it."
            ),
            pattern=r"^ACC-\d{4}$",
            examples=["ACC-1000", "ACC-1234"],
        ),
    ]
    Days = Annotated[
        int,
        Field(
            description=(
                "Lookback window in days, counted back from the dataset's frozen `as_of` "
                "clock (NOT from today's date). Use 7-30 for a velocity or takeover review; "
                "90+ to establish a spending baseline or to investigate a dormant account. "
                "An empty result means the window was too narrow, not that the account "
                "is clean."
            ),
            ge=1,
            le=365,
            examples=[30, 90],
        ),
    ]
    DeviceId = Annotated[
        str,
        Field(
            description=(
                "Device identifier such as 'DEV-2000'. Obtain it from the "
                "`device_id` field of a transaction, or from `evidence_device_ids` on a "
                "SHARED_DEVICE rule result. Device ids are NOT derived from account ids - "
                "'DEV-1013' is not the device for 'ACC-1013'."
            ),
            pattern=r"^DEV-[A-Za-z0-9-]{1,32}$",
            examples=["DEV-2000", "DEV-4F2A"],
        ),
    ]
    CaseReason = Annotated[
        str,
        Field(
            description=(
                "Written justification, minimum 20 characters. MUST name the rule ids that "
                "fired and the transaction ids they cite, e.g. 'STRUCTURING fired: five "
                "transfers TXN-001234..TXN-001238 of 9.1k-9.7k within 40h, each below the "
                "10k reporting threshold.' A reason without evidence cannot be reviewed by "
                "a human analyst and will be marked unsupported."
            ),
            min_length=20,
            max_length=2000,
        ),
    ]
    CaseSeverity = Annotated[
        Literal["low", "medium", "high", "critical"],
        Field(
            description=(
                "Case severity. Use the `highest_severity_fired` value returned by "
                "evaluate_fraud_rules. Do not pick a severity by intuition - if you deviate "
                "from the engine's value, the response will record a warning and you must "
                "justify it."
            ),
        ),
    ]
    CaseRuleIds = Annotated[
        list[str] | None,
        Field(
            description=(
                "The rule ids that justify this case - pass `fired_rule_ids` from the "
                "evaluate_fraud_rules response verbatim. Omitting this records the case as "
                "UNSUPPORTED and unauditable. Always call evaluate_fraud_rules first so you "
                "have real ids to pass."
            ),
            examples=[["STRUCTURING"], ["SHARED_DEVICE", "NEW_GEO_HIGH_VALUE"]],
        ),
    ]


V0_DESCRIPTIONS = {
    "get_transactions": "Get recent transactions for an account.",
    "evaluate_fraud_rules": "Check velocity rules for an account and return the results.",
    "lookup_device_history": "Look up the history for a device.",
    "flag_case": "Flag a case for an account.",
}


def _description(name: str) -> str | None:
    """v0 serves a one-liner; v1 serves the function's full docstring."""
    return V0_DESCRIPTIONS[name] if V0 else None

server: MCPServer = MCPServer(
    name="fraud-mcp",
    title="Fraud Investigation Tools",
    version="0.1.0",
    instructions=None if V0 else INSTRUCTIONS,
    middleware=[] if V0 else [structured_error_middleware],
)


@server.tool(
    description=_description("get_transactions"),
    title="Get recent transactions",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def get_transactions(account_id: AccountId, days: Days) -> dict[str, Any]:
    """Return an account's transactions within a lookback window, with a summary.

    This is EVIDENCE, not a verdict. It does not evaluate risk. To learn whether
    an account has tripped any fraud rule, call `evaluate_fraud_rules` - reading
    these rows and forming your own opinion is exactly what this server is built
    to prevent.

    Returns an error envelope (`ok: false`) for unknown accounts or an out-of-range
    `days`. A successful response with zero transactions carries an
    `empty_result_guidance` block explaining what the emptiness does and does not
    imply; read it before drawing any conclusion.
    """
    return tools.get_transactions(account_id, days)


@server.tool(
    description=_description("evaluate_fraud_rules"),
    title="Evaluate fraud rules (deterministic)",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def evaluate_fraud_rules(account_id: AccountId) -> dict[str, Any]:
    """Run all six fraud rules against an account and return which ones fired, and why.

    THIS IS THE AUTHORITATIVE RISK VERDICT. The engine is deterministic: the same
    account always yields the same result. Do not second-guess it, re-derive it
    from raw transactions, or soften it.

    Rules evaluated: VELOCITY_BURST, AMOUNT_SPIKE, NEW_GEO_HIGH_VALUE, STRUCTURING,
    SHARED_DEVICE, IMPOSSIBLE_TRAVEL.

    Covers, in plain terms: transaction velocity and card testing, spending
    anomalies against the account's own baseline, unfamiliar geography, money
    laundering by structuring or smurfing below a reporting threshold, account
    takeover and credential stuffing via shared devices, and cloned cards
    (impossible travel). Use this tool for ANY question about whether an account
    is risky, compromised, laundering, or worth escalating - not only questions
    phrased around velocity.

    Every rule returns one of three states, and the difference matters:
      FIRED     - the threshold was met; `evidence_txn_ids` lists the transactions.
      NOT_FIRED - evaluated, threshold not met. This is a genuine pass.
      SKIPPED   - could NOT be evaluated (e.g. too little history for a baseline).
                  This is an UNKNOWN, not a pass. Report skipped rules explicitly.

    Each result carries the thresholds that were applied and the observations
    measured against them, so any verdict can be recomputed by hand later. Cite
    `rule_id` and `evidence_txn_ids` in anything you write.

    Call this BEFORE flag_case. Takes no time window: each rule applies its own
    documented lookback.
    """
    return tools.evaluate_fraud_rules(account_id)


@server.tool(
    description=_description("lookup_device_history"),
    title="Look up device history",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
def lookup_device_history(device_id: DeviceId) -> dict[str, Any]:
    """Return every account and login event seen on a device.

    Use this to pivot from one compromised account to the others driven from the
    same machine - the standard way an account-takeover ring is mapped. Pay
    attention to `distinct_accounts_with_events`, `failed_login_count`, and
    `password_reset_count`: a device with many accounts, failed logins, and resets
    is a takeover tool, not a shared family tablet.

    Returns an error envelope for unknown or malformed device ids.
    """
    return tools.lookup_device_history(device_id)


@server.tool(
    description=_description("flag_case"),
    title="Flag an investigation case",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": False},
)
def flag_case(
    account_id: AccountId,
    reason: CaseReason,
    severity: CaseSeverity,
    rule_ids: CaseRuleIds = None,
) -> dict[str, Any]:
    """Record an investigation outcome against an account. THIS WRITES TO THE CASE LOG.

    Call this only after `evaluate_fraud_rules`, and only when you can cite the
    rule ids that fired. The response includes an `audit` block that independently
    re-runs the rules engine and compares your submission against it: if you cite
    rules that are not firing, omit rule ids entirely, or set a severity the engine
    does not support, the case is still recorded but flagged with warnings for the
    human reviewer.

    Not idempotent - each call creates a new case. Do not retry on success.
    """
    return tools.flag_case(account_id, reason, severity, rule_ids)
