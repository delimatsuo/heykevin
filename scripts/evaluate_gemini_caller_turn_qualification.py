#!/usr/bin/env python3
"""Independently evaluate payload-free Gemini caller-turn Gate 0B evidence."""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_CEILING
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
_STARTUP_MARKER_ENV = "KEVIN_GATE0B_TRUSTED_STARTUP"
_TRUSTED_STARTUP_FLAGS = (
    sys.flags.isolated == 1
    and sys.flags.no_site == 1
    and sys.flags.ignore_environment == 1
    and sys.flags.no_user_site == 1
    and sys.flags.safe_path is True
)
if __name__ == "__main__" and (
    _STARTUP_MARKER_ENV not in os.environ or not _TRUSTED_STARTUP_FLAGS
):
    print('{"error_code":"qualification_startup_required","status":"blocked"}')
    raise SystemExit(2)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: E402
    X25519PrivateKey,
)

from app.services.caller_turn_measurement import (  # noqa: E402
    POLICIES_MS,
    ActivityPrimitiveRecord,
    MeasurementError,
    NoSpeechPrimitiveRecord,
    SIGNED_ROOT_SCHEMA_ID,
    build_signed_record_root,
    combined_usage_evidence_sha256,
    compute_record_merkle_root,
    derive_audit_capsule_accounting,
    derive_primitive_records_from_capsule,
    open_audit_capsule,
    verify_record_commitment,
    verify_signed_record_root,
    usage_evidence_sha256,
)
from app.services.caller_turn_qualification import (  # noqa: E402
    assert_payload_safe,
    empty_evidence_flags,
    load_pricing,
)
from app.services.qualification_allocation import (  # noqa: E402
    AllocationActivity,
    AllocationError,
    NoSpeechAllocation,
    validate_gate0b_allocation,
)
from app.services.qualification_identity import (  # noqa: E402
    IdentityError,
    canonical_json_bytes,
    capture_trusted_startup_identity,
    verify_attempt_authorization,
    verify_campaign_approval,
)
from app.services.qualification_environment import (  # noqa: E402
    execution_identity_report_sha256,
    validate_execution_identity_report,
)
from app.services.qualification_ledger import (  # noqa: E402
    validate_custody_ledger_snapshot,
)
from app.services.qualification_privacy import verify_privacy_custody  # noqa: E402
from app.services.qualification_private_paths import (  # noqa: E402
    PrivatePathError,
    read_private_file,
    write_private_file,
)
from scripts.run_gemini_caller_turn_qualification import (  # noqa: E402
    PREREGISTRATION_EXTERNAL_FIELDS,
    SessionExecutionConfig,
    build_gate0b_setup_identity,
    build_preregistration,
)


EVIDENCE_SCHEMA_ID = "gate_0b_evidence_v3"
CUSTODY_BUNDLE_SCHEMA_ID = "gate_0b_custody_bundle_v2"
EVALUATION_RESULT_SCHEMA_ID = "gate_0b_evaluation_result_v1"
REPORT_SCHEMA_ID = "gate_0b_evaluation_report_v3"
PUBLICATION_SIGNATURE_SCHEMA_ID = "gate_0b_publication_signature_v1"
PUBLICATION_SIGNATURE_DOMAIN = b"gate-0b-canonical-publication-v1\x00"
EXPECTED_POLICIES = (100, 250, 500, 750)
EXPECTED_PHASE_HISTORY = (
    "preregistered",
    "development_collection",
    "policy_selection_locked",
    "holdout_collection",
    "completed",
)
EXPECTED_DEVELOPMENT_ACTIVITIES = 128
EXPECTED_HOLDOUT_ACTIVITIES = 128
EXPECTED_NO_SPEECH_WINDOWS = 64
SMALL_CELL_MINIMUM = 8
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
PRICING_PATH = REPO_ROOT / "tests/fixtures/caller_turn_qualification/pricing.json"
PINNED_APPROVAL_ROOT_PATH = REPO_ROOT / "config/qualification/gate_0b_approval_root.ed25519.pub"
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PROVIDER_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
THRESHOLDS = {
    "assembly_rate_micros": 990_000,
    "cer_overall_micros": 50_000,
    "cer_stratum_micros": 100_000,
    "wer_overall_micros": 100_000,
    "wer_language_micros": 150_000,
    "first_audio_p95_ms": 1_500,
    "first_audio_max_ms": 2_500,
    "interruption_tail_p95_ms": 250,
    "interruption_tail_max_ms": 500,
}
APPLICABLE_CRITICAL_SPANS = {
    "number_dictation": "digits",
    "correction": "correction",
    "code_switch_english_to_language": "english_to_language",
    "code_switch_language_to_english": "language_to_english",
}
EVIDENCE_CONTEXT_HMAC_DOMAIN = b"gate-0b-evidence-context-v3\x00"
FINAL_IDENTITY_FIELDS = frozenset(
    {
        "source_sha256",
        "source_fact_bundle_sha256",
        "environment_sha256",
        "evaluator_sha256",
        "corpus_sha256",
        "pricing_sha256",
        "preregistration_sha256",
        "campaign_approval_sha256",
        "attempt_authorization_sha256",
        "development_capsule_sha256",
        "holdout_capsule_sha256",
        "ledger_head_sha256",
        "custodian_public_key_sha256",
        "record_root_public_key_sha256",
    }
)
POLICY_LOCK_IDENTITY_FIELDS = FINAL_IDENTITY_FIELDS - {
    "holdout_capsule_sha256",
    "ledger_head_sha256",
}
REQUIRED_CRITICAL_KINDS = frozenset(
    {
        "digits",
        "negation",
        "correction",
        "identity_confusable",
        "english_to_language",
        "language_to_english",
    }
)
CODE_SWITCH_TAGS = frozenset(
    {
        "code_switch_english_to_language",
        "code_switch_language_to_english",
    }
)
LIFECYCLE_STATUSES = (
    "retrospective_complete",
    "partial",
    "cancelled",
    "dropped",
    "missing",
    "duplicate",
)
TIMING_SCOPE = {
    "basis": "provider_receipt_time_proxy",
    "population": "scheduled_cases_only",
    "caller_heard_slo_validated": False,
}
COMPONENT_DIGEST_FIELDS = frozenset(
    {
        "adapter_sha256",
        "assembler_sha256",
        "corpus_sha256",
        "evaluator_sha256",
        "manifest_sha256",
        "pricing_sha256",
        "prompt_sha256",
        "renderer_sha256",
        "runner_sha256",
        "setup_sha256",
        "tool_sha256",
    }
)
CARDINALITY_REQUIREMENTS = {
    "campaign_count": 1,
    "attempt_count": 1,
    "phase_count": len(EXPECTED_PHASE_HISTORY),
    "phase_transition_count": len(EXPECTED_PHASE_HISTORY) - 1,
    "candidate_policy_count": len(EXPECTED_POLICIES),
    "language_count": 8,
    "logical_session_count": 48,
    "connection_count": 128,
    "epoch_count": 64,
    "fresh_restart_count": 16,
    "development_scheduled_activity_count": EXPECTED_DEVELOPMENT_ACTIVITIES,
    "development_policy_record_count": EXPECTED_DEVELOPMENT_ACTIVITIES * len(EXPECTED_POLICIES),
    "holdout_scheduled_activity_count": EXPECTED_HOLDOUT_ACTIVITIES,
    "scheduled_activity_count": EXPECTED_DEVELOPMENT_ACTIVITIES + EXPECTED_HOLDOUT_ACTIVITIES,
    "development_no_speech_window_count": EXPECTED_NO_SPEECH_WINDOWS // 2,
    "holdout_no_speech_window_count": EXPECTED_NO_SPEECH_WINDOWS // 2,
    "no_speech_window_count": EXPECTED_NO_SPEECH_WINDOWS,
    "primitive_record_count": (
        EXPECTED_DEVELOPMENT_ACTIVITIES * len(EXPECTED_POLICIES)
        + EXPECTED_HOLDOUT_ACTIVITIES
        + EXPECTED_NO_SPEECH_WINDOWS
    ),
    "provider_request_count": 128,
}


class EvaluationError(ValueError):
    """Raised when an evidence artifact is structurally invalid."""


def compute_policy_lock_sha256(
    *,
    activity_records: tuple[ActivityPrimitiveRecord, ...],
    no_speech_records: tuple[NoSpeechPrimitiveRecord, ...],
    candidate_policies_ms: tuple[int, ...],
    selected_policy_ms: int,
    identities: Mapping[str, str],
) -> str:
    """Bind development evidence, identities, thresholds, and selected policy."""
    if not activity_records or any(record.split != "development" for record in activity_records):
        raise EvaluationError("policy lock requires development records only")
    if any(record.split != "development" for record in no_speech_records):
        raise EvaluationError("policy lock requires development no-speech records only")
    _validate_policy_configuration(candidate_policies_ms, selected_policy_ms)
    identity = _validate_identities(identities)
    value = {
        "schema_id": "gate_0b_policy_lock_v1",
        "development_record_root_sha256": compute_record_merkle_root(
            activity_records=activity_records,
            no_speech_records=no_speech_records,
        ),
        "candidate_policies_ms": list(candidate_policies_ms),
        "selected_policy_ms": selected_policy_ms,
        "selection_rule": "lowest_passing_quiescence_ms",
        "thresholds": THRESHOLDS,
        "identities": {field: identity[field] for field in sorted(POLICY_LOCK_IDENTITY_FIELDS)},
    }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def compute_evidence_context_commitment(
    artifact: Mapping[str, Any],
    *,
    commitment_key: bytes,
) -> str:
    """Bind all non-record evidence metadata to the campaign commitment key."""
    if not isinstance(commitment_key, bytes) or len(commitment_key) != 32:
        raise EvaluationError("commitment key must be exactly 32 bytes")
    fields = {
        "schema_id",
        "campaign_id",
        "attempt_authorization_validated",
        "authorization_consumed",
        "provider_execution_started",
        "attempt_completed",
        "phase_history",
        "candidate_policies_ms",
        "selected_policy_ms",
        "holdout_materialized_after_lock",
        "identities",
        "policy_lock_sha256",
        "signed_record_root",
        "usage",
        "run_failures",
        "execution_started_at",
        "execution_completed_at",
        "provider_revision",
        "runtime_identity_before_sha256",
        "runtime_identity_after_sha256",
        "runtime_identity_before",
        "runtime_identity_after",
        "activity_records",
        "no_speech_records",
    }
    if not isinstance(artifact, Mapping) or not fields <= set(artifact):
        raise EvaluationError("evidence context fields are incomplete")
    activity_records = artifact["activity_records"]
    no_speech_records = artifact["no_speech_records"]
    if not isinstance(activity_records, list) or not isinstance(no_speech_records, list):
        raise EvaluationError("evidence context record collections are invalid")
    context = {
        field: artifact[field] for field in fields - {"activity_records", "no_speech_records"}
    }
    context["activity_record_count"] = len(activity_records)
    context["no_speech_record_count"] = len(no_speech_records)
    return hmac.new(
        commitment_key,
        EVIDENCE_CONTEXT_HMAC_DOMAIN + canonical_json_bytes(context),
        hashlib.sha256,
    ).hexdigest()


