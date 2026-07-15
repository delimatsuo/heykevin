"""Independent Gate 0B measurement and evidence-custody tests."""

import base64
from copy import deepcopy
from dataclasses import replace
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.exceptions import InvalidTag
import pytest

from app.services.caller_turn_alignment import (
    ActivityReference,
    AlignmentPolicy,
    CriticalSpan,
    CriticalSpanKind,
    FragmentMode,
)
from app.services.caller_turn_measurement import (
    ActivityMeasurementInput,
    ActivityPrimitiveRecord,
    CriticalSpanFact,
    MeasurementError,
    WireObservation,
    build_signed_record_root,
    derive_audit_capsule_accounting,
    derive_primitive_records_from_capsule,
    measure_activity,
    open_audit_capsule,
    require_reducer_agreement,
    seal_audit_capsule,
    verify_record_commitment,
    verify_signed_record_root,
)
from app.services.caller_turns import CallerTurnEvent, CallerTurnEventKind


CAMPAIGN_KEY = b"c" * 32


def _events(text: str = "book service today") -> tuple[CallerTurnEvent, ...]:
    return (
        CallerTurnEvent(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, 10, 1, 1, text),
        CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
    )


def _activity_input() -> ActivityMeasurementInput:
    reference = ActivityReference(
        3,
        "en",
        "book service today",
        critical_spans=(CriticalSpan(CriticalSpanKind.CORRECTION, "service today"),),
    )
    return ActivityMeasurementInput(
        policy_ms=250,
        activity_ordinal=3,
        split="development",
        language="en",
        condition="clean",
        scenario_tags=("standard",),
        references=(reference,),
        events=_events(),
        expected_epoch=1,
        expected_lifecycle_status="retrospective_complete",
        advance_to_ms=270,
        wire=WireObservation(timing_covered=True, first_audio_ms=500),
    )


def test_independent_reducers_must_agree_before_payload_discard() -> None:
    primary = _events()
    independent = tuple(primary)

    agreed = require_reducer_agreement(primary, independent)

    assert agreed == primary
    with pytest.raises(MeasurementError, match="reducer disagreement"):
        require_reducer_agreement(
            primary,
            (replace(primary[0], text="different"), primary[1]),
        )


def test_audit_capsule_is_allowlisted_and_sealed_to_custodian_key() -> None:
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    payload = {
        "schema_id": "gate_0b_audit_capsule_v4",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 1,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 300,
                    "input_audio_duration_ms": 20,
                    "output_audio_bytes": 48_000,
                    "input_audio_tokens": 8,
                    "output_audio_tokens": 4,
                    "input_text_tokens": 2,
                    "output_text_tokens": 1,
                }
            ],
        },
        "sessions": [
            {
                "session_ordinal": 1,
                "split": "development",
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 10,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "purpose recorded synthetic phrase",
                    }
                ],
                "wire_facts": [],
            }
        ],
        "activities": [
            {
                "activity_ordinal": 3,
                "session_ordinal": 1,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "purpose recorded synthetic phrase",
                "critical_spans": [
                    {"kind": "correction", "text": "synthetic phrase", "language": "en"}
                ],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "speech_end_at_ms": 80,
                "advance_to_ms": 300,
            }
        ],
        "no_speech_windows": [],
    }

    envelope = seal_audit_capsule(
        payload,
        custodian_public_key=public_key,
        custodian_key_id="audit_custodian_1",
    )

    assert "purpose recorded" not in json.dumps(envelope)
    assert open_audit_capsule(
        envelope,
        custodian_private_key=private_key,
        expected_key_id="audit_custodian_1",
    ) == payload
    with pytest.raises(InvalidTag):
        open_audit_capsule(
            envelope,
            custodian_private_key=X25519PrivateKey.generate(),
            expected_key_id="audit_custodian_1",
        )

    leaked = dict(payload)
    leaked["provider_request_id"] = "forbidden"
    with pytest.raises(MeasurementError, match="audit capsule field"):
        seal_audit_capsule(
            leaked,
            custodian_public_key=public_key,
            custodian_key_id="audit_custodian_1",
        )

    nested = deepcopy(payload)
    nested["no_speech_windows"] = [
        {
            "window_ordinal": 0,
            "split": "development",
            "condition": "silence",
            "wire_facts": [
                {
                    "kind": "false_activity",
                    "at_ms": 10,
                    "response_ordinal": None,
                    "activity_ordinal": None,
                    "sequence": 0,
                    "epoch": 1,
                    "audio_bytes": 0,
                    "secret_value": "forbidden",
                }
            ],
        }
    ]
    with pytest.raises(MeasurementError, match="wire fact"):
        seal_audit_capsule(
            nested,
            custodian_public_key=public_key,
            custodian_key_id="audit_custodian_1",
        )


