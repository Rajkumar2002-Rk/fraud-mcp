"""The v0/v1 ablation must stay runnable - FINDINGS.md depends on it.

These tests assert the *difference* between the two profiles, so that a future
edit cannot quietly delete the hardening that the findings document credits.
"""

from __future__ import annotations

import importlib

import pytest

from fraud_mcp import profile as profile_mod


def reload_with_profile(monkeypatch: pytest.MonkeyPatch, value: str | None):
    if value is None:
        monkeypatch.delenv("FRAUD_MCP_PROFILE", raising=False)
    else:
        monkeypatch.setenv("FRAUD_MCP_PROFILE", value)
    importlib.reload(profile_mod)
    from fraud_mcp import server as server_mod
    from fraud_mcp import tools as tools_mod

    importlib.reload(tools_mod)
    return importlib.reload(server_mod), tools_mod


@pytest.fixture(autouse=True)
def _restore(monkeypatch: pytest.MonkeyPatch):
    yield
    monkeypatch.delenv("FRAUD_MCP_PROFILE", raising=False)
    importlib.reload(profile_mod)
    from fraud_mcp import server as server_mod
    from fraud_mcp import tools as tools_mod

    importlib.reload(tools_mod)
    importlib.reload(server_mod)


def test_default_profile_is_hardened():
    assert profile_mod.profile() == "v1"
    assert profile_mod.is_v0() is False


def test_v0_drops_the_empty_result_guidance(monkeypatch):
    _, tools_v0 = reload_with_profile(monkeypatch, "v0")
    assert "empty_result_guidance" not in tools_v0.get_transactions("ACC-1009", 30)


def test_v1_adds_the_empty_result_guidance(monkeypatch):
    _, tools_v1 = reload_with_profile(monkeypatch, "v1")
    assert "empty_result_guidance" in tools_v1.get_transactions("ACC-1009", 30)


def test_v0_drops_the_interpretation_contract(monkeypatch):
    _, tools_v0 = reload_with_profile(monkeypatch, "v0")
    assert tools_v0.check_velocity_rules("ACC-1021")["interpretation_contract"] is None


def test_v0_drops_the_case_audit(monkeypatch):
    _, tools_v0 = reload_with_profile(monkeypatch, "v0")
    resp = tools_v0.flag_case("ACC-1002", "hunch", "critical")
    assert resp["audit"] is None


def test_v0_raises_instead_of_returning_an_envelope(monkeypatch):
    _, tools_v0 = reload_with_profile(monkeypatch, "v0")
    from fraud_mcp.errors import ToolError

    with pytest.raises(ToolError):
        tools_v0.check_velocity_rules("ACC-9999")


def test_v0_serves_terse_schemas(monkeypatch):
    server_v0, _ = reload_with_profile(monkeypatch, "v0")
    assert server_v0.V0 is True
    assert len(server_v0.V0_DESCRIPTIONS["check_velocity_rules"]) < 80


def test_the_rules_engine_is_identical_across_profiles(monkeypatch):
    """Only the interface changes. The verdicts must not."""
    _, tools_v0 = reload_with_profile(monkeypatch, "v0")
    v0_verdict = tools_v0.check_velocity_rules("ACC-1013")["verdict"]
    _, tools_v1 = reload_with_profile(monkeypatch, "v1")
    v1_verdict = tools_v1.check_velocity_rules("ACC-1013")["verdict"]
    assert v0_verdict == v1_verdict