def _validated_preregistration(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise EvaluationError("custody preregistration is invalid")
    immutable = raw.get("immutable_values")
    if not isinstance(immutable, Mapping):
        raise EvaluationError("custody preregistration is invalid")
    try:
        expected = build_preregistration(
            {
                "schema_id": "gate_0b_preregistration_values_v2",
                **{field: immutable[field] for field in PREREGISTRATION_EXTERNAL_FIELDS},
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationError("custody preregistration is invalid") from exc
    if dict(raw) != expected:
        raise EvaluationError("custody preregistration digest is invalid")
    return json.loads(canonical_json_bytes(expected))


def evaluate_evidence_artifact(
    artifact: Mapping[str, Any],
    *,
    commitment_key: bytes,
    root_public_key: bytes,
    expected_root_key_id: str,
) -> dict[str, Any]:
    """Return a payload-safe report after independent validation and reduction."""
    try:
        parsed = _parse_artifact(artifact)
    except (EvaluationError, MeasurementError, KeyError, TypeError, ValueError):
        return _failure_report({"artifact_invalid": 1})

    records = parsed["activity_records"]
    windows = parsed["no_speech_records"]
    failures: dict[str, int] = {}
    expected_context_commitment = compute_evidence_context_commitment(
        artifact,
        commitment_key=commitment_key,
    )
    context_valid = hmac.compare_digest(
        parsed["context_commitment"],
        expected_context_commitment,
    )
    if not context_valid:
        failures["context_commitment_invalid"] = 1
    if any(
        not verify_record_commitment(record, commitment_key=commitment_key)
        for record in (*records, *windows)
    ):
        failures["record_commitment_invalid"] = 1
    root_valid = verify_signed_record_root(
        parsed["signed_record_root"],
        activity_records=records,
        no_speech_records=windows,
        campaign_id=parsed["campaign_id"],
        public_key=root_public_key,
        expected_key_id=expected_root_key_id,
    )
    if not root_valid:
        failures["record_root_invalid"] = 1

    cardinality_failures = _validate_cardinality(
        records,
        windows,
        candidate_policies=parsed["candidate_policies_ms"],
        selected_policy=parsed["selected_policy_ms"],
    )
    failures.update(cardinality_failures)
    if tuple(parsed["phase_history"]) != EXPECTED_PHASE_HISTORY:
        failures["phase_history_invalid"] = 1
    for name in (
        "attempt_authorization_validated",
        "authorization_consumed",
        "provider_execution_started",
    ):
        if not parsed[name]:
            failures[f"{name}_missing"] = 1
    if not parsed["attempt_completed"]:
        failures["attempt_incomplete"] = 1
    if not parsed["holdout_materialized_after_lock"]:
        failures["holdout_materialization_invalid"] = 1
    if parsed["run_failures"]:
        failures["run_failure_present"] = len(parsed["run_failures"])
    if not _usage_is_complete_and_bounded(parsed["usage"]):
        failures["usage_or_budget_invalid"] = 1
    if (
        parsed["identities"]["pricing_sha256"]
        != hashlib.sha256(PRICING_PATH.read_bytes()).hexdigest()
    ):
        failures["pricing_identity_invalid"] = 1

    development_records = tuple(record for record in records if record.split == "development")
    development_windows = tuple(window for window in windows if window.split == "development")
    holdout_windows = tuple(window for window in windows if window.split == "holdout")
    expected_lock = compute_policy_lock_sha256(
        activity_records=development_records,
        no_speech_records=development_windows,
        candidate_policies_ms=parsed["candidate_policies_ms"],
        selected_policy_ms=parsed["selected_policy_ms"],
        identities=parsed["identities"],
    )
    if parsed["policy_lock_sha256"] != expected_lock:
        failures["policy_lock_invalid"] = 1

    candidate_results: dict[str, dict[str, bool]] = {}
    candidate_samples: dict[int, dict[str, Any]] = {}
    for policy in parsed["candidate_policies_ms"]:
        sample = _evaluate_sample(
            tuple(record for record in development_records if record.policy_ms == policy),
            no_speech_records=development_windows,
        )
        candidate_samples[policy] = sample
        candidate_results[str(policy)] = {
            "assembly_passed": sample["assembly_passed"],
            "fidelity_passed": sample["fidelity_passed"],
            "interaction_passed": sample["interaction_passed"],
            "passed": sample["passed"],
        }
    passing_policies = [
        policy for policy in parsed["candidate_policies_ms"] if candidate_samples[policy]["passed"]
    ]
    selected_by_rule = min(passing_policies) if passing_policies else None
    if selected_by_rule is None:
        failures["no_development_policy_passed"] = 1
    elif selected_by_rule != parsed["selected_policy_ms"]:
        failures["policy_selection_mismatch"] = 1

    selected_development = candidate_samples[parsed["selected_policy_ms"]]
    holdout_records = tuple(record for record in records if record.split == "holdout")
    holdout_sample = _evaluate_sample(
        holdout_records,
        no_speech_records=holdout_windows,
    )
    if not selected_development["assembly_passed"] or not holdout_sample["assembly_passed"]:
        failures["assembly_gate_failed"] = 1
    if not selected_development["fidelity_passed"] or not holdout_sample["fidelity_passed"]:
        failures["fidelity_gate_failed"] = 1
    if not selected_development["interaction_passed"] or not holdout_sample["interaction_passed"]:
        failures["interaction_gate_failed"] = 1

    run_integrity_passed = not failures
    assembly_passed = (
        run_integrity_passed
        and selected_development["assembly_passed"]
        and holdout_sample["assembly_passed"]
    )
    fidelity_passed = (
        run_integrity_passed
        and selected_development["fidelity_passed"]
        and holdout_sample["fidelity_passed"]
    )
    interaction_passed = (
        run_integrity_passed
        and selected_development["interaction_passed"]
        and holdout_sample["interaction_passed"]
    )
    gate_passed = assembly_passed and fidelity_passed and interaction_passed
    evidence = empty_evidence_flags()
    evidence.update(
        {
            "attempt_authorization_validated": (
                parsed["attempt_authorization_validated"] and context_valid
            ),
            "authorization_consumed": parsed["authorization_consumed"] and context_valid,
            "provider_execution_started": (
                parsed["provider_execution_started"]
                and context_valid
                and parsed["usage"]["provider_requests"] > 0
            ),
            "attempt_completed": parsed["attempt_completed"] and root_valid and context_valid,
            "assembly_sample_passed": assembly_passed,
            "transcription_fidelity_sample_passed": fidelity_passed,
            "provider_interaction_integrity_sample_passed": interaction_passed,
            "gate_0b_sample_passed": gate_passed,
        }
    )
    signed_payload = parsed["signed_record_root"]["payload"]
    report = {
        "schema_id": EVALUATION_RESULT_SCHEMA_ID,
        "status": "pass" if gate_passed else "no_go",
        "campaign_id": parsed["campaign_id"],
        "phase_history": list(parsed["phase_history"]),
        "selected_policy_ms": parsed["selected_policy_ms"],
        "candidate_policy_results": candidate_results,
        "record_root": {
            "sha256": signed_payload["merkle_root_sha256"],
            "leaf_count": signed_payload["leaf_count"],
            "key_id": parsed["signed_record_root"]["key_id"],
        },
        "identities": parsed["identities"],
        "execution": {
            "started_at": parsed["execution_started_at"],
            "completed_at": parsed["execution_completed_at"],
            "provider_revision": parsed["provider_revision"],
            "runtime_identity_before_sha256": parsed["runtime_identity_before_sha256"],
            "runtime_identity_after_sha256": parsed["runtime_identity_after_sha256"],
            "runtime_identity_before": parsed["runtime_identity_before"],
            "runtime_identity_after": parsed["runtime_identity_after"],
        },
        "samples": {
            "development": selected_development["published"],
            "holdout": holdout_sample["published"],
        },
        "usage": parsed["usage"],
        "failures": dict(sorted(failures.items())),
        **evidence,
    }
    assert_payload_safe(report)
    return report


def evaluate_custody_bundle(
    bundle: Mapping[str, Any],
    *,
    commitment_key: bytes,
    custodian_private_key: X25519PrivateKey,
    expected_custodian_key_id: str,
    ledger_custodian_public_key: bytes,
    record_root_signing_key: Ed25519PrivateKey,
    record_root_key_id: str,
) -> dict[str, Any]:
    """Open custody evidence, independently derive primitives, and evaluate it."""
    try:
        return _evaluate_custody_bundle(
            bundle,
            commitment_key=commitment_key,
            custodian_private_key=custodian_private_key,
            expected_custodian_key_id=expected_custodian_key_id,
            ledger_custodian_public_key=ledger_custodian_public_key,
            record_root_signing_key=record_root_signing_key,
            record_root_key_id=record_root_key_id,
        )
    except (EvaluationError, MeasurementError, KeyError, TypeError, ValueError):
        return _failure_report({"custody_bundle_invalid": 1})


def _evaluate_custody_bundle(
    bundle: Mapping[str, Any],
    *,
    commitment_key: bytes,
    custodian_private_key: X25519PrivateKey,
    expected_custodian_key_id: str,
    ledger_custodian_public_key: bytes,
    record_root_signing_key: Ed25519PrivateKey,
    record_root_key_id: str,
) -> dict[str, Any]:
    fields = {
        "schema_id",
        "campaign_id",
        "development_capsule",
        "holdout_capsule",
        "development_privacy_envelope",
        "holdout_privacy_envelope",
        "privacy_custodian_public_key",
        "ledger",
        "preregistration",
        "campaign_envelope",
        "attempt_envelope",
    }
    if not isinstance(bundle, Mapping) or set(bundle) != fields:
        raise EvaluationError("custody bundle fields are invalid")
    if bundle["schema_id"] != CUSTODY_BUNDLE_SCHEMA_ID:
        raise EvaluationError("custody bundle schema is invalid")
    campaign_id = _safe_id(bundle["campaign_id"], label="campaign ID")
    ledger = bundle["ledger"]
    if not isinstance(ledger, Mapping) or ledger.get("campaign_id") != campaign_id:
        raise EvaluationError("custody ledger campaign is invalid")

    preregistration = _validated_preregistration(bundle["preregistration"])
    immutable = preregistration["immutable_values"]
    if (
        not isinstance(ledger_custodian_public_key, bytes)
        or hashlib.sha256(ledger_custodian_public_key).hexdigest()
        != immutable["ledger_custodian_public_key_sha256"]
    ):
        raise EvaluationError("ledger custodian root is not preregistered")
    campaign_payload = bundle["campaign_envelope"]
    if not isinstance(campaign_payload, Mapping) or not isinstance(
        campaign_payload.get("payload"),
        Mapping,
    ):
        raise EvaluationError("campaign approval envelope is invalid")
    authorization_id = campaign_payload["payload"].get("authorization_id")
    if not isinstance(authorization_id, str):
        raise EvaluationError("campaign approval envelope is invalid")
    state = validate_custody_ledger_snapshot(
        ledger,
        public_key=ledger_custodian_public_key,
        expected_key_id=immutable["ledger_custodian_key_id"],
        expected_ledger_instance_id=immutable["ledger_instance_id"],
        expected_campaign_id=campaign_id,
        expected_authorization_id=authorization_id,
        expected_preregistration_sha256=preregistration["preregistration_sha256"],
        expected_source_sha=immutable["source_sha"],
        expected_ledger_location_sha256=immutable["ledger_location_sha256"],
    )
    if (
        state.phase != "completed"
        or state.phase_history != EXPECTED_PHASE_HISTORY
        or not state.holdout_execution_claimed
        or state.holdout_execution_claimed_at is None
        or state.selected_policy_ms is None
        or state.policy_lock_sha256 is None
    ):
        raise EvaluationError("custody ledger phase history is incomplete")

    development_envelope = bundle["development_capsule"]
    holdout_envelope = bundle["holdout_capsule"]
    if not isinstance(development_envelope, Mapping) or not isinstance(holdout_envelope, Mapping):
        raise EvaluationError("custody capsule envelope is invalid")
    development_capsule_sha256 = hashlib.sha256(
        canonical_json_bytes(development_envelope)
    ).hexdigest()
    holdout_capsule_sha256 = hashlib.sha256(canonical_json_bytes(holdout_envelope)).hexdigest()

    if (
        state.development_capsule_sha256 != development_capsule_sha256
        or state.holdout_capsule_sha256 != holdout_capsule_sha256
    ):
        raise EvaluationError("custody capsule and ledger identities disagree")
    if (
        state.completed_attempt_id is None
        or state.attempt_authorization_sha256 is None
        or state.attempt_claimed_at is None
        or state.development_ledger_head_sha256 is None
    ):
        raise EvaluationError("completed attempt authorization is missing")

    if not isinstance(custodian_private_key, X25519PrivateKey) or not isinstance(
        record_root_signing_key, Ed25519PrivateKey
    ):
        raise EvaluationError("custody key type is invalid")
    custodian_public_key = custodian_private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    record_root_public_key = record_root_signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    approval_public_key = _load_pinned_approval_public_key()
    if hashlib.sha256(approval_public_key).hexdigest() != immutable["approval_public_key_sha256"]:
        raise EvaluationError("custody approval root is not preregistered")
    claim_time = state.attempt_claimed_at
    campaign = verify_campaign_approval(
        bundle["campaign_envelope"],
        public_key=approval_public_key,
        expected_key_id=immutable["approval_key_id"],
        expected_preregistration_sha256=preregistration["preregistration_sha256"],
        expected_source_sha=immutable["source_sha"],
        now=claim_time,
    )
    authorization = verify_attempt_authorization(
        bundle["attempt_envelope"],
        public_key=approval_public_key,
        expected_key_id=immutable["approval_key_id"],
        campaign=campaign,
        now=claim_time,
    )
    encoded_privacy_public_key = bundle["privacy_custodian_public_key"]
    if not isinstance(encoded_privacy_public_key, str):
        raise EvaluationError("privacy custodian public key is invalid")
    try:
        privacy_public_key = base64.b64decode(
            encoded_privacy_public_key,
            validate=True,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationError("privacy custodian public key is invalid") from exc
    if (
        len(privacy_public_key) != 32
        or hashlib.sha256(privacy_public_key).hexdigest()
        != immutable["privacy_custodian_public_key_sha256"]
    ):
        raise EvaluationError("privacy custodian root is not preregistered")
    privacy_arguments = {
        "public_key": privacy_public_key,
        "expected_key_id": immutable["privacy_custodian_key_id"],
        "expected_campaign_id": campaign.campaign_id,
        "expected_authorization_id": campaign.authorization_id,
        "expected_attempt_id": authorization.attempt_id,
        "expected_preregistration_sha256": preregistration["preregistration_sha256"],
        "expected_source_sha": immutable["source_sha"],
        "expected_corpus_sha256": immutable["corpus_sha256"],
        "expected_project": immutable["project"],
        "expected_model": immutable["model"],
        "expected_consent_registry_sha256": immutable["consent_attestation_sha256"],
        "expected_retention_policy_sha256": immutable["retention_attestation_sha256"],
        "expected_residual_retention_acceptance_sha256": immutable[
            "zdr_or_residual_retention_acceptance_sha256"
        ],
    }
    verify_privacy_custody(
        bundle["development_privacy_envelope"],
        **privacy_arguments,
        expected_split="development",
        expected_schedule_sha256=immutable["development_schedule_sha256"],
        now=claim_time,
    )
    verify_privacy_custody(
        bundle["holdout_privacy_envelope"],
        **privacy_arguments,
        expected_split="holdout",
        expected_schedule_sha256=state.holdout_manifest_sha256,
        now=state.holdout_execution_claimed_at,
    )
    if (
        ledger["preregistration_sha256"] != preregistration["preregistration_sha256"]
        or ledger["source_sha"] != immutable["source_sha"]
        or state.campaign_id != campaign.campaign_id
        or state.authorization_id != campaign.authorization_id
        or state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or state.attempt_authorization_sha256 != authorization.signed_payload_sha256
        or authorization.attempt_id != state.completed_attempt_id
        or state.provider_requests_reserved != authorization.provider_request_reservation
        or state.cost_reserved_microusd != authorization.cost_reservation_microusd
        or campaign.max_attempts != immutable["attempt_caps"]["whole_run_attempts"]
        or campaign.max_provider_requests
        != immutable["usage_caps"]["provider_requests_per_campaign"]
        or campaign.max_cost_microusd != immutable["cost_caps_microusd"]["per_campaign"]
        or state.campaign_max_attempts != campaign.max_attempts
        or state.campaign_max_provider_requests != campaign.max_provider_requests
        or state.campaign_max_cost_microusd != campaign.max_cost_microusd
        or authorization.provider_request_reservation
        != immutable["usage_caps"]["provider_requests_per_run"]
        or authorization.cost_reservation_microusd != immutable["cost_caps_microusd"]["per_run"]
        or campaign.ledger_instance_id != immutable["ledger_instance_id"]
        or campaign.ledger_custodian_key_id != immutable["ledger_custodian_key_id"]
        or campaign.ledger_custodian_public_key_sha256
        != immutable["ledger_custodian_public_key_sha256"]
        or campaign.ledger_location_sha256 != immutable["ledger_location_sha256"]
    ):
        raise EvaluationError("custody authorization and ledger identities disagree")
    if (
        expected_custodian_key_id != immutable["custodian_key_id"]
        or record_root_key_id != immutable["record_root_key_id"]
    ):
        raise EvaluationError("custody key identity is not preregistered")
    identities = _validate_identities(
        {
            "source_sha256": hashlib.sha256(ledger["source_sha"].encode("ascii")).hexdigest(),
            "source_fact_bundle_sha256": immutable["source_fact_bundle_sha256"],
            "environment_sha256": immutable["environment_identity_sha256"],
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "corpus_sha256": immutable["corpus_sha256"],
            "pricing_sha256": hashlib.sha256(PRICING_PATH.read_bytes()).hexdigest(),
            "preregistration_sha256": ledger["preregistration_sha256"],
            "campaign_approval_sha256": state.campaign_approval_sha256,
            "attempt_authorization_sha256": state.attempt_authorization_sha256,
            "development_capsule_sha256": development_capsule_sha256,
            "holdout_capsule_sha256": holdout_capsule_sha256,
            "ledger_head_sha256": state.development_ledger_head_sha256,
            "custodian_public_key_sha256": hashlib.sha256(custodian_public_key).hexdigest(),
            "record_root_public_key_sha256": hashlib.sha256(record_root_public_key).hexdigest(),
        }
    )
    if (
        identities["evaluator_sha256"] != immutable["evaluator_sha256"]
        or identities["pricing_sha256"] != immutable["pricing_sha256"]
        or identities["custodian_public_key_sha256"] != immutable["custodian_public_key_sha256"]
        or identities["record_root_public_key_sha256"] != immutable["record_root_public_key_sha256"]
    ):
        raise EvaluationError("custody evidence identity is not preregistered")

    development_capsule = open_audit_capsule(
        development_envelope,
        custodian_private_key=custodian_private_key,
        expected_key_id=expected_custodian_key_id,
    )
    if development_capsule["campaign_id"] != campaign_id:
        raise EvaluationError("development capsule campaign mismatch")
    development_records, development_windows = derive_primitive_records_from_capsule(
        development_capsule,
        policies_ms=EXPECTED_POLICIES,
        commitment_key=commitment_key,
    )
    development_usage, development_failures = derive_audit_capsule_accounting(development_capsule)
    development_cost_microusd = _cost_microusd_from_usage(development_usage)
    development_usage_digest = usage_evidence_sha256(
        development_usage,
        provider_requests=int(development_usage["provider_requests"]),
        cost_microusd=development_cost_microusd,
    )
    if (
        development_failures
        or development_usage["provider_requests"] != 64
        or state.development_usage_evidence_sha256 != development_usage_digest
        or state.development_provider_requests != development_usage["provider_requests"]
        or state.development_cost_microusd != development_cost_microusd
    ):
        raise EvaluationError("development capsule accounting is invalid")
    candidate_samples = {
        policy: _evaluate_sample(
            tuple(record for record in development_records if record.policy_ms == policy),
            no_speech_records=development_windows,
        )
        for policy in EXPECTED_POLICIES
    }
    passing_policies = [
        policy for policy in EXPECTED_POLICIES if candidate_samples[policy]["passed"]
    ]
    selected_policy_ms = min(passing_policies) if passing_policies else None
    if selected_policy_ms is None or selected_policy_ms != state.selected_policy_ms:
        raise EvaluationError("custody policy selection disagrees with development evidence")
    expected_policy_lock = compute_policy_lock_sha256(
        activity_records=development_records,
        no_speech_records=development_windows,
        candidate_policies_ms=EXPECTED_POLICIES,
        selected_policy_ms=selected_policy_ms,
        identities=identities,
    )
    if expected_policy_lock != state.policy_lock_sha256:
        raise EvaluationError("custody policy lock is invalid")

    holdout_capsule = open_audit_capsule(
        holdout_envelope,
        custodian_private_key=custodian_private_key,
        expected_key_id=expected_custodian_key_id,
    )
    if holdout_capsule["campaign_id"] != campaign_id:
        raise EvaluationError("holdout capsule campaign mismatch")
    execution_metadata = _capsule_execution_metadata(
        development_capsule,
        holdout_capsule,
        immutable=immutable,
    )
    holdout_records, holdout_windows = derive_primitive_records_from_capsule(
        holdout_capsule,
        policies_ms=(selected_policy_ms,),
        commitment_key=commitment_key,
    )
    holdout_usage, holdout_failures = derive_audit_capsule_accounting(holdout_capsule)
    usage = _combine_usage(development_usage, holdout_usage)
    holdout_cost_microusd = _cost_microusd_from_usage(holdout_usage)
    holdout_usage_digest = usage_evidence_sha256(
        holdout_usage,
        provider_requests=int(holdout_usage["provider_requests"]),
        cost_microusd=holdout_cost_microusd,
    )
    cost_microusd = development_cost_microusd + holdout_cost_microusd
    usage_with_cost = {**usage, "cost_microusd": cost_microusd}
    run_failures = (*development_failures, *holdout_failures)
    if (
        holdout_usage["provider_requests"] != 64
        or usage["provider_requests"] != 128
        or state.final_usage_evidence_sha256
        != combined_usage_evidence_sha256(
            development_usage_evidence_sha256=development_usage_digest,
            holdout_usage_evidence_sha256=holdout_usage_digest,
            provider_requests=int(usage["provider_requests"]),
            cost_microusd=cost_microusd,
        )
        or state.actual_provider_requests != usage["provider_requests"]
        or state.actual_cost_microusd != cost_microusd
        or usage["provider_requests"] > authorization.provider_request_reservation
        or cost_microusd > authorization.cost_reservation_microusd
    ):
        raise EvaluationError("final capsule accounting and custody receipt disagree")
    activity_records = (*development_records, *holdout_records)
    no_speech_records = (*development_windows, *holdout_windows)
    signed_root = build_signed_record_root(
        activity_records=activity_records,
        no_speech_records=no_speech_records,
        campaign_id=campaign_id,
        signing_key=record_root_signing_key,
        key_id=record_root_key_id,
    )
    artifact = {
        "schema_id": EVIDENCE_SCHEMA_ID,
        "campaign_id": campaign_id,
        "attempt_authorization_validated": True,
        "authorization_consumed": True,
        "provider_execution_started": usage["provider_requests"] > 0,
        "attempt_completed": True,
        "phase_history": list(state.phase_history),
        "candidate_policies_ms": list(EXPECTED_POLICIES),
        "selected_policy_ms": selected_policy_ms,
        "holdout_materialized_after_lock": state.holdout_manifest_sha256 is not None,
        "identities": identities,
        "policy_lock_sha256": state.policy_lock_sha256,
        "activity_records": [record.to_dict() for record in activity_records],
        "no_speech_records": [record.to_dict() for record in no_speech_records],
        "signed_record_root": signed_root,
        "usage": usage_with_cost,
        "run_failures": list(run_failures),
        **execution_metadata,
    }
    artifact["context_commitment"] = compute_evidence_context_commitment(
        artifact,
        commitment_key=commitment_key,
    )
    evaluation = evaluate_evidence_artifact(
        artifact,
        commitment_key=commitment_key,
        root_public_key=record_root_public_key,
        expected_root_key_id=record_root_key_id,
    )
    if evaluation["campaign_id"] is None:
        return evaluation
    return _build_published_report(
        evaluation,
        preregistration=preregistration,
        signed_record_root=signed_root,
        activity_records=activity_records,
        no_speech_records=no_speech_records,
        opened_capsules=(development_capsule, holdout_capsule),
        signing_key=record_root_signing_key,
        key_id=record_root_key_id,
    )


def _build_published_report(
    evaluation: Mapping[str, Any],
    *,
    preregistration: Mapping[str, Any],
    signed_record_root: Mapping[str, Any],
    activity_records: tuple[ActivityPrimitiveRecord, ...],
    no_speech_records: tuple[NoSpeechPrimitiveRecord, ...],
    opened_capsules: tuple[Mapping[str, Any], Mapping[str, Any]],
    signing_key: Ed25519PrivateKey,
    key_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(evaluation, Mapping)
        or evaluation.get("schema_id") != EVALUATION_RESULT_SCHEMA_ID
        or evaluation.get("campaign_id") is None
        or not isinstance(signing_key, Ed25519PrivateKey)
    ):
        raise EvaluationError("publication inputs are invalid")
    canonical_preregistration = _validated_preregistration(preregistration)
    immutable = canonical_preregistration["immutable_values"]
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if (
        key_id != immutable["record_root_key_id"]
        or hashlib.sha256(public_key).hexdigest() != immutable["record_root_public_key_sha256"]
    ):
        raise EvaluationError("publication signing identity is not preregistered")

    payload = json.loads(canonical_json_bytes(evaluation))
    payload["schema_id"] = REPORT_SCHEMA_ID
    payload.pop("record_root", None)
    payload.update(
        {
            "preregistration": canonical_preregistration,
            "component_digests": _component_digests(
                canonical_preregistration,
                execution=payload["execution"],
            ),
            "cardinalities": _recomputed_cardinalities(
                activity_records,
                no_speech_records,
                opened_capsules=opened_capsules,
                phase_history=tuple(payload["phase_history"]),
                candidate_policy_count=len(payload["candidate_policy_results"]),
                provider_request_count=payload["usage"]["provider_requests"],
            ),
            "signed_record_root": json.loads(canonical_json_bytes(signed_record_root)),
        }
    )
    assert_payload_safe(payload)
    report = {
        **payload,
        "publication_signature": {
            "schema_id": PUBLICATION_SIGNATURE_SCHEMA_ID,
            "key_id": key_id,
            "signature": base64.b64encode(
                signing_key.sign(PUBLICATION_SIGNATURE_DOMAIN + canonical_json_bytes(payload))
            ).decode("ascii"),
        },
    }
    assert_payload_safe(report)
    return json.loads(canonical_json_bytes(report))


def _component_digests(
    preregistration: Mapping[str, Any],
    *,
    execution: Mapping[str, Any],
) -> dict[str, str]:
    immutable = preregistration["immutable_values"]
    config = SessionExecutionConfig(
        endpoint=immutable["endpoint"],
        model=immutable["model"],
        project=immutable["project"],
        max_message_bytes=1_024,
        session_timeout_seconds=immutable["usage_caps"]["session_timeout_seconds"],
        response_gap_limit_ms=500,
    )
    setup_identity = build_gate0b_setup_identity(config)
    setup_sha256 = hashlib.sha256(canonical_json_bytes(setup_identity)).hexdigest()
    if setup_sha256 != immutable["setup_sha256"]:
        raise EvaluationError("published setup digest is invalid")
    standard_setup = setup_identity["standard"]["setup"]
    synthetic_tool_setup = setup_identity["synthetic_tool"]["setup"]
    imports = execution["runtime_identity_before"]["environment"]["import_sha256"]
    source_component_digests = {
        "runner_sha256": _execution_source_dependency_sha256(
            execution,
            relative_path="scripts/run_gemini_caller_turn_qualification.py",
        ),
        "evaluator_sha256": _execution_source_dependency_sha256(
            execution,
            relative_path="scripts/evaluate_gemini_caller_turn_qualification.py",
        ),
    }
    if any(
        source_component_digests[field] != immutable[field]
        for field in source_component_digests
    ):
        raise EvaluationError("published source component identities disagree")
    try:
        result = {
            "manifest_sha256": immutable["manifest_sha256"],
            "corpus_sha256": immutable["corpus_sha256"],
            "setup_sha256": setup_sha256,
            "pricing_sha256": immutable["pricing_sha256"],
            "prompt_sha256": hashlib.sha256(
                canonical_json_bytes(standard_setup["systemInstruction"])
            ).hexdigest(),
            "tool_sha256": hashlib.sha256(
                canonical_json_bytes(synthetic_tool_setup["tools"])
            ).hexdigest(),
            "runner_sha256": source_component_digests["runner_sha256"],
            "adapter_sha256": imports["app.services.gemini_turn_events"],
            "assembler_sha256": imports["app.services.caller_turns"],
            "renderer_sha256": imports["app.utils.audio"],
            "evaluator_sha256": source_component_digests["evaluator_sha256"],
        }
    except (KeyError, TypeError) as exc:
        raise EvaluationError("published component identities are incomplete") from exc
    if set(result) != COMPONENT_DIGEST_FIELDS or any(
        not isinstance(value, str) or not SHA256.fullmatch(value) for value in result.values()
    ):
        raise EvaluationError("published component identities are invalid")
    return dict(sorted(result.items()))


def _execution_source_dependency_sha256(
    execution: Mapping[str, Any],
    *,
    relative_path: str,
) -> str:
    dependency_key = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    try:
        identity = execution["runtime_identity_before"]["source"]["dependencies"][
            dependency_key
        ]
        value = identity["worktree_sha256"]
    except (KeyError, TypeError) as exc:
        raise EvaluationError("published source component identity is incomplete") from exc
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise EvaluationError("published source component identity is invalid")
    return value


def _recomputed_cardinalities(
    activity_records: Sequence[ActivityPrimitiveRecord],
    no_speech_records: Sequence[NoSpeechPrimitiveRecord],
    *,
    opened_capsules: tuple[Mapping[str, Any], Mapping[str, Any]],
    phase_history: tuple[str, ...],
    candidate_policy_count: int,
    provider_request_count: int,
) -> dict[str, dict[str, int | bool]]:
    development = tuple(record for record in activity_records if record.split == "development")
    holdout = tuple(record for record in activity_records if record.split == "holdout")
    selected_development_ordinals = {record.activity_ordinal for record in development}
    holdout_ordinals = {record.activity_ordinal for record in holdout}
    languages = {
        record.language
        for record in activity_records
        if record.split == "holdout" or record.policy_ms == min(EXPECTED_POLICIES)
    }
    execution_counts = _capsule_execution_cardinalities(opened_capsules)
    observed = {
        "campaign_count": 1,
        "attempt_count": 1,
        "phase_count": len(phase_history),
        "phase_transition_count": max(0, len(phase_history) - 1),
        "candidate_policy_count": candidate_policy_count,
        "language_count": len(languages),
        **execution_counts,
        "development_scheduled_activity_count": len(selected_development_ordinals),
        "development_policy_record_count": len(development),
        "holdout_scheduled_activity_count": len(holdout_ordinals),
        "scheduled_activity_count": len(selected_development_ordinals | holdout_ordinals),
        "development_no_speech_window_count": sum(
            record.split == "development" for record in no_speech_records
        ),
        "holdout_no_speech_window_count": sum(
            record.split == "holdout" for record in no_speech_records
        ),
        "no_speech_window_count": len(no_speech_records),
        "primitive_record_count": len(activity_records) + len(no_speech_records),
        "provider_request_count": provider_request_count,
    }
    return _cardinality_report(observed)


def _capsule_execution_cardinalities(
    capsules: tuple[Mapping[str, Any], Mapping[str, Any]],
) -> dict[str, int]:
    logical_session_count = 0
    connection_count = 0
    epoch_count = 0
    fresh_restart_count = 0
    for capsule in capsules:
        sessions = capsule.get("sessions")
        activities = capsule.get("activities")
        no_speech_windows = capsule.get("no_speech_windows")
        accounting = capsule.get("accounting")
        if (
            not isinstance(sessions, list)
            or not isinstance(activities, list)
            or not isinstance(no_speech_windows, list)
            or not isinstance(accounting, Mapping)
        ):
            raise EvaluationError("published execution topology is unavailable")
        units = accounting.get("units")
        if not isinstance(units, list):
            raise EvaluationError("published execution topology is unavailable")

        accounting_counts: dict[tuple[str, int], int] = {}
        for unit in units:
            if (
                not isinstance(unit, Mapping)
                or unit.get("kind") not in {"session", "no_speech_window"}
                or isinstance(unit.get("ordinal"), bool)
                or not isinstance(unit.get("ordinal"), int)
                or unit["ordinal"] < 0
                or isinstance(unit.get("provider_request_count"), bool)
                or not isinstance(unit.get("provider_request_count"), int)
                or unit["provider_request_count"] < 0
            ):
                raise EvaluationError("published execution topology is invalid")
            identity = (unit["kind"], unit["ordinal"])
            if identity in accounting_counts:
                raise EvaluationError("published execution topology is invalid")
            accounting_counts[identity] = unit["provider_request_count"]

        session_index = _execution_topology_index(
            sessions,
            kind="session",
            ordinal_field="session_ordinal",
        )
        window_index = _execution_topology_index(
            no_speech_windows,
            kind="no-speech window",
            ordinal_field="window_ordinal",
        )
        expected_accounting_units = {
            *(("session", ordinal) for ordinal in session_index),
            *(("no_speech_window", ordinal) for ordinal in window_index),
        }
        if set(accounting_counts) != expected_accounting_units:
            raise EvaluationError("published execution topology is invalid")

        activity_epochs: dict[int, set[int]] = defaultdict(set)
        activity_ordinals: set[int] = set()
        for activity in activities:
            if not isinstance(activity, Mapping):
                raise EvaluationError("published execution topology is invalid")
            activity_ordinal = _execution_topology_int(activity.get("activity_ordinal"))
            session_ordinal = _execution_topology_int(activity.get("session_ordinal"))
            expected_epoch = _execution_topology_int(activity.get("expected_epoch"), positive=True)
            if activity_ordinal in activity_ordinals or session_ordinal not in session_index:
                raise EvaluationError("published execution topology is invalid")
            activity_ordinals.add(activity_ordinal)
            activity_epochs[session_ordinal].add(expected_epoch)
        if set(activity_epochs) != set(session_index):
            raise EvaluationError("published execution topology is invalid")

        for session_ordinal, session in session_index.items():
            connection_epochs, wire_epochs = _execution_wire_epochs(session.get("wire_facts"))
            event_epochs = _execution_event_epochs(session.get("events"))
            expected_epochs = activity_epochs[session_ordinal]
            canonical_epochs = tuple(range(1, len(connection_epochs) + 1))
            if (
                connection_epochs != canonical_epochs
                or wire_epochs != set(connection_epochs)
                or set(event_epochs) != expected_epochs
                or expected_epochs != set(connection_epochs)
                or any(left > right for left, right in zip(event_epochs, event_epochs[1:]))
                or accounting_counts[("session", session_ordinal)] != len(connection_epochs)
            ):
                raise EvaluationError("published execution topology is invalid")
            logical_session_count += 1
            connection_count += len(connection_epochs)
            epoch_count += len(connection_epochs)
            fresh_restart_count += len(connection_epochs) - 1

        for window_ordinal, window in window_index.items():
            connection_epochs, wire_epochs = _execution_wire_epochs(window.get("wire_facts"))
            if (
                connection_epochs != (1,)
                or wire_epochs != {1}
                or accounting_counts[("no_speech_window", window_ordinal)] != 1
            ):
                raise EvaluationError("published execution topology is invalid")
            connection_count += 1

    observed = {
        "logical_session_count": logical_session_count,
        "connection_count": connection_count,
        "epoch_count": epoch_count,
        "fresh_restart_count": fresh_restart_count,
    }
    if any(observed[name] != CARDINALITY_REQUIREMENTS[name] for name in observed):
        raise EvaluationError("published execution topology cardinality is invalid")
    return observed


def _execution_topology_index(
    raw_items: list[object],
    *,
    kind: str,
    ordinal_field: str,
) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise EvaluationError(f"published {kind} execution topology is invalid")
        ordinal = _execution_topology_int(item.get(ordinal_field))
        if ordinal in result:
            raise EvaluationError(f"published {kind} execution topology is invalid")
        result[ordinal] = item
    return result


def _execution_topology_int(value: object, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvaluationError("published execution topology is invalid")
    return value


def _execution_wire_epochs(raw_facts: object) -> tuple[tuple[int, ...], set[int]]:
    if not isinstance(raw_facts, list):
        raise EvaluationError("published execution topology is invalid")
    connection_epochs = []
    wire_epochs = set()
    for fact in raw_facts:
        if not isinstance(fact, Mapping) or not isinstance(fact.get("kind"), str):
            raise EvaluationError("published execution topology is invalid")
        epoch = _execution_topology_int(fact.get("epoch"), positive=True)
        wire_epochs.add(epoch)
        if fact["kind"] == "connection_open":
            connection_epochs.append(epoch)
    return tuple(connection_epochs), wire_epochs


def _execution_event_epochs(raw_events: object) -> tuple[int, ...]:
    if not isinstance(raw_events, list):
        raise EvaluationError("published execution topology is invalid")
    result = []
    for event in raw_events:
        if not isinstance(event, Mapping):
            raise EvaluationError("published execution topology is invalid")
        result.append(_execution_topology_int(event.get("epoch"), positive=True))
    return tuple(result)


def _cardinality_report(
    observed: Mapping[str, int],
) -> dict[str, dict[str, int | bool]]:
    if set(observed) != set(CARDINALITY_REQUIREMENTS):
        raise EvaluationError("published cardinalities are incomplete")
    return {
        name: {
            "required": required,
            "observed": observed[name],
            "matches": observed[name] == required,
        }
        for name, required in sorted(CARDINALITY_REQUIREMENTS.items())
    }


def verify_published_report(
    report: Mapping[str, Any],
    *,
    root_public_key: bytes,
    expected_root_key_id: str,
) -> dict[str, Any]:
    """Verify one canonical aggregate package without primitive or corpus access."""
    try:
        return _validate_published_report(
            report,
            root_public_key=root_public_key,
            expected_root_key_id=expected_root_key_id,
        )
    except (EvaluationError, InvalidSignature, KeyError, TypeError, ValueError):
        return _failure_report({"published_report_invalid": 1})


def _validate_published_report(
    report: Mapping[str, Any],
    *,
    root_public_key: bytes,
    expected_root_key_id: str,
) -> dict[str, Any]:
    evidence_fields = set(empty_evidence_flags())
    fields = {
        "schema_id",
        "status",
        "campaign_id",
        "phase_history",
        "selected_policy_ms",
        "candidate_policy_results",
        "signed_record_root",
        "identities",
        "execution",
        "samples",
        "usage",
        "failures",
        "preregistration",
        "component_digests",
        "cardinalities",
        "publication_signature",
        *evidence_fields,
    }
    if not isinstance(report, Mapping) or set(report) != fields:
        raise EvaluationError("published report fields are invalid")
    if report["schema_id"] != REPORT_SCHEMA_ID or report["status"] not in {
        "pass",
        "no_go",
    }:
        raise EvaluationError("published report schema or status is invalid")
    assert_payload_safe(report)
    campaign_id = _safe_id(report["campaign_id"], label="campaign ID")
    if tuple(report["phase_history"]) != EXPECTED_PHASE_HISTORY:
        raise EvaluationError("published phase history is invalid")
    _validate_policy_configuration(EXPECTED_POLICIES, report["selected_policy_ms"])
    candidate_results = report["candidate_policy_results"]
    if (
        not isinstance(candidate_results, Mapping)
        or set(candidate_results) != {str(policy) for policy in EXPECTED_POLICIES}
        or any(
            not isinstance(value, Mapping)
            or set(value) != {"assembly_passed", "fidelity_passed", "interaction_passed", "passed"}
            or any(not isinstance(flag, bool) for flag in value.values())
            for value in candidate_results.values()
        )
    ):
        raise EvaluationError("published candidate policy results are invalid")

    canonical_preregistration = _validated_preregistration(report["preregistration"])
    immutable = canonical_preregistration["immutable_values"]
    if (
        not isinstance(root_public_key, bytes)
        or len(root_public_key) != 32
        or expected_root_key_id != immutable["record_root_key_id"]
        or hashlib.sha256(root_public_key).hexdigest() != immutable["record_root_public_key_sha256"]
    ):
        raise EvaluationError("published root identity is invalid")

    signature_envelope = report["publication_signature"]
    if not isinstance(signature_envelope, Mapping) or set(signature_envelope) != {
        "schema_id",
        "key_id",
        "signature",
    }:
        raise EvaluationError("publication signature envelope is invalid")
    if (
        signature_envelope["schema_id"] != PUBLICATION_SIGNATURE_SCHEMA_ID
        or signature_envelope["key_id"] != expected_root_key_id
    ):
        raise EvaluationError("publication signature identity is invalid")
    publication_signature = _decode_signature(signature_envelope["signature"])
    publication_payload = {
        key: value for key, value in report.items() if key != "publication_signature"
    }
    Ed25519PublicKey.from_public_bytes(root_public_key).verify(
        publication_signature,
        PUBLICATION_SIGNATURE_DOMAIN + canonical_json_bytes(publication_payload),
    )
    _verify_detached_record_root(
        report["signed_record_root"],
        campaign_id=campaign_id,
        public_key=root_public_key,
        expected_key_id=expected_root_key_id,
    )

    execution = report["execution"]
    if not isinstance(execution, Mapping):
        raise EvaluationError("published execution evidence is invalid")
    if report["component_digests"] != _component_digests(
        canonical_preregistration,
        execution=execution,
    ):
        raise EvaluationError("published component identities disagree")
    identities = _validate_identities(report["identities"])
    identity_bindings = {
        "source_sha256": hashlib.sha256(immutable["source_sha"].encode("ascii")).hexdigest(),
        "source_fact_bundle_sha256": immutable["source_fact_bundle_sha256"],
        "environment_sha256": immutable["environment_identity_sha256"],
        "evaluator_sha256": immutable["evaluator_sha256"],
        "corpus_sha256": immutable["corpus_sha256"],
        "pricing_sha256": immutable["pricing_sha256"],
        "preregistration_sha256": canonical_preregistration["preregistration_sha256"],
        "record_root_public_key_sha256": immutable["record_root_public_key_sha256"],
    }
    if any(identities[field] != value for field, value in identity_bindings.items()):
        raise EvaluationError("published identities disagree with preregistration")
    usage = _validate_usage(report["usage"])
    samples = report["samples"]
    if not isinstance(samples, Mapping) or set(samples) != {"development", "holdout"}:
        raise EvaluationError("published samples are invalid")
    _validate_published_sample(samples["development"])
    _validate_published_sample(samples["holdout"])
    root_payload = report["signed_record_root"]["payload"]
    detached_observed = {
        "campaign_count": 1,
        "attempt_count": 1,
        "phase_count": len(report["phase_history"]),
        "phase_transition_count": len(report["phase_history"]) - 1,
        "candidate_policy_count": len(candidate_results),
        "language_count": len(
            set(samples["development"]["languages"]) | set(samples["holdout"]["languages"])
        ),
        "logical_session_count": CARDINALITY_REQUIREMENTS["logical_session_count"],
        "connection_count": CARDINALITY_REQUIREMENTS["connection_count"],
        "epoch_count": CARDINALITY_REQUIREMENTS["epoch_count"],
        "fresh_restart_count": CARDINALITY_REQUIREMENTS["fresh_restart_count"],
        "development_scheduled_activity_count": samples["development"]["activity_count"],
        "development_policy_record_count": samples["development"]["activity_count"]
        * len(candidate_results),
        "holdout_scheduled_activity_count": samples["holdout"]["activity_count"],
        "scheduled_activity_count": samples["development"]["activity_count"]
        + samples["holdout"]["activity_count"],
        "development_no_speech_window_count": samples["development"]["no_speech_window_count"],
        "holdout_no_speech_window_count": samples["holdout"]["no_speech_window_count"],
        "no_speech_window_count": samples["development"]["no_speech_window_count"]
        + samples["holdout"]["no_speech_window_count"],
        "primitive_record_count": root_payload["leaf_count"],
        "provider_request_count": usage["provider_requests"],
    }
    expected_cardinalities = _cardinality_report(detached_observed)
    if report["cardinalities"] != expected_cardinalities or any(
        not value["matches"] for value in expected_cardinalities.values()
    ):
        raise EvaluationError("published cardinalities are invalid")
    if root_payload["leaf_count"] != (
        detached_observed["development_policy_record_count"]
        + detached_observed["holdout_scheduled_activity_count"]
        + detached_observed["no_speech_window_count"]
    ):
        raise EvaluationError("published record-root cardinality is invalid")
    if any(not isinstance(report[field], bool) for field in evidence_fields):
        raise EvaluationError("published evidence flags are invalid")
    if (report["status"] == "pass") != report["gate_0b_sample_passed"]:
        raise EvaluationError("published status and gate result disagree")
    if report["status"] == "pass" and report["failures"]:
        raise EvaluationError("passing publication contains failures")
    return json.loads(canonical_json_bytes(report))


def _verify_detached_record_root(
    envelope: object,
    *,
    campaign_id: str,
    public_key: bytes,
    expected_key_id: str,
) -> None:
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "key_id",
        "payload",
        "signature",
    }:
        raise EvaluationError("published record-root envelope is invalid")
    payload = envelope["payload"]
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_id",
        "campaign_id",
        "leaf_count",
        "merkle_root_sha256",
    }:
        raise EvaluationError("published record-root payload is invalid")
    if (
        envelope["key_id"] != expected_key_id
        or payload["schema_id"] != SIGNED_ROOT_SCHEMA_ID
        or payload["campaign_id"] != campaign_id
        or isinstance(payload["leaf_count"], bool)
        or not isinstance(payload["leaf_count"], int)
        or payload["leaf_count"] <= 0
        or not isinstance(payload["merkle_root_sha256"], str)
        or not SHA256.fullmatch(payload["merkle_root_sha256"])
    ):
        raise EvaluationError("published record-root identity is invalid")
    Ed25519PublicKey.from_public_bytes(public_key).verify(
        _decode_signature(envelope["signature"]),
        canonical_json_bytes(payload),
    )


def _decode_signature(value: object) -> bytes:
    if not isinstance(value, str):
        raise EvaluationError("publication signature is invalid")
    try:
        signature = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise EvaluationError("publication signature is invalid") from exc
    if len(signature) != 64:
        raise EvaluationError("publication signature is invalid")
    return signature


def _validate_published_sample(sample: object) -> None:
    fields = {
        "activity_count",
        "no_speech_window_count",
        "assembly_rate",
        "cer_micros",
        "wer_micros",
        "cer",
        "wer",
        "critical_span_rate",
        "no_speech_response_free_rate",
        "first_audio_ms",
        "interruption_tail_ms",
        "languages",
        "conditions",
        "scenarios",
        "structural_outcomes",
        "timing_scope",
    }
    if not isinstance(sample, Mapping) or set(sample) != fields:
        raise EvaluationError("published sample fields are invalid")
    if sample["timing_scope"] != TIMING_SCOPE:
        raise EvaluationError("published timing scope is invalid")
    activity_count = sample["activity_count"]
    no_speech_count = sample["no_speech_window_count"]
    if (
        isinstance(activity_count, bool)
        or not isinstance(activity_count, int)
        or activity_count < 0
        or isinstance(no_speech_count, bool)
        or not isinstance(no_speech_count, int)
        or no_speech_count < 0
    ):
        raise EvaluationError("published sample counts are invalid")
    _validate_timing_summary(
        sample["first_audio_ms"],
        scheduled_case_count=activity_count,
    )
    scenarios = sample["scenarios"]
    if not isinstance(scenarios, Mapping):
        raise EvaluationError("published scenario groups are invalid")
    interruption_group = scenarios.get("tool_cancellation_interruption")
    if not isinstance(interruption_group, Mapping):
        raise EvaluationError("published interruption population is missing")
    interruption_count = interruption_group.get("count")
    if isinstance(interruption_count, bool) or not isinstance(interruption_count, int):
        raise EvaluationError("published interruption population is invalid")
    _validate_timing_summary(
        sample["interruption_tail_ms"],
        scheduled_case_count=interruption_count,
    )
    structural = sample["structural_outcomes"]
    if not isinstance(structural, Mapping):
        raise EvaluationError("published structural outcomes are invalid")
    lifecycle = structural.get("lifecycle_status")
    if not isinstance(lifecycle, Mapping) or set(lifecycle) != {
        "scheduled_case_count",
        "zero_denominator",
        "exact_match_count",
        "expected_counts",
        "observed_counts",
    }:
        raise EvaluationError("published lifecycle outcomes are invalid")
    expected_counts = lifecycle["expected_counts"]
    observed_counts = lifecycle["observed_counts"]
    if (
        lifecycle["scheduled_case_count"] != activity_count
        or lifecycle["zero_denominator"] is not (activity_count == 0)
        or not isinstance(expected_counts, Mapping)
        or set(expected_counts) != set(LIFECYCLE_STATUSES)
        or not isinstance(observed_counts, Mapping)
        or set(observed_counts) != set(LIFECYCLE_STATUSES)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (*expected_counts.values(), *observed_counts.values())
        )
        or sum(expected_counts.values()) != activity_count
        or sum(observed_counts.values()) != activity_count
        or isinstance(lifecycle["exact_match_count"], bool)
        or not isinstance(lifecycle["exact_match_count"], int)
        or not 0 <= lifecycle["exact_match_count"] <= activity_count
    ):
        raise EvaluationError("published lifecycle counts are invalid")


def _validate_timing_summary(
    summary: object,
    *,
    scheduled_case_count: int,
) -> None:
    fields = {
        "count",
        "observed_count",
        "scheduled_case_count",
        "zero_denominator",
        "p50",
        "p95",
        "max",
        "histogram",
    }
    if not isinstance(summary, Mapping) or set(summary) != fields:
        raise EvaluationError("published timing summary is invalid")
    count = summary["count"]
    histogram = summary["histogram"]
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or summary["observed_count"] != count
        or summary["scheduled_case_count"] != scheduled_case_count
        or summary["zero_denominator"] is not (scheduled_case_count == 0)
        or count > scheduled_case_count
        or not isinstance(histogram, Mapping)
        or set(histogram) != {"le_250", "251_500", "501_1000", "1001_1500", "1501_2500", "gt_2500"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in histogram.values()
        )
        or sum(histogram.values()) != count
    ):
        raise EvaluationError("published timing counts are invalid")


def _parse_artifact(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_id",
        "campaign_id",
        "attempt_authorization_validated",
        "authorization_consumed",
        "provider_execution_started",
        "attempt_completed",
        "phase_history",
        "candidate_policies_ms",
        "selected_policy_ms",
        "holdout_materialized_after_lock",
        "identities",
        "policy_lock_sha256",
        "activity_records",
        "no_speech_records",
        "signed_record_root",
        "usage",
        "run_failures",
        "execution_started_at",
        "execution_completed_at",
        "provider_revision",
        "runtime_identity_before_sha256",
        "runtime_identity_after_sha256",
        "runtime_identity_before",
        "runtime_identity_after",
        "context_commitment",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise EvaluationError("evidence artifact fields are invalid")
    if raw["schema_id"] != EVIDENCE_SCHEMA_ID:
        raise EvaluationError("evidence artifact schema is invalid")
    campaign_id = _safe_id(raw["campaign_id"], label="campaign ID")
    for name in (
        "attempt_authorization_validated",
        "authorization_consumed",
        "provider_execution_started",
        "attempt_completed",
        "holdout_materialized_after_lock",
    ):
        if not isinstance(raw[name], bool):
            raise EvaluationError(f"{name} must be boolean")
    phase_history = _safe_id_list(raw["phase_history"], label="phase history", maximum=8)
    policies_raw = raw["candidate_policies_ms"]
    if not isinstance(policies_raw, list):
        raise EvaluationError("candidate policies must be an array")
    policies = tuple(policies_raw)
    _validate_policy_configuration(policies, raw["selected_policy_ms"])
    identities = _validate_identities(raw["identities"])
    if not isinstance(raw["policy_lock_sha256"], str) or not SHA256.fullmatch(
        raw["policy_lock_sha256"]
    ):
        raise EvaluationError("policy lock digest is invalid")
    if not isinstance(raw["context_commitment"], str) or not SHA256.fullmatch(
        raw["context_commitment"]
    ):
        raise EvaluationError("evidence context commitment is invalid")
    raw_records = raw["activity_records"]
    raw_windows = raw["no_speech_records"]
    if not isinstance(raw_records, list) or len(raw_records) > 1_000:
        raise EvaluationError("activity record collection is invalid")
    if not isinstance(raw_windows, list) or len(raw_windows) > 64:
        raise EvaluationError("no-speech record collection is invalid")
    records = tuple(ActivityPrimitiveRecord.from_dict(value) for value in raw_records)
    windows = tuple(NoSpeechPrimitiveRecord.from_dict(value) for value in raw_windows)
    signed_root = raw["signed_record_root"]
    if not isinstance(signed_root, Mapping):
        raise EvaluationError("signed record root is invalid")
    usage = _validate_usage(raw["usage"])
    run_failures = _safe_id_list(raw["run_failures"], label="run failures", maximum=64)
    started_at = _parse_utc_time(raw["execution_started_at"])
    completed_at = _parse_utc_time(raw["execution_completed_at"])
    if completed_at < started_at:
        raise EvaluationError("execution timestamps are invalid")
    provider_revision = raw["provider_revision"]
    if provider_revision is not None and (
        not isinstance(provider_revision, str) or not PROVIDER_REVISION.fullmatch(provider_revision)
    ):
        raise EvaluationError("provider revision is invalid")
    runtime_before = raw["runtime_identity_before_sha256"]
    runtime_after = raw["runtime_identity_after_sha256"]
    if (
        not isinstance(runtime_before, str)
        or not SHA256.fullmatch(runtime_before)
        or not isinstance(runtime_after, str)
        or not SHA256.fullmatch(runtime_after)
        or runtime_before != identities["environment_sha256"]
        or runtime_after != identities["environment_sha256"]
    ):
        raise EvaluationError("runtime identity evidence is invalid")
    try:
        runtime_before_report = validate_execution_identity_report(raw["runtime_identity_before"])
        runtime_after_report = validate_execution_identity_report(raw["runtime_identity_after"])
    except ValueError as exc:
        raise EvaluationError("runtime identity report is invalid") from exc
    if (
        runtime_before_report != runtime_after_report
        or execution_identity_report_sha256(runtime_before_report) != runtime_before
        or execution_identity_report_sha256(runtime_after_report) != runtime_after
        or hashlib.sha256(runtime_before_report["source"]["source_sha"].encode("ascii")).hexdigest()
        != identities["source_sha256"]
    ):
        raise EvaluationError("runtime identity report drifted")
    return {
        "campaign_id": campaign_id,
        "attempt_authorization_validated": raw["attempt_authorization_validated"],
        "authorization_consumed": raw["authorization_consumed"],
        "provider_execution_started": raw["provider_execution_started"],
        "attempt_completed": raw["attempt_completed"],
        "phase_history": phase_history,
        "candidate_policies_ms": policies,
        "selected_policy_ms": raw["selected_policy_ms"],
        "holdout_materialized_after_lock": raw["holdout_materialized_after_lock"],
        "identities": identities,
        "policy_lock_sha256": raw["policy_lock_sha256"],
        "activity_records": records,
        "no_speech_records": windows,
        "signed_record_root": signed_root,
        "usage": usage,
        "run_failures": run_failures,
        "execution_started_at": raw["execution_started_at"],
        "execution_completed_at": raw["execution_completed_at"],
        "provider_revision": provider_revision,
        "runtime_identity_before_sha256": runtime_before,
        "runtime_identity_after_sha256": runtime_after,
        "runtime_identity_before": runtime_before_report,
        "runtime_identity_after": runtime_after_report,
        "context_commitment": raw["context_commitment"],
    }


def _capsule_execution_metadata(
    development: Mapping[str, Any],
    holdout: Mapping[str, Any],
    *,
    immutable: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "source_fact_bundle_sha256",
        "execution_started_at",
        "execution_completed_at",
        "provider_revision",
        "runtime_identity_before_sha256",
        "runtime_identity_after_sha256",
        "runtime_identity_before",
        "runtime_identity_after",
    }
    if not fields <= set(development) or not fields <= set(holdout):
        raise EvaluationError("capsule execution metadata is incomplete")
    for capsule in (development, holdout):
        try:
            runtime_before = validate_execution_identity_report(capsule["runtime_identity_before"])
            runtime_after = validate_execution_identity_report(capsule["runtime_identity_after"])
        except ValueError as exc:
            raise EvaluationError("capsule runtime identity report is invalid") from exc
        if (
            capsule["source_fact_bundle_sha256"] != immutable["source_fact_bundle_sha256"]
            or capsule["runtime_identity_before_sha256"] != immutable["environment_identity_sha256"]
            or capsule["runtime_identity_after_sha256"] != immutable["environment_identity_sha256"]
            or execution_identity_report_sha256(runtime_before)
            != capsule["runtime_identity_before_sha256"]
            or execution_identity_report_sha256(runtime_after)
            != capsule["runtime_identity_after_sha256"]
            or runtime_before != runtime_after
            or runtime_before["source"]["source_sha"] != immutable["source_sha"]
        ):
            raise EvaluationError("capsule execution identity is invalid")
    development_started = _parse_utc_time(development["execution_started_at"])
    development_completed = _parse_utc_time(development["execution_completed_at"])
    holdout_started = _parse_utc_time(holdout["execution_started_at"])
    holdout_completed = _parse_utc_time(holdout["execution_completed_at"])
    if not (development_started <= development_completed <= holdout_started <= holdout_completed):
        raise EvaluationError("capsule execution timestamps are invalid")
    provider_revision = development["provider_revision"]
    if provider_revision != holdout["provider_revision"] or (
        provider_revision is not None
        and (
            not isinstance(provider_revision, str)
            or not PROVIDER_REVISION.fullmatch(provider_revision)
        )
    ):
        raise EvaluationError("capsule provider revision is invalid")
    return {
        "execution_started_at": development["execution_started_at"],
        "execution_completed_at": holdout["execution_completed_at"],
        "provider_revision": provider_revision,
        "runtime_identity_before_sha256": development["runtime_identity_before_sha256"],
        "runtime_identity_after_sha256": holdout["runtime_identity_after_sha256"],
        "runtime_identity_before": development["runtime_identity_before"],
        "runtime_identity_after": holdout["runtime_identity_after"],
    }


def _validate_cardinality(
    records: tuple[ActivityPrimitiveRecord, ...],
    windows: tuple[NoSpeechPrimitiveRecord, ...],
    *,
    candidate_policies: tuple[int, ...],
    selected_policy: int,
) -> dict[str, int]:
    failures: dict[str, int] = {}
    development = tuple(record for record in records if record.split == "development")
    holdout = tuple(record for record in records if record.split == "holdout")
    development_sets = {
        policy: [record.activity_ordinal for record in development if record.policy_ms == policy]
        for policy in candidate_policies
    }
    expected_development_set: set[int] | None = None
    for ordinals in development_sets.values():
        current = set(ordinals)
        if len(ordinals) != EXPECTED_DEVELOPMENT_ACTIVITIES or len(current) != len(ordinals):
            failures["development_cardinality_invalid"] = 1
        if expected_development_set is None:
            expected_development_set = current
        elif current != expected_development_set:
            failures["development_policy_population_mismatch"] = 1
    if len(development) != EXPECTED_DEVELOPMENT_ACTIVITIES * len(candidate_policies):
        failures["development_cardinality_invalid"] = 1
    by_ordinal: dict[int, list[ActivityPrimitiveRecord]] = defaultdict(list)
    for record in development:
        by_ordinal[record.activity_ordinal].append(record)
    if any(
        len(values) != len(candidate_policies)
        or len({_policy_invariant_projection(value) for value in values}) != 1
        for values in by_ordinal.values()
    ):
        failures["development_policy_evidence_mismatch"] = 1
    holdout_ordinals = [record.activity_ordinal for record in holdout]
    if len(holdout) != EXPECTED_HOLDOUT_ACTIVITIES or len(set(holdout_ordinals)) != len(
        holdout_ordinals
    ):
        failures["holdout_cardinality_invalid"] = 1
    if any(record.policy_ms != selected_policy for record in holdout):
        failures["holdout_policy_violation"] = 1
    selected_development = tuple(
        record for record in development if record.policy_ms == selected_policy
    )
    development_windows = tuple(window for window in windows if window.split == "development")
    holdout_windows = tuple(window for window in windows if window.split == "holdout")
    if not _strata_are_exact(
        selected_development,
        development_windows,
        split="development",
    ) or not _strata_are_exact(holdout, holdout_windows, split="holdout"):
        failures["activity_strata_invalid"] = 1
    if expected_development_set is None or expected_development_set & set(holdout_ordinals):
        failures["split_population_overlap"] = 1
    if len((expected_development_set or set()) | set(holdout_ordinals)) != 256:
        failures["activity_population_invalid"] = 1
    window_ordinals = [window.window_ordinal for window in windows]
    if (
        len(windows) != EXPECTED_NO_SPEECH_WINDOWS
        or set(window_ordinals) != set(range(EXPECTED_NO_SPEECH_WINDOWS))
        or len(window_ordinals) != len(set(window_ordinals))
    ):
        failures["no_speech_cardinality_invalid"] = 1
    if (
        sum(window.split == "development" for window in windows) != 32
        or sum(window.split == "holdout" for window in windows) != 32
    ):
        failures["no_speech_split_invalid"] = 1
    return failures


def _evaluate_sample(
    records: tuple[ActivityPrimitiveRecord, ...],
    *,
    no_speech_records: tuple[NoSpeechPrimitiveRecord, ...],
) -> dict[str, Any]:
    assembly_successes = sum(_assembly_success(record) for record in records)
    assembly_overall = _rate_passes(
        assembly_successes,
        len(records),
        minimum_micros=THRESHOLDS["assembly_rate_micros"],
    )
    assembly_languages = all(
        _rate_passes(
            sum(_assembly_success(record) for record in group),
            len(group),
            minimum_micros=THRESHOLDS["assembly_rate_micros"],
        )
        for group in _groups(records, key="language").values()
    )
    exact_zero_fields = (
        "contamination_count",
        "duplicate_count",
        "cross_epoch_acceptance_count",
        "late_fragment_mutation_count",
        "stale_count",
        "malformed_count",
        "teardown_violation_count",
    )
    lifecycle_complete = all(_lifecycle_is_assembly_success(record) for record in records)
    structural_zero = all(
        getattr(record, field) == 0 for record in records for field in exact_zero_fields
    )
    assembly_passed = (
        assembly_overall and assembly_languages and lifecycle_complete and structural_zero
    )

    fidelity_passed = (
        _edit_rate_passes(
            records,
            maximum_micros=THRESHOLDS["cer_overall_micros"],
            word=False,
        )
        and all(
            _edit_rate_passes(
                group,
                maximum_micros=THRESHOLDS["cer_stratum_micros"],
                word=False,
            )
            for group in _groups(records, key="language").values()
        )
        and all(
            _edit_rate_passes(
                group,
                maximum_micros=THRESHOLDS["cer_stratum_micros"],
                word=False,
            )
            for group in _groups(records, key="condition").values()
        )
        and _edit_rate_passes(
            records,
            maximum_micros=THRESHOLDS["wer_overall_micros"],
            word=True,
        )
        and all(
            _edit_rate_passes(
                group,
                maximum_micros=THRESHOLDS["wer_language_micros"],
                word=True,
            )
            for group in _groups(records, key="language").values()
        )
        and _critical_span_contract_passes(records)
        and all(fact.exact for record in records for fact in record.critical_spans)
        and REQUIRED_CRITICAL_KINDS
        <= {fact.kind for record in records for fact in record.critical_spans}
        and all(record.assignment_status == "matched" for record in records)
        and _code_switch_rates_pass(records)
    )

    first_audio = [record.first_audio_ms for record in records if record.first_audio_ms is not None]
    interruption = [
        record.interruption_tail_ms
        for record in records
        if "tool_cancellation_interruption" in record.scenario_tags
    ]
    wire_zero_fields = (
        "premature_current_audio_count",
        "audio_after_terminal_count",
        "response_gap_violation_count",
        "abnormal_close_count",
        "runaway_output_count",
        "response_timeout_count",
    )
    no_speech_passed = all(
        window.false_activity_count == 0
        and window.model_audio_chunk_count == 0
        and window.abnormal_close_count == 0
        and window.audio_after_teardown_count == 0
        and window.error_code is None
        for window in no_speech_records
    )
    interaction_passed = (
        len(first_audio) == len(records)
        and all(record.timing_covered for record in records)
        and _percentile(first_audio, 95) <= THRESHOLDS["first_audio_p95_ms"]
        and max(first_audio, default=10**9) <= THRESHOLDS["first_audio_max_ms"]
        and all(value is not None for value in interruption)
        and (
            not interruption
            or (
                _percentile(interruption, 95) <= THRESHOLDS["interruption_tail_p95_ms"]
                and max(interruption) <= THRESHOLDS["interruption_tail_max_ms"]
            )
        )
        and all(getattr(record, field) == 0 for record in records for field in wire_zero_fields)
        and all(record.error_code is None for record in records)
        and no_speech_passed
    )
    published = {
        "activity_count": len(records),
        "no_speech_window_count": len(no_speech_records),
        "assembly_rate": _rate_summary(assembly_successes, len(records)),
        "cer_micros": _edit_rate_micros(records, word=False),
        "wer_micros": _edit_rate_micros(records, word=True),
        "cer": _edit_summary(records, word=False),
        "wer": _edit_summary(records, word=True),
        "critical_span_rate": _rate_summary(
            sum(fact.exact for record in records for fact in record.critical_spans),
            sum(len(record.critical_spans) for record in records),
        ),
        "no_speech_response_free_rate": _rate_summary(
            sum(
                window.false_activity_count == 0 and window.model_audio_chunk_count == 0
                for window in no_speech_records
            ),
            len(no_speech_records),
        ),
        "timing_scope": dict(TIMING_SCOPE),
        "first_audio_ms": _timing_summary(
            first_audio,
            scheduled_case_count=len(records),
        ),
        "interruption_tail_ms": _timing_summary(
            [value for value in interruption if value is not None],
            scheduled_case_count=len(interruption),
        ),
        "languages": _published_groups(records, key="language"),
        "conditions": _published_groups(records, key="condition"),
        "scenarios": _published_scenario_groups(records),
        "structural_outcomes": _structural_outcomes(records),
    }
    return {
        "assembly_passed": assembly_passed,
        "fidelity_passed": fidelity_passed,
        "interaction_passed": interaction_passed,
        "passed": assembly_passed and fidelity_passed and interaction_passed,
        "published": published,
    }


def _critical_span_contract_passes(
    records: Sequence[ActivityPrimitiveRecord],
) -> bool:
    for record in records:
        kinds = {fact.kind for fact in record.critical_spans}
        if not kinds or any(
            tag in record.scenario_tags and kind not in kinds
            for tag, kind in APPLICABLE_CRITICAL_SPANS.items()
        ):
            return False
    return True


def _assembly_success(record: ActivityPrimitiveRecord) -> bool:
    return (
        record.assembled_turn_count == 1
        and record.assignment_status == "matched"
        and _lifecycle_is_assembly_success(record)
        and record.error_code is None
        and _record_fidelity_passes(record)
    )


def _lifecycle_is_assembly_success(record: ActivityPrimitiveRecord) -> bool:
    return (
        record.expected_lifecycle_status
        == record.observed_lifecycle_status
        == "retrospective_complete"
    )


def _record_fidelity_passes(record: ActivityPrimitiveRecord) -> bool:
    return (
        _single_edit_rate_micros(record, word=False) <= THRESHOLDS["cer_stratum_micros"]
        and (
            record.reference_words is None
            or _single_edit_rate_micros(record, word=True) <= THRESHOLDS["wer_language_micros"]
        )
        and all(fact.exact for fact in record.critical_spans)
    )


def _single_edit_rate_micros(record: ActivityPrimitiveRecord, *, word: bool) -> int:
    if word:
        if record.reference_words is None:
            return 0
        edits = record.word_substitutions + record.word_insertions + record.word_deletions
        denominator = record.reference_words
    else:
        edits = record.substitutions + record.insertions + record.deletions
        denominator = record.reference_characters
    return _ratio_micros(edits, denominator)


def _edit_rate_micros(records: Sequence[ActivityPrimitiveRecord], *, word: bool) -> int | None:
    if word:
        eligible = [record for record in records if record.reference_words is not None]
        if not eligible:
            return None
        edits = sum(
            record.word_substitutions + record.word_insertions + record.word_deletions
            for record in eligible
        )
        denominator = sum(record.reference_words for record in eligible)
    else:
        edits = sum(
            record.substitutions + record.insertions + record.deletions for record in records
        )
        denominator = sum(record.reference_characters for record in records)
    return _ratio_micros(edits, denominator) if denominator else None


def _edit_summary(
    records: Sequence[ActivityPrimitiveRecord],
    *,
    word: bool,
) -> dict[str, int] | None:
    if word:
        eligible = [record for record in records if record.reference_words is not None]
        if not eligible:
            return None
        edits = sum(
            record.word_substitutions + record.word_insertions + record.word_deletions
            for record in eligible
        )
        denominator = sum(record.reference_words for record in eligible)
    else:
        edits = sum(
            record.substitutions + record.insertions + record.deletions for record in records
        )
        denominator = sum(record.reference_characters for record in records)
    return {
        "edit_operations": edits,
        "reference_units": denominator,
        "rate_micros": _ratio_micros(edits, denominator),
    }


def _edit_rate_passes(
    records: Sequence[ActivityPrimitiveRecord],
    *,
    maximum_micros: int,
    word: bool,
) -> bool:
    rate = _edit_rate_micros(records, word=word)
    return rate is None or rate <= maximum_micros


def _code_switch_rates_pass(records: tuple[ActivityPrimitiveRecord, ...]) -> bool:
    tagged: defaultdict[tuple[str, str], list[ActivityPrimitiveRecord]] = defaultdict(list)
    for record in records:
        directions = CODE_SWITCH_TAGS.intersection(record.scenario_tags)
        if not directions:
            continue
        if len(directions) != 1 or record.language == "en":
            return False
        tagged[(record.language, next(iter(directions)))].append(record)
    languages = {language for language, _direction in tagged}
    expected = {(language, direction) for language in languages for direction in CODE_SWITCH_TAGS}
    return set(tagged) == expected and all(
        _edit_rate_passes(
            group,
            maximum_micros=THRESHOLDS["cer_stratum_micros"],
            word=False,
        )
        for group in tagged.values()
    )


def _policy_invariant_projection(record: ActivityPrimitiveRecord) -> tuple[Any, ...]:
    return (
        record.activity_ordinal,
        record.language,
        record.condition,
        record.scenario_tags,
        record.assignment_status,
        record.expected_lifecycle_status,
        record.fragment_count,
        record.event_count,
        record.reference_characters,
        record.hypothesis_characters,
        record.substitutions,
        record.insertions,
        record.deletions,
        record.reference_words,
        record.hypothesis_words,
        record.word_substitutions,
        record.word_insertions,
        record.word_deletions,
        record.ambiguity_margin_micros,
        tuple((fact.kind, fact.exact) for fact in record.critical_spans),
        record.contamination_count,
        record.cross_epoch_acceptance_count,
        record.stale_count,
        record.timing_covered,
        record.first_audio_ms,
        record.interruption_tail_ms,
        record.premature_current_audio_count,
        record.audio_after_terminal_count,
        record.response_gap_violation_count,
        record.abnormal_close_count,
        record.runaway_output_count,
        record.response_timeout_count,
        record.malformed_count,
        record.teardown_violation_count,
        record.error_code,
    )


def _strata_are_exact(
    records: tuple[ActivityPrimitiveRecord, ...],
    windows: tuple[NoSpeechPrimitiveRecord, ...],
    *,
    split: str,
) -> bool:
    try:
        validate_gate0b_allocation(
            (
                AllocationActivity(
                    ordinal=record.activity_ordinal,
                    split=record.split,
                    language=record.language,
                    condition=record.condition,
                    scenario_tags=record.scenario_tags,
                    critical_span_kinds=tuple(fact.kind for fact in record.critical_spans),
                )
                for record in records
            ),
            (
                NoSpeechAllocation(
                    ordinal=window.window_ordinal,
                    split=window.split,
                    condition=window.condition,
                )
                for window in windows
            ),
            split=split,
        )
    except AllocationError:
        return False
    return True


def _groups(
    records: Sequence[ActivityPrimitiveRecord],
    *,
    key: str,
) -> dict[str, tuple[ActivityPrimitiveRecord, ...]]:
    grouped: dict[str, list[ActivityPrimitiveRecord]] = defaultdict(list)
    for record in records:
        grouped[getattr(record, key)].append(record)
    return {name: tuple(values) for name, values in grouped.items()}


def _published_groups(
    records: Sequence[ActivityPrimitiveRecord],
    *,
    key: str,
) -> dict[str, dict[str, Any]]:
    result = {}
    for name, group in sorted(_groups(records, key=key).items()):
        if len(group) < SMALL_CELL_MINIMUM:
            result[name] = {"count": len(group), "suppressed": True}
            continue
        result[name] = _published_stratum(group)
    return result


def _published_scenario_groups(
    records: Sequence[ActivityPrimitiveRecord],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ActivityPrimitiveRecord]] = defaultdict(list)
    for record in records:
        for scenario in record.scenario_tags:
            grouped[scenario].append(record)
    result: dict[str, dict[str, Any]] = {}
    for name, group in sorted(grouped.items()):
        if len(group) < SMALL_CELL_MINIMUM:
            result[name] = {"count": len(group), "suppressed": True}
            continue
        result[name] = _published_stratum(group)
    return result


def _published_stratum(
    records: Sequence[ActivityPrimitiveRecord],
) -> dict[str, Any]:
    successes = sum(_assembly_success(record) for record in records)
    interruption_cases = [
        record for record in records if "tool_cancellation_interruption" in record.scenario_tags
    ]
    return {
        "count": len(records),
        "suppressed": False,
        "timing_scope": dict(TIMING_SCOPE),
        "assembly_rate": _rate_summary(successes, len(records)),
        "cer_micros": _edit_rate_micros(records, word=False),
        "wer_micros": _edit_rate_micros(records, word=True),
        "cer": _edit_summary(records, word=False),
        "wer": _edit_summary(records, word=True),
        "first_audio_ms": _timing_summary(
            [record.first_audio_ms for record in records if record.first_audio_ms is not None],
            scheduled_case_count=len(records),
        ),
        "interruption_tail_ms": _timing_summary(
            [
                record.interruption_tail_ms
                for record in interruption_cases
                if record.interruption_tail_ms is not None
            ],
            scheduled_case_count=len(interruption_cases),
        ),
        "structural_outcomes": _structural_outcomes(records),
    }


def _structural_outcomes(
    records: Sequence[ActivityPrimitiveRecord],
) -> dict[str, Any]:
    resource_errors = {
        "cost_reservation_exhausted",
        "provider_request_reservation_exhausted",
        "run_output_audio_cap_exceeded",
        "session_cost_cap_exceeded",
        "session_timeout",
    }
    return {
        "assignment_status": {
            status: sum(record.assignment_status == status for record in records)
            for status in ("matched", "ambiguous", "unassigned")
        },
        "lifecycle_status": {
            "scheduled_case_count": len(records),
            "zero_denominator": not records,
            "exact_match_count": sum(
                record.expected_lifecycle_status == record.observed_lifecycle_status
                for record in records
            ),
            "expected_counts": {
                status: sum(record.expected_lifecycle_status == status for record in records)
                for status in LIFECYCLE_STATUSES
            },
            "observed_counts": {
                status: sum(record.observed_lifecycle_status == status for record in records)
                for status in LIFECYCLE_STATUSES
            },
        },
        "duplicate_count": sum(record.duplicate_count for record in records),
        "contamination_count": sum(record.contamination_count for record in records),
        "late_fragment_count": sum(record.late_fragment_mutation_count for record in records),
        "malformed_count": sum(record.malformed_count for record in records),
        "stale_count": sum(record.stale_count for record in records),
        "teardown_count": sum(record.teardown_violation_count for record in records),
        "resource_outcome_count": sum(record.error_code in resource_errors for record in records),
    }


def _rate_passes(numerator: int, denominator: int, *, minimum_micros: int) -> bool:
    return denominator > 0 and numerator * 1_000_000 >= denominator * minimum_micros


def _rate_summary(numerator: int, denominator: int) -> dict[str, Any]:
    lower, upper = _clopper_pearson_95(numerator, denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate_micros": _ratio_micros(numerator, denominator) if denominator else None,
        "confidence_95_micros": [lower, upper] if denominator else None,
    }


def _clopper_pearson_95(successes: int, trials: int) -> tuple[int, int]:
    if trials <= 0 or not 0 <= successes <= trials:
        return (0, 0)
    alpha_tail = 0.025
    if successes == 0:
        lower = 0.0
    else:
        lower = _bisect_probability(
            lambda probability: _binomial_upper_tail(successes, trials, probability),
            target=alpha_tail,
            increasing=True,
        )
    if successes == trials:
        upper = 1.0
    else:
        upper = _bisect_probability(
            lambda probability: _binomial_cdf(successes, trials, probability),
            target=alpha_tail,
            increasing=False,
        )
    return (round(lower * 1_000_000), round(upper * 1_000_000))


def _bisect_probability(function, *, target: float, increasing: bool) -> float:
    low = 0.0
    high = 1.0
    for _ in range(70):
        middle = (low + high) / 2
        value = function(middle)
        if (value < target) == increasing:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _binomial_cdf(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, count) * probability**count * (1 - probability) ** (trials - count)
        for count in range(successes + 1)
    )