def test_custodian_capsule_derives_all_development_policies_and_no_speech_records() -> None:
    capsule = {
        "schema_id": "gate_0b_audit_capsule_v4",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 1,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 300,
                    "input_audio_duration_ms": 20,
                    "output_audio_bytes": 48_000,
                    "input_audio_tokens": 8,
                    "output_audio_tokens": 4,
                    "input_text_tokens": 2,
                    "output_text_tokens": 1,
                },
                {
                    "kind": "no_speech_window",
                    "ordinal": 0,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 100,
                    "input_audio_duration_ms": 20,
                    "output_audio_bytes": 0,
                    "input_audio_tokens": 6,
                    "output_audio_tokens": 0,
                    "input_text_tokens": 1,
                    "output_text_tokens": 0,
                },
            ],
        },
        "sessions": [
            {
                "session_ordinal": 1,
                "split": "development",
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 10,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "book service today",
                    },
                    {
                        "kind": "turn_complete",
                        "at_ms": 20,
                        "sequence": 2,
                        "epoch": 1,
                        "text": "",
                    },
                ],
                "wire_facts": [
                    {
                        "kind": "caller_activity_start",
                        "at_ms": 0,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 0,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "caller_audio_sent",
                        "at_ms": 20,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 1,
                        "epoch": 1,
                        "audio_bytes": 640,
                    },
                    {
                        "kind": "caller_activity_end",
                        "at_ms": 100,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 2,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "response_open",
                        "at_ms": 220,
                        "response_ordinal": 1,
                        "activity_ordinal": 3,
                        "sequence": 3,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "audio_received",
                        "at_ms": 220,
                        "response_ordinal": 1,
                        "activity_ordinal": 3,
                        "sequence": 4,
                        "epoch": 1,
                        "audio_bytes": 48_000,
                    },
                    {
                        "kind": "response_terminal",
                        "at_ms": 230,
                        "response_ordinal": 1,
                        "activity_ordinal": 3,
                        "sequence": 5,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "teardown_complete",
                        "at_ms": 240,
                        "response_ordinal": None,
                        "activity_ordinal": None,
                        "sequence": 6,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "caller_speech_end",
                        "at_ms": 80,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 7,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                ],
            }
        ],
        "activities": [
            {
                "activity_ordinal": 3,
                "session_ordinal": 1,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "book service today",
                "critical_spans": [
                    {"kind": "correction", "text": "service today", "language": "en"}
                ],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "speech_end_at_ms": 80,
                "advance_to_ms": 900,
            }
        ],
        "no_speech_windows": [
            {
                "window_ordinal": 0,
                "split": "development",
                "condition": "silence",
                "wire_facts": [],
            }
        ],
    }

    _normalize_wire_sequences(capsule)
    activity_records, no_speech_records = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(100, 250, 500, 750),
        commitment_key=CAMPAIGN_KEY,
    )

    assert [record.policy_ms for record in activity_records] == [100, 250, 500, 750]
    assert all(record.first_audio_ms == 140 for record in activity_records)
    assert all(record.commitment for record in activity_records)
    assert len(no_speech_records) == 1
    assert no_speech_records[0].commitment

    usage, failures = derive_audit_capsule_accounting(capsule)
    assert usage == {
        "metadata_complete": True,
        "provider_requests": 2,
        "wall_clock_seconds": 1,
        "input_audio_seconds": 1,
        "output_audio_seconds": 1,
        "input_audio_tokens": 14,
        "output_audio_tokens": 4,
        "input_text_tokens": 3,
        "output_text_tokens": 1,
    }
    assert failures == ()


