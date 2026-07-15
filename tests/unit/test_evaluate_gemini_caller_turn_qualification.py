"""Independent evaluator tests for complete Gate 0B evidence."""

import base64
import json
from hashlib import sha256
from pathlib import Path
import stat

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest
import scripts.evaluate_gemini_caller_turn_qualification as evaluator_module

from app.services.caller_turn_measurement import (
    ACTIVITY_PRIMITIVE_SCHEMA_ID,
    NO_SPEECH_PRIMITIVE_SCHEMA_ID,
    ActivityPrimitiveRecord,
    CriticalSpanFact,
    NoSpeechPrimitiveRecord,
    build_signed_record_root,
)
from app.services.qualification_identity import canonical_json_bytes
from scripts.evaluate_gemini_caller_turn_qualification import (
    compute_evidence_context_commitment,
    compute_policy_lock_sha256,
    evaluate_custody_bundle,
    evaluate_evidence_artifact,
    main,
)
from scripts.run_gemini_caller_turn_qualification import build_preregistration


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
    "preregistration_sha256": "e" * 64,
    "campaign_approval_sha256": "f" * 64,
    "attempt_authorization_sha256": "1" * 64,
    "development_capsule_sha256": "2" * 64,
    "holdout_capsule_sha256": "3" * 64,
    "ledger_head_sha256": "4" * 64,
    "custodian_public_key_sha256": "5" * 64,
    "record_root_public_key_sha256": "6" * 64,
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
        else ("code_switch_english_to_language",)
        if ordinal % 16 == 1
        else ("code_switch_language_to_english",)
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
            no_speech_records=tuple(
                record for record in no_speech_records if record.split == "development"
            ),
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
            "cost_microusd": 903_000,
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


def _append_ledger_record(
    private_key: Ed25519PrivateKey,
    ledger: dict[str, object],
    *,
    event: str,
    phase_before: str,
    phase_after: str,
    attempt_id: str | None,
    body: dict[str, object],
    at: str,
) -> None:
    records = ledger["records"]
    assert isinstance(records, list)
    payload = {
        "schema_id": "gate_0b_custodian_ledger_record_v1",
        "ledger_instance_id": ledger["ledger_instance_id"],
        "campaign_id": ledger["campaign_id"],
        "authorization_id": ledger["authorization_id"],
        "preregistration_sha256": ledger["preregistration_sha256"],
        "source_sha": ledger["source_sha"],
        "ledger_location_sha256": ledger["ledger_location_sha256"],
        "sequence": len(records) + 1,
        "previous_hash": ledger["head_hash"],
        "event": event,
        "phase_before": phase_before,
        "phase_after": phase_after,
        "attempt_id": attempt_id,
        "at": at,
        "body": body,
    }
    entry = {
        "key_id": ledger["ledger_key_id"],
        "payload": payload,
        "signature": base64.b64encode(private_key.sign(canonical_json_bytes(payload))).decode(
            "ascii"
        ),
    }
    records.append(entry)
    ledger["head_hash"] = sha256(canonical_json_bytes(payload)).hexdigest()


def _signed_authorization(
    private_key: Ed25519PrivateKey,
    payload: dict[str, object],
    *,
    key_id: str,
) -> dict[str, object]:
    return {
        "key_id": key_id,
        "payload": payload,
        "signature": base64.b64encode(private_key.sign(canonical_json_bytes(payload))).decode(
            "ascii"
        ),
    }


