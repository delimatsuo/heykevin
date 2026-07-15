"""Independent evaluator tests for complete Gate 0B evidence."""

import json
from hashlib import sha256
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from app.services.caller_turn_measurement import (
    ACTIVITY_PRIMITIVE_SCHEMA_ID,
    NO_SPEECH_PRIMITIVE_SCHEMA_ID,
    ActivityPrimitiveRecord,
    CriticalSpanFact,
    NoSpeechPrimitiveRecord,
    build_signed_record_root,
)
from scripts.evaluate_gemini_caller_turn_qualification import (
    compute_evidence_context_commitment,
    compute_policy_lock_sha256,
    evaluate_evidence_artifact,
    main,
)


CAMPAIGN_KEY = b"k" * 32
LANGUAGES = ("en", "es", "pt", "fr", "zh", "hi", "ar", "ht")
CONDITIONS = ("clean", "twilio_codec_only", "acoustic_impairment", "interaction_stress")
POLICIES = (100, 250, 500, 750)
ROOT_KEY_ID = "evidence_custodian_1"
IDENTITIES = {
    "source_sha256": "a" * 64,
    "environment_sha256": "b" * 64,
    "evaluator_sha256": "c" * 64,
    "corpus_sha256": "d" * 64,
    "pricing_sha256": sha256(
        Path("tests/fixtures/caller_turn_qualification/pricing.json").read_bytes()
    ).hexdigest(),
}


