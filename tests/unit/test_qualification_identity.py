from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from app.services.qualification_identity import (
    AttemptLedger,
    IdentityError,
    canonical_json_bytes,
    capture_environment_identity,
    capture_source_identity,
    derive_ledger_campaign_state,
    ledger_location_sha256,
    verify_attempt_authorization,
    verify_campaign_approval,
)


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
PREREGISTRATION_SHA = "a" * 64
SOURCE_SHA = "b" * 40
KEY_ID = "qualification-reviewer-v1"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "qualification@example.invalid")
    _git(repo, "config", "user.name", "Qualification Test")
    (repo / "tracked.py").write_text("VALUE = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _key_pair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, public


def _signed(private: Ed25519PrivateKey, payload: dict[str, object]) -> dict[str, object]:
    return {
        "key_id": KEY_ID,
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode("ascii"),
    }


def _campaign_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_id": "gate_0b_campaign_approval_v1",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "nonce": "nonce_001",
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "max_attempts": 3,
        "max_provider_requests": 384,
        "max_cost_microusd": 30_000_000,
        "ledger_location_sha256": "c" * 64,
        "real_caller_data_authorized": False,
        "runtime_wiring_authorized": False,
        "deployment_authorized": False,
        "production_authorized": False,
        "release_authorized": False,
    }
    values.update(overrides)
    return values


def _attempt_payload(attempt_index: int = 1, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_id": "gate_0b_attempt_authorization_v1",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "attempt_id": f"attempt_{attempt_index:03d}",
        "attempt_index": attempt_index,
        "prior_attempt_id": None,
        "outage_enum": None,
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "provider_request_reservation": 128,
        "cost_reservation_microusd": 10_000_000,
    }
    values.update(overrides)
    return values