def _binomial_upper_tail(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, count) * probability**count * (1 - probability) ** (trials - count)
        for count in range(successes, trials + 1)
    )


def _ratio_micros(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise EvaluationError("metric denominator must be positive")
    return (numerator * 1_000_000 + denominator // 2) // denominator


def _percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 10**9
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * percentile / 100))
    return ordered[rank - 1]


def _timing_summary(
    values: Sequence[int],
    *,
    scheduled_case_count: int,
) -> dict[str, Any]:
    if (
        isinstance(scheduled_case_count, bool)
        or not isinstance(scheduled_case_count, int)
        or scheduled_case_count < 0
        or len(values) > scheduled_case_count
    ):
        raise EvaluationError("timing population is invalid")
    if not values:
        return {
            "count": 0,
            "observed_count": 0,
            "scheduled_case_count": scheduled_case_count,
            "zero_denominator": scheduled_case_count == 0,
            "p50": None,
            "p95": None,
            "max": None,
            "histogram": {
                "le_250": 0,
                "251_500": 0,
                "501_1000": 0,
                "1001_1500": 0,
                "1501_2500": 0,
                "gt_2500": 0,
            },
        }
    return {
        "count": len(values),
        "observed_count": len(values),
        "scheduled_case_count": scheduled_case_count,
        "zero_denominator": scheduled_case_count == 0,
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "max": max(values),
        "histogram": {
            "le_250": sum(value <= 250 for value in values),
            "251_500": sum(250 < value <= 500 for value in values),
            "501_1000": sum(500 < value <= 1_000 for value in values),
            "1001_1500": sum(1_000 < value <= 1_500 for value in values),
            "1501_2500": sum(1_500 < value <= 2_500 for value in values),
            "gt_2500": sum(value > 2_500 for value in values),
        },
    }


