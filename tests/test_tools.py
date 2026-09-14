"""Tool-handler tests: happy paths, error envelopes, and the anti-misuse guards."""

from __future__ import annotations

import pytest

from fraud_mcp import tools
from fraud_mcp.errors import ErrorCode

PROVENANCE_KEYS = {"tool", "data_source", "as_of", "rules_version", "query_parameters"}


def assert_error(resp, code: ErrorCode):
    assert resp["ok"] is False
    assert resp["error"]["code"] == str(code)
    assert resp["error"]["remediation"], "every error must tell the agent what to do next"
    return resp["error"]


# ---------------------------------------------------------- get_transactions

def test_get_transactions_happy_path():
    resp = tools.get_transactions("ACC-1007", 30)
    assert resp["ok"] is True
    assert resp["transaction_count"] == len(resp["transactions"])
    assert resp["transaction_count"] > 0
    assert PROVENANCE_KEYS <= set(resp["provenance"])
    assert resp["provenance"]["query_parameters"] == {"account_id": "ACC-1007", "days": 30}


def test_get_transactions_window_is_respected():
    narrow = tools.get_transactions("ACC-1007", 3)
    wide = tools.get_transactions("ACC-1007", 90)
    assert narrow["transaction_count"] < wide["transaction_count"]


def test_get_transactions_unknown_account():
    err = assert_error(tools.get_transactions("ACC-9999", 30), ErrorCode.UNKNOWN_ACCOUNT)
    assert err["details"]["example_valid_ids"], "should hand back real ids to recover with"


@pytest.mark.parametrize("bad", ["acc-1007", "1007", "ACC-13", "", "ACC-1013 "])
def test_get_transactions_malformed_account_id(bad):
    if bad == "ACC-1013 ":  # surrounding whitespace is tolerated, not an error
        assert tools.get_transactions(bad, 30)["ok"] is True
        return
    assert_error(tools.get_transactions(bad, 30), ErrorCode.INVALID_ARGUMENT)


@pytest.mark.parametrize("bad", [0, -5, 366, 10_000])
def test_get_transactions_days_out_of_range(bad):
    err = assert_error(tools.get_transactions("ACC-1007", bad), ErrorCode.INVALID_ARGUMENT)
    assert err["details"]["max"] == 365


@pytest.mark.parametrize("bad", ["30", 30.5, None, True])
def test_get_transactions_days_wrong_type(bad):
    assert_error(tools.get_transactions("ACC-1007", bad), ErrorCode.INVALID_ARGUMENT)


def test_empty_result_carries_guidance_not_an_all_clear():
    """The single most important anti-misuse guard in the server."""
    resp = tools.get_transactions("ACC-1009", 30)
    assert resp["ok"] is True
    assert resp["transaction_count"] == 0
    guidance = resp["empty_result_guidance"]
    assert guidance["total_transactions_all_time"] > 0
    assert "not" in guidance["do_not_conclude"].lower()
    assert guidance["next_step"]


def test_non_empty_result_has_no_empty_guidance():
    assert "empty_result_guidance" not in tools.get_transactions("ACC-1007", 30)


# ------------------------------------------------------- check_velocity_rules

def test_check_velocity_rules_happy_path():
    resp = tools.check_velocity_rules("ACC-1021")
    assert resp["ok"] is True
    assert "STRUCTURING" in resp["verdict"]["fired_rule_ids"]
    assert resp["verdict"]["highest_severity_fired"] == "critical"
    assert len(resp["rule_results"]) == resp["verdict"]["rules_evaluated"] == 6


def test_check_velocity_rules_reports_non_firing_rules_too():
    resp = tools.check_velocity_rules("ACC-1002")
    assert resp["verdict"]["fired_rule_ids"] == []
    statuses = {r["rule_id"]: r["status"] for r in resp["rule_results"]}
    assert len(statuses) == 6
    assert "NOT_FIRED" in statuses.values()


def test_check_velocity_rules_carries_interpretation_contract():
    contract = tools.check_velocity_rules("ACC-1013")["interpretation_contract"]
    assert "deterministic" in contract["authority"]
    assert contract["your_role"]
    assert contract["skipped_is_not_clean"]


def test_check_velocity_rules_unknown_account():
    assert_error(tools.check_velocity_rules("ACC-4242"), ErrorCode.UNKNOWN_ACCOUNT)


def test_fired_rules_expose_thresholds_and_evidence():
    for result in tools.check_velocity_rules("ACC-1013")["rule_results"]:
        if result["status"] == "FIRED":
            assert result["thresholds"]
            assert result["observed"]
            assert result["evidence_txn_ids"] or result["evidence_device_ids"]