def _literal_wire_capsule() -> dict[str, object]:
    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        response_ordinal: int | None = None,
        activity_ordinal: int | None = 3,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response_ordinal,
            "activity_ordinal": activity_ordinal,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    return {
        "schema_id": "gate_0b_audit_capsule_v4",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 1,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 900,
                    "input_audio_duration_ms": 100,
                    "output_audio_bytes": 640,
                    "input_audio_tokens": 1,
                    "output_audio_tokens": 1,
                    "input_text_tokens": 0,
                    "output_text_tokens": 0,
                }
            ],
        },
        "sessions": [
            {
                "session_ordinal": 1,
                "split": "development",
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 10,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "book service today",
                    },
                    {
                        "kind": "turn_complete",
                        "at_ms": 20,
                        "sequence": 2,
                        "epoch": 1,
                        "text": "",
                    },
                ],
                "wire_facts": [
                    fact("caller_activity_start", 0, 0),
                    fact("caller_audio_sent", 20, 1, audio_bytes=640),
                    fact("caller_speech_end", 80, 2),
                    fact("caller_activity_end", 100, 3),
                    fact("response_open", 220, 4, response_ordinal=1),
                    fact(
                        "audio_received",
                        220,
                        5,
                        response_ordinal=1,
                        audio_bytes=640,
                    ),
                    fact("response_terminal", 230, 6, response_ordinal=1),
                    fact(
                        "teardown_complete",
                        240,
                        7,
                        activity_ordinal=None,
                    ),
                ],
            }
        ],
        "activities": [
            {
                "activity_ordinal": 3,
                "session_ordinal": 1,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "book service today",
                "critical_spans": [],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "speech_end_at_ms": 80,
                "advance_to_ms": 900,
            }
        ],
        "no_speech_windows": [],
    }


def _normalize_wire_sequences(capsule: dict[str, object]) -> None:
    sessions = capsule["sessions"]
    assert isinstance(sessions, list)
    facts = sessions[0]["wire_facts"]
    assert isinstance(facts, list)
    facts.sort(key=lambda value: (value["at_ms"], value["sequence"]))
    for sequence, fact in enumerate(facts):
        fact["sequence"] = sequence


@pytest.mark.parametrize(
    ("mutation", "field"),
    (
        ("premature", "premature_current_audio_count"),
        ("gap", "response_gap_violation_count"),
        ("after_terminal", "audio_after_terminal_count"),
        ("missing_terminal", "response_timeout_count"),
    ),
)
def test_literal_capsule_wire_mutations_derive_failing_primitives(
    mutation: str,
    field: str,
) -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    response_open = next(fact for fact in facts if fact["kind"] == "response_open")
    response_audio = next(fact for fact in facts if fact["kind"] == "audio_received")
    response_terminal = next(
        fact for fact in facts if fact["kind"] == "response_terminal"
    )
    teardown = next(fact for fact in facts if fact["kind"] == "teardown_complete")
    if mutation == "premature":
        response_open["at_ms"] = 70
        response_audio["at_ms"] = 70
    elif mutation == "gap":
        facts.append({**response_audio, "at_ms": 721})
        response_terminal["at_ms"] = 730
        teardown["at_ms"] = 740
    elif mutation == "after_terminal":
        facts.append({**response_audio, "at_ms": 235})
        teardown["at_ms"] = 240
    else:
        facts.remove(response_terminal)
    _normalize_wire_sequences(capsule)

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert getattr(records[0], field) == 1
    if mutation == "premature":
        assert records[0].first_audio_ms == 0


def test_capsule_first_audio_latency_uses_labeled_speech_end() -> None:
    capsule = _literal_wire_capsule()
    activity = capsule["activities"][0]  # type: ignore[index]
    activity.update(
        {
            "speech_end_at_ms": 80,
        }
    )

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert records[0].first_audio_ms == 140
    assert records[0].premature_current_audio_count == 0


