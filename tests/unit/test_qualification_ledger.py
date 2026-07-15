"""Signed external-custodian ledger contract tests for Gate 0B."""

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import scripts.run_gemini_caller_turn_qualification as runner_module

from app.services.qualification_identity import canonical_json_bytes
from app.services.qualification_ledger import (
    CustodyLedgerError,
    LedgerCustodyClient,
    validate_custody_ledger_snapshot as _validate_custody_ledger_snapshot,
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
LEASE_ID = "lease-capability-1"
LEASE_ID_SHA = sha256(LEASE_ID.encode("ascii")).hexdigest()


def validate_custody_ledger_snapshot(
    raw: dict[str, object],
    *,
    public_key: bytes,
    expected_key_id: str,
    expected_ledger_instance_id: str,
    **identity_overrides: str,
):
    expected = {
        "expected_campaign_id": CAMPAIGN_ID,
        "expected_authorization_id": AUTHORIZATION_ID,
        "expected_preregistration_sha256": PREREGISTRATION_SHA,
        "expected_source_sha": SOURCE_SHA,
        "expected_ledger_location_sha256": LEDGER_LOCATION_SHA,
        **identity_overrides,
    }
    return _validate_custody_ledger_snapshot(
        raw,
        public_key=public_key,
        expected_key_id=expected_key_id,
        expected_ledger_instance_id=expected_ledger_instance_id,
        **expected,
    )


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
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode("ascii"),
    }
    records.append(envelope)
    snapshot["head_hash"] = sha256(canonical_json_bytes(payload)).hexdigest()


def _snapshot(
    private: Ed25519PrivateKey,
    *,
    genesis_at: datetime = NOW,
) -> dict[str, object]:
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
        at=genesis_at,
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
            "lease_id_sha256": LEASE_ID_SHA,
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
        event="holdout_execution_claim",
        phase_before="holdout_collection",
        phase_after="holdout_collection",
        attempt_id="attempt_1",
        body={
            "holdout_release_receipt_sha256": snapshot["head_hash"],
            "selected_policy_ms": 100,
            "holdout_manifest_sha256": "4" * 64,
            "provider_requests_remaining": 64,
            "cost_remaining_microusd": 9_000_000,
            "execution_nonce": "execution_nonce_1",
        },
        at=NOW + timedelta(seconds=5),
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
        at=NOW + timedelta(seconds=6),
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
    assert state.campaign_id == CAMPAIGN_ID
    assert state.authorization_id == AUTHORIZATION_ID
    assert state.preregistration_sha256 == PREREGISTRATION_SHA
    assert state.source_sha == SOURCE_SHA
    assert state.ledger_location_sha256 == LEDGER_LOCATION_SHA
    assert state.completed_attempt_id == "attempt_1"
    assert state.campaign_approval_sha256 == CAMPAIGN_APPROVAL_SHA
    assert state.attempt_authorization_sha256 == ATTEMPT_AUTHORIZATION_SHA
    assert state.attempt_claimed_at == NOW + timedelta(seconds=1)
    assert state.provider_requests_reserved == 128
    assert state.cost_reserved_microusd == 10_000_000
    assert state.selected_policy_ms == 100
    assert state.development_ledger_head_sha256 is not None
    assert state.development_usage_evidence_sha256 == "2" * 64
    assert state.final_usage_evidence_sha256 == "6" * 64
    assert state.holdout_execution_claimed is True
    assert state.actual_provider_requests == 120
    assert state.actual_cost_microusd == 2_000_000
    assert state.final_ledger_head_sha256 == snapshot["head_hash"]


@pytest.mark.parametrize(
    ("expected_field", "wrong_value"),
    (
        ("expected_campaign_id", "campaign_wrong"),
        ("expected_authorization_id", "authorization_wrong"),
        ("expected_preregistration_sha256", "0" * 64),
        ("expected_source_sha", "0" * 40),
        ("expected_ledger_location_sha256", "0" * 64),
    ),
)
def test_signed_export_must_match_every_approved_external_identity(
    expected_field: str,
    wrong_value: str,
) -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)

    with pytest.raises(CustodyLedgerError, match="approval identity"):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
            **{expected_field: wrong_value},
        )


def test_holdout_cannot_complete_without_one_shot_execution_claim() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    _append_completed_lifecycle(private, snapshot)
    records = snapshot["records"]
    assert isinstance(records, list)
    del records[5:]
    snapshot["head_hash"] = sha256(canonical_json_bytes(records[-1]["payload"])).hexdigest()
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
        at=NOW + timedelta(seconds=6),
    )

    with pytest.raises(CustodyLedgerError, match="completed outcome"):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
        )


