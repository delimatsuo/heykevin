"""Replay tests for receptionist planner regressions."""

from pathlib import Path

from app.services.receptionist_replay import load_replay_fixture, run_replay_scenario
from app.services.receptionist_state import ServiceAction


FIXTURE_DIR = Path("tests/fixtures/receptionist_replays")


def test_known_caller_toilet_replacement_replay_blocks_duplicate_question():
    scenario = load_replay_fixture(FIXTURE_DIR / "known_caller_toilet_replacement.json")

    result = run_replay_scenario(scenario)

    assert result.violations == []
    assert result.final_state.service_object == "toilet"
    assert result.final_state.service_action == ServiceAction.REPLACE
    first_step = result.steps[0]
    assert "service_action" in first_step.next_action.forbidden_slots
    assert "service_object" in first_step.next_action.forbidden_slots
    assert "callback_number" in first_step.next_action.forbidden_slots
    assert "service_address" in first_step.next_action.forbidden_slots
    assert "whether this is repair, replacement, installation, or inspection" in first_step.instructions


def test_product_acceptance_replay_scenarios_have_no_policy_violations():
    scenarios = load_replay_fixture(FIXTURE_DIR / "product_acceptance_scenarios.json")

    for scenario in scenarios:
        result = run_replay_scenario(scenario)

        assert result.violations == [], scenario["scenario"]