def test_causal_cancellation_tail_distinguishes_zero_from_missing_evidence() -> None:
    capsule = _literal_wire_capsule()
    sessions = capsule["sessions"]
    activities = capsule["activities"]
    assert isinstance(sessions, list)
    assert isinstance(activities, list)
    session = sessions[0]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 50,
            "sequence": 1,
            "epoch": 1,
            "text": "first phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 120,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 250,
            "sequence": 3,
            "epoch": 1,
            "text": "second phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 320,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        response: int | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    session["wire_facts"] = [
        fact("caller_activity_start", 0, 0, activity=2),
        fact("caller_audio_sent", 20, 1, activity=2, audio_bytes=640),
        fact("caller_activity_end", 100, 2, activity=2),
        fact("response_open", 150, 3, activity=2, response=1),
        fact("audio_received", 150, 4, activity=2, response=1, audio_bytes=320),
        fact("tool_call_open", 160, 5, activity=2),
        fact("caller_activity_start", 200, 6, activity=3),
        fact("caller_audio_sent", 250, 7, activity=3, audio_bytes=640),
        fact("audio_received", 250, 8, activity=2, response=1, audio_bytes=320),
        fact("tool_call_cancelled", 250, 9, activity=3),
        fact("interrupted", 250, 10, activity=3),
        fact("response_terminal", 250, 11, activity=2, response=1),
        fact("caller_activity_end", 300, 12, activity=3),
        fact("response_open", 400, 13, activity=3, response=2),
        fact("audio_received", 400, 14, activity=3, response=2, audio_bytes=320),
        fact("response_terminal", 410, 15, activity=3, response=2),
        fact("teardown_complete", 420, 16, activity=None),
    ]
    session["wire_facts"].extend(
        (
            fact("caller_speech_end", 80, 17, activity=2),
            fact("caller_speech_end", 280, 18, activity=3),
        )
    )
    activities[:] = [
        {
            "activity_ordinal": 2,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "interaction_stress",
            "scenario_tags": ["synchronous_tool_use"],
            "reference_text": "first phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 80,
            "advance_to_ms": 800,
        },
        {
            "activity_ordinal": 3,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "interaction_stress",
            "scenario_tags": ["tool_cancellation_interruption"],
            "reference_text": "second phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 280,
            "advance_to_ms": 800,
        },
    ]

    _normalize_wire_sequences(capsule)
    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )
    cancellation = next(record for record in records if record.activity_ordinal == 3)
    assert cancellation.interruption_tail_ms == 0

    no_open = deepcopy(capsule)
    no_open["sessions"][0]["wire_facts"] = [  # type: ignore[index]
        fact
        for fact in no_open["sessions"][0]["wire_facts"]  # type: ignore[index]
        if fact["kind"] != "tool_call_open"
    ]
    _normalize_wire_sequences(no_open)
    records, _ = derive_primitive_records_from_capsule(
        no_open,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )
    cancellation = next(record for record in records if record.activity_ordinal == 3)
    assert cancellation.interruption_tail_ms is None

    wrong_epoch = deepcopy(capsule)
    open_fact = next(
        fact
        for fact in wrong_epoch["sessions"][0]["wire_facts"]  # type: ignore[index]
        if fact["kind"] == "tool_call_open"
    )
    open_fact["epoch"] = 2
    records, _ = derive_primitive_records_from_capsule(
        wrong_epoch,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )
    cancellation = next(record for record in records if record.activity_ordinal == 3)
    assert cancellation.interruption_tail_ms is None


def test_multi_activity_capsule_attributes_one_assembled_turn_per_activity() -> None:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 50,
            "sequence": 1,
            "epoch": 1,
            "text": "first phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 100,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 500,
            "sequence": 3,
            "epoch": 1,
            "text": "second phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 550,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        response: int | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    session["wire_facts"] = [
        fact("caller_activity_start", 0, 0, activity=2),
        fact("caller_audio_sent", 20, 1, activity=2, audio_bytes=640),
        fact("caller_activity_end", 100, 2, activity=2),
        fact("response_open", 200, 3, activity=2, response=1),
        fact("audio_received", 200, 4, activity=2, response=1, audio_bytes=320),
        fact("response_terminal", 220, 5, activity=2, response=1),
        fact("caller_activity_start", 450, 6, activity=3),
        fact("caller_audio_sent", 470, 7, activity=3, audio_bytes=640),
        fact("caller_activity_end", 550, 8, activity=3),
        fact("response_open", 650, 9, activity=3, response=2),
        fact("audio_received", 650, 10, activity=3, response=2, audio_bytes=320),
        fact("response_terminal", 670, 11, activity=3, response=2),
        fact("teardown_complete", 700, 12, activity=None),
    ]
    session["wire_facts"].extend(
        (
            fact("caller_speech_end", 80, 13, activity=2),
            fact("caller_speech_end", 530, 14, activity=3),
        )
    )
    capsule["activities"] = [
        {
            "activity_ordinal": 2,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "first phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 80,
            "advance_to_ms": 900,
        },
        {
            "activity_ordinal": 3,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "second phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 530,
            "advance_to_ms": 900,
        },
    ]

    _normalize_wire_sequences(capsule)
    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    by_activity = {record.activity_ordinal: record for record in records}
    assert {
        ordinal: (
            record.assignment_status,
            record.observed_lifecycle_status,
            record.assembled_turn_count,
            record.duplicate_count,
        )
        for ordinal, record in by_activity.items()
    } == {
        2: ("matched", "retrospective_complete", 1, 0),
        3: ("matched", "retrospective_complete", 1, 0),
    }
    assert by_activity[2].hypothesis_characters == len("firstphrase")
    assert by_activity[3].hypothesis_characters == len("secondphrase")


