"""Offline security-contract tests for the future Task 4.8 execution gate."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
import hashlib
from pathlib import Path
from threading import Event

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.voice_bakeoff_security_contracts import (
    ActiveSecuritySession,
    ApprovalArm,
    ApprovalCaps,
    ApprovalProvenanceSigner,
    ApprovalProvenanceVerifier,
    ControlState,
    ControlActivationSigner,
    ControlActivationVerifier,
    ControlGrantSigner,
    ControlGrantVerifier,
    CustodyLockAttestationSigner,
    CustodyLockAttestationVerifier,
    DetachedApprovalSignature,
    EvidenceKind,
    EvidenceResult,
    EvidenceRoutingContract,
    ExecutionControlStore,
    ExecutionSecuritySaga,
    InMemoryExecutionControlStore,
    InMemoryPayloadSafeRouter,
    InMemoryPreAuthTokenStore,
    InMemoryTrustGenerationPinStore,
    KmsKeyVersionRef,
    OfflineApprovalVerifier,
    PayloadSafeReceipt,
    PreAuthAcknowledgementSigner,
    PreAuthAcknowledgementVerifier,
    PreAuthActivationGrant,
    PreAuthTokenStore,
    PreAuthState,
    RevocationReason,
    SignedApproval,
    SignedCustodyLockAttestation,
    SignerRole,
    TrustSnapshot,
    TrustGenerationPin,
    TrustGenerationPinStore,
    TrustSnapshotRootSigner,
    TrustSnapshotRootVerifier,
    TrustedSignerKey,
    approval_signature_message,
)


_SOURCE = Path("app/services/voice_bakeoff_security_contracts.py")
_ROLES = (
    SignerRole.STAFF,
    SignerRole.SECURITY_PRIVACY,
    SignerRole.CONVERSATION_PRODUCT,
)
_TRUST_ROOT_PRIVATE_KEY = Ed25519PrivateKey.generate()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _caps() -> ApprovalCaps:
    return ApprovalCaps(
        requests=4,
        attempts=4,
        calls=4,
        concurrency=1,
        duration_ms=60_000,
        bytes=1_000_000,
        audio_ms=30_000,
        retries=1,
        tokens=2_000,
        cost_minor_units=100,
        artifact_ttl_ms=86_400_000,
    )


def _trust_material(
    *,
    generation: int = 1,
    snapshot_valid_from_ms: int = 1,
    snapshot_expires_at_ms: int = 1_000,
    revoked_role: SignerRole | None = None,
) -> tuple[TrustSnapshot, dict[SignerRole, Ed25519PrivateKey]]:
    private_keys = {
        role: Ed25519PrivateKey.generate()
        for role in _ROLES
    }
    signers = tuple(
        TrustedSignerKey(
            role=role,
            identity_ref=f"ref_identity_{role.value}",
            key_id=f"ref_key_{role.value}",
            public_key=private_keys[role].public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
            effective_at_ms=1,
            expires_at_ms=1_000,
            revoked_at_ms=10 if role is revoked_role else None,
        )
        for role in _ROLES
    )
    return (
        TrustSnapshotRootSigner(_TRUST_ROOT_PRIVATE_KEY).issue(TrustSnapshot(
            generation=generation,
            version_ref=f"ref_trust_generation_{generation}",
            policy_digest=_digest(f"policy:{generation}"),
            immutable_custody_ref=f"ref_custody_{generation}",
            effective_at_ms=snapshot_valid_from_ms,
            expires_at_ms=snapshot_expires_at_ms,
            signers=signers,
            no_self_approval=True,
            no_break_glass=True,
            root_signature=b"\x00" * 64,
        )),
        private_keys,
    )


def _public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _verifier_for(
    snapshot: TrustSnapshot,
    *,
    pin_store: TrustGenerationPinStore | None = None,
) -> tuple[OfflineApprovalVerifier, ApprovalProvenanceVerifier]:
    provenance_key = Ed25519PrivateKey.generate()
    root_verifier = TrustSnapshotRootVerifier(
        _public_key_bytes(_TRUST_ROOT_PRIVATE_KEY)
    )
    if pin_store is None:
        pin_store = InMemoryTrustGenerationPinStore(
            TrustGenerationPin(
                generation=snapshot.generation,
                snapshot_digest=snapshot.snapshot_digest,
                root_key_fingerprint=root_verifier.key_fingerprint,
                persistence_ref="ref_trust_generation_pin",
                cas_version_digest=_digest(
                    f"cas:{snapshot.generation}:{snapshot.snapshot_digest}"
                ),
            )
        )
    return (
        OfflineApprovalVerifier(
            provenance_signer=ApprovalProvenanceSigner(provenance_key),
            snapshot_root_verifier=root_verifier,
            generation_pin_store=pin_store,
        ),
        ApprovalProvenanceVerifier(_public_key_bytes(provenance_key)),
    )


def _signed_approval(
    snapshot: TrustSnapshot,
    private_keys: dict[SignerRole, Ed25519PrivateKey],
    *,
    nonce: str = "nonce",
    approval: str = "approval",
    binding: str = "binding",
    issued_at_ms: int = 2,
    expires_at_ms: int = 900,
) -> SignedApproval:
    unsigned = SignedApproval(
        payload_digest=_digest("payload"),
        approval_id_digest=_digest(approval),
        nonce_digest=_digest(nonce),
        binding_digest=_digest(binding),
        trust_snapshot_digest=snapshot.snapshot_digest,
        signer_set_digest=snapshot.signer_set_digest,
        environment="bakeoff",
        arm=ApprovalArm.B1,
        epoch=1,
        issued_at_ms=issued_at_ms,
        expires_at_ms=expires_at_ms,
        caps=_caps(),
        signatures=(),
    )
    message = approval_signature_message(unsigned)
    return replace(
        unsigned,
        signatures=tuple(
            DetachedApprovalSignature(
                role=signer.role,
                identity_ref=signer.identity_ref,
                key_id=signer.key_id,
                signature=private_keys[signer.role].sign(message),
            )
            for signer in snapshot.signers
        ),
    )


def _verified(
    *,
    nonce: str = "nonce",
) -> tuple[
    object,
    OfflineApprovalVerifier,
    TrustSnapshot,
    ApprovalProvenanceVerifier,
]:
    snapshot, private_keys = _trust_material()
    verifier, provenance_verifier = _verifier_for(snapshot)
    approval = _signed_approval(snapshot, private_keys, nonce=nonce)
    verified = verifier.verify(approval, snapshot, now_ms=5)
    assert verified is not None
    return verified, verifier, snapshot, provenance_verifier


def _verify_with(
    verifier: OfflineApprovalVerifier,
    snapshot: TrustSnapshot,
    private_keys: dict[SignerRole, Ed25519PrivateKey],
    *,
    nonce: str,
    approval: str,
    binding: str,
) -> object:
    verified = verifier.verify(
        _signed_approval(
            snapshot,
            private_keys,
            nonce=nonce,
            approval=approval,
            binding=binding,
        ),
        snapshot,
        now_ms=5,
    )
    assert verified is not None
    return verified


def _stores(
    *,
    provenance_verifier: ApprovalProvenanceVerifier,
    commit_authorizer=None,
    token_factory=None,
) -> tuple[
    InMemoryExecutionControlStore,
    InMemoryPreAuthTokenStore,
    ExecutionSecuritySaga,
]:
    grant_key = Ed25519PrivateKey.generate()
    acknowledgement_key = Ed25519PrivateKey.generate()
    activation_key = Ed25519PrivateKey.generate()
    control = InMemoryExecutionControlStore(
        approval_provenance_verifier=provenance_verifier,
        grant_signer=ControlGrantSigner(grant_key),
        acknowledgement_verifier=PreAuthAcknowledgementVerifier(
            _public_key_bytes(acknowledgement_key)
        ),
        activation_signer=ControlActivationSigner(activation_key),
        issuer_ref="ref_execution_control",
        preauth_audience_ref="ref_preauth_store",
        commit_authorizer=commit_authorizer,
    )
    preauth = InMemoryPreAuthTokenStore(
        grant_verifier=ControlGrantVerifier(_public_key_bytes(grant_key)),
        acknowledgement_signer=PreAuthAcknowledgementSigner(
            acknowledgement_key
        ),
        activation_verifier=ControlActivationVerifier(
            _public_key_bytes(activation_key)
        ),
        audience_ref="ref_preauth_store",
        trusted_issuer_ref="ref_execution_control",
        token_factory=token_factory,
    )
    return control, preauth, ExecutionSecuritySaga(control=control, preauth=preauth)


class _DurableControlDouble(ExecutionControlStore):
    """Nominal adapter double; it delegates but exposes no record internals."""

    def __init__(self, inner: InMemoryExecutionControlStore) -> None:
        self.inner = inner
        self.admitted_approvals: list[object] = []
        self.session_token_digests: list[str] = []

    def admit(self, approval, *, now_ms, activation_ttl_ms):
        self.admitted_approvals.append(approval)
        return self.inner.admit(
            approval,
            now_ms=now_ms,
            activation_ttl_ms=activation_ttl_ms,
        )

    def finalize(self, acknowledgement, *, now_ms):
        return self.inner.finalize(acknowledgement, now_ms=now_ms)

    def authorizes_session(
        self,
        *,
        control_ref,
        preauth_ref,
        binding_digest,
        token_digest,
        now_ms,
    ):
        self.session_token_digests.append(token_digest)
        return self.inner.authorizes_session(
            control_ref=control_ref,
            preauth_ref=preauth_ref,
            binding_digest=binding_digest,
            token_digest=token_digest,
            now_ms=now_ms,
        )

    def revoke(self, control_ref, *, reason, now_ms):
        return self.inner.revoke(control_ref, reason=reason, now_ms=now_ms)


class _DurablePreAuthDouble(PreAuthTokenStore):
    """Nominal adapter double; it receives grants/proofs but not approvals."""

    def __init__(self, inner: InMemoryPreAuthTokenStore) -> None:
        self.inner = inner
        self.activation_grants: list[object] = []
        self.activation_proofs: list[object] = []

    def activate(self, grant, *, now_ms, token_ttl_ms):
        self.activation_grants.append(grant)
        return self.inner.activate(
            grant,
            now_ms=now_ms,
            token_ttl_ms=token_ttl_ms,
        )

    def confirm_control_active(self, proof, *, now_ms):
        self.activation_proofs.append(proof)
        return self.inner.confirm_control_active(proof, now_ms=now_ms)

    def recover_activation(self, grant):
        return self.inner.recover_activation(grant)

    def consume(
        self,
        preauth_ref,
        *,
        control_ref,
        binding_digest,
        protected_token,
        now_ms,
    ):
        return self.inner.consume(
            preauth_ref,
            control_ref=control_ref,
            binding_digest=binding_digest,
            protected_token=protected_token,
            now_ms=now_ms,
        )

    def revoke(self, preauth_ref, *, reason, now_ms):
        return self.inner.revoke(preauth_ref, reason=reason, now_ms=now_ms)


def test_three_role_ed25519_verification_and_closed_trust_policy():
    snapshot, private_keys = _trust_material()
    approval = _signed_approval(snapshot, private_keys)
    verifier, _ = _verifier_for(snapshot)

    verified = verifier.verify(approval, snapshot, now_ms=5)

    assert verified is not None
    assert verified.trust_snapshot_digest == snapshot.snapshot_digest
    assert verified.signer_set_digest == snapshot.signer_set_digest
    assert verified.caps == _caps()
    assert verified.caps_digest == _caps().caps_digest

    duplicate = replace(
        snapshot.signers[1],
        identity_ref=snapshot.signers[0].identity_ref,
    )
    with pytest.raises(ValueError, match="distinct"):
        replace(snapshot, signers=(snapshot.signers[0], duplicate, snapshot.signers[2]))


def test_approval_arm_wire_values_match_the_existing_four_arm_gate():
    assert {arm.value for arm in ApprovalArm} == {"A", "B1", "B2", "C"}


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, payload_digest=_digest("tampered")),
        lambda value: replace(
            value,
            signatures=(
                replace(value.signatures[0], signature=b"x" * 64),
                *value.signatures[1:],
            ),
        ),
        lambda value: replace(
            value,
            signatures=(
                replace(value.signatures[0], key_id="ref_unknown_key"),
                *value.signatures[1:],
            ),
        ),
        lambda value: replace(
            value,
            signatures=(
                replace(value.signatures[0], role=SignerRole.SECURITY_PRIVACY),
                *value.signatures[1:],
            ),
        ),
    ),
)
def test_trust_verifier_rejects_payload_signature_key_and_role_tampering(mutation):
    snapshot, private_keys = _trust_material()
    approval = _signed_approval(snapshot, private_keys)
    verifier, _ = _verifier_for(snapshot)
    assert verifier.verify(
        mutation(approval),
        snapshot,
        now_ms=5,
    ) is None


def test_trust_windows_revocation_and_old_snapshot_replay_fail_closed():
    not_yet, keys = _trust_material(snapshot_valid_from_ms=20)
    verifier, _ = _verifier_for(not_yet)
    assert verifier.verify(
        _signed_approval(not_yet, keys),
        not_yet,
        now_ms=5,
    ) is None

    expired, keys = _trust_material(snapshot_expires_at_ms=4)
    verifier, _ = _verifier_for(expired)
    assert verifier.verify(
        _signed_approval(expired, keys),
        expired,
        now_ms=5,
    ) is None

    revoked, keys = _trust_material(revoked_role=SignerRole.STAFF)
    verifier, _ = _verifier_for(revoked)
    assert verifier.verify(
        _signed_approval(revoked, keys),
        revoked,
        now_ms=10,
    ) is None

    newer, newer_keys = _trust_material(generation=2)
    verifier, _ = _verifier_for(newer)
    assert verifier.verify(
        _signed_approval(newer, newer_keys),
        newer,
        now_ms=5,
    ) is not None
    older, older_keys = _trust_material(generation=1)
    assert verifier.verify(
        _signed_approval(older, older_keys),
        older,
        now_ms=5,
    ) is None
    restarted, _ = _verifier_for(newer)
    assert restarted.verify(
        _signed_approval(older, older_keys),
        older,
        now_ms=5,
    ) is None


def test_approval_must_be_valid_at_issuance_and_verification_time():
    snapshot, private_keys = _trust_material(
        snapshot_valid_from_ms=20,
        snapshot_expires_at_ms=100,
    )
    verifier, _ = _verifier_for(snapshot)
    backdated = _signed_approval(
        snapshot,
        private_keys,
        issued_at_ms=2,
        expires_at_ms=90,
    )
    assert verifier.verify(backdated, snapshot, now_ms=25) is None

    valid = _signed_approval(
        snapshot,
        private_keys,
        issued_at_ms=20,
        expires_at_ms=90,
    )
    assert verifier.verify(valid, snapshot, now_ms=25) is not None


def test_scheduled_signer_revocation_bounds_approval_and_later_admission():
    snapshot, private_keys = _trust_material(
        revoked_role=SignerRole.STAFF
    )
    verifier, provenance_verifier = _verifier_for(snapshot)
    long_lived = _signed_approval(
        snapshot,
        private_keys,
        issued_at_ms=2,
        expires_at_ms=900,
    )
    assert verifier.verify(long_lived, snapshot, now_ms=5) is None

    bounded = _signed_approval(
        snapshot,
        private_keys,
        issued_at_ms=2,
        expires_at_ms=10,
    )
    verified = verifier.verify(bounded, snapshot, now_ms=5)
    assert verified is not None
    before, _, _ = _stores(provenance_verifier=provenance_verifier)
    assert before.admit(
        verified,
        now_ms=9,
        activation_ttl_ms=1,
    ) is not None
    after, _, _ = _stores(provenance_verifier=provenance_verifier)
    assert after.admit(
        verified,
        now_ms=11,
        activation_ttl_ms=1,
    ) is None


def test_trust_generation_pin_advances_by_cas_and_survives_verifier_restart():
    older, _ = _trust_material(generation=1)
    pin_store = InMemoryTrustGenerationPinStore(
        TrustGenerationPin(
            generation=older.generation,
            snapshot_digest=older.snapshot_digest,
            root_key_fingerprint=TrustSnapshotRootVerifier(
                _public_key_bytes(_TRUST_ROOT_PRIVATE_KEY)
            ).key_fingerprint,
            persistence_ref="ref_trust_generation_pin",
            cas_version_digest=_digest("cas:initial"),
        )
    )
    newer, newer_keys = _trust_material(generation=2)
    verifier, _ = _verifier_for(older, pin_store=pin_store)
    assert verifier.verify(
        _signed_approval(newer, newer_keys),
        newer,
        now_ms=5,
    ) is not None
    assert pin_store.read().generation == 2

    restarted, _ = _verifier_for(older, pin_store=pin_store)
    older_keys = {
        signer.role: Ed25519PrivateKey.generate()
        for signer in older.signers
    }
    replay_signers = tuple(
        replace(
            signer,
            public_key=_public_key_bytes(older_keys[signer.role]),
        )
        for signer in older.signers
    )
    replay_snapshot = replace(older, signers=replay_signers)
    assert restarted.verify(
        _signed_approval(replay_snapshot, older_keys),
        replay_snapshot,
        now_ms=5,
    ) is None


def test_untrusted_forward_generation_cannot_advance_pin_or_gain_provenance():
    trusted, _ = _trust_material(generation=1)
    root_verifier = TrustSnapshotRootVerifier(
        _public_key_bytes(_TRUST_ROOT_PRIVATE_KEY)
    )
    pin_store = InMemoryTrustGenerationPinStore(
        TrustGenerationPin(
            generation=trusted.generation,
            snapshot_digest=trusted.snapshot_digest,
            root_key_fingerprint=root_verifier.key_fingerprint,
            persistence_ref="ref_trust_generation_pin",
            cas_version_digest=_digest("cas:trusted-initial"),
        )
    )
    verifier, provenance_verifier = _verifier_for(
        trusted,
        pin_store=pin_store,
    )
    attacker_snapshot, attacker_signer_keys = _trust_material(generation=2)
    attacker_root = Ed25519PrivateKey.generate()
    attacker_snapshot = TrustSnapshotRootSigner(attacker_root).issue(
        attacker_snapshot
    )
    attacker_verified = verifier.verify(
        _signed_approval(
            attacker_snapshot,
            attacker_signer_keys,
            nonce="attacker-nonce",
            approval="attacker-approval",
            binding="attacker-binding",
        ),
        attacker_snapshot,
        now_ms=5,
    )
    assert attacker_verified is None
    assert pin_store.read().generation == 1

    control, _, _ = _stores(provenance_verifier=provenance_verifier)
    assert control.admit(
        attacker_verified,
        now_ms=6,
        activation_ttl_ms=20,
    ) is None


def test_verifier_depends_on_pin_store_interface_not_in_memory_concrete():
    snapshot, private_keys = _trust_material()
    initial = TrustGenerationPin(
        generation=snapshot.generation,
        snapshot_digest=snapshot.snapshot_digest,
        root_key_fingerprint=TrustSnapshotRootVerifier(
            _public_key_bytes(_TRUST_ROOT_PRIVATE_KEY)
        ).key_fingerprint,
        persistence_ref="ref_durable_trust_generation_pin",
        cas_version_digest=_digest("durable-cas"),
    )

    class DurablePinStoreDouble(TrustGenerationPinStore):
        def __init__(self):
            self.inner = InMemoryTrustGenerationPinStore(initial)

        def read(self):
            return self.inner.read()

        def compare_and_swap(self, *, expected, replacement):
            return self.inner.compare_and_swap(
                expected=expected,
                replacement=replacement,
            )

    verifier, _ = _verifier_for(
        snapshot,
        pin_store=DurablePinStoreDouble(),
    )
    assert verifier.verify(
        _signed_approval(snapshot, private_keys),
        snapshot,
        now_ms=5,
    ) is not None


def test_atomic_nonce_admission_has_one_concurrent_winner():
    verified, _, _, provenance_verifier = _verified()
    control, _, _ = _stores(provenance_verifier=provenance_verifier)

    with ThreadPoolExecutor(max_workers=4) as executor:
        bundles = list(
            executor.map(
                lambda _: control.admit(
                    verified,
                    now_ms=6,
                    activation_ttl_ms=20,
                ),
                range(4),
            )
        )

    assert sum(bundle is not None for bundle in bundles) == 1
    assert control.consumed_nonce_count == 1
    assert control.record_count == 1
    winner = next(bundle for bundle in bundles if bundle is not None)
    assert control.state(winner.receipt.control_ref, now_ms=6) is ControlState.PENDING_PREAUTH
    assert not control.is_active(winner.receipt.control_ref, now_ms=6)


def test_admission_rejects_forged_verified_approval_and_caps_rebinding():
    verified, _, _, provenance_verifier = _verified()
    control, _, _ = _stores(provenance_verifier=provenance_verifier)

    forged = replace(verified, verification_proof=b"x" * 64)
    assert control.admit(forged, now_ms=6, activation_ttl_ms=20) is None

    changed_caps = replace(verified.caps, requests=3)
    rebound = replace(
        verified,
        caps=changed_caps,
        caps_digest=changed_caps.caps_digest,
    )
    assert control.admit(rebound, now_ms=6, activation_ttl_ms=20) is None
    assert control.consumed_nonce_count == 0


def test_atomic_admission_uniqueness_covers_approval_and_binding_epoch():
    snapshot, private_keys = _trust_material()
    verifier, provenance_verifier = _verifier_for(snapshot)
    first = _verify_with(
        verifier,
        snapshot,
        private_keys,
        nonce="nonce-one",
        approval="shared-approval",
        binding="binding-one",
    )
    duplicate_approval = _verify_with(
        verifier,
        snapshot,
        private_keys,
        nonce="nonce-two",
        approval="shared-approval",
        binding="binding-two",
    )
    control, _, _ = _stores(provenance_verifier=provenance_verifier)
    assert control.admit(first, now_ms=6, activation_ttl_ms=20) is not None
    assert control.admit(
        duplicate_approval,
        now_ms=6,
        activation_ttl_ms=20,
    ) is None

    binding_one = _verify_with(
        verifier,
        snapshot,
        private_keys,
        nonce="nonce-three",
        approval="approval-three",
        binding="shared-binding",
    )
    binding_two = _verify_with(
        verifier,
        snapshot,
        private_keys,
        nonce="nonce-four",
        approval="approval-four",
        binding="shared-binding",
    )
    other_control, _, _ = _stores(
        provenance_verifier=provenance_verifier
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        bundles = list(
            executor.map(
                lambda value: other_control.admit(
                    value,
                    now_ms=6,
                    activation_ttl_ms=20,
                ),
                (binding_one, binding_two),
            )
        )
    assert sum(bundle is not None for bundle in bundles) == 1


def test_transaction_failure_consumes_nothing_and_expired_or_duplicate_fails():
    verified, _, _, provenance_verifier = _verified()
    failed, _, _ = _stores(
        provenance_verifier=provenance_verifier,
        commit_authorizer=lambda: False,
    )
    assert failed.admit(verified, now_ms=6, activation_ttl_ms=20) is None
    assert failed.consumed_nonce_count == 0
    assert failed.record_count == 0

    def unavailable_commit():
        raise RuntimeError("transaction unavailable")

    failed, _, _ = _stores(
        provenance_verifier=provenance_verifier,
        commit_authorizer=unavailable_commit,
    )
    assert failed.admit(verified, now_ms=6, activation_ttl_ms=20) is None
    assert failed.consumed_nonce_count == 0
    assert failed.record_count == 0

    control, _, _ = _stores(provenance_verifier=provenance_verifier)
    assert control.admit(verified, now_ms=901, activation_ttl_ms=20) is None
    first = control.admit(verified, now_ms=6, activation_ttl_ms=20)
    assert first is not None
    assert control.admit(verified, now_ms=7, activation_ttl_ms=20) is None


def test_passive_receipt_cannot_activate_and_grant_is_authenticated_and_bound():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier
    )
    bundle = control.admit(verified, now_ms=6, activation_ttl_ms=20)
    assert bundle is not None

    assert not isinstance(bundle.receipt, PreAuthActivationGrant)
    assert preauth.activate(bundle.receipt, now_ms=7, token_ttl_ms=10) is None
    assert preauth.activate(
        replace(bundle.grant, proof=b"x" * 64),
        now_ms=7,
        token_ttl_ms=10,
    ) is None
    assert preauth.activate(
        replace(bundle.grant, audience_ref="ref_wrong_audience"),
        now_ms=7,
        token_ttl_ms=10,
    ) is None
    assert preauth.activate(
        replace(bundle.grant, issuer_ref="ref_wrong_issuer"),
        now_ms=7,
        token_ttl_ms=10,
    ) is None
    assert preauth.activate(bundle.grant, now_ms=26, token_ttl_ms=10) is None


def test_saga_requires_control_finalization_before_token_is_usable():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, saga = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "t" * 32,
    )
    bundle = control.admit(verified, now_ms=6, activation_ttl_ms=20)
    assert bundle is not None

    activation = preauth.activate(bundle.grant, now_ms=7, token_ttl_ms=10)
    assert activation is not None
    assert activation.issued_token is not None
    session = ActiveSecuritySession(
        control_ref=bundle.receipt.control_ref,
        preauth_ref=activation.acknowledgement.preauth_ref,
        binding_digest=bundle.receipt.binding_digest,
        issued_token=activation.issued_token,
    )
    assert preauth.state(session.preauth_ref, now_ms=7) is PreAuthState.PENDING_CONTROL
    assert not saga.consume(
        session,
        protected_token=activation.issued_token.protected_token,
        now_ms=8,
    )

    proof = control.finalize(activation.acknowledgement, now_ms=8)
    assert proof is not None
    assert preauth.confirm_control_active(proof, now_ms=8)
    assert control.is_active(session.control_ref, now_ms=8)
    assert preauth.state(session.preauth_ref, now_ms=8) is PreAuthState.ACTIVE
    assert saga.consume(
        session,
        protected_token=activation.issued_token.protected_token,
        now_ms=9,
    )


def test_activation_retry_never_issues_a_second_token():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "r" * 32,
    )
    bundle = control.admit(verified, now_ms=6, activation_ttl_ms=20)
    assert bundle is not None
    first = preauth.activate(bundle.grant, now_ms=7, token_ttl_ms=10)
    retry = preauth.activate(bundle.grant, now_ms=8, token_ttl_ms=10)
    assert first is not None and first.issued_token is not None
    assert retry is not None and retry.issued_token is None
    assert retry.acknowledgement == first.acknowledgement
    assert preauth.record_count == 1


def test_authenticated_ack_and_control_proof_reject_tampering_and_retry_cleanly():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "a" * 32,
    )
    bundle = control.admit(verified, now_ms=6, activation_ttl_ms=20)
    assert bundle is not None
    activation = preauth.activate(bundle.grant, now_ms=7, token_ttl_ms=10)
    assert activation is not None

    tampered_ack = replace(
        activation.acknowledgement,
        token_digest=_digest("different-token"),
    )
    assert control.finalize(tampered_ack, now_ms=8) is None
    assert control.finalize(
        replace(
            activation.acknowledgement,
            proof=bundle.grant.proof,
        ),
        now_ms=8,
    ) is None
    assert control.state(bundle.receipt.control_ref, now_ms=8) is ControlState.PENDING_PREAUTH

    first_proof = control.finalize(activation.acknowledgement, now_ms=8)
    retry_proof = control.finalize(activation.acknowledgement, now_ms=9)
    assert first_proof is not None
    assert retry_proof == first_proof
    assert not preauth.confirm_control_active(
        replace(first_proof, token_digest=_digest("different-token")),
        now_ms=9,
    )
    assert not preauth.confirm_control_active(
        replace(
            first_proof,
            proof=activation.acknowledgement.proof,
        ),
        now_ms=9,
    )
    assert preauth.confirm_control_active(first_proof, now_ms=9)
    assert preauth.confirm_control_active(first_proof, now_ms=9)


def test_concurrent_token_consumption_has_exactly_one_winner():
    verified, _, _, provenance_verifier = _verified()
    _, _, saga = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "c" * 32,
    )
    session = saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session is not None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: saga.consume(
                    session,
                    protected_token=session.issued_token.protected_token,
                    now_ms=7,
                ),
                range(4),
            )
        )
    assert sum(results) == 1


def test_saga_accepts_nominal_durable_store_doubles_without_cross_store_leaks():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "d" * 32,
    )
    durable_control = _DurableControlDouble(control)
    durable_preauth = _DurablePreAuthDouble(preauth)
    saga = ExecutionSecuritySaga(
        control=durable_control,
        preauth=durable_preauth,
    )

    session = saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session is not None
    assert len(durable_control.admitted_approvals) == 1
    assert len(durable_preauth.activation_grants) == 1
    assert not hasattr(durable_preauth.activation_grants[0], "nonce")
    assert not hasattr(durable_preauth.activation_grants[0], "signatures")
    assert not hasattr(durable_preauth.activation_grants[0], "trust_snapshot")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: saga.consume(
                    session,
                    protected_token=session.issued_token.protected_token,
                    now_ms=7,
                ),
                range(4),
            )
        )
    assert sum(results) == 1
    assert durable_control.session_token_digests == [
        session.issued_token.token_digest
    ] * 4
    assert session.issued_token.protected_token not in (
        durable_control.session_token_digests
    )

    result = saga.teardown(
        session,
        reason=RevocationReason.TEARDOWN,
        now_ms=8,
    )
    assert result.preauth_revoked and result.control_revoked


@pytest.mark.parametrize(
    ("failure_point", "raises"),
    (
        ("activate", True),
        ("finalize", False),
        ("finalize", True),
        ("confirm", False),
        ("confirm", True),
    ),
)
def test_nominal_durable_store_doubles_preserve_failure_compensation(
    failure_point,
    raises,
):
    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "g" * 32,
    )
    durable_control = _DurableControlDouble(control)
    durable_preauth = _DurablePreAuthDouble(preauth)
    saga = ExecutionSecuritySaga(
        control=durable_control,
        preauth=durable_preauth,
    )

    if raises:
        def replacement(*args, **kwargs):
            raise RuntimeError(f"{failure_point} unavailable")
    else:
        def replacement(*args, **kwargs):
            return False

    if failure_point == "activate":
        durable_preauth.activate = replacement  # type: ignore[method-assign]
    elif failure_point == "finalize":
        durable_control.finalize = replacement  # type: ignore[method-assign]
    else:
        durable_preauth.confirm_control_active = replacement  # type: ignore[method-assign]

    assert saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    ) is None
    assert control.only_record_state(now_ms=7) is ControlState.REVOKED
    if failure_point == "activate":
        assert preauth.record_count == 0
    else:
        assert preauth.only_record_state(now_ms=7) is PreAuthState.REVOKED


def test_saga_recovers_committed_preauth_activation_after_lost_reply():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "h" * 32,
    )
    durable_control = _DurableControlDouble(control)
    durable_preauth = _DurablePreAuthDouble(preauth)
    saga = ExecutionSecuritySaga(
        control=durable_control,
        preauth=durable_preauth,
    )
    original_activate = durable_preauth.activate

    def commit_then_raise(*args, **kwargs):
        assert original_activate(*args, **kwargs) is not None
        raise RuntimeError("preauth reply lost after commit")

    durable_preauth.activate = commit_then_raise  # type: ignore[method-assign]

    assert saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    ) is None
    assert control.only_record_state(now_ms=7) is ControlState.REVOKED
    assert preauth.only_record_state(now_ms=7) is PreAuthState.REVOKED


def test_shared_durable_preauth_fences_consume_against_other_saga_teardown():
    class InterleavingPreAuthDouble(_DurablePreAuthDouble):
        def __init__(self, inner):
            super().__init__(inner)
            self.consume_entered = Event()
            self.release_consume = Event()

        def consume(
            self,
            preauth_ref,
            *,
            control_ref,
            binding_digest,
            protected_token,
            now_ms,
        ):
            self.consume_entered.set()
            assert self.release_consume.wait(timeout=1)
            return super().consume(
                preauth_ref,
                control_ref=control_ref,
                binding_digest=binding_digest,
                protected_token=protected_token,
                now_ms=now_ms,
            )

    verified, _, _, provenance_verifier = _verified()
    control, preauth, _ = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "i" * 32,
    )
    durable_control = _DurableControlDouble(control)
    durable_preauth = InterleavingPreAuthDouble(preauth)
    consumer_saga = ExecutionSecuritySaga(
        control=durable_control,
        preauth=durable_preauth,
    )
    teardown_saga = ExecutionSecuritySaga(
        control=durable_control,
        preauth=durable_preauth,
    )
    session = consumer_saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        consume = executor.submit(
            consumer_saga.consume,
            session,
            protected_token=session.issued_token.protected_token,
            now_ms=7,
        )
        assert durable_preauth.consume_entered.wait(timeout=1)
        teardown = teardown_saga.teardown(
            session,
            reason=RevocationReason.TEARDOWN,
            now_ms=7,
        )
        durable_preauth.release_consume.set()
        assert not consume.result(timeout=1)

    assert teardown.preauth_revoked and teardown.control_revoked


def test_saga_rejects_non_nominal_store_types():
    _, preauth, _ = _stores(provenance_verifier=_verified()[3])
    control, _, _ = _stores(provenance_verifier=_verified()[3])

    with pytest.raises(ValueError, match="control store"):
        ExecutionSecuritySaga(control=object(), preauth=preauth)
    with pytest.raises(ValueError, match="preauth store"):
        ExecutionSecuritySaga(control=control, preauth=object())


def test_cross_session_splicing_and_single_side_revocation_fail_closed():
    snapshot, private_keys = _trust_material()
    verifier, provenance_verifier = _verifier_for(snapshot)
    approval_a = _verify_with(
        verifier,
        snapshot,
        private_keys,
        nonce="splice-nonce-a",
        approval="splice-approval-a",
        binding="splice-binding-a",
    )
    approval_b = _verify_with(
        verifier,
        snapshot,
        private_keys,
        nonce="splice-nonce-b",
        approval="splice-approval-b",
        binding="splice-binding-b",
    )
    control, _, saga = _stores(
        provenance_verifier=provenance_verifier
    )
    session_a = saga.admit_and_activate(
        approval_a,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    session_b = saga.admit_and_activate(
        approval_b,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session_a is not None and session_b is not None

    spliced = ActiveSecuritySession(
        control_ref=session_a.control_ref,
        preauth_ref=session_b.preauth_ref,
        binding_digest=session_b.binding_digest,
        issued_token=session_b.issued_token,
    )
    assert not saga.consume(
        spliced,
        protected_token=session_b.issued_token.protected_token,
        now_ms=7,
    )
    assert control.revoke(
        session_a.control_ref,
        reason=RevocationReason.TEARDOWN,
        now_ms=7,
    )
    assert not saga.consume(
        session_a,
        protected_token=session_a.issued_token.protected_token,
        now_ms=8,
    )
    assert saga.consume(
        session_b,
        protected_token=session_b.issued_token.protected_token,
        now_ms=8,
    )


def test_compensation_never_restores_nonce_and_pending_orphan_expires():
    verified, _, _, provenance_verifier = _verified()
    control, _, _ = _stores(provenance_verifier=provenance_verifier)
    bundle = control.admit(verified, now_ms=6, activation_ttl_ms=5)
    assert bundle is not None
    assert control.state(bundle.receipt.control_ref, now_ms=11) is ControlState.EXPIRED
    assert control.consumed_nonce_count == 1

    control, preauth, saga = _stores(
        provenance_verifier=provenance_verifier
    )
    preauth.activate = lambda *args, **kwargs: None  # type: ignore[method-assign]
    assert saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    ) is None
    assert control.consumed_nonce_count == 1
    assert control.only_record_state(now_ms=7) is ControlState.REVOKED


@pytest.mark.parametrize(
    ("failure_point", "raises"),
    (
        ("activate", True),
        ("finalize", False),
        ("finalize", True),
        ("confirm", False),
        ("confirm", True),
    ),
)
def test_saga_compensates_both_sides_at_each_post_activation_failure(
    failure_point,
    raises,
):
    verified, _, _, provenance_verifier = _verified()
    control, preauth, saga = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "f" * 32,
    )
    if raises:
        def unavailable(*args, **kwargs):
            raise RuntimeError(f"{failure_point} unavailable")

        replacement = unavailable
    else:
        def reject(*args, **kwargs):
            return False

        replacement = reject
    if failure_point == "activate":
        preauth.activate = replacement  # type: ignore[method-assign]
    elif failure_point == "finalize":
        control.finalize = replacement  # type: ignore[method-assign]
    else:
        preauth.confirm_control_active = replacement  # type: ignore[method-assign]

    assert saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    ) is None
    assert control.only_record_state(now_ms=7) is ControlState.REVOKED
    if failure_point == "activate":
        assert preauth.record_count == 0
    else:
        assert preauth.only_record_state(now_ms=7) is PreAuthState.REVOKED


def test_teardown_attempts_both_stores_and_either_revocation_blocks_use():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, saga = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "x" * 32,
    )
    session = saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session is not None
    result = saga.teardown(
        session,
        reason=RevocationReason.TEARDOWN,
        now_ms=7,
    )
    assert result.preauth_revoked and result.control_revoked
    assert not saga.consume(
        session,
        protected_token=session.issued_token.protected_token,
        now_ms=8,
    )
    assert saga.teardown(
        session,
        reason=RevocationReason.TEARDOWN,
        now_ms=9,
    ) == result


def test_teardown_control_revocation_is_not_skipped_when_preauth_is_unavailable():
    verified, _, _, provenance_verifier = _verified()
    control, preauth, saga = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "u" * 32,
    )
    session = saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session is not None

    def unavailable(*args, **kwargs):
        raise RuntimeError("preauth unavailable")

    preauth.revoke = unavailable  # type: ignore[method-assign]
    result = saga.teardown(
        session,
        reason=RevocationReason.TEARDOWN,
        now_ms=7,
    )
    assert not result.preauth_revoked
    assert result.control_revoked
    assert not saga.consume(
        session,
        protected_token=session.issued_token.protected_token,
        now_ms=8,
    )


def test_preauth_record_schema_excludes_nonce_signers_public_keys_and_raw_token():
    preauth_fields = {
        field.name
        for field in fields(InMemoryPreAuthTokenStore.record_type())
    }
    assert not {
        "nonce",
        "nonce_digest",
        "signers",
        "public_key",
        "protected_token",
        "approval_payload",
    } & preauth_fields

    verified, _, _, provenance_verifier = _verified()
    _, preauth, saga = _stores(
        provenance_verifier=provenance_verifier,
        token_factory=lambda: "s" * 32,
    )
    session = saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )
    assert session is not None
    assert session.issued_token.protected_token not in repr(preauth)
    assert session.issued_token.protected_token not in repr(
        session.issued_token
    )


def _safe_receipt(
    *,
    kind: EvidenceKind = EvidenceKind.CAPABILITY_FACT,
    suffix: str = "one",
) -> PayloadSafeReceipt:
    return PayloadSafeReceipt(
        schema_version=1,
        kind=kind,
        result=EvidenceResult.PASS,
        arm=ApprovalArm.B1,
        event_digest=_digest(f"event:{suffix}"),
        correlation_digest=_digest("correlation:session"),
        occurred_at_ms=10,
    )


def _custody_material() -> tuple[
    SignedCustodyLockAttestation,
    CustodyLockAttestationVerifier,
]:
    key = Ed25519PrivateKey.generate()
    unsigned = SignedCustodyLockAttestation(
        schema_version=1,
        evidence_destination_ref="ref_evidence_sink",
        residue_destination_ref="ref_residue_sink",
        kms_key_version_ref=KmsKeyVersionRef(
            project_id="hk-voice-bakeoff-0724-iso",
            location="us-central1",
            key_ring="voice-bakeoff-security",
            key_name="evidence-envelope",
            version=1,
        ),
        immutable_custody_ref="ref_locked_custody",
        retention_policy_digest=_digest("retention-policy"),
        locked_at_ms=1,
        expires_at_ms=1_000,
        proof=b"\x00" * 64,
    )
    return (
        CustodyLockAttestationSigner(key).issue(unsigned),
        CustodyLockAttestationVerifier(_public_key_bytes(key)),
    )


def _router(
    *,
    contract_changes=None,
    attestation_changes=None,
) -> tuple[InMemoryPayloadSafeRouter, SignedCustodyLockAttestation]:
    verified, _, _, provenance_verifier = _verified()
    attestation, custody_verifier = _custody_material()
    if attestation_changes:
        attestation = replace(attestation, **attestation_changes)
    values = {
        "approval_id_digest": verified.approval_id_digest,
        "binding_digest": verified.binding_digest,
        "correlation_digest": _digest("correlation:session"),
        "custody_attestation": attestation,
        "artifact_ttl_ms": 100,
        "max_receipts": 1,
    }
    values.update(contract_changes or {})
    contract = EvidenceRoutingContract(**values)
    return (
        InMemoryPayloadSafeRouter(
            contract,
            approval=verified,
            approval_provenance_verifier=provenance_verifier,
            custody_verifier=custody_verifier,
        ),
        attestation,
    )


def test_payload_safe_routing_requires_distinct_locked_bounded_destinations():
    attestation, _ = _custody_material()
    with pytest.raises(ValueError, match="distinct"):
        replace(
            attestation,
            residue_destination_ref=attestation.evidence_destination_ref,
        )

    unverified, _ = _router(
        attestation_changes={"proof": b"x" * 64}
    )
    assert unverified.route(_safe_receipt(), now_ms=10) is None

    router, attestation = _router()
    routed = router.route(_safe_receipt(), now_ms=10)
    assert routed is not None
    assert routed.kms_key_version_ref.resource_name.endswith(
        "/cryptoKeys/evidence-envelope/cryptoKeyVersions/1"
    )
    assert routed.custody_attestation_digest == attestation.attestation_digest
    assert routed.expires_at_ms == 110
    assert router.route(_safe_receipt(suffix="two"), now_ms=11) is None

    over_cap, _ = _router(contract_changes={"artifact_ttl_ms": 86_400_001})
    assert over_cap.route(_safe_receipt(), now_ms=10) is None
    wrong_binding, _ = _router(
        contract_changes={"binding_digest": _digest("wrong-binding")}
    )
    assert wrong_binding.route(_safe_receipt(), now_ms=10) is None
    wrong_correlation, _ = _router(
        contract_changes={"correlation_digest": _digest("wrong-correlation")}
    )
    assert wrong_correlation.route(_safe_receipt(), now_ms=10) is None
    stale_receipt, _ = _router()
    assert stale_receipt.route(
        replace(_safe_receipt(), occurred_at_ms=1),
        now_ms=10,
    ) is None
    expired_custody, _ = _router()
    assert expired_custody.route(_safe_receipt(), now_ms=1_000) is None


def test_payload_safe_receipt_schema_rejects_unknown_and_raw_fields():
    with pytest.raises(ValueError, match="kind"):
        replace(_safe_receipt(), kind="raw_audio")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PayloadSafeReceipt(  # type: ignore[call-arg]
            schema_version=1,
            kind=EvidenceKind.CAPABILITY_FACT,
            result=EvidenceResult.PASS,
            arm=ApprovalArm.B1,
            event_digest=_digest("event"),
            correlation_digest=_digest("correlation"),
            occurred_at_ms=10,
            transcript="forbidden",
        )
    assert {field.name for field in fields(PayloadSafeReceipt)} == {
        "schema_version",
        "kind",
        "result",
        "arm",
        "event_digest",
        "correlation_digest",
        "occurred_at_ms",
    }


def test_closed_transfer_schemas_reject_boolean_integers_and_bad_versions():
    with pytest.raises(ValueError, match="integers"):
        replace(_caps(), concurrency=True)
    with pytest.raises(ValueError, match="schema_version"):
        replace(_safe_receipt(), schema_version=True)
    with pytest.raises(ValueError, match="version"):
        KmsKeyVersionRef(
            project_id="hk-voice-bakeoff-0724-iso",
            location="us-central1",
            key_ring="voice-bakeoff-security",
            key_name="evidence-envelope",
            version=True,
        )


@pytest.mark.parametrize(
    "invalid_ref",
    (
        "ref_",
        "ref_person@example.com",
        "ref_" + ("a" * 201),
    ),
)
def test_opaque_references_are_nonempty_bounded_and_payload_safe(invalid_ref):
    attestation, _ = _custody_material()
    with pytest.raises(ValueError, match="opaque reference"):
        replace(attestation, evidence_destination_ref=invalid_ref)


def test_directional_signatures_cannot_cross_protocol_domains():
    source = _SOURCE.read_text(encoding="utf-8")
    assert "HmacActivationGrantAuthenticator" not in source
    assert "grant_authenticator" not in source
    assert "ControlGrantSigner" in source
    assert "PreAuthAcknowledgementSigner" in source
    assert "ControlActivationSigner" in source

    _, _, _, provenance_verifier = _verified()
    reused_key = Ed25519PrivateKey.generate()
    grant_signer = ControlGrantSigner(reused_key)
    with pytest.raises(AttributeError):
        grant_signer.key_fingerprint = "spoofed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="distinct"):
        InMemoryExecutionControlStore(
            approval_provenance_verifier=provenance_verifier,
            grant_signer=grant_signer,
            acknowledgement_verifier=PreAuthAcknowledgementVerifier(
                _public_key_bytes(reused_key)
            ),
            activation_signer=ControlActivationSigner(reused_key),
            issuer_ref="ref_execution_control",
            preauth_audience_ref="ref_preauth_store",
        )


def test_security_contract_module_is_offline_and_not_runtime_wired():
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported_roots <= {
        "__future__",
        "collections",
        "cryptography",
        "dataclasses",
        "enum",
        "hashlib",
        "hmac",
        "json",
        "secrets",
        "threading",
    }
    assert not {
        "app",
        "asyncio",
        "google",
        "http",
        "pathlib",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    } & imported_roots
    assert not {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"open", "exec", "eval", "compile", "__import__"}
    }
    for path in (
        Path("app/experiments/voice_bakeoff_app.py"),
        Path("app/services/voice_session_auth.py"),
        Path("scripts/run_voice_architecture_bakeoff.py"),
        Path("app/main.py"),
    ):
        assert "voice_bakeoff_security_contracts" not in path.read_text(
            encoding="utf-8"
        )
