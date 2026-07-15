"""Fresh signed privacy custody and opaque Gate 0B asset release contracts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.qualification_identity import canonical_json_bytes


PRIVACY_CUSTODY_SCHEMA_ID = "gate_0b_privacy_custody_authorization_v1"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA = re.compile(r"[0-9a-f]{40,64}")
MAX_RECEIPT_AGE = timedelta(minutes=5)
MAX_RECEIPT_LIFETIME = timedelta(minutes=15)
MAX_RETENTION = timedelta(days=30)


class PrivacyCustodyError(ValueError):
    """Raised when current privacy custody cannot authorize asset access."""


@dataclass(frozen=True, slots=True)
class PrivacyCustodyAuthorization:
    campaign_id: str
    authorization_id: str
    attempt_id: str
    split: str
    preregistration_sha256: str
    source_sha: str
    schedule_sha256: str
    corpus_sha256: str
    project: str
    model: str
    consent_registry_sha256: str
    withdrawal_registry_sha256: str
    purpose_attestation_sha256: str
    rights_attestation_sha256: str
    provider_disclosure_sha256: str
    subject_set_sha256: str
    retention_policy_sha256: str
    provider_retention_decision: str
    residual_retention_acceptance_sha256: str
    issued_at: datetime
    expires_at: datetime
    deletion_deadline: datetime
    nonce: str
    signed_payload_sha256: str


@dataclass(frozen=True, slots=True)
class QualificationAssets:
    plans: tuple[Any, ...]
    no_speech_plans: tuple[Any, ...]


class OpaqueQualificationAssetLoader(Protocol):
    def load(self, authorization: PrivacyCustodyAuthorization) -> QualificationAssets:
        """Release one exact split only after receiving verified privacy custody."""


def verify_privacy_custody(
    envelope: Mapping[str, Any],
    *,
    public_key: bytes,
    expected_key_id: str,
    expected_campaign_id: str,
    expected_authorization_id: str,
    expected_attempt_id: str,
    expected_split: str,
    expected_preregistration_sha256: str,
    expected_source_sha: str,
    expected_schedule_sha256: str,
    expected_corpus_sha256: str,
    expected_project: str,
    expected_model: str,
    expected_consent_registry_sha256: str,
    expected_retention_policy_sha256: str,
    expected_residual_retention_acceptance_sha256: str,
    now: datetime,
) -> PrivacyCustodyAuthorization:
    """Verify one short-lived receipt before any corpus or schedule materialization."""
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "key_id",
        "payload",
        "signature",
    }:
        raise PrivacyCustodyError("privacy custody envelope is invalid")
    if envelope["key_id"] != expected_key_id or not SAFE_ID.fullmatch(expected_key_id):
        raise PrivacyCustodyError("privacy custody key binding is invalid")
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise PrivacyCustodyError("privacy custody trust root is invalid")
    payload = envelope["payload"]
    fields = {
        "schema_id",
        "campaign_id",
        "authorization_id",
        "attempt_id",
        "split",
        "preregistration_sha256",
        "source_sha",
        "schedule_sha256",
        "corpus_sha256",
        "project",
        "model",
        "consent_registry_sha256",
        "withdrawal_registry_sha256",
        "purpose_attestation_sha256",
        "rights_attestation_sha256",
        "provider_disclosure_sha256",
        "subject_set_sha256",
        "retention_policy_sha256",
        "provider_retention_decision",
        "residual_retention_acceptance_sha256",
        "consent_active",
        "withdrawal_clear",
        "purpose_limited",
        "usage_rights_active",
        "provider_disclosures_current",
        "issued_at",
        "expires_at",
        "deletion_deadline",
        "nonce",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise PrivacyCustodyError("privacy custody payload fields are invalid")
    if payload["schema_id"] != PRIVACY_CUSTODY_SCHEMA_ID:
        raise PrivacyCustodyError("privacy custody schema is invalid")
    signature = envelope["signature"]
    try:
        decoded_signature = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            decoded_signature,
            canonical_json_bytes(payload),
        )
    except (TypeError, ValueError, InvalidSignature) as exc:
        raise PrivacyCustodyError("privacy custody signature is invalid") from exc

    expected_bindings = {
        "campaign_id": expected_campaign_id,
        "authorization_id": expected_authorization_id,
        "attempt_id": expected_attempt_id,
        "split": expected_split,
        "preregistration_sha256": expected_preregistration_sha256,
        "source_sha": expected_source_sha,
        "schedule_sha256": expected_schedule_sha256,
        "corpus_sha256": expected_corpus_sha256,
        "project": expected_project,
        "model": expected_model,
        "consent_registry_sha256": expected_consent_registry_sha256,
        "retention_policy_sha256": expected_retention_policy_sha256,
        "residual_retention_acceptance_sha256": (
            expected_residual_retention_acceptance_sha256
        ),
    }
    if any(payload[field] != value for field, value in expected_bindings.items()):
        raise PrivacyCustodyError("privacy custody binding is invalid")
    for field in (
        "campaign_id",
        "authorization_id",
        "attempt_id",
        "nonce",
    ):
        if not isinstance(payload[field], str) or not SAFE_ID.fullmatch(payload[field]):
            raise PrivacyCustodyError("privacy custody identifier is invalid")
    if payload["split"] not in {"development", "holdout"}:
        raise PrivacyCustodyError("privacy custody split is invalid")
    if not SOURCE_SHA.fullmatch(str(payload["source_sha"])):
        raise PrivacyCustodyError("privacy custody source binding is invalid")
    digest_fields = {
        "preregistration_sha256",
        "schedule_sha256",
        "corpus_sha256",
        "consent_registry_sha256",
        "withdrawal_registry_sha256",
        "purpose_attestation_sha256",
        "rights_attestation_sha256",
        "provider_disclosure_sha256",
        "subject_set_sha256",
        "retention_policy_sha256",
        "residual_retention_acceptance_sha256",
    }
    if any(not SHA256.fullmatch(str(payload[field])) for field in digest_fields):
        raise PrivacyCustodyError("privacy custody digest is invalid")
    if payload["provider_retention_decision"] not in {
        "zdr_verified",
        "residual_retention_accepted",
    }:
        raise PrivacyCustodyError("provider retention decision is invalid")
    if any(
        payload[field] is not True
        for field in (
            "consent_active",
            "withdrawal_clear",
            "purpose_limited",
            "usage_rights_active",
            "provider_disclosures_current",
        )
    ):
        raise PrivacyCustodyError("privacy status is not currently authorizing")

    current = _utc_datetime(now, label="verification time")
    issued_at = _parse_datetime(payload["issued_at"], label="issued time")
    expires_at = _parse_datetime(payload["expires_at"], label="expiry time")
    deletion_deadline = _parse_datetime(
        payload["deletion_deadline"], label="deletion deadline"
    )
    if (
        issued_at > current
        or current - issued_at > MAX_RECEIPT_AGE
        or expires_at <= current
        or expires_at - issued_at > MAX_RECEIPT_LIFETIME
    ):
        raise PrivacyCustodyError("privacy custody receipt is not fresh")
    if (
        deletion_deadline <= current
        or deletion_deadline - issued_at > MAX_RETENTION
    ):
        raise PrivacyCustodyError("privacy deletion deadline exceeds the fixed retention")

    return PrivacyCustodyAuthorization(
        campaign_id=str(payload["campaign_id"]),
        authorization_id=str(payload["authorization_id"]),
        attempt_id=str(payload["attempt_id"]),
        split=str(payload["split"]),
        preregistration_sha256=str(payload["preregistration_sha256"]),
        source_sha=str(payload["source_sha"]),
        schedule_sha256=str(payload["schedule_sha256"]),
        corpus_sha256=str(payload["corpus_sha256"]),
        project=str(payload["project"]),
        model=str(payload["model"]),
        consent_registry_sha256=str(payload["consent_registry_sha256"]),
        withdrawal_registry_sha256=str(payload["withdrawal_registry_sha256"]),
        purpose_attestation_sha256=str(payload["purpose_attestation_sha256"]),
        rights_attestation_sha256=str(payload["rights_attestation_sha256"]),
        provider_disclosure_sha256=str(payload["provider_disclosure_sha256"]),
        subject_set_sha256=str(payload["subject_set_sha256"]),
        retention_policy_sha256=str(payload["retention_policy_sha256"]),
        provider_retention_decision=str(payload["provider_retention_decision"]),
        residual_retention_acceptance_sha256=str(
            payload["residual_retention_acceptance_sha256"]
        ),
        issued_at=issued_at,
        expires_at=expires_at,
        deletion_deadline=deletion_deadline,
        nonce=str(payload["nonce"]),
        signed_payload_sha256=sha256(canonical_json_bytes(payload)).hexdigest(),
    )


def _parse_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise PrivacyCustodyError(f"privacy {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PrivacyCustodyError(f"privacy {label} is invalid") from exc
    return _utc_datetime(parsed, label=label)


def _utc_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PrivacyCustodyError(f"privacy {label} is invalid")
    normalized = value.astimezone(timezone.utc)
    if value.utcoffset() != timedelta(0):
        raise PrivacyCustodyError(f"privacy {label} must use UTC")
    return normalized
