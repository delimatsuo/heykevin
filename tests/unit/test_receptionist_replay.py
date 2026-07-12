"""Replay tests for receptionist planner regressions."""

from pathlib import Path

from app.services.receptionist_replay import (
    ReplaySuiteThresholds,
    evaluate_replay_suite,
    load_replay_fixture,
    run_replay_scenario,
)
from app.services.receptionist_state import IntakeState, ServiceAction


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


def test_enterprise_replay_scenarios_cover_multi_turn_policy_and_timing():
    scenarios = load_replay_fixture(FIXTURE_DIR / "enterprise_controller_scenarios.json")

    assert len(scenarios) >= 10
    for scenario in scenarios:
        assert scenario["policy"]["require_metrics"] is True
        assert any(turn["speaker"] == "assistant" for turn in scenario["turns"])

        result = run_replay_scenario(scenario)

        assert result.violations == [], scenario["scenario"]


def test_replay_commits_only_completed_assistant_questions():
    scenario = {
        "scenario": "interrupted_clarifying_question",
        "policy": {
            "require_metrics": True,
            "max_response_first_audio_ms": 1500,
            "max_generated_audio_ms": 6000,
        },
        "initial_state": IntakeState.new(call_sid="CA_redacted").to_dict(),
        "turns": [
            {
                "speaker": "caller",
                "text": "I need plumbing help.",
                "expect": {
                    "action_name": "ask_one_clarifying_question",
                    "allowed_slots": ["service_action"],
                },
            },
            {
                "speaker": "assistant",
                "text": "Is this a repair, replacement, installation, or inspection?",
                "interrupted": True,
                "observed": {"asked_slots": ["service_action"]},
                "metrics": {
                    "response_first_audio_ms": 800,
                    "generated_audio_ms": 1200,
                },
                "expect": {"unasked_slots_state": ["service_action"]},
            },
            {
                "speaker": "caller",
                "text": "Sorry, go ahead.",
                "expect": {
                    "action_name": "ask_one_clarifying_question",
                    "allowed_slots": ["service_action"],
                },
            },
            {
                "speaker": "assistant",
                "text": "Is this a repair, replacement, installation, or inspection?",
                "observed": {"asked_slots": ["service_action"]},
                "metrics": {
                    "response_first_audio_ms": 700,
                    "generated_audio_ms": 1200,
                },
                "expect": {"asked_slots_state": ["service_action"]},
            },
        ],
    }

    result = run_replay_scenario(scenario)

    assert result.violations == []
    assert result.final_state.asked_slots == {"service_action"}
    assert result.steps[1].interrupted is True
    assert result.steps[3].response_first_audio_ms == 700


def test_replay_detects_output_latency_privacy_and_side_effect_violations():
    scenario = {
        "scenario": "bad_model_output",
        "policy": {
            "require_metrics": True,
            "max_questions": 1,
            "max_response_first_audio_ms": 1500,
            "max_generated_audio_ms": 6000,
        },
        "initial_state": IntakeState.new(call_sid="CA_redacted").to_dict(),
        "turns": [
            {"speaker": "caller", "text": "I need to replace a toilet."},
            {
                "speaker": "assistant",
                "text": (
                    "Jobber says SENSITIVE_SENTINEL. Is this repair or replacement? "
                    "What is the service address?"
                ),
                "observed": {
                    "asked_slots": ["service_action", "service_address"],
                },
                "metrics": {
                    "response_first_audio_ms": 3000,
                    "generated_audio_ms": 9000,
                },
                "tool_calls": ["create_job"],
            },
        ],
    }

    result = run_replay_scenario(scenario)

    assert {
        "assistant_private_source",
        "assistant_secret_marker",
        "assistant_question_count",
        "response_first_audio_budget",
        "generated_audio_budget",
        "assistant_forbidden_slot",
        "assistant_tool_call_forbidden",
    } <= set(result.violation_codes)


def test_replay_catches_long_generated_answer_separately_from_start_latency():
    scenario = {
        "scenario": "staging_long_answer_regression",
        "policy": {
            "require_metrics": True,
            "max_response_first_audio_ms": 1500,
            "max_generated_audio_ms": 6000,
        },
        "initial_state": IntakeState.new(call_sid="CA_redacted").to_dict(),
        "turns": [
            {"speaker": "caller", "text": "How much to replace a faucet?"},
            {
                "speaker": "assistant",
                "text": "Pricing depends on the installation. Is the existing faucet still installed?",
                "observed": {"asked_slots": ["job_complexity"]},
                "metrics": {
                    "response_first_audio_ms": 920,
                    "generated_audio_ms": 8840,
                },
            },
        ],
    }

    result = run_replay_scenario(scenario)

    assert result.violation_codes == ("generated_audio_budget",)


def test_enterprise_replay_suite_gate_reports_aggregate_pass_without_scenario_data():
    scenarios = load_replay_fixture(FIXTURE_DIR / "enterprise_controller_scenarios.json")

    report = evaluate_replay_suite(scenarios)

    assert report["status"] == "pass"
    assert report["sample"]["scenarios"] >= 10
    assert report["sample"]["assistant_turns"] >= 10
    assert report["sample"]["interrupted_assistant_turns"] >= 1
    assert report["sample"]["metric_coverage"] == 1.0
    assert report["violation_counts"] == {}
    serialized = str(report)
    assert "caller_correction_replaces_stale_facts" not in serialized
    assert "Actually, it is the sink" not in serialized


def test_replay_suite_gate_fails_small_or_violating_sample():
    scenario = {
        "scenario": "small_bad_sample",
        "policy": {"require_metrics": True, "max_questions": 1},
        "initial_state": IntakeState.new(call_sid="CA_redacted").to_dict(),
        "turns": [
            {"speaker": "caller", "text": "I need a faucet repair."},
            {
                "speaker": "assistant",
                "text": "What happened? When did it start?",
                "metrics": {
                    "response_first_audio_ms": 700,
                    "generated_audio_ms": 1200,
                },
            },
        ],
    }
    thresholds = ReplaySuiteThresholds(
        min_scenarios=2,
        min_assistant_turns=2,
        min_interrupted_assistant_turns=1,
    )

    report = evaluate_replay_suite([scenario], thresholds=thresholds)
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}

    assert report["status"] == "fail"
    assert {
        "minimum_scenarios",
        "minimum_assistant_turns",
        "minimum_interrupted_assistant_turns",
        "policy_violations",
    } <= failed
    assert report["violation_counts"] == {"assistant_question_count": 1}
