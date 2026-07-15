"""Signed external-custodian ledger contract tests for Gate 0B."""

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from app.services.qualification_identity import canonical_json_bytes
from app.services.qualification_ledger import (
    CustodyLedgerError,
    LedgerCustodyClient,
    validate_custody_ledger_snapshot,
)


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
KEY_ID = "ledger_custodian_1"
LEDGER_INSTANCE_ID = "ledger_instance_1"
CAMPAIGN_ID = "campaign_1"
AUTHORIZATION_ID = "authorization_1"
PREREGISTRATION_SHA = "a" * 64
SOURCE_SHA = "b" * 40
LEDGER_LOCATION_SHA = "c" * 64
CAMPAIGN_APPROVAL_SHA = "d" * 64
ATTEMPT_AUTHORIZATION_SHA = "e" * 64


def _key_pair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, public


def _record(
    private: Ed25519PrivateKey,
    snapshot: dict[str, object],
    *,
    event: str,
    phase_before: str,
    phase_after: str,
    attempt_id: str | None,
    body: dict[str, object],
    at: datetime,
) -> None:
    records = snapshot["records"]
    assert isinstance(records, list)
    payload = {
        "schema_id": "gate_0b_custodian_ledger_record_v1",
        "ledger_instance_id": LEDGER_INSTANCE_ID,
        "campaign_id": CAMPAIGN_ID,
        "authorization_id": AUTHORIZATION_ID,
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "ledger_location_sha256": LEDGER_LOCATION_SHA,
        "sequence": len(records) + 1,
        "previous_hash": snapshot["head_hash"],
        "event": event,
        "phase_before": phase_before,
        "phase_after": phase_after,
        "attempt_id": attempt_id,
        "at": at.isoformat().replace("+00:00", "Z"),
        "body": body,
    }
    envelope = {
        "key_id": KEY_ID,
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode(
            "ascii"
        ),
    }
    records.append(envelope)
    snapshot["head_hash"] = sha256(canonical_json_bytes(payload)).hexdigest()


def _snapshot(private: Ed25519PrivateKey) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_id": "gate_0b_custodian_ledger_export_v1",
        "ledger_instance_id": LEDGER_INSTANCE_ID,
        "ledger_key_id": KEY_ID,
        "campaign_id": CAMPAIGN_ID,
        "authorization_id": AUTHORIZATION_ID,
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "ledger_location_sha256": LEDGER_LOCATION_SHA,
        "records": [],
        "head_hash": "0" * 64,
    }
    _record(
        private,
        value,
        event="genesis",
        phase_before="preregistered",
        phase_after="preregistered",
        attempt_id=None,
        body={
            "campaign_approval_sha256": CAMPAIGN_APPROVAL_SHA,
            "max_attempts": 3,
            "max_provider_requests": 384,
            "max_cost_microusd": 30_000_000,
        },
        at=NOW,
    )
    return value


def _append_claim(
    private: Ed25519PrivateKey,
    snapshot: dict[str, object],
    *,
    attempt_id: str = "attempt_1",
    attempt_index: int = 1,
    prior_attempt_id: str | None = None,
    outage_enum: str | None = None,
    at: datetime | None = None,
) -> None:
    _record(
        private,
        snapshot,
        event="claim",
        phase_before="preregistered" if attempt_index == 1 else "development_collection",
        phase_after="development_collection",
        attempt_id=attempt_id,
        body={
            "attempt_index": attempt_index,
            "authorization_sha256": ATTEMPT_AUTHORIZATION_SHA,
            "prior_attempt_id": prior_attempt_id,
            "outage_enum": outage_enum,
            "provider_requests_reserved": 128,
            "cost_reserved_microusd": 10_000_000,
        },
        at=at or NOW + timedelta(seconds=attempt_index),
    )


def _append_development_checkpoint(
    private: Ed25519PrivateKey,
    snapshot: dict[str, object],
    *,
    attempt_id: str = "attempt_1",
) -> str:
    _record(
        private,
        snapshot,
        event="development_checkpoint",
        phase_before="development_collection",
        phase_after="development_collection",
        attempt_id=attempt_id,
        body={
            "development_capsule_sha256": "1" * 64,
            "usage_evidence_sha256": "2" * 64,
            "actual_provider_requests": 64,
            "actual_cost_microusd": 1_000_000,
        },
        at=NOW + timedelta(seconds=2),
    )
    head = snapshot["head_hash"]
    assert isinstance(head, str)
    return head


