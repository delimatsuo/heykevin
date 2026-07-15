from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import struct

import pytest

from app.services.caller_turn_qualification import (
    AUDIT_EVENT_SCHEMA_ID,
    CORPUS_SCHEMA_ID,
    PRICING_SCHEMA_ID,
    AuditEvent,
    CampaignPhase,
    PrimitiveRecord,
    QualificationContractError,
    assert_payload_safe,
    compute_twilio_roundtrip_sha256,
    empty_evidence_flags,
    load_corpus_manifest,
    load_pricing,
    validate_phase_transition,
)


FIXTURE_ROOT = Path("tests/fixtures/caller_turn_qualification")
LANGUAGES = ("en", "es", "pt", "fr", "zh", "hi", "ar", "ht")
SPLITS = ("development", "holdout")
PRIMARY_CONDITIONS = (
    "clean",
    "twilio_codec_only",
    "acoustic_impairment",
    "interaction_stress",
)
STRESS_TAGS = (
    "jitter_packet_loss",
    "clipping",
    "echo_crosstalk",
    "far_field_low_volume",
    "background_noise",
    "long_pause",
    "fast_speech",
    "correction",
    "number_dictation",
    "synchronous_tool_use",
    "tool_cancellation_interruption",
    "fresh_connection_restart",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _write_pcm(path: Path, *, ordinal: int, silent: bool = False) -> tuple[str, str]:
    sample = 0 if silent else 500 + ordinal
    samples = [sample] * 320
    if silent:
        samples[-1] = ordinal % 5
    payload = struct.pack(f"<{len(samples)}h", *samples)
    path.write_bytes(payload)
    return sha256(payload).hexdigest(), compute_twilio_roundtrip_sha256(payload)


def _ready_manifest(tmp_path: Path) -> dict[str, object]:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    subjects: list[dict[str, object]] = []
    sessions: list[dict[str, object]] = []
    activities: list[dict[str, object]] = []
    no_speech_windows: list[dict[str, object]] = []

    activity_ordinal = 0
    session_ordinal = 0
    for language in LANGUAGES:
        for split in SPLITS:
            subject_ids = [f"subject_{language}_{split}_{index}" for index in range(2)]
            for subject_id in subject_ids:
                subjects.append(
                    {
                        "subject_id": subject_id,
                        "language": language,
                        "split": split,
                        "adult_attested": True,
                        "consent_status": "active",
                        "consent_version": "gate0b-v1",
                        "consent_record_sha256": sha256(subject_id.encode()).hexdigest(),
                        "rights": "gate_0b_qualification_only",
                    }
                )

            for session_index in range(4):
                session_id = f"session_{session_ordinal:03d}"
                session_ordinal += 1
                session_activities: list[str] = []
                sessions.append(
                    {
                        "session_id": session_id,
                        "language": language,
                        "split": split,
                        "subject_id": subject_ids[session_index % 2],
                        "activity_ids": session_activities,
                    }
                )
                for within_session in range(4):
                    activity_id = f"activity_{activity_ordinal:03d}"
                    session_activities.append(activity_id)
                    audio_path = corpus_root / f"{activity_id}.pcm"
                    audio_sha, roundtrip_sha = _write_pcm(
                        audio_path,
                        ordinal=activity_ordinal,
                    )
                    position = session_index * 4 + within_session
                    tags = [STRESS_TAGS[position % len(STRESS_TAGS)]]
                    if language != "en" and position == 0:
                        tags.append("code_switch_english_to_language")
                    if language != "en" and position == 1:
                        tags.append("code_switch_language_to_english")
                    activities.append(
                        {
                            "activity_id": activity_id,
                            "session_id": session_id,
                            "subject_id": subject_ids[session_index % 2],
                            "language": language,
                            "split": split,
                            "primary_condition": PRIMARY_CONDITIONS[position // 4],
                            "scenario_tags": tags,
                            "audio_path": str(audio_path.relative_to(corpus_root)),
                            "audio_sha256": audio_sha,
                            "twilio_roundtrip_sha256": roundtrip_sha,
                            "sample_rate_hz": 16_000,
                            "duration_ms": 20,
                            "speech_start_ms": 0,
                            "speech_end_ms": 20,
                            "script_sha256": sha256(f"script:{activity_id}".encode()).hexdigest(),
                            "script_provenance": "synthetic_v1",
                        }
                    )
                    activity_ordinal += 1

    for ordinal in range(64):
        window_id = f"window_{ordinal:03d}"
        audio_path = corpus_root / f"{window_id}.pcm"
        audio_sha, roundtrip_sha = _write_pcm(audio_path, ordinal=ordinal, silent=True)
        no_speech_windows.append(
            {
                "window_id": window_id,
                "split": SPLITS[ordinal % 2],
                "condition": "silence" if ordinal % 2 == 0 else "background_noise",
                "audio_path": str(audio_path.relative_to(corpus_root)),
                "audio_sha256": audio_sha,
                "twilio_roundtrip_sha256": roundtrip_sha,
                "sample_rate_hz": 16_000,
                "duration_ms": 20,
            }
        )

    return {
        "schema_id": CORPUS_SCHEMA_ID,
        "collection_status": "ready",
        "corpus_root": str(corpus_root),
        "lower_resource_language": "ht",
        "attestations": {
            "consent_registry_sha256": "a" * 64,
            "holdout_custodian_sha256": "b" * 64,
            "paid_project_attestation_sha256": "c" * 64,
            "retention_policy_sha256": "d" * 64,
            "provider_retention_decision": "zdr_verified",
            "real_call_data_prohibited": True,
            "production_audio_prohibited": True,
            "voiceprint_extraction_prohibited": True,
        },
        "subjects": subjects,
        "sessions": sessions,
        "activities": activities,
        "no_speech_windows": no_speech_windows,
    }


def test_pending_example_is_schema_valid_but_not_execution_ready() -> None:
    summary = load_corpus_manifest(
        FIXTURE_ROOT / "gate-0b-corpus-v1.example.json",
        require_ready=False,
    )

    assert summary.schema_id == CORPUS_SCHEMA_ID
    assert summary.execution_ready is False
    assert summary.activity_count == 0


def test_ready_manifest_enforces_exact_matrix_and_audio(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)

    summary = load_corpus_manifest(manifest_path, require_ready=True)

    assert summary.execution_ready is True
    assert summary.activity_count == 256
    assert summary.holdout_activity_count == 128
    assert summary.no_speech_window_count == 64
    assert summary.language_counts == {language: 32 for language in LANGUAGES}


def test_unknown_manifest_field_fails_closed(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)
    manifest["unexpected"] = True
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)

    with pytest.raises(QualificationContractError, match="unknown manifest field"):
        load_corpus_manifest(manifest_path, require_ready=True)


def test_subject_cannot_cross_development_and_holdout(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)
    first_subject = manifest["subjects"][0]["subject_id"]  # type: ignore[index]
    holdout = next(
        activity
        for activity in manifest["activities"]  # type: ignore[union-attr]
        if activity["split"] == "holdout"
    )
    holdout["subject_id"] = first_subject
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)

    with pytest.raises(QualificationContractError, match="subject.*split"):
        load_corpus_manifest(manifest_path, require_ready=True)