# ------------------------------------------------------ lookup_device_history

def test_lookup_device_history_finds_the_attacker_device():
    resp = tools.lookup_device_history("DEV-ATO-01")
    assert resp["ok"] is True
    assert resp["distinct_accounts_with_events"] == 3
    assert resp["password_reset_count"] == 3
    assert resp["failed_login_count"] == 6
    assert PROVENANCE_KEYS <= set(resp["provenance"])


def test_lookup_device_history_unknown_device():
    assert_error(tools.lookup_device_history("DEV-NOPE"), ErrorCode.UNKNOWN_DEVICE)


@pytest.mark.parametrize("bad", ["ACC-1013", "2007", "", None, "dev 2007"])
def test_lookup_device_history_malformed_id(bad):
    assert_error(tools.lookup_device_history(bad), ErrorCode.INVALID_ARGUMENT)


# ------------------------------------------------------------------ flag_case

GOOD_REASON = (
    "STRUCTURING fired: five transfers of 9.1k-9.7k USD within 40 hours, each below the "
    "10,000 USD reporting threshold."
)


def test_flag_case_happy_path_is_engine_supported():
    resp = tools.flag_case("ACC-1021", GOOD_REASON, "critical", ["STRUCTURING", "AMOUNT_SPIKE"])
    assert resp["ok"] is True
    assert resp["case_id"].startswith("CASE-")
    assert resp["audit"]["supported_by_engine"] is True
    assert resp["audit"]["warnings"] == []


def test_flag_case_without_rule_ids_is_recorded_but_flagged_unsupported():
    resp = tools.flag_case("ACC-1021", GOOD_REASON, "critical")
    assert resp["ok"] is True
    assert resp["audit"]["supported_by_engine"] is False
    assert any("No rule_ids" in w for w in resp["audit"]["warnings"])


def test_flag_case_citing_a_rule_that_did_not_fire_is_warned():
    resp = tools.flag_case("ACC-1021", GOOD_REASON, "critical", ["IMPOSSIBLE_TRAVEL"])
    assert any("not currently FIRED" in w for w in resp["audit"]["warnings"])


def test_flag_case_escalating_a_clean_account_is_warned():
    resp = tools.flag_case(
        "ACC-1002", "Analyst intuition suggests elevated risk on this account.", "critical"
    )
    assert resp["audit"]["engine_fired_rule_ids"] == []
    assert any("no rule fired" in w for w in resp["audit"]["warnings"])


def test_flag_case_severity_mismatch_is_warned():
    resp = tools.flag_case("ACC-1021", GOOD_REASON, "low", ["STRUCTURING"])
    assert any("differs from the engine" in w for w in resp["audit"]["warnings"])


@pytest.mark.parametrize("bad", ["", "too short", "   "])
def test_flag_case_rejects_thin_reasons(bad):
    assert_error(tools.flag_case("ACC-1021", bad, "high"), ErrorCode.INVALID_ARGUMENT)


@pytest.mark.parametrize("bad", ["urgent", "HIGHEST", "", None, 3])
def test_flag_case_rejects_invalid_severity(bad):
    assert_error(tools.flag_case("ACC-1021", GOOD_REASON, bad), ErrorCode.INVALID_ARGUMENT)


def test_flag_case_rejects_malformed_rule_ids():
    assert_error(
        tools.flag_case("ACC-1021", GOOD_REASON, "critical", "STRUCTURING"),
        ErrorCode.INVALID_ARGUMENT,
    )


def test_flag_case_unknown_account():
    assert_error(tools.flag_case("ACC-7777", GOOD_REASON, "high"), ErrorCode.UNKNOWN_ACCOUNT)


def test_flag_case_persists_and_is_not_idempotent():
    a = tools.flag_case("ACC-1007", GOOD_REASON, "high", ["VELOCITY_BURST"])
    b = tools.flag_case("ACC-1007", GOOD_REASON, "high", ["VELOCITY_BURST"])
    assert a["case_id"] != b["case_id"]


def test_no_handler_ever_raises():
    """The contract: garbage in, structured error out - never a traceback."""
    for call in (
        lambda: tools.get_transactions(None, None),
        lambda: tools.check_velocity_rules(12345),
        lambda: tools.lookup_device_history({"device_id": "x"}),
        lambda: tools.flag_case([], [], []),
    ):
        resp = call()
        assert resp["ok"] is False
        assert resp["error"]["code"]