def _append_completed_lifecycle(
    private: Ed25519PrivateKey,
    snapshot: dict[str, object],
) -> None:
    development_head = _append_development_checkpoint(private, snapshot)
    _record(
        private,
        snapshot,
        event="policy_lock",
        phase_before="development_collection",
        phase_after="policy_selection_locked",
        attempt_id="attempt_1",
        body={
            "development_ledger_head_sha256": development_head,
            "development_capsule_sha256": "1" * 64,
            "selected_policy_ms": 100,
            "policy_lock_sha256": "3" * 64,
        },
        at=NOW + timedelta(seconds=3),
    )
    lock_head = snapshot["head_hash"]
    _record(
        private,
        snapshot,
        event="holdout_release",
        phase_before="policy_selection_locked",
        phase_after="holdout_collection",
        attempt_id="attempt_1",
        body={
            "policy_lock_receipt_sha256": lock_head,
            "selected_policy_ms": 100,
            "policy_lock_sha256": "3" * 64,
            "holdout_manifest_sha256": "4" * 64,
            "release_nonce": "release_nonce_1",
        },
        at=NOW + timedelta(seconds=4),
    )
    _record(
        private,
        snapshot,
        event="terminal_outcome",
        phase_before="holdout_collection",
        phase_after="completed",
        attempt_id="attempt_1",
        body={
            "outcome": "completed",
            "outage_enum": None,
            "holdout_capsule_sha256": "5" * 64,
            "usage_evidence_sha256": "6" * 64,
            "actual_provider_requests": 120,
            "actual_cost_microusd": 2_000_000,
        },
        at=NOW + timedelta(seconds=5),
    )


def test_signed_custody_ledger_replays_one_attempt_across_both_splits() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    _append_completed_lifecycle(private, snapshot)

    state = validate_custody_ledger_snapshot(
        snapshot,
        public_key=public,
        expected_key_id=KEY_ID,
        expected_ledger_instance_id=LEDGER_INSTANCE_ID,
    )

    assert state.phase == "completed"
    assert state.phase_history == (
        "preregistered",
        "development_collection",
        "policy_selection_locked",
        "holdout_collection",
        "completed",
    )
    assert state.attempt_ids == ("attempt_1",)
    assert state.active_attempt_id is None
    assert state.selected_policy_ms == 100
    assert state.development_ledger_head_sha256 is not None
    assert state.final_ledger_head_sha256 == snapshot["head_hash"]


@pytest.mark.parametrize(
    "mutation",
    ("unsigned", "wrong_key", "fork", "truncate", "duplicate_sequence", "recreated"),
)
def test_custody_ledger_rejects_tamper_replay_and_reinitialization(
    mutation: str,
) -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    _append_completed_lifecycle(private, snapshot)
    records = snapshot["records"]
    assert isinstance(records, list)

    if mutation == "unsigned":
        records[2]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    elif mutation == "wrong_key":
        records[2]["key_id"] = "other_custodian"
    elif mutation == "fork":
        records[3]["payload"]["previous_hash"] = "f" * 64
    elif mutation == "truncate":
        records.pop()
    elif mutation == "duplicate_sequence":
        records[3]["payload"]["sequence"] = records[2]["payload"]["sequence"]
    else:
        records.pop(0)
        records[0]["payload"]["sequence"] = 1
        records[0]["payload"]["previous_hash"] = "0" * 64

    with pytest.raises(CustodyLedgerError):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
        )


def test_holdout_requires_signed_development_checkpoint_and_policy_lock() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    _record(
        private,
        snapshot,
        event="holdout_release",
        phase_before="development_collection",
        phase_after="holdout_collection",
        attempt_id="attempt_1",
        body={
            "policy_lock_receipt_sha256": snapshot["head_hash"],
            "selected_policy_ms": 100,
            "policy_lock_sha256": "3" * 64,
            "holdout_manifest_sha256": "4" * 64,
            "release_nonce": "release_nonce_1",
        },
        at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(CustodyLedgerError, match="phase|policy"):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
        )


def test_post_lock_failure_is_terminal_and_cannot_be_replaced() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    development_head = _append_development_checkpoint(private, snapshot)
    _record(
        private,
        snapshot,
        event="policy_lock",
        phase_before="development_collection",
        phase_after="policy_selection_locked",
        attempt_id="attempt_1",
        body={
            "development_ledger_head_sha256": development_head,
            "development_capsule_sha256": "1" * 64,
            "selected_policy_ms": 100,
            "policy_lock_sha256": "3" * 64,
        },
        at=NOW + timedelta(seconds=3),
    )
    _record(
        private,
        snapshot,
        event="terminal_outcome",
        phase_before="policy_selection_locked",
        phase_after="aborted",
        attempt_id="attempt_1",
        body={
            "outcome": "failed",
            "outage_enum": None,
            "holdout_capsule_sha256": None,
            "usage_evidence_sha256": "6" * 64,
            "actual_provider_requests": 64,
            "actual_cost_microusd": 1_000_000,
        },
        at=NOW + timedelta(seconds=4),
    )
    _append_claim(
        private,
        snapshot,
        attempt_id="attempt_2",
        attempt_index=2,
        prior_attempt_id="attempt_1",
        outage_enum="provider_dns_outage",
        at=NOW + timedelta(seconds=5),
    )

    with pytest.raises(CustodyLedgerError, match="terminal"):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
        )


def test_executor_dependency_is_only_a_custodian_client_protocol() -> None:
    assert getattr(LedgerCustodyClient, "_is_protocol", False) is True
    assert {
        "claim_attempt",
        "record_development_checkpoint",
        "record_policy_lock",
        "release_holdout",
        "record_terminal_outcome",
        "export_snapshot",
    } <= set(LedgerCustodyClient.__dict__)