def test_ready_manifest_rejects_wrong_language_cardinality(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)
    manifest["activities"].pop()  # type: ignore[union-attr]
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)

    with pytest.raises(QualificationContractError, match="exactly 256"):
        load_corpus_manifest(manifest_path, require_ready=True)


def test_audio_digest_and_roundtrip_digest_are_verified(tmp_path: Path) -> None:
    manifest = _ready_manifest(tmp_path)
    activity = manifest["activities"][0]  # type: ignore[index]
    activity["twilio_roundtrip_sha256"] = "f" * 64
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)

    with pytest.raises(QualificationContractError, match="roundtrip digest mismatch"):
        load_corpus_manifest(manifest_path, require_ready=True)


def test_pricing_fixture_is_strict_and_computes_bounded_cost() -> None:
    pricing = load_pricing(FIXTURE_ROOT / "pricing.json")

    assert pricing.schema_id == PRICING_SCHEMA_ID
    assert pricing.model == "gemini-3.1-flash-live-preview"
    assert pricing.cost_usd(input_audio_tokens=1_000_000, output_audio_tokens=1_000_000) == 15

    raw = json.loads((FIXTURE_ROOT / "pricing.json").read_text())
    raw["unknown"] = True
    with pytest.raises(QualificationContractError, match="unknown pricing field"):
        load_pricing(raw)


def test_campaign_phases_are_forward_only() -> None:
    validate_phase_transition(CampaignPhase.PREREGISTERED, CampaignPhase.DEVELOPMENT_COLLECTION)
    validate_phase_transition(
        CampaignPhase.DEVELOPMENT_COLLECTION,
        CampaignPhase.POLICY_SELECTION_LOCKED,
    )

    with pytest.raises(QualificationContractError, match="phase transition"):
        validate_phase_transition(
            CampaignPhase.HOLDOUT_COLLECTION,
            CampaignPhase.DEVELOPMENT_COLLECTION,
        )


def test_audit_event_schema_allows_only_recomputable_fields() -> None:
    event = AuditEvent.from_dict(
        {
            "schema_id": AUDIT_EVENT_SCHEMA_ID,
            "activity_ordinal": 3,
            "at_ms": 120,
            "sequence": 4,
            "epoch": 1,
            "kind": "input_transcript_fragment",
            "text": "purpose-recorded phrase",
        }
    )
    assert event.activity_ordinal == 3

    with pytest.raises(QualificationContractError, match="unknown audit event field"):
        AuditEvent.from_dict(
            {
                "schema_id": AUDIT_EVENT_SCHEMA_ID,
                "activity_ordinal": 3,
                "at_ms": 120,
                "sequence": 4,
                "epoch": 1,
                "kind": "input_transcript_fragment",
                "text": "purpose-recorded phrase",
                "provider_request_id": "forbidden",
            }
        )


def test_primitive_record_and_published_report_are_payload_free() -> None:
    record = PrimitiveRecord.from_dict(
        {
            "schema_id": "gate_0b_primitive_v1",
            "activity_ordinal": 7,
            "split": "holdout",
            "language": "es",
            "assignment_status": "assigned",
            "lifecycle_status": "retrospective_complete",
            "fragment_count": 2,
            "event_count": 4,
            "reference_codepoints": 10,
            "hypothesis_codepoints": 9,
            "substitutions": 1,
            "insertions": 0,
            "deletions": 0,
            "cer_micros": 100_000,
            "wer_micros": 100_000,
            "ambiguity_margin_micros": 500_000,
            "critical_spans_exact": True,
            "contamination_count": 0,
            "duplicate_count": 0,
            "late_fragment_mutation_count": 0,
            "first_audio_ms": 800,
            "interruption_tail_ms": None,
            "error_code": None,
            "commitment": "e" * 64,
        }
    )

    report = {
        "schema_id": "gate_0b_report_v1",
        "evidence": empty_evidence_flags(),
        "records": [record.to_dict()],
    }
    assert_payload_safe(report)
    assert "not allowed" not in json.dumps(report)
    assert all(value is False for value in report["evidence"].values())

    leaked = deepcopy(report)
    leaked["transcript"] = "not allowed"
    with pytest.raises(QualificationContractError, match="forbidden payload field"):
        assert_payload_safe(leaked)
