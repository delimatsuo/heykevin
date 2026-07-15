"""Signed, fresh privacy-custody authorization tests."""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from app.services.qualification_identity import canonical_json_bytes
from app.services.qualification_privacy import PrivacyCustodyError, verify_privacy_custody


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)


def _key_pair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, public


def _payload(*, split: str = "development") -> dict[str, object]:
    return {
        "schema_id": "gate_0b_privacy_custody_authorization_v1",
        "campaign_id": "campaign_1",
        "authorization_id": "authorization_1",
        "attempt_id": "attempt_1",
        "split": split,
        "preregistration_sha256": "a" * 64,
        "source_sha": "b" * 40,
        "schedule_sha256": "c" * 64,
        "corpus_sha256": "d" * 64,
        "project": "kevin-qualification-test",
        "model": "models/gemini-3.1-flash-live-preview",
        "consent_registry_sha256": "e" * 64,
        "withdrawal_registry_sha256": "f" * 64,
        "purpose_attestation_sha256": "1" * 64,
        "rights_attestation_sha256": "2" * 64,
        "provider_disclosure_sha256": "3" * 64,
        "subject_set_sha256": "4" * 64,
        "retention_policy_sha256": "5" * 64,
        "provider_retention_decision": "zdr_verified",
        "residual_retention_acceptance_sha256": "6" * 64,
        "consent_active": True,
        "withdrawal_clear": True,
        "purpose_limited": True,
        "usage_rights_active": True,
        "provider_disclosures_current": True,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=4)).isoformat(),
        "deletion_deadline": (NOW + timedelta(days=29)).isoformat(),
        "nonce": "privacy_nonce_1",
    }


def _envelope(private: Ed25519PrivateKey, payload: dict[str, object]) -> dict[str, object]:
    return {
        "key_id": "privacy_custodian_1",
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode("ascii"),
    }


def _verify(envelope: dict[str, object], public: bytes, *, split: str = "development"):
    return verify_privacy_custody(
        envelope,
        public_key=public,
        expected_key_id="privacy_custodian_1",
        expected_campaign_id="campaign_1",
        expected_authorization_id="authorization_1",
        expected_attempt_id="attempt_1",
        expected_split=split,
        expected_preregistration_sha256="a" * 64,
        expected_source_sha="b" * 40,
        expected_schedule_sha256="c" * 64,
        expected_corpus_sha256="d" * 64,
        expected_project="kevin-qualification-test",
        expected_model="models/gemini-3.1-flash-live-preview",
        expected_consent_registry_sha256="e" * 64,
        expected_retention_policy_sha256="5" * 64,
        expected_residual_retention_acceptance_sha256="6" * 64,
        now=NOW,
    )


def test_fresh_exact_privacy_custody_authorization_is_typed() -> None:
    private, public = _key_pair()

    authorization = _verify(_envelope(private, _payload()), public)

    assert authorization.split == "development"
    assert authorization.nonce == "privacy_nonce_1"
    assert authorization.withdrawal_registry_sha256 == "f" * 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("withdrawal_clear", False, "privacy status"),
        ("schedule_sha256", "9" * 64, "privacy custody binding"),
        ("split", "holdout", "privacy custody binding"),
        ("deletion_deadline", (NOW + timedelta(days=31)).isoformat(), "deletion deadline"),
    ),
)
def test_privacy_custody_rejects_status_binding_and_retention_mutations(
    field: str,
    value: object,
    message: str,
) -> None:
    private, public = _key_pair()
    payload = _payload()
    payload[field] = value

    with pytest.raises(PrivacyCustodyError, match=message):
        _verify(_envelope(private, payload), public)


def test_privacy_custody_rejects_stale_and_alternate_fork_receipts() -> None:
    private, public = _key_pair()
    stale = _payload()
    stale["issued_at"] = (NOW - timedelta(minutes=6)).isoformat()
    stale["expires_at"] = (NOW + timedelta(minutes=1)).isoformat()

    with pytest.raises(PrivacyCustodyError, match="fresh"):
        _verify(_envelope(private, stale), public)

    envelope = _envelope(private, _payload())
    fork = deepcopy(envelope)
    fork["payload"]["nonce"] = "privacy_nonce_2"  # type: ignore[index]
    with pytest.raises(PrivacyCustodyError, match="signature"):
        _verify(fork, public)