def _custody_bundle():
    artifact, _ = _artifact()
    activity_records = tuple(
        ActivityPrimitiveRecord.from_dict(value) for value in artifact["activity_records"]
    )
    no_speech_records = tuple(
        NoSpeechPrimitiveRecord.from_dict(value) for value in artifact["no_speech_records"]
    )
    development_records = tuple(
        record for record in activity_records if record.split == "development"
    )
    holdout_records = tuple(record for record in activity_records if record.split == "holdout")
    development_windows = tuple(
        record for record in no_speech_records if record.split == "development"
    )
    holdout_windows = tuple(record for record in no_speech_records if record.split == "holdout")
    custodian_key = X25519PrivateKey.generate()
    root_key = Ed25519PrivateKey.generate()
    approval_key = Ed25519PrivateKey.generate()
    ledger_key = Ed25519PrivateKey.generate()
    approval_public_key = approval_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    custodian_public_key = custodian_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    root_public_key = root_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    ledger_public_key = ledger_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    development_envelope = {"kind": "development"}
    holdout_envelope = {"kind": "holdout"}
    development_digest = sha256(canonical_json_bytes(development_envelope)).hexdigest()
    holdout_digest = sha256(canonical_json_bytes(holdout_envelope)).hexdigest()
    source_sha = "b" * 40
    approval_key_id = "qualification_reviewer_1"
    preregistration = build_preregistration(
        {
            "schema_id": "gate_0b_preregistration_values_v1",
            "project": "kevin-qualification-test",
            "credential_reference": "qualification_secret_v1",
            "approval_key_id": approval_key_id,
            "approval_public_key_sha256": sha256(approval_public_key).hexdigest(),
            "custodian_key_id": "audit_custodian_1",
            "custodian_public_key_sha256": sha256(custodian_public_key).hexdigest(),
            "record_root_key_id": ROOT_KEY_ID,
            "record_root_public_key_sha256": sha256(root_public_key).hexdigest(),
            "ledger_instance_id": "ledger_instance_1",
            "ledger_custodian_key_id": "ledger_custodian_1",
            "ledger_custodian_public_key_sha256": sha256(ledger_public_key).hexdigest(),
            "source_sha": source_sha,
            "environment_identity_sha256": "b" * 64,
            "manifest_sha256": "c" * 64,
            "corpus_sha256": "d" * 64,
            "development_schedule_sha256": "e" * 64,
            "setup_sha256": "f" * 64,
            "pricing_sha256": sha256(
                Path("tests/fixtures/caller_turn_qualification/pricing.json").read_bytes()
            ).hexdigest(),
            "runner_sha256": sha256(
                Path("scripts/run_gemini_caller_turn_qualification.py").read_bytes()
            ).hexdigest(),
            "evaluator_sha256": sha256(Path(evaluator_module.__file__).read_bytes()).hexdigest(),
            "ledger_location_sha256": "7" * 64,
            "audit_capsule_location_sha256": "8" * 64,
            "evidence_location_sha256": "9" * 64,
            "consent_attestation_sha256": "a" * 64,
            "retention_attestation_sha256": "b" * 64,
            "zdr_or_residual_retention_acceptance_sha256": "c" * 64,
        }
    )
    campaign_payload = {
        "schema_id": "gate_0b_campaign_approval_v1",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "campaign_id": "campaign_1",
        "authorization_id": "authorization_1",
        "nonce": "nonce_1",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "source_sha": source_sha,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "max_attempts": 3,
        "max_provider_requests": 384,
        "max_cost_microusd": 30_000_000,
        "ledger_instance_id": "ledger_instance_1",
        "ledger_custodian_key_id": "ledger_custodian_1",
        "ledger_custodian_public_key_sha256": sha256(ledger_public_key).hexdigest(),
        "ledger_location_sha256": "7" * 64,
        "real_caller_data_authorized": False,
        "runtime_wiring_authorized": False,
        "deployment_authorized": False,
        "production_authorized": False,
        "release_authorized": False,
    }
    attempt_payload = {
        "schema_id": "gate_0b_attempt_authorization_v1",
        "campaign_id": "campaign_1",
        "authorization_id": "authorization_1",
        "attempt_id": "attempt_1",
        "attempt_index": 1,
        "prior_attempt_id": None,
        "outage_enum": None,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "source_sha": source_sha,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "provider_request_reservation": 128,
        "cost_reservation_microusd": 10_000_000,
    }
    campaign_envelope = _signed_authorization(
        approval_key,
        campaign_payload,
        key_id=approval_key_id,
    )
    attempt_envelope = _signed_authorization(
        approval_key,
        attempt_payload,
        key_id=approval_key_id,
    )
    campaign_approval_sha = sha256(canonical_json_bytes(campaign_payload)).hexdigest()
    authorization_sha = sha256(canonical_json_bytes(attempt_payload)).hexdigest()
    identities = {
        "source_sha256": sha256(source_sha.encode("ascii")).hexdigest(),
        "environment_sha256": "b" * 64,
        "evaluator_sha256": sha256(Path(evaluator_module.__file__).read_bytes()).hexdigest(),
        "corpus_sha256": "d" * 64,
        "pricing_sha256": preregistration["immutable_values"]["pricing_sha256"],
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "campaign_approval_sha256": campaign_approval_sha,
        "attempt_authorization_sha256": authorization_sha,
        "development_capsule_sha256": development_digest,
        "holdout_capsule_sha256": holdout_digest,
        "ledger_head_sha256": "0" * 64,
        "custodian_public_key_sha256": sha256(custodian_public_key).hexdigest(),
        "record_root_public_key_sha256": sha256(root_public_key).hexdigest(),
    }
    ledger = {
        "schema_id": "gate_0b_custodian_ledger_export_v1",
        "ledger_instance_id": "ledger_instance_1",
        "ledger_key_id": "ledger_custodian_1",
        "campaign_id": "campaign_1",
        "authorization_id": "authorization_1",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "source_sha": source_sha,
        "ledger_location_sha256": "7" * 64,
        "records": [],
        "head_hash": "0" * 64,
    }
    _append_ledger_record(
        ledger_key,
        ledger,
        event="genesis",
        phase_before="preregistered",
        phase_after="preregistered",
        attempt_id=None,
        body={
            "campaign_approval_sha256": campaign_approval_sha,
            "max_attempts": 3,
            "max_provider_requests": 384,
            "max_cost_microusd": 30_000_000,
        },
        at="2026-07-15T14:59:59Z",
    )
    _append_ledger_record(
        ledger_key,
        ledger,
        event="claim",
        phase_before="preregistered",
        phase_after="development_collection",
        attempt_id="attempt_1",
        body={
            "attempt_index": 1,
            "authorization_sha256": authorization_sha,
            "prior_attempt_id": None,
            "outage_enum": None,
            "provider_requests_reserved": 128,
            "cost_reserved_microusd": 10_000_000,
        },
        at="2026-07-15T15:00:00Z",
    )
    _append_ledger_record(
        ledger_key,
        ledger,
        event="development_checkpoint",
        phase_before="development_collection",
        phase_after="development_collection",
        attempt_id="attempt_1",
        body={
            "development_capsule_sha256": development_digest,
            "usage_evidence_sha256": "2" * 64,
            "actual_provider_requests": 64,
            "actual_cost_microusd": artifact["usage"]["cost_microusd"],
        },
        at="2026-07-15T15:10:00Z",
    )
    identities["ledger_head_sha256"] = ledger["head_hash"]
    policy_lock = compute_policy_lock_sha256(
        activity_records=development_records,
        no_speech_records=development_windows,
        candidate_policies_ms=POLICIES,
        selected_policy_ms=100,
        identities=identities,
    )
    _append_ledger_record(
        ledger_key,
        ledger,
        event="policy_lock",
        phase_before="development_collection",
        phase_after="policy_selection_locked",
        attempt_id="attempt_1",
        body={
            "development_ledger_head_sha256": identities["ledger_head_sha256"],
            "development_capsule_sha256": development_digest,
            "selected_policy_ms": 100,
            "policy_lock_sha256": policy_lock,
        },
        at="2026-07-15T15:11:00Z",
    )
    policy_lock_receipt = ledger["head_hash"]
    _append_ledger_record(
        ledger_key,
        ledger,
        event="holdout_release",
        phase_before="policy_selection_locked",
        phase_after="holdout_collection",
        attempt_id="attempt_1",
        body={
            "policy_lock_receipt_sha256": policy_lock_receipt,
            "selected_policy_ms": 100,
            "policy_lock_sha256": policy_lock,
            "holdout_manifest_sha256": "4" * 64,
            "release_nonce": "release_nonce_1",
        },
        at="2026-07-15T15:12:00Z",
    )
    _append_ledger_record(
        ledger_key,
        ledger,
        event="terminal_outcome",
        phase_before="holdout_collection",
        phase_after="completed",
        attempt_id="attempt_1",
        body={
            "outcome": "completed",
            "outage_enum": None,
            "holdout_capsule_sha256": holdout_digest,
            "usage_evidence_sha256": "6" * 64,
            "actual_provider_requests": 120,
            "actual_cost_microusd": artifact["usage"]["cost_microusd"],
        },
        at="2026-07-15T15:20:00Z",
    )
    bundle = {
        "schema_id": "gate_0b_custody_bundle_v1",
        "campaign_id": "campaign_1",
        "development_capsule": development_envelope,
        "holdout_capsule": holdout_envelope,
        "ledger": ledger,
        "preregistration": preregistration,
        "campaign_envelope": campaign_envelope,
        "attempt_envelope": attempt_envelope,
        "usage": artifact["usage"],
        "run_failures": [],
    }
    derived = {
        "development": (development_records, development_windows),
        "holdout": (holdout_records, holdout_windows),
    }
    return bundle, custodian_key, root_key, approval_public_key, ledger_public_key, derived


