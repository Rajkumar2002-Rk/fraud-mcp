"""Tool handlers.

These are plain functions returning plain dicts. The MCP layer in `server.py` is
a thin wrapper over them, which means the pytest suite exercises the same code
path an agent hits - no mock server, no drift between "what we tested" and "what
the model calls".

Two conventions run through every handler:

1. **Structured errors, never exceptions.** See `errors.py`.
2. **Provenance on every response.** Each payload carries the data source, the
   frozen `as_of` clock, the rules version, and the exact query parameters used.
   An agent's narrative can then be checked against the evidence that produced
   it, which is the difference between an investigation and a guess.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import db as dbmod
from .errors import ErrorCode, ToolError, error_envelope
from .rules import RULES_VERSION, Status, evaluate_account, overall_severity

ACCOUNT_ID_RE = re.compile(r"^ACC-\d{4}$")
DEVICE_ID_RE = re.compile(r"^DEV-[A-Za-z0-9-]{1,32}$")

MAX_DAYS = 365
MIN_DAYS = 1
VALID_SEVERITIES = ("low", "medium", "high", "critical")
MAX_REASON_CHARS = 2000
MIN_REASON_CHARS = 20


def _provenance(conn: sqlite3.Connection, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": tool,
        "data_source": str(dbmod.db_path().name),
        "as_of": dbmod.as_of(conn).isoformat(),
        "rules_version": RULES_VERSION,
        "query_parameters": params,
        "note": (
            "All timestamps are evaluated against 'as_of', a frozen dataset clock, not "
            "wall-clock now. Quote 'as_of' in any written finding so the result can be "
            "reproduced."
        ),
    }


def _require_account(conn: sqlite3.Connection, account_id: Any) -> str:
    if not isinstance(account_id, str) or not ACCOUNT_ID_RE.match(account_id.strip()):
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"account_id must match 'ACC-####' (four digits); got {account_id!r}.",
            remediation=(
                "Use an account_id exactly as it appears in a previous tool response, "
                "e.g. 'ACC-1013'. Do not invent or reformat identifiers."
            ),
            details={"expected_pattern": ACCOUNT_ID_RE.pattern, "received": repr(account_id)},
        )
    account_id = account_id.strip()
    row = conn.execute(
        "SELECT account_id FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None:
        sample = [
            r["account_id"]
            for r in conn.execute("SELECT account_id FROM accounts ORDER BY account_id LIMIT 5")
        ]
        raise ToolError(
            ErrorCode.UNKNOWN_ACCOUNT,
            f"No account with id {account_id!r} exists in this dataset.",
            remediation=(
                "Check the identifier against a prior tool response. This dataset is a closed "
                "synthetic set; accounts outside it cannot be investigated. Do not report "
                "findings about an account that does not exist."
            ),
            details={"received": account_id, "example_valid_ids": sample},
        )
    return account_id


def _require_days(days: Any) -> int:
    if isinstance(days, bool) or not isinstance(days, int):
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"days must be an integer, got {type(days).__name__}.",
            remediation=f"Pass an integer between {MIN_DAYS} and {MAX_DAYS}, e.g. 30.",
            details={"received": repr(days)},
        )
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"days must be between {MIN_DAYS} and {MAX_DAYS}; got {days}.",
            remediation=(
                f"Clamp the lookback to at most {MAX_DAYS} days. If you need a wider view, "
                "call this tool repeatedly with successive windows rather than widening "
                "beyond the supported range."
            ),
            details={"min": MIN_DAYS, "max": MAX_DAYS, "received": days},
        )
    return days


# ---------------------------------------------------------------- tools

def get_transactions(account_id: str, days: int) -> dict[str, Any]:
    conn = dbmod.connect()
    try:
        account_id = _require_account(conn, account_id)
        days = _require_days(days)
        now = dbmod.as_of(conn)
        cutoff = (now - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT txn_id, ts, amount, currency, merchant, mcc, country, channel,"
            " device_id, status FROM transactions"
            " WHERE account_id = ? AND ts >= ? ORDER BY ts DESC",
            (account_id, cutoff),
        ).fetchall()
        txns = [dict(r) for r in rows]

        account = dict(
            conn.execute(
                "SELECT account_id, customer_name, home_country, opened_at, status"
                " FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        )

        payload: dict[str, Any] = {
            "ok": True,
            "account": account,
            "window": {
                "days": days,
                "from": cutoff,
                "to": now.isoformat(),
            },
            "transaction_count": len(txns),
            "transactions": txns,
            "summary": {
                "total_amount": round(sum(t["amount"] for t in txns), 2),
                "countries": sorted({t["country"] for t in txns}),
                "declined_count": sum(1 for t in txns if t["status"] == "declined"),
            },
            "provenance": _provenance(conn, "get_transactions",
                                      {"account_id": account_id, "days": days}),
        }

        if not txns:
            # The empty-result guard. An agent that reads a zero-row response as
            # "this account is clean" is making an unsupported inference, so the
            # response says so in the payload rather than leaving it implicit.
            total_ever = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (account_id,)
            ).fetchone()["n"]
            latest = conn.execute(
                "SELECT MAX(ts) AS ts FROM transactions WHERE account_id = ?", (account_id,)
            ).fetchone()["ts"]
            payload["empty_result_guidance"] = {
                "meaning": (
                    f"This account has no transactions in the last {days} days. That is a "
                    "statement about the WINDOW, not about the account's risk."
                ),
                "do_not_conclude": (
                    "Do not report 'no fraud found' or 'account is clean' on the basis of this "
                    "empty result alone."
                ),
                "next_step": (
                    f"This account has {total_ever} transactions overall"
                    + (f", most recently at {latest}." if latest else ".")
                    + " Widen `days` to cover that period, and run check_velocity_rules, which "
                    "evaluates device and history signals this window does not contain."
                ),
                "total_transactions_all_time": total_ever,
                "latest_transaction_ts": latest,
            }
        return payload
    except ToolError as exc:
        return exc.to_envelope()
    except sqlite3.Error as exc:
        return error_envelope(
            ErrorCode.INTERNAL_ERROR, f"Database error: {exc}",
            remediation="Re-run `uv run fraud-mcp seed` to rebuild the dataset.",
        )
    finally:
        conn.close()


def check_velocity_rules(account_id: str) -> dict[str, Any]:
    conn = dbmod.connect()
    try:
        account_id = _require_account(conn, account_id)
        now = dbmod.as_of(conn)
        outcomes = evaluate_account(conn, account_id, now)
        fired = [o for o in outcomes if o.status is Status.FIRED]
        skipped = [o for o in outcomes if o.status is Status.SKIPPED]

        return {
            "ok": True,
            "account_id": account_id,
            "verdict": {
                "rules_fired": len(fired),
                "rules_evaluated": len(outcomes),
                "rules_skipped": len(skipped),
                "highest_severity_fired": overall_severity(outcomes),
                "fired_rule_ids": [o.rule_id for o in fired],
                "skipped_rule_ids": [o.rule_id for o in skipped],
            },
            "rule_results": [o.to_dict() for o in outcomes],
            "interpretation_contract": {
                "authority": (
                    "This engine is the sole authority on WHICH rules fired. It is "
                    "deterministic: identical inputs always produce identical verdicts. Do not "
                    "re-derive, override, soften, or supplement these verdicts with your own "
                    "judgement about the raw transactions."
                ),
                "your_role": (
                    "Explain what the fired rules mean for this account and recommend an "
                    "action. Cite rule_id and the evidence_txn_ids for every claim you make."
                ),
                "skipped_is_not_clean": (
                    f"{len(skipped)} rule(s) could not be evaluated. A SKIPPED rule is an "
                    "unknown, not a pass. Report skipped rules explicitly rather than treating "
                    "them as absence of risk."
                ),
            },
            "provenance": _provenance(conn, "check_velocity_rules",
                                      {"account_id": account_id}),
        }
    except ToolError as exc:
        return exc.to_envelope()
    except sqlite3.Error as exc:
        return error_envelope(
            ErrorCode.INTERNAL_ERROR, f"Database error: {exc}",
            remediation="Re-run `uv run fraud-mcp seed` to rebuild the dataset.",
        )
    finally:
        conn.close()


def lookup_device_history(device_id: str) -> dict[str, Any]:
    conn = dbmod.connect()
    try:
        if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id.strip()):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"device_id must match 'DEV-<alphanumeric>'; got {device_id!r}.",
                remediation=(
                    "Use a device_id exactly as returned by get_transactions "
                    "(the `device_id` field) or by check_velocity_rules "
                    "(`evidence_device_ids` on the SHARED_DEVICE rule)."
                ),
                details={"expected_pattern": DEVICE_ID_RE.pattern, "received": repr(device_id)},
            )
        device_id = device_id.strip()
        device = conn.execute(
            "SELECT device_id, platform, user_agent, first_seen FROM devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if device is None:
            raise ToolError(
                ErrorCode.UNKNOWN_DEVICE,
                f"No device with id {device_id!r} exists in this dataset.",
                remediation=(
                    "Device ids appear on transactions and in SHARED_DEVICE rule evidence. "
                    "Copy one from a prior response rather than constructing it."
                ),
                details={"received": device_id},
            )
        now = dbmod.as_of(conn)
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, account_id, event_type, ts, ip, country FROM device_events"
                " WHERE device_id = ? ORDER BY ts DESC LIMIT 200",
                (device_id,),
            )
        ]
        accounts = [
            dict(r)
            for r in conn.execute(
                "SELECT e.account_id, a.customer_name, COUNT(*) AS event_count,"
                " MIN(e.ts) AS first_event, MAX(e.ts) AS last_event"
                " FROM device_events e JOIN accounts a ON a.account_id = e.account_id"
                " WHERE e.device_id = ? GROUP BY e.account_id ORDER BY last_event DESC",
                (device_id,),
            )
        ]
        txn_accounts = [
            r["account_id"]
            for r in conn.execute(
                "SELECT DISTINCT account_id FROM transactions WHERE device_id = ?"
                " ORDER BY account_id",
                (device_id,),
            )
        ]

        payload: dict[str, Any] = {
            "ok": True,
            "device": dict(device),
            "distinct_accounts_with_events": len(accounts),
            "accounts": accounts,
            "accounts_with_transactions": txn_accounts,
            "event_count": len(events),
            "events": events,
            "failed_login_count": sum(1 for e in events if e["event_type"] == "login_failed"),
            "password_reset_count": sum(1 for e in events if e["event_type"] == "password_reset"),
            "provenance": _provenance(conn, "lookup_device_history", {"device_id": device_id}),
        }
        if not events:
            payload["empty_result_guidance"] = {
                "meaning": (
                    "This device is registered but has no recorded login events. Device "
                    "telemetry coverage is incomplete in this dataset."
                ),
                "do_not_conclude": (
                    "Absence of login events is not evidence the device is trustworthy."
                ),
                "next_step": (
                    "Use `accounts_with_transactions` to see which accounts transacted from "
                    "this device even without login telemetry."
                ),
            }
        return payload
    except ToolError as exc:
        return exc.to_envelope()
    except sqlite3.Error as exc:
        return error_envelope(
            ErrorCode.INTERNAL_ERROR, f"Database error: {exc}",
            remediation="Re-run `uv run fraud-mcp seed` to rebuild the dataset.",
        )
    finally:
        conn.close()


def flag_case(
    account_id: str, reason: str, severity: str, rule_ids: list[str] | None = None
) -> dict[str, Any]:
    conn = dbmod.connect()
    try:
        account_id = _require_account(conn, account_id)

        if not isinstance(severity, str) or severity.lower() not in VALID_SEVERITIES:
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"severity must be one of {VALID_SEVERITIES}; got {severity!r}.",
                remediation=(
                    "Use the `highest_severity_fired` value from check_velocity_rules rather "
                    "than choosing a severity yourself. The rules engine assigns severity; "
                    "your job is to justify it."
                ),
                details={"allowed": list(VALID_SEVERITIES), "received": repr(severity)},
            )
        severity = severity.lower()

        if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON_CHARS:
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"reason must be at least {MIN_REASON_CHARS} characters of substantive "
                f"justification; got {len(reason.strip()) if isinstance(reason, str) else 0}.",
                remediation=(
                    "Write a reason that names the rule ids that fired and the transaction ids "
                    "they cite, e.g. 'STRUCTURING fired: TXN-001234..TXN-001238, five transfers "
                    "of 9.1k-9.7k within 40h.' A case without evidence cannot be reviewed."
                ),
                details={"min_chars": MIN_REASON_CHARS},
            )
        reason = reason.strip()[:MAX_REASON_CHARS]

        if rule_ids is not None and (
            not isinstance(rule_ids, list) or not all(isinstance(r, str) for r in rule_ids)
        ):
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT,
                "rule_ids must be a list of strings, or omitted.",
                remediation="Pass `fired_rule_ids` from the check_velocity_rules response.",
                details={"received": repr(rule_ids)},
            )

        # Provenance check: a case whose severity contradicts the engine, or which
        # cites no rules at all, is recorded but explicitly marked unsupported.
        now = dbmod.as_of(conn)
        outcomes = evaluate_account(conn, account_id, now)
        engine_fired = [o.rule_id for o in outcomes if o.status is Status.FIRED]
        engine_severity = overall_severity(outcomes)
        cited = rule_ids or []
        uncited = [r for r in cited if r not in engine_fired]

        warnings: list[str] = []
        if not cited:
            warnings.append(
                "No rule_ids were cited. This case is not linked to any deterministic finding "
                f"and cannot be audited. Rules currently firing for this account: "
                f"{engine_fired or 'none'}."
            )
        if uncited:
            warnings.append(
                f"Cited rule ids {uncited} are not currently FIRED for this account. "
                f"Rules actually firing: {engine_fired or 'none'}."
            )
        if engine_severity is None and severity in ("high", "critical"):
            warnings.append(
                f"Severity '{severity}' was recorded, but no rule fired for this account. "
                "Escalating without a deterministic finding is unsupported."
            )
        if engine_severity is not None and severity != engine_severity:
            warnings.append(
                f"Severity '{severity}' differs from the engine's highest fired severity "
                f"'{engine_severity}'. Justify the deviation or use the engine's value."
            )

        case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
        created_at = datetime.now().astimezone().isoformat()
        conn.execute(
            "INSERT INTO cases(case_id, account_id, reason, severity, rule_ids, created_at,"
            " created_by) VALUES (?,?,?,?,?,?,?)",
            (case_id, account_id, reason, severity, ",".join(cited), created_at, "mcp-agent"),
        )
        conn.commit()

        return {
            "ok": True,
            "case_id": case_id,
            "account_id": account_id,
            "severity": severity,
            "reason": reason,
            "cited_rule_ids": cited,
            "created_at": created_at,
            "audit": {
                "engine_fired_rule_ids": engine_fired,
                "engine_highest_severity": engine_severity,
                "supported_by_engine": bool(cited) and not uncited and severity == engine_severity,
                "warnings": warnings,
            },
            "provenance": _provenance(conn, "flag_case",
                                      {"account_id": account_id, "severity": severity}),
        }
    except ToolError as exc:
        return exc.to_envelope()
    except sqlite3.Error as exc:
        return error_envelope(
            ErrorCode.INTERNAL_ERROR, f"Database error: {exc}",
            remediation="Re-run `uv run fraud-mcp seed` to rebuild the dataset.",
        )
    finally:
        conn.close()
