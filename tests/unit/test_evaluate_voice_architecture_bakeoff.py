"""Tests for the offline aggregate voice bakeoff evaluator."""

import pytest

from scripts.evaluate_voice_architecture_bakeoff import evaluate_ndjson, parse_ndjson


_BASE = {
    "schema_version": 1,
    "candidate_arm": "B1",
    "revision": "revision_1",
    "source_sha": "a" * 40,
    "manifest_digest": "b" * 64,
    "session": "c" * 24,
    "turn": 1,
    "act": "act_1",
}


def _line(event: str, **changes: object) -> dict[str, object]:
    return {**_BASE, "event": event, "ceiling_reached_candidate": False, **changes}


def _evaluate(lines: list[dict[str, object]], **changes: object) -> dict[str, object]:
    expected = {
        "expected_arm": "B1",
        "expected_revision": "revision_1",
        "expected_source_sha": "a" * 40,
        "expected_manifest_digest": "b" * 64,
        "expected_sessions": 1,
        "expected_turns": 1,
    }
    return evaluate_ndjson(lines, **{**expected, **changes})


def test_reports_aggregate_complete_cohort_only():
    report = _evaluate([_line("generated"), _line("sent"), _line("played")])

    assert report == {
        "status": "pass",
        "sessions": 1,
        "turns": 1,
        "terminal_counts": {"played": 1},
        "integrity_errors": 0,
    }


def test_rejects_unknown_fields_and_privacy_canaries():
    with pytest.raises(ValueError, match="unrecognized or missing field"):
        _evaluate([_line("generated", transcript="caller words")])

    with pytest.raises(ValueError, match="invalid revision identifier"):
        _evaluate([_line("generated", revision="caller_words")])


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", True, "invalid schema version"),
        ("candidate_arm", [], "invalid schema or arm"),
        ("event", {}, "invalid event"),
    ],
)
def test_rejects_malformed_allowed_field_values(
    field: str, value: object, error: str
):
    record = _line("generated")
    record[field] = value
    with pytest.raises(ValueError, match=error):
        _evaluate([record])

    with pytest.raises(ValueError, match="expected sessions must be positive"):
        _evaluate([], expected_sessions="1")  # type: ignore[arg-type]


def test_fails_contradictory_or_ceiling_hit_terminal_turns():
    report = _evaluate(
        [
            _line("generated"),
            _line("sent"),
            _line("played", ceiling_reached_candidate=True),
            _line("failed"),
        ]
    )

    assert report["status"] == "insufficient_evidence"
    assert report["integrity_errors"] == 1


def test_fails_a_consistent_but_wrong_cohort_and_incomplete_counts():
    wrong_cohort = [
        _line("generated", candidate_arm="B2"),
        _line("sent", candidate_arm="B2"),
        _line("played", candidate_arm="B2"),
    ]
    report = _evaluate(wrong_cohort)

    assert report["status"] == "insufficient_evidence"
    assert report["sessions"] == 0
    assert report["turns"] == 0

    short_report = _evaluate(
        [_line("generated"), _line("sent"), _line("played")],
        expected_sessions=2,
        expected_turns=2,
    )
    assert short_report["status"] == "insufficient_evidence"


def test_rejects_trailing_events_and_duplicate_json_keys():
    report = _evaluate(
        [_line("generated"), _line("sent"), _line("played"), _line("failed")]
    )
    assert report["status"] == "insufficient_evidence"

    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_ndjson(['{"schema_version": 1, "schema_version": 1}'])

    with pytest.raises(ValueError, match="event must be an object"):
        parse_ndjson(["[]"])
