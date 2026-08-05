"""Aggregate replay certification for multilingual urgency corrections."""

import json
from pathlib import Path
import subprocess
import sys

from app.services.urgency_replay import (
    evaluate_urgency_replays,
    load_urgency_replay_fixture,
)


FIXTURE = Path("tests/fixtures/urgency_replays/multilingual_corrections.json")


def test_multilingual_correction_fixture_passes_release_gates():
    scenarios = load_urgency_replay_fixture(FIXTURE)

    report = evaluate_urgency_replays(scenarios)

    assert report["status"] == "pass"
    assert report["sample"] == {
        "scenarios": 26,
        "languages": 2,
        "correction_scenarios": 9,
    }
    assert report["violation_counts"] == {}
    assert all(gate["passed"] for gate in report["gates"])
    rendered = json.dumps(report)
    assert "english_active_smoke" not in rendered
    assert "spanish_active_gas_leak" not in rendered
    assert "There is smoke" not in rendered
    assert "fuga de gas en la cocina" not in rendered


def test_urgency_replay_gate_fails_mismatch_and_malformed_scenarios():
    report = evaluate_urgency_replays(
        [
            {
                "scenario": "mismatch",
                "language": "en",
                "tags": ["correction"],
                "text": "There is smoke.",
                "expected_signal": None,
            },
            {"scenario": "malformed", "language": "es"},
        ]
    )

    assert report["status"] == "fail"
    assert report["violation_counts"] == {
        "classifier_mismatch": 1,
        "invalid_scenario": 1,
    }
    rendered = json.dumps(report)
    assert '"scenario": "mismatch"' not in rendered
    assert '"scenario": "malformed"' not in rendered
    assert '"text"' not in rendered


def test_urgency_replay_cli_prints_aggregate_json_only():
    result = subprocess.run(
        [sys.executable, "scripts/evaluate_urgency_replays.py"],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert report["status"] == "pass"
    assert report["sample"]["scenarios"] == 26
    assert "english_active_smoke" not in result.stdout
    assert "spanish_active_gas_leak" not in result.stdout
    assert '"text"' not in result.stdout
    assert "fuga de gas" not in result.stdout