def test_custody_bundle_derives_records_and_phase_from_capsules_and_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        approval_public_key,
        ledger_public_key,
        derived,
    ) = _custody_bundle()
    opened: list[str] = []

    def open_capsule(envelope, **_kwargs):
        opened.append(envelope["kind"])
        return {"campaign_id": "campaign_1", "kind": envelope["kind"]}

    def derive(capsule, **_kwargs):
        return derived[capsule["kind"]]

    monkeypatch.setattr(evaluator_module, "open_audit_capsule", open_capsule)
    monkeypatch.setattr(evaluator_module, "derive_primitive_records_from_capsule", derive)
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: approval_public_key,
    )

    report = evaluate_custody_bundle(
        bundle,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )

    assert report["status"] == "pass"
    assert opened == ["development", "holdout"]
    assert "activity_records" not in bundle
    assert "phase_history" not in bundle
    assert "attempt_completed" not in bundle


def test_custody_bundle_rejects_prebuilt_primitives_and_gates_holdout_decryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        approval_public_key,
        ledger_public_key,
        derived,
    ) = _custody_bundle()
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: approval_public_key,
    )
    injected = dict(bundle)
    injected["activity_records"] = []
    injected_report = evaluate_custody_bundle(
        injected,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )
    assert injected_report["status"] == "no_go"
    assert injected_report["failures"] == {"custody_bundle_invalid": 1}

    failing_development = tuple(
        _activity_record(
            policy_ms=policy,
            ordinal=ordinal,
            split="development",
            assembly_failure=True,
        )
        for policy in POLICIES
        for ordinal in range(128)
    )
    opened: list[str] = []

    def open_capsule(envelope, **_kwargs):
        opened.append(envelope["kind"])
        if envelope["kind"] == "holdout":
            pytest.fail("holdout must remain encrypted when development has no passing policy")
        return {"campaign_id": "campaign_1", "kind": "development"}

    monkeypatch.setattr(evaluator_module, "open_audit_capsule", open_capsule)
    monkeypatch.setattr(
        evaluator_module,
        "derive_primitive_records_from_capsule",
        lambda _capsule, **_kwargs: (failing_development, derived["development"][1]),
    )

    blocked = evaluate_custody_bundle(
        bundle,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )

    assert blocked["status"] == "no_go"
    assert opened == ["development"]


