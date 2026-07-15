#!/usr/bin/env python3
"""Independently evaluate payload-free Gemini caller-turn Gate 0B evidence."""

from __future__ import annotations

import argparse
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
import stat
import sys
from typing import Any, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.caller_turn_measurement import (  # noqa: E402
    POLICIES_MS,
    ActivityPrimitiveRecord,
    MeasurementError,
    NoSpeechPrimitiveRecord,
    build_signed_record_root,
    compute_record_merkle_root,
    derive_primitive_records_from_capsule,
    open_audit_capsule,
    verify_record_commitment,
    verify_signed_record_root,
)
from app.services.caller_turn_qualification import (  # noqa: E402
    assert_payload_safe,
    empty_evidence_flags,
    load_pricing,
)
from app.services.qualification_identity import (  # noqa: E402
    canonical_json_bytes,
    validate_ledger_snapshot,
    verify_attempt_authorization,
    verify_campaign_approval,
)
from scripts.run_gemini_caller_turn_qualification import (  # noqa: E402
    PREREGISTRATION_EXTERNAL_FIELDS,
    build_preregistration,
)


EVIDENCE_SCHEMA_ID = "gate_0b_evidence_v1"
CUSTODY_BUNDLE_SCHEMA_ID = "gate_0b_custody_bundle_v1"
REPORT_SCHEMA_ID = "gate_0b_evaluation_report_v1"
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
PINNED_APPROVAL_ROOT_PATH = (
    REPO_ROOT / "config/qualification/gate_0b_approval_root.ed25519.pub"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
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
EVIDENCE_CONTEXT_HMAC_DOMAIN = b"gate-0b-evidence-context-v1\x00"
FINAL_IDENTITY_FIELDS = frozenset(
    {
        "source_sha256",
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
        "identities": {
            field: identity[field] for field in sorted(POLICY_LOCK_IDENTITY_FIELDS)
        },
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
        field: artifact[field]
        for field in fields - {"activity_records", "no_speech_records"}
    }
    context["activity_record_count"] = len(activity_records)
    context["no_speech_record_count"] = len(no_speech_records)
    return hmac.new(
        commitment_key,
        EVIDENCE_CONTEXT_HMAC_DOMAIN + canonical_json_bytes(context),
        hashlib.sha256,
    ).hexdigest()


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
    if not parsed["attempt_completed"]:
        failures["attempt_incomplete"] = 1
    if not parsed["holdout_materialized_after_lock"]:
        failures["holdout_materialization_invalid"] = 1
    if parsed["run_failures"]:
        failures["run_failure_present"] = len(parsed["run_failures"])
    if not _usage_is_complete_and_bounded(parsed["usage"]):
        failures["usage_or_budget_invalid"] = 1
    if parsed["identities"]["pricing_sha256"] != hashlib.sha256(
        PRICING_PATH.read_bytes()
    ).hexdigest():
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
            tuple(
                record
                for record in development_records
                if record.policy_ms == policy
            ),
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
        policy
        for policy in parsed["candidate_policies_ms"]
        if candidate_samples[policy]["passed"]
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
            "attempt_completed": parsed["attempt_completed"] and root_valid and context_valid,
            "assembly_sample_passed": assembly_passed,
            "transcription_fidelity_sample_passed": fidelity_passed,
            "provider_interaction_integrity_sample_passed": interaction_passed,
            "gate_0b_sample_passed": gate_passed,
        }
    )
    signed_payload = parsed["signed_record_root"]["payload"]
    report = {
        "schema_id": REPORT_SCHEMA_ID,
        "status": "pass" if gate_passed else "no_go",
        "campaign_id": parsed["campaign_id"],
        "selected_policy_ms": parsed["selected_policy_ms"],
        "candidate_policy_results": candidate_results,
        "record_root": {
            "sha256": signed_payload["merkle_root_sha256"],
            "leaf_count": signed_payload["leaf_count"],
            "key_id": parsed["signed_record_root"]["key_id"],
        },
        "identities": parsed["identities"],
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
    record_root_signing_key: Ed25519PrivateKey,
    record_root_key_id: str,
) -> dict[str, Any]:
    fields = {
        "schema_id",
        "campaign_id",
        "development_capsule",
        "holdout_capsule",
        "ledger",
        "preregistration",
        "campaign_envelope",
        "attempt_envelope",
        "usage",
        "run_failures",
    }
    if not isinstance(bundle, Mapping) or set(bundle) != fields:
        raise EvaluationError("custody bundle fields are invalid")
    if bundle["schema_id"] != CUSTODY_BUNDLE_SCHEMA_ID:
        raise EvaluationError("custody bundle schema is invalid")
    campaign_id = _safe_id(bundle["campaign_id"], label="campaign ID")
    ledger = bundle["ledger"]
    if not isinstance(ledger, Mapping) or ledger.get("campaign_id") != campaign_id:
        raise EvaluationError("custody ledger campaign is invalid")
    state = validate_ledger_snapshot(ledger)
    if (
        state.phase.value != "completed"
        or tuple(value.value for value in state.phase_history) != EXPECTED_PHASE_HISTORY
        or not state.holdout_materialized
        or state.selected_policy_ms is None
        or state.policy_lock_sha256 is None
    ):
        raise EvaluationError("custody ledger phase history is incomplete")

    development_envelope = bundle["development_capsule"]
    holdout_envelope = bundle["holdout_capsule"]
    if not isinstance(development_envelope, Mapping) or not isinstance(
        holdout_envelope, Mapping
    ):
        raise EvaluationError("custody capsule envelope is invalid")
    development_capsule_sha256 = hashlib.sha256(
        canonical_json_bytes(development_envelope)
    ).hexdigest()
    holdout_capsule_sha256 = hashlib.sha256(
        canonical_json_bytes(holdout_envelope)
    ).hexdigest()

    completed_outcomes = [
        record
        for record in ledger["records"]
        if record.get("event") == "outcome" and record.get("outcome") == "completed"
    ]
    lock_transitions = [
        record
        for record in ledger["records"]
        if record.get("event") == "phase_transition"
        and record.get("to_phase") == "policy_selection_locked"
    ]
    completion_transitions = [
        record
        for record in ledger["records"]
        if record.get("event") == "phase_transition"
        and record.get("to_phase") == "completed"
    ]
    if (
        len(completed_outcomes) != 1
        or len(lock_transitions) != 1
        or len(completion_transitions) != 1
        or completed_outcomes[0].get("capsule_sha256") != development_capsule_sha256
        or lock_transitions[0].get("capsule_sha256") != development_capsule_sha256
        or completion_transitions[0].get("capsule_sha256") != holdout_capsule_sha256
    ):
        raise EvaluationError("custody capsule and ledger identities disagree")

    claims = [record for record in ledger["records"] if record.get("event") == "claim"]
    completed_attempt_id = completed_outcomes[0].get("attempt_id")
    completed_claims = [
        record for record in claims if record.get("attempt_id") == completed_attempt_id
    ]
    if len(completed_claims) != 1:
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
    preregistration = bundle["preregistration"]
    if not isinstance(preregistration, Mapping):
        raise EvaluationError("custody preregistration is invalid")
    immutable = preregistration.get("immutable_values")
    if not isinstance(immutable, Mapping):
        raise EvaluationError("custody preregistration is invalid")
    try:
        expected_preregistration = build_preregistration(
            {
                "schema_id": "gate_0b_preregistration_values_v1",
                **{field: immutable[field] for field in PREREGISTRATION_EXTERNAL_FIELDS},
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationError("custody preregistration is invalid") from exc
    if dict(preregistration) != expected_preregistration:
        raise EvaluationError("custody preregistration digest is invalid")
    approval_public_key = _load_pinned_approval_public_key()
    if hashlib.sha256(approval_public_key).hexdigest() != immutable[
        "approval_public_key_sha256"
    ]:
        raise EvaluationError("custody approval root is not preregistered")
    claim_time = _parse_utc_time(completed_claims[0].get("at"))
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
    if (
        ledger["preregistration_sha256"] != preregistration["preregistration_sha256"]
        or ledger["source_sha"] != immutable["source_sha"]
        or ledger["campaign_approval_sha256"] != campaign.signed_payload_sha256
        or completed_claims[0]["authorization_sha256"] != authorization.signed_payload_sha256
        or authorization.attempt_id != completed_attempt_id
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
        "environment_sha256": immutable["environment_identity_sha256"],
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "corpus_sha256": immutable["corpus_sha256"],
        "pricing_sha256": hashlib.sha256(PRICING_PATH.read_bytes()).hexdigest(),
        "preregistration_sha256": ledger["preregistration_sha256"],
        "campaign_approval_sha256": ledger["campaign_approval_sha256"],
        "attempt_authorization_sha256": completed_claims[0]["authorization_sha256"],
        "development_capsule_sha256": development_capsule_sha256,
        "holdout_capsule_sha256": holdout_capsule_sha256,
        "ledger_head_sha256": lock_transitions[0]["previous_hash"],
        "custodian_public_key_sha256": hashlib.sha256(custodian_public_key).hexdigest(),
        "record_root_public_key_sha256": hashlib.sha256(record_root_public_key).hexdigest(),
        }
    )
    if (
        identities["evaluator_sha256"] != immutable["evaluator_sha256"]
        or identities["pricing_sha256"] != immutable["pricing_sha256"]
        or identities["custodian_public_key_sha256"]
        != immutable["custodian_public_key_sha256"]
        or identities["record_root_public_key_sha256"]
        != immutable["record_root_public_key_sha256"]
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
    holdout_records, holdout_windows = derive_primitive_records_from_capsule(
        holdout_capsule,
        policies_ms=(selected_policy_ms,),
        commitment_key=commitment_key,
    )
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
        "attempt_completed": True,
        "phase_history": [value.value for value in state.phase_history],
        "candidate_policies_ms": list(EXPECTED_POLICIES),
        "selected_policy_ms": selected_policy_ms,
        "holdout_materialized_after_lock": state.holdout_materialized,
        "identities": identities,
        "policy_lock_sha256": state.policy_lock_sha256,
        "activity_records": [record.to_dict() for record in activity_records],
        "no_speech_records": [record.to_dict() for record in no_speech_records],
        "signed_record_root": signed_root,
        "usage": bundle["usage"],
        "run_failures": bundle["run_failures"],
    }
    artifact["context_commitment"] = compute_evidence_context_commitment(
        artifact,
        commitment_key=commitment_key,
    )
    return evaluate_evidence_artifact(
        artifact,
        commitment_key=commitment_key,
        root_public_key=record_root_public_key,
        expected_root_key_id=record_root_key_id,
    )


def _parse_artifact(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_id",
        "campaign_id",
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
        "context_commitment",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise EvaluationError("evidence artifact fields are invalid")
    if raw["schema_id"] != EVIDENCE_SCHEMA_ID:
        raise EvaluationError("evidence artifact schema is invalid")
    campaign_id = _safe_id(raw["campaign_id"], label="campaign ID")
    for name in ("attempt_completed", "holdout_materialized_after_lock"):
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
    return {
        "campaign_id": campaign_id,
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
        "context_commitment": raw["context_commitment"],
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
    if (
        len(holdout) != EXPECTED_HOLDOUT_ACTIVITIES
        or len(set(holdout_ordinals)) != len(holdout_ordinals)
    ):
        failures["holdout_cardinality_invalid"] = 1
    if any(record.policy_ms != selected_policy for record in holdout):
        failures["holdout_policy_violation"] = 1
    selected_development = tuple(
        record
        for record in development
        if record.policy_ms == selected_policy
    )
    if not _strata_are_exact(selected_development) or not _strata_are_exact(holdout):
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
    if sum(window.split == "development" for window in windows) != 32 or sum(
        window.split == "holdout" for window in windows
    ) != 32:
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
    lifecycle_exact = all(
        record.expected_lifecycle_status == record.observed_lifecycle_status
        for record in records
    )
    structural_zero = all(
        getattr(record, field) == 0
        for record in records
        for field in exact_zero_fields
    )
    assembly_passed = assembly_overall and assembly_languages and lifecycle_exact and structural_zero

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
        and all(
            getattr(record, field) == 0
            for record in records
            for field in wire_zero_fields
        )
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
                window.false_activity_count == 0
                and window.model_audio_chunk_count == 0
                for window in no_speech_records
            ),
            len(no_speech_records),
        ),
        "first_audio_ms": _timing_summary(first_audio),
        "interruption_tail_ms": _timing_summary(
            [value for value in interruption if value is not None]
        ),
        "languages": _published_groups(records, key="language"),
        "conditions": _published_groups(records, key="condition"),
    }
    return {
        "assembly_passed": assembly_passed,
        "fidelity_passed": fidelity_passed,
        "interaction_passed": interaction_passed,
        "passed": assembly_passed and fidelity_passed and interaction_passed,
        "published": published,
    }