def test_active_claim_exposes_only_the_signed_lease_digest() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)

    state = validate_custody_ledger_snapshot(
        snapshot,
        public_key=public,
        expected_key_id=KEY_ID,
        expected_ledger_instance_id=LEDGER_INSTANCE_ID,
    )

    assert state.lease_id_sha256 == LEASE_ID_SHA
    assert LEASE_ID not in repr(state)


def test_replacement_attempt_id_cannot_reuse_any_prior_attempt_id() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    _record(
        private,
        snapshot,
        event="terminal_outcome",
        phase_before="development_collection",
        phase_after="development_collection",
        attempt_id="attempt_1",
        body={
            "outcome": "infrastructure_outage",
            "outage_enum": "provider_dns_outage",
            "holdout_capsule_sha256": None,
            "usage_evidence_sha256": "6" * 64,
            "actual_provider_requests": 0,
            "actual_cost_microusd": 0,
        },
        at=NOW + timedelta(seconds=2),
    )
    _append_claim(
        private,
        snapshot,
        attempt_id="attempt_1",
        attempt_index=2,
        prior_attempt_id="attempt_1",
        outage_enum="provider_dns_outage",
        at=NOW + timedelta(seconds=3),
    )

    with pytest.raises(CustodyLedgerError, match="claim state"):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
        )


@pytest.mark.parametrize(
    ("boundary", "expected_event"),
    (
        ("claim", "claim"),
        ("checkpoint", "development_checkpoint"),
        ("holdout_resume", "holdout_execution_claim"),
        ("terminal", "terminal_outcome"),
    ),
)
def test_validly_signed_alternate_fork_cannot_satisfy_mutation_continuity(
    boundary: str,
    expected_event: str,
) -> None:
    private, public = _key_pair()
    accepted = _snapshot(private)
    fork = _snapshot(private, genesis_at=NOW + timedelta(microseconds=1))

    if boundary != "claim":
        _append_claim(private, accepted)
    _append_claim(private, fork)
    if boundary == "checkpoint":
        _append_development_checkpoint(private, fork)
    elif boundary in {"holdout_resume", "terminal"}:
        _append_completed_lifecycle(private, accepted)
        _append_completed_lifecycle(private, fork)
        accepted_records = accepted["records"]
        fork_records = fork["records"]
        assert isinstance(accepted_records, list)
        assert isinstance(fork_records, list)
        accepted_length = 6 if boundary == "terminal" else 5
        del accepted_records[accepted_length:]
        accepted["head_hash"] = sha256(
            canonical_json_bytes(accepted_records[-1]["payload"])
        ).hexdigest()
        if boundary == "holdout_resume":
            del fork_records[6:]
            fork["head_hash"] = sha256(
                canonical_json_bytes(fork_records[-1]["payload"])
            ).hexdigest()

    before = validate_custody_ledger_snapshot(
        accepted,
        public_key=public,
        expected_key_id=KEY_ID,
        expected_ledger_instance_id=LEDGER_INSTANCE_ID,
    )
    after = validate_custody_ledger_snapshot(
        fork,
        public_key=public,
        expected_key_id=KEY_ID,
        expected_ledger_instance_id=LEDGER_INSTANCE_ID,
    )

    with pytest.raises(runner_module.RunnerError, match="accepted chain"):
        runner_module._require_single_signed_append(
            before,
            after,
            expected_event=expected_event,
        )


def test_holdout_execution_claim_cannot_be_appended_twice() -> None:
    private, public = _key_pair()
    snapshot = _snapshot(private)
    _append_claim(private, snapshot)
    _append_completed_lifecycle(private, snapshot)
    records = snapshot["records"]
    assert isinstance(records, list)
    del records[6:]
    snapshot["head_hash"] = sha256(canonical_json_bytes(records[-1]["payload"])).hexdigest()
    _record(
        private,
        snapshot,
        event="holdout_execution_claim",
        phase_before="holdout_collection",
        phase_after="holdout_collection",
        attempt_id="attempt_1",
        body={
            "holdout_release_receipt_sha256": snapshot["head_hash"],
            "selected_policy_ms": 100,
            "holdout_manifest_sha256": "4" * 64,
            "provider_requests_remaining": 64,
            "cost_remaining_microusd": 9_000_000,
            "execution_nonce": "execution_nonce_2",
        },
        at=NOW + timedelta(seconds=6),
    )

    with pytest.raises(CustodyLedgerError, match="execution claim"):
        validate_custody_ledger_snapshot(
            snapshot,
            public_key=public,
            expected_key_id=KEY_ID,
            expected_ledger_instance_id=LEDGER_INSTANCE_ID,
        )
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
        "resume_holdout",
        "record_development_checkpoint",
        "record_policy_lock",
        "release_holdout",
        "record_terminal_outcome",
        "export_snapshot",
    } <= set(LedgerCustodyClient.__dict__)