def test_custody_bundle_rejects_replaced_approval_root_before_capsule_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        _approval_public_key,
        ledger_public_key,
        _derived,
    ) = _custody_bundle()
    opened: list[str] = []
    replacement = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: replacement,
    )
    monkeypatch.setattr(
        evaluator_module,
        "open_audit_capsule",
        lambda envelope, **_kwargs: opened.append(envelope["kind"]),
    )

    report = evaluate_custody_bundle(
        bundle,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )

    assert report["status"] == "no_go"
    assert report["failures"] == {"custody_bundle_invalid": 1}
    assert opened == []


def test_custody_bundle_rejects_tampered_authorization_before_capsule_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        approval_public_key,
        ledger_public_key,
        _derived,
    ) = _custody_bundle()
    bundle["campaign_envelope"]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    opened: list[str] = []
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: approval_public_key,
    )
    monkeypatch.setattr(
        evaluator_module,
        "open_audit_capsule",
        lambda envelope, **_kwargs: opened.append(envelope["kind"]),
    )

    report = evaluate_custody_bundle(
        bundle,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )

    assert report["status"] == "no_go"
    assert report["failures"] == {"custody_bundle_invalid": 1}
    assert opened == []


@pytest.mark.parametrize("mutation", ("signature", "public_key"))
def test_custody_bundle_rejects_substituted_ledger_custody_before_capsule_open(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        approval_public_key,
        ledger_public_key,
        _derived,
    ) = _custody_bundle()
    if mutation == "signature":
        bundle["ledger"]["records"][2]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    else:
        ledger_public_key = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
    opened: list[str] = []
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: approval_public_key,
    )
    monkeypatch.setattr(
        evaluator_module,
        "open_audit_capsule",
        lambda envelope, **_kwargs: opened.append(envelope["kind"]),
    )

    report = evaluate_custody_bundle(
        bundle,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )

    assert report["status"] == "no_go"
    assert report["failures"] == {"custody_bundle_invalid": 1}
    assert opened == []