def test_multi_activity_capsule_does_not_reassign_reversed_exact_transcripts() -> None:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 50,
            "sequence": 1,
            "epoch": 1,
            "text": "second phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 100,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 500,
            "sequence": 3,
            "epoch": 1,
            "text": "first phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 550,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        response: int | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    session["wire_facts"] = [
        fact("caller_activity_start", 0, 0, activity=2),
        fact("caller_audio_sent", 20, 1, activity=2, audio_bytes=640),
        fact("caller_activity_end", 100, 2, activity=2),
        fact("response_open", 200, 3, activity=2, response=1),
        fact("audio_received", 200, 4, activity=2, response=1, audio_bytes=320),
        fact("response_terminal", 220, 5, activity=2, response=1),
        fact("caller_activity_start", 450, 6, activity=3),
        fact("caller_audio_sent", 470, 7, activity=3, audio_bytes=640),
        fact("caller_activity_end", 550, 8, activity=3),
        fact("response_open", 650, 9, activity=3, response=2),
        fact("audio_received", 650, 10, activity=3, response=2, audio_bytes=320),
        fact("response_terminal", 670, 11, activity=3, response=2),
        fact("teardown_complete", 700, 12, activity=None),
    ]
    session["wire_facts"].extend(
        (
            fact("caller_speech_end", 80, 13, activity=2),
            fact("caller_speech_end", 530, 14, activity=3),
        )
    )
    capsule["activities"] = [
        {
            "activity_ordinal": 2,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "first phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 80,
            "advance_to_ms": 900,
        },
        {
            "activity_ordinal": 3,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "second phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 530,
            "advance_to_ms": 900,
        },
    ]

    _normalize_wire_sequences(capsule)
    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert all(record.contamination_count == 1 for record in records)
    assert all(
        record.substitutions + record.insertions + record.deletions > 0
        for record in records
    )


def test_measurement_detects_foreign_fragment_even_when_whole_turn_matches() -> None:
    current = ActivityReference(3, "en", "book service today with customer details")
    foreign = ActivityReference(4, "en", "intruder token")
    measurement = replace(
        _activity_input(),
        references=(current, foreign),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                current.text,
            ),
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                15,
                2,
                1,
                foreign.text,
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 3, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.contamination_count == 1