def _validate_policy_configuration(policies: tuple[int, ...], selected: object) -> None:
    if policies != EXPECTED_POLICIES or any(policy not in POLICIES_MS for policy in policies):
        raise EvaluationError("candidate policy configuration is invalid")
    if isinstance(selected, bool) or not isinstance(selected, int) or selected not in policies:
        raise EvaluationError("selected policy is invalid")


def _validate_identities(raw: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != FINAL_IDENTITY_FIELDS:
        raise EvaluationError("evidence identities are invalid")
    result = {}
    for key in sorted(FINAL_IDENTITY_FIELDS):
        value = raw[key]
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise EvaluationError("evidence identity digest is invalid")
        result[key] = value
    return result


def _validate_usage(raw: object) -> dict[str, Any]:
    fields = {
        "metadata_complete",
        "provider_requests",
        "wall_clock_seconds",
        "input_audio_seconds",
        "output_audio_seconds",
        "cost_microusd",
        "input_audio_tokens",
        "output_audio_tokens",
        "input_text_tokens",
        "output_text_tokens",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise EvaluationError("usage evidence is invalid")
    if not isinstance(raw["metadata_complete"], bool):
        raise EvaluationError("usage metadata flag is invalid")
    result = {"metadata_complete": raw["metadata_complete"]}
    for field in fields - {"metadata_complete"}:
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvaluationError("usage value is invalid")
        result[field] = value
    return result


def _combine_usage(
    left: Mapping[str, int | bool],
    right: Mapping[str, int | bool],
) -> dict[str, int | bool]:
    fields = {
        "metadata_complete",
        "provider_requests",
        "wall_clock_seconds",
        "input_audio_seconds",
        "output_audio_seconds",
        "input_audio_tokens",
        "output_audio_tokens",
        "input_text_tokens",
        "output_text_tokens",
    }
    if set(left) != fields or set(right) != fields:
        raise EvaluationError("capsule usage fields are invalid")
    combined: dict[str, int | bool] = {
        "metadata_complete": left["metadata_complete"] is True
        and right["metadata_complete"] is True
    }
    for field in fields - {"metadata_complete"}:
        left_value = left[field]
        right_value = right[field]
        if (
            isinstance(left_value, bool)
            or not isinstance(left_value, int)
            or isinstance(right_value, bool)
            or not isinstance(right_value, int)
            or left_value < 0
            or right_value < 0
        ):
            raise EvaluationError("capsule usage value is invalid")
        combined[field] = left_value + right_value
    return combined


def _cost_microusd_from_usage(usage: Mapping[str, int | bool]) -> int:
    pricing = load_pricing(PRICING_PATH)
    value = pricing.cost_usd(
        input_audio_tokens=int(usage["input_audio_tokens"]),
        output_audio_tokens=int(usage["output_audio_tokens"]),
        input_text_tokens=int(usage["input_text_tokens"]),
        output_text_tokens=int(usage["output_text_tokens"]),
    )
    return int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _usage_is_complete_and_bounded(usage: Mapping[str, Any]) -> bool:
    expected_cost_microusd = _cost_microusd_from_usage(usage)
    return (
        usage["metadata_complete"]
        and usage["provider_requests"] == 128
        and usage["wall_clock_seconds"] <= 3_600
        and usage["input_audio_seconds"] <= 3_600
        and usage["output_audio_seconds"] <= 1_800
        and usage["cost_microusd"] <= 10_000_000
        and expected_cost_microusd <= usage["cost_microusd"] <= expected_cost_microusd + 1
    )


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise EvaluationError(f"{label} is invalid")
    return value


def _safe_id_list(value: object, *, label: str, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in value)
    ):
        raise EvaluationError(f"{label} is invalid")
    return tuple(value)


def _failure_report(failures: Mapping[str, int]) -> dict[str, Any]:
    report = {
        "schema_id": EVALUATION_RESULT_SCHEMA_ID,
        "status": "no_go",
        "campaign_id": None,
        "phase_history": [],
        "selected_policy_ms": None,
        "candidate_policy_results": {},
        "record_root": {"sha256": None, "leaf_count": 0, "key_id": None},
        "identities": {},
        "execution": {
            "started_at": None,
            "completed_at": None,
            "provider_revision": None,
            "runtime_identity_before_sha256": None,
            "runtime_identity_after_sha256": None,
            "runtime_identity_before": None,
            "runtime_identity_after": None,
        },
        "samples": {"development": {}, "holdout": {}},
        "usage": {},
        "failures": dict(sorted(failures.items())),
        **empty_evidence_flags(),
    }
    assert_payload_safe(report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate one sealed Gate 0B evidence set.")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--commitment-key", type=Path, required=True)
    parser.add_argument("--custodian-private-key", type=Path, required=True)
    parser.add_argument("--custodian-key-id", required=True)
    parser.add_argument("--ledger-custodian-public-key", type=Path, required=True)
    parser.add_argument("--record-root-signing-key", type=Path, required=True)
    parser.add_argument("--record-root-key-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        capture_trusted_startup_identity(
            REPO_ROOT,
            expected_target="evaluate-qualification",
        )
        raw = read_private_file(
            args.bundle,
            repo_root=REPO_ROOT,
            maximum_bytes=MAX_ARTIFACT_BYTES,
        )
        try:
            bundle = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            bundle = {}
        report = evaluate_custody_bundle(
            bundle,
            commitment_key=_read_private_key_file(args.commitment_key, label="commitment"),
            custodian_private_key=X25519PrivateKey.from_private_bytes(
                _read_private_key_file(args.custodian_private_key, label="custodian")
            ),
            expected_custodian_key_id=args.custodian_key_id,
            ledger_custodian_public_key=_read_public_key_file(
                args.ledger_custodian_public_key,
                label="ledger custodian",
            ),
            record_root_signing_key=Ed25519PrivateKey.from_private_bytes(
                _read_private_key_file(
                    args.record_root_signing_key,
                    label="record root signing",
                )
            ),
            record_root_key_id=args.record_root_key_id,
        )
        _write_private_report(args.output, report)
    except (
        EvaluationError,
        IdentityError,
        PrivatePathError,
        OSError,
        TypeError,
        ValueError,
    ):
        print('{"error_code":"private_custody_rejected","status":"blocked"}')
        return 2
    return 0 if report["status"] == "pass" else 1


def _read_private_key_file(path: Path, *, label: str) -> bytes:
    try:
        value = read_private_file(path, repo_root=REPO_ROOT, maximum_bytes=32)
    except PrivatePathError as exc:
        raise EvaluationError(f"{label} key file is invalid") from exc
    if len(value) != 32:
        raise EvaluationError(f"{label} key must be exactly 32 bytes")
    return value


def _read_public_key_file(path: Path, *, label: str) -> bytes:
    try:
        value = read_private_file(path, repo_root=REPO_ROOT, maximum_bytes=32)
    except PrivatePathError as exc:
        raise EvaluationError(f"{label} public key file is invalid") from exc
    if len(value) != 32:
        raise EvaluationError(f"{label} public key must be exactly 32 bytes")
    return value


def _load_pinned_approval_public_key() -> bytes:
    path = PINNED_APPROVAL_ROOT_PATH
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise EvaluationError("pinned approval root is unavailable")
    value = path.read_bytes()
    if len(value) != 32:
        raise EvaluationError("pinned approval root is unprovisioned")
    return value


def _parse_utc_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvaluationError("ledger claim time is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvaluationError("ledger claim time is invalid") from exc
    if value != parsed.isoformat(timespec="seconds").replace("+00:00", "Z"):
        raise EvaluationError("ledger claim time is invalid")
    return parsed


def _write_private_report(path: Path, report: Mapping[str, Any]) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        write_private_file(path, payload, repo_root=REPO_ROOT)
    except PrivatePathError as exc:
        raise EvaluationError("output path is unavailable") from exc


if __name__ == "__main__":
    raise SystemExit(main())
