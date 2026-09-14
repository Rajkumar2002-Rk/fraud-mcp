"""Rules-engine tests.

These assert on the planted patterns from `seed.py`, and on the three-state
FIRED / NOT_FIRED / SKIPPED contract that the whole design rests on.
"""

from __future__ import annotations

import pytest

from fraud_mcp.rules import (
    RULES_VERSION,
    Status,
    evaluate_account,
    overall_severity,
)

ALL_RULE_IDS = {
    "VELOCITY_BURST", "AMOUNT_SPIKE", "NEW_GEO_HIGH_VALUE",
    "STRUCTURING", "SHARED_DEVICE", "IMPOSSIBLE_TRAVEL",
}


def outcomes_by_id(conn, account_id, now):
    return {o.rule_id: o for o in evaluate_account(conn, account_id, now)}


def test_every_rule_reports_on_every_account(conn, now):
    """No rule may silently omit itself - an unreported rule is an unchecked rule."""
    for account_id in ("ACC-1002", "ACC-1007", "ACC-1009", "ACC-1021"):
        assert set(outcomes_by_id(conn, account_id, now)) == ALL_RULE_IDS


def test_evaluation_is_deterministic(conn, now):
    first = [o.to_dict() for o in evaluate_account(conn, "ACC-1013", now)]
    second = [o.to_dict() for o in evaluate_account(conn, "ACC-1013", now)]
    assert first == second


@pytest.mark.parametrize(
    ("account_id", "rule_id"),
    [
        ("ACC-1007", "VELOCITY_BURST"),
        ("ACC-1013", "SHARED_DEVICE"),
        ("ACC-1013", "NEW_GEO_HIGH_VALUE"),
        ("ACC-1021", "STRUCTURING"),
        ("ACC-1030", "IMPOSSIBLE_TRAVEL"),
    ],
)
def test_planted_patterns_fire(conn, now, account_id, rule_id):
    outcome = outcomes_by_id(conn, account_id, now)[rule_id]
    assert outcome.status is Status.FIRED, outcome.explanation


def test_clean_control_account_fires_nothing(conn, now):
    outcomes = evaluate_account(conn, "ACC-1002", now)
    assert [o.rule_id for o in outcomes if o.status is Status.FIRED] == []
    assert overall_severity(outcomes) is None


def test_dormant_account_skips_rather_than_passes(conn, now):
    """The core false-negative guard.

    ACC-1009 has no recent activity. Rules that need recent history must report
    SKIPPED, not NOT_FIRED - otherwise an empty account looks like a clean one.
    """
    outcomes = outcomes_by_id(conn, "ACC-1009", now)
    assert outcomes["AMOUNT_SPIKE"].status is Status.SKIPPED
    assert outcomes["SHARED_DEVICE"].status is Status.SKIPPED
    for rule_id, outcome in outcomes.items():
        if outcome.status is Status.SKIPPED:
            assert outcome.skipped_reason, f"{rule_id} skipped without a reason"


def test_fired_rules_always_carry_evidence(conn, now):
    for account_id in ("ACC-1007", "ACC-1013", "ACC-1021", "ACC-1030"):
        for outcome in evaluate_account(conn, account_id, now):
            if outcome.status is Status.FIRED:
                assert outcome.evidence_txn_ids or outcome.evidence_device_ids, (
                    f"{account_id}/{outcome.rule_id} fired with no evidence"
                )
                assert outcome.explanation


def test_every_outcome_quotes_its_thresholds(conn, now):
    for outcome in evaluate_account(conn, "ACC-1021", now):
        assert outcome.thresholds, f"{outcome.rule_id} reported no thresholds"
        assert outcome.description


def test_velocity_burst_evidence_matches_threshold(conn, now):
    outcome = outcomes_by_id(conn, "ACC-1007", now)["VELOCITY_BURST"]
    assert len(outcome.evidence_txn_ids) >= outcome.thresholds["min_txns"]
    assert outcome.observed["max_transactions_in_window"] == len(outcome.evidence_txn_ids)


def test_structuring_evidence_is_all_sub_threshold(conn, now):
    outcome = outcomes_by_id(conn, "ACC-1021", now)["STRUCTURING"]
    ceiling = outcome.thresholds["reporting_threshold"]
    placeholders = ",".join("?" * len(outcome.evidence_txn_ids))
    rows = conn.execute(
        f"SELECT amount FROM transactions WHERE txn_id IN ({placeholders})",
        outcome.evidence_txn_ids,
    ).fetchall()
    assert rows
    assert all(r["amount"] < ceiling for r in rows)


def test_shared_device_names_the_other_accounts(conn, now):
    outcome = outcomes_by_id(conn, "ACC-1013", now)["SHARED_DEVICE"]
    assert "DEV-ATO-01" in outcome.evidence_device_ids
    peers = outcome.observed["co_located_accounts"]
    assert {"ACC-1014", "ACC-1015"} <= set(peers)


def test_impossible_travel_pair_is_cross_border_and_within_threshold(conn, now):
    outcome = outcomes_by_id(conn, "ACC-1030", now)["IMPOSSIBLE_TRAVEL"]
    pair = outcome.observed["pairs"][0]
    assert pair["from_country"] != pair["to_country"]
    assert pair["minutes_apart"] <= outcome.thresholds["max_minutes_between"]


def test_overall_severity_is_the_max_of_fired_rules(conn, now):
    assert overall_severity(evaluate_account(conn, "ACC-1021", now)) == "critical"
    assert overall_severity(evaluate_account(conn, "ACC-1007", now)) == "high"


def test_rules_version_is_pinned():
    assert RULES_VERSION


def test_seeding_is_byte_reproducible(tmp_path):
    """The dataset must be rebuildable identically, or no verdict is reproducible."""
    import hashlib

    from fraud_mcp.seed import build_database

    digests = []
    for name in ("a.sqlite3", "b.sqlite3"):
        path = build_database(tmp_path / name)
        rows = []
        import sqlite3

        conn = sqlite3.connect(path)
        for table in ("accounts", "devices", "device_events", "transactions"):
            rows.extend(map(str, conn.execute(f"SELECT * FROM {table} ORDER BY 1")))
        conn.close()
        digests.append(hashlib.sha256("".join(rows).encode()).hexdigest())
    assert digests[0] == digests[1]