def test_duplicate_terminal_cannot_hide_audio_after_the_first_terminal() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    terminal = next(fact for fact in facts if fact["kind"] == "response_terminal")
    facts.append({**terminal, "at_ms": 240})
    _normalize_wire_sequences(capsule)

    with pytest.raises(MeasurementError, match="response-terminal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_orphan_terminal_is_rejected_by_capsule_recomputation() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    terminal = next(fact for fact in facts if fact["kind"] == "response_terminal")
    facts.append({**terminal, "at_ms": 225, "response_ordinal": 99})
    _normalize_wire_sequences(capsule)

    with pytest.raises(MeasurementError, match="response-terminal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_audio_reusing_closed_response_ordinal_with_wrong_identity_is_rejected() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    response_audio = next(fact for fact in facts if fact["kind"] == "audio_received")
    facts.append({**response_audio, "at_ms": 235, "activity_ordinal": 4})
    _normalize_wire_sequences(capsule)

    with pytest.raises(MeasurementError, match="causally attributable"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_response_activity_label_must_match_latest_actual_caller_audio() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    for fact in facts:
        if fact["kind"] in {"response_open", "audio_received", "response_terminal"}:
            fact["activity_ordinal"] = 4

    with pytest.raises(MeasurementError, match="ownership is not causal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_capsule_accounting_rejects_missing_units_identity_and_unbounded_failures() -> None:
    capsule = {
        "schema_id": "gate_0b_audit_capsule_v4",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 9,
                    "metadata_complete": False,
                    "complete": False,
                    "error_code": "private provider exception text",
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 1,
                    "input_audio_duration_ms": 1,
                    "output_audio_bytes": 0,
                    "input_audio_tokens": 0,
                    "output_audio_tokens": 0,
                    "input_text_tokens": 0,
                    "output_text_tokens": 0,
                }
            ],
        },
        "sessions": [],
        "activities": [],
        "no_speech_windows": [],
    }

    with pytest.raises(MeasurementError, match="accounting"):
        derive_audit_capsule_accounting(capsule)


def test_measurement_recomputes_alignment_lifecycle_and_keyed_commitment() -> None:
    record = measure_activity(
        _activity_input(),
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.observed_lifecycle_status == "retrospective_complete"
    assert record.assembled_turn_count == 1
    assert record.reference_characters == record.hypothesis_characters
    assert (record.substitutions, record.insertions, record.deletions) == (0, 0, 0)
    assert record.reference_words == record.hypothesis_words == 3
    assert record.critical_spans == (
        CriticalSpanFact(kind="correction", exact=True),
    )
    assert verify_record_commitment(record, commitment_key=CAMPAIGN_KEY) is True

    tampered = replace(record, substitutions=1)
    assert verify_record_commitment(tampered, commitment_key=CAMPAIGN_KEY) is False


def test_measurement_counts_prior_epoch_events_as_stale_and_fails_fidelity() -> None:
    current = _activity_input()
    measurement = replace(
        current,
        expected_epoch=2,
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                5,
                1,
                1,
                "stale prior epoch text",
            ),
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                2,
                2,
                "book service today",
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 3, 2),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.stale_count == 1
    assert record.cross_epoch_acceptance_count == 0
    assert record.observed_lifecycle_status == "retrospective_complete"
    assert record.insertions + record.substitutions + record.deletions > 0


def test_measurement_preserves_low_fidelity_and_duplicate_failures() -> None:
    duplicated = replace(
        _activity_input(),
        events=(
            *_events("book service someday"),
            CallerTurnEvent(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, 300, 3, 1, "extra"),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 310, 4, 1),
        ),
        advance_to_ms=600,
    )

    record = measure_activity(
        duplicated,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assembled_turn_count == 2
    assert record.duplicate_count == 1
    assert record.late_fragment_mutation_count == 1
    assert record.substitutions > 0 or record.insertions > 0 or record.deletions > 0


def test_primitive_cross_fields_prevent_wer_and_ambiguity_bypass() -> None:
    record = measure_activity(
        _activity_input(),
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    with pytest.raises(MeasurementError, match="requires word measurements"):
        replace(
            record,
            reference_words=None,
            hypothesis_words=None,
            word_substitutions=None,
            word_insertions=None,
            word_deletions=None,
        )
    with pytest.raises(MeasurementError, match="ambiguous assignment"):
        replace(record, assignment_status="ambiguous")


def test_signed_merkle_root_binds_complete_order_independent_record_set() -> None:
    first = measure_activity(
        _activity_input(),
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )
    second = replace(
        first,
        activity_ordinal=4,
        commitment="",
    )
    second = ActivityPrimitiveRecord.with_commitment(second, commitment_key=CAMPAIGN_KEY)
    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    signed = build_signed_record_root(
        activity_records=(second, first),
        no_speech_records=(),
        campaign_id="campaign_1",
        signing_key=signing_key,
        key_id="evidence_custodian_1",
    )

    assert verify_signed_record_root(
        signed,
        activity_records=(first, second),
        no_speech_records=(),
        campaign_id="campaign_1",
        public_key=public_key,
        expected_key_id="evidence_custodian_1",
    ) is True
    changed_signature = dict(signed)
    changed_signature["signature"] = base64.b64encode(b"x" * 64).decode("ascii")
    assert verify_signed_record_root(
        changed_signature,
        activity_records=(first, second),
        no_speech_records=(),
        campaign_id="campaign_1",
        public_key=public_key,
        expected_key_id="evidence_custodian_1",
    ) is False