def _assembly_success(record: ActivityPrimitiveRecord) -> bool:
    return (
        record.assembled_turn_count == 1
        and record.assignment_status == "matched"
        and record.expected_lifecycle_status == record.observed_lifecycle_status
        and record.error_code is None
        and _record_fidelity_passes(record)
    )


def _record_fidelity_passes(record: ActivityPrimitiveRecord) -> bool:
    return (
        _single_edit_rate_micros(record, word=False) <= THRESHOLDS["cer_stratum_micros"]
        and (
            record.reference_words is None
            or _single_edit_rate_micros(record, word=True)
            <= THRESHOLDS["wer_language_micros"]
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
        edits = sum(record.substitutions + record.insertions + record.deletions for record in records)
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
            record.substitutions + record.insertions + record.deletions
            for record in records
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
    tagged = defaultdict(list)
    for record in records:
        if any(tag.startswith("code_switch_") for tag in record.scenario_tags):
            tagged[record.language].append(record)
    return all(
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


def _strata_are_exact(records: tuple[ActivityPrimitiveRecord, ...]) -> bool:
    language_groups = _groups(records, key="language")
    if set(language_groups) != {"ar", "en", "es", "fr", "hi", "ht", "pt", "zh"}:
        return False
    if any(len(group) != 16 for group in language_groups.values()):
        return False
    return all(
        len([record for record in group if record.condition == condition]) == 4
        for group in language_groups.values()
        for condition in (
            "clean",
            "twilio_codec_only",
            "acoustic_impairment",
            "interaction_stress",
        )
    )


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
        successes = sum(_assembly_success(record) for record in group)
        result[name] = {
            "count": len(group),
            "suppressed": False,
            "assembly_rate": _rate_summary(successes, len(group)),
            "cer_micros": _edit_rate_micros(group, word=False),
            "wer_micros": _edit_rate_micros(group, word=True),
            "cer": _edit_summary(group, word=False),
            "wer": _edit_summary(group, word=True),
        }
    return result


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
        math.comb(trials, count)
        * probability**count
        * (1 - probability) ** (trials - count)
        for count in range(successes + 1)
    )


def _binomial_upper_tail(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, count)
        * probability**count
        * (1 - probability) ** (trials - count)
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


def _timing_summary(values: Sequence[int]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "max": max(values),
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


def _usage_is_complete_and_bounded(usage: Mapping[str, Any]) -> bool:
    pricing = load_pricing(PRICING_PATH)
    expected_cost_microusd = int(
        (
            pricing.cost_usd(
            input_audio_tokens=usage["input_audio_tokens"],
            output_audio_tokens=usage["output_audio_tokens"],
            input_text_tokens=usage["input_text_tokens"],
            output_text_tokens=usage["output_text_tokens"],
        )
            * 1_000_000
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    return (
        usage["metadata_complete"]
        and usage["provider_requests"] <= 128
        and usage["wall_clock_seconds"] <= 3_600
        and usage["input_audio_seconds"] <= 3_600
        and usage["output_audio_seconds"] <= 1_800
        and usage["cost_microusd"] <= 10_000_000
        and usage["cost_microusd"] == expected_cost_microusd
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
        "schema_id": REPORT_SCHEMA_ID,
        "status": "no_go",
        "campaign_id": None,
        "selected_policy_ms": None,
        "candidate_policy_results": {},
        "record_root": {"sha256": None, "leaf_count": 0, "key_id": None},
        "identities": {},
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
    parser.add_argument("--record-root-signing-key", type=Path, required=True)
    parser.add_argument("--record-root-key-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw = args.bundle.read_bytes()
    if len(raw) > MAX_ARTIFACT_BYTES:
        report = _failure_report({"custody_bundle_invalid": 1})
    else:
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
            record_root_signing_key=Ed25519PrivateKey.from_private_bytes(
                _read_private_key_file(
                    args.record_root_signing_key,
                    label="record root signing",
                )
            ),
            record_root_key_id=args.record_root_key_id,
        )
    _write_private_report(args.output, report)
    return 0 if report["status"] == "pass" else 1


def _read_private_key_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise EvaluationError(f"{label} key file is invalid")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise EvaluationError(f"{label} key file permissions are too broad")
    value = path.read_bytes()
    if len(value) != 32:
        raise EvaluationError(f"{label} key must be exactly 32 bytes")
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
    return parsed


def _write_private_report(path: Path, report: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise EvaluationError("output path must be an absent file in an existing directory")
    payload = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EvaluationError("output path is unavailable") from exc
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise EvaluationError("output write did not make progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == "__main__":
    raise SystemExit(main())
