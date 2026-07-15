"""Tests for the offline caller-turn assembly evaluator."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts.evaluate_caller_turn_assembly import (
    EVIDENCE_FIELDS,
    evaluate_caller_turn_fixture,
    main,
)


FIXTURE_PATH = Path("tests/fixtures/caller_turn_events/permutations.json")
SOURCE_SHA = "a" * 40
MODEL_ID = "gemini-3.1-flash-live-preview"


def test_evaluator_reports_exact_identity_and_aggregate_fixture_result():
    report = evaluate_caller_turn_fixture(
        FIXTURE_PATH,
        source_sha=SOURCE_SHA,
        model_id=MODEL_ID,
    )

    assert report["status"] == "pass"
    assert report["scope"] == "offline_synthetic_permutations"
    assert report["identity"] == {
        "fixture_sha256": sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
        "source_sha": SOURCE_SHA,
        "model_id": MODEL_ID,
        "caller_turn_schema_version": 1,
        "gemini_event_schema_version": 1,
    }
    assert report["sample"]["scenarios"] == 10
    assert report["sample"]["failed_scenarios"] == 0
    assert report["sample"]["observed_turns"] == 9
    assert report["failures"] == {}
    assert set(report["metrics"]["completion_status_counts"]) <= {
        "retrospective_complete",
        "partial",
        "cancelled",
        "dropped",
    }


def test_evaluator_pins_nonauthorization_evidence_taxonomy():
    report = evaluate_caller_turn_fixture(
        FIXTURE_PATH,
        source_sha=SOURCE_SHA,
        model_id=MODEL_ID,
    )

    assert all(field in report for field in EVIDENCE_FIELDS)
    assert all(report[field] is False for field in EVIDENCE_FIELDS)
    assert report["release_authorized"] is False
    assert report["provider_execution_authorized"] is False


def test_evaluator_report_never_contains_fixture_transcripts_or_raw_events():
    fixture = json.loads(FIXTURE_PATH.read_text())
    report = evaluate_caller_turn_fixture(
        FIXTURE_PATH,
        source_sha=SOURCE_SHA,
        model_id=MODEL_ID,
    )
    serialized = json.dumps(report, sort_keys=True)

    for case in fixture["cases"]:
        for event in case["events"]:
            if event.get("text"):
                assert event["text"] not in serialized
        for turn in case["expected_turns"]:
            if turn.get("transcript"):
                assert turn["transcript"] not in serialized
    assert "events" not in report
    assert "expected_turns" not in report


def test_evaluator_fails_closed_with_bounded_code_for_invalid_fixture(tmp_path):
    fixture = tmp_path / "invalid.json"
    fixture.write_text('{"version": 1, "cases": "private-invalid-shape"}')

    report = evaluate_caller_turn_fixture(
        fixture,
        source_sha=SOURCE_SHA,
        model_id=MODEL_ID,
    )

    assert report["status"] == "fail"
    assert report["failures"] == {"fixture_invalid": 1}
    assert "private-invalid-shape" not in json.dumps(report)
    assert report["release_authorized"] is False


def test_evaluator_fails_closed_for_missing_fixture(tmp_path):
    report = evaluate_caller_turn_fixture(
        tmp_path / "missing.json",
        source_sha=SOURCE_SHA,
        model_id=MODEL_ID,
    )

    assert report["status"] == "fail"
    assert report["identity"]["fixture_sha256"] is None
    assert report["failures"] == {"fixture_unavailable": 1}
    assert report["release_authorized"] is False


@pytest.mark.parametrize(
    ("source_sha", "model_id"),
    [
        ("short", MODEL_ID),
        (SOURCE_SHA, "latest"),
        (SOURCE_SHA, "gemini-latest"),
    ],
)
def test_evaluator_requires_exact_source_and_model_identity(source_sha, model_id):
    with pytest.raises(ValueError):
        evaluate_caller_turn_fixture(
            FIXTURE_PATH,
            source_sha=source_sha,
            model_id=model_id,
        )


def test_cli_writes_only_redacted_fixture_report(tmp_path):
    output = tmp_path / "report.json"

    exit_code = main(
        [
            "--fixture",
            str(FIXTURE_PATH),
            "--source-sha",
            SOURCE_SHA,
            "--model-id",
            MODEL_ID,
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    persisted = json.loads(output.read_text())
    assert persisted["status"] == "pass"
    assert persisted["provider_execution_authorized"] is False
    assert "Need a plumber" not in output.read_text()


def test_new_offline_modules_are_not_imported_by_live_pipelines():
    for path in (
        Path("app/services/gemini_pipeline.py"),
        Path("app/services/voice_pipeline.py"),
    ):
        source = path.read_text()
        assert "caller_turns" not in source
        assert "gemini_turn_events" not in source