@pytest.mark.parametrize("substitution", ("custodian", "record_root"))
def test_custody_bundle_rejects_substituted_preregistered_keys_before_capsule_open(
    substitution: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        approval_public_key,
        ledger_public_key,
        _derived,
    ) = _custody_bundle()
    opened: list[str] = []
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: approval_public_key,
    )
    monkeypatch.setattr(
        evaluator_module,
        "open_audit_capsule",
        lambda envelope, **_kwargs: opened.append(envelope["kind"]),
    )
    if substitution == "custodian":
        custodian_key = X25519PrivateKey.generate()
    else:
        root_key = Ed25519PrivateKey.generate()

    report = evaluate_custody_bundle(
        bundle,
        commitment_key=CAMPAIGN_KEY,
        custodian_private_key=custodian_key,
        expected_custodian_key_id="audit_custodian_1",
        ledger_custodian_public_key=ledger_public_key,
        record_root_signing_key=root_key,
        record_root_key_id=ROOT_KEY_ID,
    )

    assert report["status"] == "no_go"
    assert report["failures"] == {"custody_bundle_invalid": 1}
    assert opened == []


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


def test_cli_requires_custody_bundle_and_writes_only_a_private_aggregate_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        bundle,
        custodian_key,
        root_key,
        approval_public_key,
        ledger_public_key,
        derived,
    ) = _custody_bundle()
    bundle_path = tmp_path / "custody-bundle.json"
    commitment_path = tmp_path / "commitment.key"
    custodian_path = tmp_path / "custodian.key"
    root_path = tmp_path / "record-root.key"
    ledger_public_path = tmp_path / "ledger-custodian.pub"
    output = tmp_path / "report.json"
    bundle_path.write_text(json.dumps(bundle))
    commitment_path.write_bytes(CAMPAIGN_KEY)
    commitment_path.chmod(0o600)
    custodian_path.write_bytes(
        custodian_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    custodian_path.chmod(0o600)
    root_path.write_bytes(
        root_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    root_path.chmod(0o600)
    ledger_public_path.write_bytes(ledger_public_key)
    monkeypatch.setattr(
        evaluator_module,
        "open_audit_capsule",
        lambda envelope, **_kwargs: {
            "campaign_id": "campaign_1",
            "kind": envelope["kind"],
        },
    )
    monkeypatch.setattr(
        evaluator_module,
        "derive_primitive_records_from_capsule",
        lambda capsule, **_kwargs: derived[capsule["kind"]],
    )
    monkeypatch.setattr(
        evaluator_module,
        "_load_pinned_approval_public_key",
        lambda: approval_public_key,
    )

    exit_code = main(
        [
            "--bundle",
            str(bundle_path),
            "--commitment-key",
            str(commitment_path),
            "--custodian-private-key",
            str(custodian_path),
            "--custodian-key-id",
            "audit_custodian_1",
            "--ledger-custodian-public-key",
            str(ledger_public_path),
            "--record-root-signing-key",
            str(root_path),
            "--record-root-key-id",
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
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "--artifact" not in evaluator_module.build_parser().format_help()


@pytest.mark.parametrize("field", ("future_execution_authorized", "production_authorized"))
def test_all_nonauthorization_fields_remain_false(field: str) -> None:
    artifact, public_key = _artifact()
    report = _evaluate(artifact, public_key)

    assert report[field] is False
