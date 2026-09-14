"""Run the end-to-end scenario script as part of the test suite.

The scenario runner is the artifact a reader is most likely to execute first, so
a regression in it should break CI rather than a demo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_scenarios  # noqa: E402


async def test_all_scenarios_pass():
    assert await run_scenarios.run(as_json=False) == 0


def test_every_scenario_asserts_something():
    for scenario in run_scenarios.SCENARIOS:
        assert scenario.steps
        assert any(step.expect for step in scenario.steps), scenario.name
        for step in scenario.steps:
            assert step.why, f"{scenario.name}/{step.tool} has no stated rationale"


def test_scenarios_cover_both_outcomes_and_failures():
    tools_exercised = {s.tool for sc in run_scenarios.SCENARIOS for s in sc.steps}
    assert tools_exercised == {
        "get_transactions", "evaluate_fraud_rules", "lookup_device_history", "flag_case",
    }
    expects_failure = [
        s for sc in run_scenarios.SCENARIOS for s in sc.steps if ("ok", False) in s.expect
    ]
    assert len(expects_failure) >= 3


@pytest.mark.parametrize("path", ["verdict.fired_rule_ids.0", "error.code", "missing.key"])
def test_dig_handles_paths_and_absences(path):
    payload = {"verdict": {"fired_rule_ids": ["STRUCTURING"]}, "error": {"code": "X"}}
    result = run_scenarios.dig(payload, path)
    assert result in ("STRUCTURING", "X", None)