def _activity_record(
    *,
    policy_ms: int,
    ordinal: int,
    split: str,
    assembly_failure: bool = False,
    rare_failure: bool = False,
    wire_drift: bool = False,
) -> ActivityPrimitiveRecord:
    language = LANGUAGES[ordinal % len(LANGUAGES)]
    interruption = ordinal % 16 == 0
    kinds = (
        "digits",
        "negation",
        "correction",
        "identity_confusable",
        "english_to_language",
        "language_to_english",
    )
    scenario_tags = (
        ("tool_cancellation_interruption",)
        if interruption
        else (
            "code_switch_english_to_language",
        )
        if ordinal % 16 == 1
        else (
            "code_switch_language_to_english",
        )
        if ordinal % 16 == 2
        else ("standard",)
    )
    word_values = (None, None, None, None, None) if language == "zh" else (3, 3, 0, 0, 0)
    record = ActivityPrimitiveRecord(
        schema_id=ACTIVITY_PRIMITIVE_SCHEMA_ID,
        policy_ms=policy_ms,
        activity_ordinal=ordinal,
        split=split,
        language=language,
        condition="rare" if rare_failure else CONDITIONS[(ordinal // 8) % len(CONDITIONS)],
        scenario_tags=scenario_tags,
        assignment_status="matched",
        expected_lifecycle_status="retrospective_complete",
        observed_lifecycle_status="missing" if assembly_failure else "retrospective_complete",
        assembled_turn_count=0 if assembly_failure else 1,
        fragment_count=1,
        event_count=2,
        reference_characters=10,
        hypothesis_characters=10,
        substitutions=0,
        insertions=0,
        deletions=0,
        reference_words=word_values[0],
        hypothesis_words=word_values[1],
        word_substitutions=word_values[2],
        word_insertions=word_values[3],
        word_deletions=word_values[4],
        ambiguity_margin_micros=500_000,
        critical_spans=(
            CriticalSpanFact(kind=kinds[ordinal % len(kinds)], exact=not rare_failure),
        ),
        contamination_count=0,
        duplicate_count=0,
        cross_epoch_acceptance_count=0,
        late_fragment_mutation_count=0,
        stale_count=0,
        timing_covered=True,
        first_audio_ms=501 if wire_drift else 500,
        interruption_tail_ms=100 if interruption else None,
        premature_current_audio_count=0,
        audio_after_terminal_count=0,
        response_gap_violation_count=0,
        abnormal_close_count=0,
        runaway_output_count=0,
        response_timeout_count=0,
        malformed_count=0,
        teardown_violation_count=0,
        error_code=None,
        commitment="",
    )
    return ActivityPrimitiveRecord.with_commitment(record, commitment_key=CAMPAIGN_KEY)


def _no_speech_record(ordinal: int) -> NoSpeechPrimitiveRecord:
    record = NoSpeechPrimitiveRecord(
        schema_id=NO_SPEECH_PRIMITIVE_SCHEMA_ID,
        window_ordinal=ordinal,
        split="development" if ordinal % 2 == 0 else "holdout",
        condition="silence" if ordinal % 2 == 0 else "background_noise",
        false_activity_count=0,
        model_audio_chunk_count=0,
        abnormal_close_count=0,
        audio_after_teardown_count=0,
        error_code=None,
        commitment="",
    )
    return NoSpeechPrimitiveRecord.with_commitment(record, commitment_key=CAMPAIGN_KEY)


def _artifact(
    *,
    selected_policy_ms: int = 100,
    failing_policies: frozenset[int] = frozenset(),
    attempt_completed: bool = True,
    rare_holdout_failure: bool = False,
    extra_holdout_policy: int | None = None,
    drift_policy_evidence: bool = False,
) -> tuple[dict[str, object], bytes]:
    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    development = tuple(
        _activity_record(
            policy_ms=policy,
            ordinal=ordinal,
            split="development",
            assembly_failure=policy in failing_policies,
            wire_drift=drift_policy_evidence and policy == 750 and ordinal == 0,
        )
        for policy in POLICIES
        for ordinal in range(128)
    )
    holdout = tuple(
        _activity_record(
            policy_ms=selected_policy_ms,
            ordinal=ordinal,
            split="holdout",
            rare_failure=rare_holdout_failure and ordinal == 128,
        )
        for ordinal in range(128, 256)
    )
    if extra_holdout_policy is not None:
        holdout += (
            _activity_record(
                policy_ms=extra_holdout_policy,
                ordinal=128,
                split="holdout",
            ),
        )
    activity_records = development + holdout
    no_speech_records = tuple(_no_speech_record(ordinal) for ordinal in range(64))
    signed_root = build_signed_record_root(
        activity_records=activity_records,
        no_speech_records=no_speech_records,
        campaign_id="campaign_1",
        signing_key=signing_key,
        key_id=ROOT_KEY_ID,
    )
    artifact = {
        "schema_id": "gate_0b_evidence_v1",
        "campaign_id": "campaign_1",
        "attempt_completed": attempt_completed,
        "phase_history": [
            "preregistered",
            "development_collection",
            "policy_selection_locked",
            "holdout_collection",
            "completed",
        ],
        "candidate_policies_ms": list(POLICIES),
        "selected_policy_ms": selected_policy_ms,
        "holdout_materialized_after_lock": True,
        "identities": IDENTITIES,
        "policy_lock_sha256": compute_policy_lock_sha256(
            activity_records=development,
            candidate_policies_ms=POLICIES,
            selected_policy_ms=selected_policy_ms,
            identities=IDENTITIES,
        ),
        "activity_records": [record.to_dict() for record in activity_records],
        "no_speech_records": [record.to_dict() for record in no_speech_records],
        "signed_record_root": signed_root,
        "usage": {
            "metadata_complete": True,
            "provider_requests": 64,
            "wall_clock_seconds": 600,
            "input_audio_seconds": 300,
            "output_audio_seconds": 100,
            "cost_microusd": 901_500,
            "input_audio_tokens": 100_000,
            "output_audio_tokens": 50_000,
            "input_text_tokens": 1_000,
            "output_text_tokens": 500,
        },
        "run_failures": [],
    }
    artifact["context_commitment"] = compute_evidence_context_commitment(
        artifact,
        commitment_key=CAMPAIGN_KEY,
    )
    return artifact, public_key


def _evaluate(artifact: dict[str, object], public_key: bytes) -> dict[str, object]:
    return evaluate_evidence_artifact(
        artifact,
        commitment_key=CAMPAIGN_KEY,
        root_public_key=public_key,
        expected_root_key_id=ROOT_KEY_ID,
    )


def test_evaluator_recomputes_complete_gate_and_publishes_only_aggregates() -> None:
    artifact, public_key = _artifact()

    report = _evaluate(artifact, public_key)

    assert report["status"] == "pass"
    assert report["selected_policy_ms"] == 100
    assert report["assembly_sample_passed"] is True
    assert report["transcription_fidelity_sample_passed"] is True
    assert report["provider_interaction_integrity_sample_passed"] is True
    assert report["gate_0b_sample_passed"] is True
    assert report["release_authorized"] is False
    assert report["runtime_wiring_authorized"] is False
    assert report["record_root"]["leaf_count"] == 704
    assert report["samples"]["development"]["assembly_rate"]["denominator"] == 128
    assert report["samples"]["holdout"]["assembly_rate"]["numerator"] == 128
    assert report["samples"]["holdout"]["assembly_rate"]["confidence_95_micros"]
    assert report["samples"]["holdout"]["cer"] == {
        "edit_operations": 0,
        "reference_units": 1_280,
        "rate_micros": 0,
    }
    serialized = json.dumps(report, sort_keys=True)
    assert "activity_records" not in serialized
    assert "no_speech_records" not in serialized
    assert "commitment" not in serialized


def test_policy_selection_uses_lowest_passing_development_policy_only() -> None:
    artifact, public_key = _artifact(
        selected_policy_ms=250,
        failing_policies=frozenset({100}),
    )

    report = _evaluate(artifact, public_key)

    assert report["status"] == "pass"
    assert report["selected_policy_ms"] == 250
    assert report["candidate_policy_results"]["100"]["passed"] is False
    assert report["candidate_policy_results"]["250"]["passed"] is True

    wrong, wrong_key = _artifact(
        selected_policy_ms=100,
        failing_policies=frozenset({100}),
    )
    blocked = _evaluate(wrong, wrong_key)
    assert blocked["status"] == "no_go"
    assert "policy_selection_mismatch" in blocked["failures"]


def test_nonselected_holdout_policy_and_phase_or_partial_runs_fail_closed() -> None:
    extra, extra_key = _artifact(extra_holdout_policy=250)
    extra_report = _evaluate(extra, extra_key)
    assert extra_report["status"] == "no_go"
    assert "holdout_policy_violation" in extra_report["failures"]

    partial, partial_key = _artifact(attempt_completed=False)
    partial["phase_history"] = partial["phase_history"][:-1]
    partial_report = _evaluate(partial, partial_key)
    assert partial_report["status"] == "no_go"
    assert {"attempt_incomplete", "phase_history_invalid"} <= set(partial_report["failures"])
    assert "context_commitment_invalid" in partial_report["failures"]
    assert partial_report["gate_0b_sample_passed"] is False


def test_policy_replays_cannot_drift_immutable_wire_or_reference_facts() -> None:
    artifact, public_key = _artifact(drift_policy_evidence=True)

    report = _evaluate(artifact, public_key)

    assert report["status"] == "no_go"
    assert "development_policy_evidence_mismatch" in report["failures"]


def test_small_cells_are_suppressed_only_after_internal_failure_veto() -> None:
    artifact, public_key = _artifact(rare_holdout_failure=True)

    report = _evaluate(artifact, public_key)

    assert report["status"] == "no_go"
    assert report["transcription_fidelity_sample_passed"] is False
    rare = report["samples"]["holdout"]["conditions"]["rare"]
    assert rare == {"count": 1, "suppressed": True}


def test_contradictory_or_tampered_records_never_reach_metric_algebra() -> None:
    artifact, public_key = _artifact()
    artifact["activity_records"][0]["substitutions"] = 100

    report = _evaluate(artifact, public_key)

    assert report["status"] == "no_go"
    assert report["failures"] == {"artifact_invalid": 1}
    assert report["gate_0b_sample_passed"] is False


def test_cli_writes_payload_safe_report_and_uses_external_key_material(tmp_path: Path) -> None:
    artifact, public_key = _artifact()
    artifact_path = tmp_path / "evidence.json"
    commitment_path = tmp_path / "commitment.key"
    public_path = tmp_path / "root.pub"
    output = tmp_path / "report.json"
    artifact_path.write_text(json.dumps(artifact))
    commitment_path.write_bytes(CAMPAIGN_KEY)
    commitment_path.chmod(0o600)
    public_path.write_bytes(public_key)

    exit_code = main(
        [
            "--artifact",
            str(artifact_path),
            "--commitment-key",
            str(commitment_path),
            "--root-public-key",
            str(public_path),
            "--root-key-id",
            ROOT_KEY_ID,
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    persisted = json.loads(output.read_text())
    assert persisted["status"] == "pass"
    assert persisted["release_authorized"] is False
    assert CAMPAIGN_KEY.hex() not in output.read_text()


@pytest.mark.parametrize("field", ("future_execution_authorized", "production_authorized"))
def test_all_nonauthorization_fields_remain_false(field: str) -> None:
    artifact, public_key = _artifact()
    report = _evaluate(artifact, public_key)

    assert report[field] is False