def test_clean_source_identity_binds_head_blob_and_file(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")

    identity = capture_source_identity(
        repo,
        expected_source_sha=head,
        dependency_paths=("tracked.py",),
    )

    assert identity.source_sha == head
    assert identity.clean is True
    assert identity.dependencies["tracked.py"].worktree_sha256
    assert identity.dependencies["tracked.py"].git_blob_id == _git(
        repo,
        "rev-parse",
        "HEAD:tracked.py",
    )


@pytest.mark.parametrize("dirty_kind", ["unstaged", "staged", "untracked"])
def test_source_identity_rejects_every_dirty_tree_state(tmp_path: Path, dirty_kind: str) -> None:
    repo = _git_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    if dirty_kind == "untracked":
        (repo / "shadow.py").write_text("VALUE = 2\n")
    else:
        (repo / "tracked.py").write_text("VALUE = 2\n")
        if dirty_kind == "staged":
            _git(repo, "add", "tracked.py")

    with pytest.raises(IdentityError, match="worktree is not clean"):
        capture_source_identity(
            repo,
            expected_source_sha=head,
            dependency_paths=("tracked.py",),
        )


def test_source_identity_rejects_sha_blob_and_symlink_drift(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)

    with pytest.raises(IdentityError, match="source SHA mismatch"):
        capture_source_identity(
            repo,
            expected_source_sha="0" * 40,
            dependency_paths=("tracked.py",),
        )

    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n")
    (repo / "linked.py").symlink_to(outside)
    _git(repo, "add", "linked.py")
    _git(repo, "commit", "-m", "link")
    linked_head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(IdentityError, match="symlink"):
        capture_source_identity(
            repo,
            expected_source_sha=linked_head,
            dependency_paths=("linked.py",),
        )


def test_environment_identity_binds_exact_runtime_and_imports() -> None:
    identity = capture_environment_identity(
        repo_root=Path.cwd(),
        expected_python="3.12.13",
        expected_uv="0.11.7",
        import_names=("websockets", "app.utils.audio"),
    )

    assert identity.python_version == "3.12.13"
    assert identity.uv_version == "0.11.7"
    assert identity.lock_sha256
    assert set(identity.import_sha256) == {"websockets", "app.utils.audio"}
    assert all("/" not in value for value in identity.redacted_report_dict().values() if isinstance(value, str))


def test_signed_campaign_and_attempt_are_exact_short_lived_and_non_authorizing() -> None:
    private, public = _key_pair()
    campaign = verify_campaign_approval(
        _signed(private, _campaign_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    attempt = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )

    assert campaign.max_attempts == 3
    assert attempt.attempt_index == 1
    assert campaign.production_authorized is False


def test_signature_expiry_unknown_fields_and_identity_mismatch_fail_closed() -> None:
    private, public = _key_pair()
    other_private, _ = _key_pair()

    with pytest.raises(IdentityError, match="signature"):
        verify_campaign_approval(
            _signed(other_private, _campaign_payload()),
            public_key=public,
            expected_key_id=KEY_ID,
            expected_preregistration_sha256=PREREGISTRATION_SHA,
            expected_source_sha=SOURCE_SHA,
            now=NOW,
        )

    expired = _campaign_payload(expires_at="2026-07-15T14:59:30Z")
    with pytest.raises(IdentityError, match="expired"):
        verify_campaign_approval(
            _signed(private, expired),
            public_key=public,
            expected_key_id=KEY_ID,
            expected_preregistration_sha256=PREREGISTRATION_SHA,
            expected_source_sha=SOURCE_SHA,
            now=NOW,
        )

    unknown = _campaign_payload(unexpected=True)
    with pytest.raises(IdentityError, match="unknown campaign approval field"):
        verify_campaign_approval(
            _signed(private, unknown),
            public_key=public,
            expected_key_id=KEY_ID,
            expected_preregistration_sha256=PREREGISTRATION_SHA,
            expected_source_sha=SOURCE_SHA,
            now=NOW,
        )


def test_ledger_consumes_attempt_before_work_and_blocks_concurrency(tmp_path: Path) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "ledger.json"
    campaign = verify_campaign_approval(
        _signed(
            private,
            _campaign_payload(ledger_location_sha256=ledger_location_sha256(ledger_path)),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    attempt = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )
    ledger = AttemptLedger(ledger_path)

    claim = ledger.claim_attempt(
        campaign=campaign,
        authorization=attempt,
        now=NOW,
    )

    assert claim.attempt_id == "attempt_001"
    assert claim.provider_requests_reserved == 128
    with pytest.raises(IdentityError, match="active attempt|already consumed"):
        ledger.claim_attempt(
            campaign=campaign,
            authorization=attempt,
            now=NOW + timedelta(seconds=1),
        )


def test_replacement_requires_prelock_outage_and_new_signed_attempt(tmp_path: Path) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "ledger.json"
    campaign = verify_campaign_approval(
        _signed(
            private,
            _campaign_payload(ledger_location_sha256=ledger_location_sha256(ledger_path)),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    first = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )
    second = verify_attempt_authorization(
        _signed(
            private,
            _attempt_payload(
                2,
                prior_attempt_id="attempt_001",
                outage_enum="provider_dns_outage",
            ),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )
    ledger = AttemptLedger(ledger_path)
    first_claim = ledger.claim_attempt(
        campaign=campaign,
        authorization=first,
        now=NOW,
    )
    ledger.record_outcome(
        first_claim,
        outcome="infrastructure_outage",
        outage_enum="provider_dns_outage",
        actual_provider_requests=0,
        actual_cost_microusd=0,
        now=NOW + timedelta(seconds=1),
    )

    replacement = ledger.claim_attempt(
        campaign=campaign,
        authorization=second,
        now=NOW + timedelta(seconds=2),
    )
    assert replacement.attempt_id == "attempt_002"

    other_path = tmp_path / "other-ledger.json"
    other_campaign = verify_campaign_approval(
        _signed(
            private,
            _campaign_payload(ledger_location_sha256=ledger_location_sha256(other_path)),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    other_ledger = AttemptLedger(other_path)
    other_claim = other_ledger.claim_attempt(
        campaign=other_campaign,
        authorization=first,
        now=NOW,
    )
    other_ledger.record_outcome(
        other_claim,
        outcome="completed",
        outage_enum=None,
        actual_provider_requests=0,
        actual_cost_microusd=0,
        capsule_sha256="d" * 64,
        now=NOW + timedelta(seconds=1),
    )
    other_ledger.record_policy_lock(
        campaign_id=other_campaign.campaign_id,
        selected_policy_ms=250,
        policy_lock_sha256="e" * 64,
        development_capsule_sha256="d" * 64,
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(IdentityError, match="before policy lock|holdout"):
        other_ledger.claim_attempt(
            campaign=other_campaign,
            authorization=second,
            now=NOW + timedelta(seconds=3),
        )


def test_ledger_replays_policy_lock_holdout_release_and_completion_state(tmp_path: Path) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "ledger.json"
    campaign = verify_campaign_approval(
        _signed(
            private,
            _campaign_payload(ledger_location_sha256=ledger_location_sha256(ledger_path)),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    attempt = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )
    ledger = AttemptLedger(ledger_path)
    claim = ledger.claim_attempt(campaign=campaign, authorization=attempt, now=NOW)
    ledger.record_outcome(
        claim,
        outcome="completed",
        outage_enum=None,
        actual_provider_requests=64,
        actual_cost_microusd=1_000,
        capsule_sha256="d" * 64,
        now=NOW + timedelta(seconds=1),
    )
    ledger.record_policy_lock(
        campaign_id=campaign.campaign_id,
        selected_policy_ms=250,
        policy_lock_sha256="e" * 64,
        development_capsule_sha256="d" * 64,
        now=NOW + timedelta(seconds=2),
    )
    locked = derive_ledger_campaign_state(ledger.snapshot())
    assert locked.phase.value == "policy_selection_locked"
    assert locked.holdout_materialized is False
    assert locked.selected_policy_ms == 250

    ledger.record_holdout_materialized(
        campaign_id=campaign.campaign_id,
        policy_lock_sha256="e" * 64,
        now=NOW + timedelta(seconds=3),
    )
    ledger.record_completed(
        campaign_id=campaign.campaign_id,
        holdout_capsule_sha256="f" * 64,
        now=NOW + timedelta(seconds=4),
    )

    completed = derive_ledger_campaign_state(ledger.snapshot())
    assert [phase.value for phase in completed.phase_history] == [
        "preregistered",
        "development_collection",
        "policy_selection_locked",
        "holdout_collection",
        "completed",
    ]
    assert completed.phase.value == "completed"
    assert completed.holdout_materialized is True
    assert completed.policy_lock_sha256 == "e" * 64


def test_ledger_hash_chain_detects_tampering(tmp_path: Path) -> None:
    private, public = _key_pair()
    path = tmp_path / "ledger.json"
    campaign = verify_campaign_approval(
        _signed(
            private,
            _campaign_payload(ledger_location_sha256=ledger_location_sha256(path)),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    attempt = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )
    ledger = AttemptLedger(path)
    ledger.claim_attempt(
        campaign=campaign,
        authorization=attempt,
        now=NOW,
    )
    raw = json.loads(path.read_text())
    raw["records"][1]["provider_requests_reserved"] = 1
    path.write_text(json.dumps(raw))

    with pytest.raises(IdentityError, match="ledger hash chain"):
        ledger.snapshot()


def test_campaign_approval_binds_exact_ledger_location(tmp_path: Path) -> None:
    private, public = _key_pair()
    approved_path = tmp_path / "approved.json"
    campaign = verify_campaign_approval(
        _signed(
            private,
            _campaign_payload(ledger_location_sha256=ledger_location_sha256(approved_path)),
        ),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    attempt = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )

    with pytest.raises(IdentityError, match="ledger location mismatch"):
        AttemptLedger(tmp_path / "different.json").claim_attempt(
            campaign=campaign,
            authorization=attempt,
            now=NOW,
        )
