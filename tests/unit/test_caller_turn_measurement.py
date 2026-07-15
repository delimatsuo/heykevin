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
        "schema_id": "gate_0b_audit_capsule_v1",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "activities": [
            {
                "activity_ordinal": 3,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "purpose recorded synthetic phrase",
                "critical_spans": [
                    {"kind": "correction", "text": "synthetic phrase", "language": "en"}
                ],
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 10,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "purpose recorded synthetic phrase",
                    }
                ],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "advance_to_ms": 300,
                "wire_facts": [],
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
