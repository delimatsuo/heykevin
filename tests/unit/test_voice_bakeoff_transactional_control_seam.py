"""Acceptance tests for the non-authorizing transactional control-store seam."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
from threading import RLock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.voice_bakeoff_security_contracts import (
    ApprovalArm,
    ApprovalCaps,
    ApprovalProvenanceSigner,
    ApprovalProvenanceVerifier,
    ControlActivationSigner,
    ControlActivationVerifier,
    ControlGrantSigner,
    ControlGrantVerifier,
    DetachedApprovalSignature,
    ExecutionSecuritySaga,
    InMemoryPreAuthTokenStore,
    OfflineApprovalVerifier,
    PreAuthAcknowledgementSigner,
    PreAuthAcknowledgementVerifier,
    RevocationReason,
    SignedApproval,
    SignerRole,
    TechnicalReviewReceipt,
    TrustGenerationPin,
    TrustSnapshot,
    TrustSnapshotRootSigner,
    TrustSnapshotRootVerifier,
    TrustedSignerKey,
    approval_signature_message,
)
from app.services.voice_bakeoff_transactional_control_seam import (
    StoreRole,
    StoreNamespace,
    StoredRecord,
    TransactionAborted,
    TransactionPort,
    TransactionResult,
    TransactionScope,
    TransactionStatus,
    TransactionView,
    TransactionalExecutionControlStore,
    TransactionalTrustGenerationPinStore,
)


_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "app/services/voice_bakeoff_transactional_control_seam.py"
_TRUST_ROOT_PRIVATE_KEY = Ed25519PrivateKey.generate()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


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


def _trust_material() -> tuple[TrustSnapshot, Ed25519PrivateKey]:
    owner_key = Ed25519PrivateKey.generate()
    snapshot = TrustSnapshot(
        generation=1,
        version_ref="ref_trust_generation_1",
        policy_digest=_digest("policy"),
        immutable_custody_ref="ref_custody",
        effective_at_ms=1,
        expires_at_ms=1_000,
        signers=(
            TrustedSignerKey(
                role=SignerRole.OWNER,
                identity_ref="ref_owner",
                key_id="ref_owner_key",
                public_key=_public_key_bytes(owner_key),
                effective_at_ms=1,
                expires_at_ms=1_000,
            ),
        ),
        sole_owner_authorization=True,
        no_break_glass=True,
        root_signature=b"\x00" * 64,
    )
    return TrustSnapshotRootSigner(_TRUST_ROOT_PRIVATE_KEY).issue(snapshot), owner_key


def _signed_approval(
    snapshot: TrustSnapshot,
    owner_key: Ed25519PrivateKey,
    *,
    nonce: str = "nonce",
    approval: str = "approval",
    binding: str = "binding",
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
        issued_at_ms=2,
        expires_at_ms=900,
        caps=_caps(),
        technical_review=TechnicalReviewReceipt(
            review_digest=_digest("review"),
            provenance_ref="ref_review",
            reviewed_payload_digest=_digest("payload"),
            reviewed_binding_digest=_digest(binding),
            unresolved_p1_count=0,
            advisory_only=True,
        ),
        signatures=(),
    )
    return replace(
        unsigned,
        signatures=(
            DetachedApprovalSignature(
                role=SignerRole.OWNER,
                identity_ref="ref_owner",
                key_id="ref_owner_key",
                signature=owner_key.sign(approval_signature_message(unsigned)),
            ),
        ),
    )


class _FakeTransactionView(TransactionView):
    def __init__(
        self,
        records: dict[str, StoredRecord],
        *,
        abort_after_mutation: int | None,
    ) -> None:
        self.records = records
        self._abort_after_mutation = abort_after_mutation
        self._mutation_count = 0

    def _mutated(self) -> None:
        self._mutation_count += 1
        if (
            self._abort_after_mutation is not None
            and self._mutation_count >= self._abort_after_mutation
        ):
            raise TransactionAborted("injected late transaction failure")

    def read(self, key: str) -> StoredRecord | None:
        return self.records.get(key)

    def create(self, key: str, record: StoredRecord) -> bool:
        if key in self.records:
            return False
        self.records[key] = record
        self._mutated()
        return True

    def replace(self, key: str, *, expected_version: int, record: StoredRecord) -> bool:
        current = self.records.get(key)
        if current is None or current.version != expected_version:
            return False
        self.records[key] = StoredRecord(version=current.version + 1, fields=record.fields)
        self._mutated()
        return True


class _FakeTransactionPort(TransactionPort):
    """Shared serializable fake; it never contacts a datastore."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, StoredRecord] = {}
        self._next_status: TransactionStatus | None = None
        self._unknown_after_commit = False
        self._abort_after_mutation: int | None = None

    def fail_next(self, status: TransactionStatus) -> None:
        if status is TransactionStatus.COMMITTED:
            raise ValueError("failure status must not commit")
        self._next_status = status

    def unknown_after_next_commit(self) -> None:
        self._unknown_after_commit = True

    def abort_after_next_mutation(self, mutation_count: int) -> None:
        if type(mutation_count) is not int or mutation_count < 1:
            raise ValueError("abort mutation count must be positive")
        self._abort_after_mutation = mutation_count

    def inject(self, key: str, record: StoredRecord) -> None:
        self._records[key] = record

    def transact(self, callback) -> TransactionResult:
        with self._lock:
            if self._next_status is not None:
                status = self._next_status
                self._next_status = None
                return TransactionResult(status=status)
            abort_after_mutation = self._abort_after_mutation
            self._abort_after_mutation = None
            view = _FakeTransactionView(
                dict(self._records),
                abort_after_mutation=abort_after_mutation,
            )
            try:
                value = callback(view)
            except TransactionAborted:
                return TransactionResult(status=TransactionStatus.ABORTED)
            self._records = view.records
            if self._unknown_after_commit:
                self._unknown_after_commit = False
                return TransactionResult(status=TransactionStatus.UNKNOWN)
            return TransactionResult(status=TransactionStatus.COMMITTED, value=value)

    def payloads(self) -> list[dict[str, object]]:
        with self._lock:
            return [record.values() for record in self._records.values()]


def _scope() -> TransactionScope:
    return TransactionScope(
        role=StoreRole.EXECUTION_CONTROL,
        project_ref="ref_control_project",
        database_ref="ref_control_database",
    )


def _pin(snapshot: TrustSnapshot) -> TrustGenerationPin:
    return TrustGenerationPin(
        generation=snapshot.generation,
        snapshot_digest=snapshot.snapshot_digest,
        root_key_fingerprint=TrustSnapshotRootVerifier(
            _public_key_bytes(_TRUST_ROOT_PRIVATE_KEY)
        ).key_fingerprint,
        persistence_ref="ref_trust_generation_pin",
        cas_version_digest=_digest(f"cas:{snapshot.generation}"),
    )


def _verification_fixture(port: _FakeTransactionPort):
    snapshot, owner_key = _trust_material()
    pins = TransactionalTrustGenerationPinStore(port=port, scope=_scope())
    assert pins.bootstrap(_pin(snapshot))
    provenance_key = Ed25519PrivateKey.generate()
    verifier = OfflineApprovalVerifier(
        provenance_signer=ApprovalProvenanceSigner(provenance_key),
        snapshot_root_verifier=TrustSnapshotRootVerifier(
            _public_key_bytes(_TRUST_ROOT_PRIVATE_KEY)
        ),
        generation_pin_store=pins,
    )
    return snapshot, owner_key, verifier, ApprovalProvenanceVerifier(
        _public_key_bytes(provenance_key)
    )


def _verified_approval(
    fixture,
    *,
    nonce: str = "nonce",
    approval: str = "approval",
    binding: str = "binding",
):
    snapshot, owner_key, verifier, provenance_verifier = fixture
    verified = verifier.verify(
        _signed_approval(
            snapshot,
            owner_key,
            nonce=nonce,
            approval=approval,
            binding=binding,
        ),
        snapshot,
        now_ms=5,
    )
    assert verified is not None
    return verified, provenance_verifier


def _control(
    *,
    port: _FakeTransactionPort,
    provenance_verifier: ApprovalProvenanceVerifier,
) -> tuple[TransactionalExecutionControlStore, InMemoryPreAuthTokenStore]:
    grant_key = Ed25519PrivateKey.generate()
    acknowledgement_key = Ed25519PrivateKey.generate()
    activation_key = Ed25519PrivateKey.generate()
    control = TransactionalExecutionControlStore(
        port=port,
        scope=_scope(),
        approval_provenance_verifier=provenance_verifier,
        grant_signer=ControlGrantSigner(grant_key),
        acknowledgement_verifier=PreAuthAcknowledgementVerifier(
            _public_key_bytes(acknowledgement_key)
        ),
        activation_signer=ControlActivationSigner(activation_key),
        issuer_ref="ref_execution_control",
        preauth_audience_ref="ref_preauth_store",
    )
    preauth = InMemoryPreAuthTokenStore(
        grant_verifier=ControlGrantVerifier(_public_key_bytes(grant_key)),
        acknowledgement_signer=PreAuthAcknowledgementSigner(acknowledgement_key),
        activation_verifier=ControlActivationVerifier(_public_key_bytes(activation_key)),
        audience_ref="ref_preauth_store",
        trusted_issuer_ref="ref_execution_control",
        token_factory=lambda: "a" * 32,
    )
    return control, preauth


def test_shared_transaction_port_allows_only_one_nonce_reservation() -> None:
    port = _FakeTransactionPort()
    verified, provenance_verifier = _verified_approval(_verification_fixture(port))
    first, _ = _control(port=port, provenance_verifier=provenance_verifier)
    second, _ = _control(port=port, provenance_verifier=provenance_verifier)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda store: store.admit(verified, now_ms=6, activation_ttl_ms=20),
                (first, second),
            )
        )

    assert sum(result is not None for result in results) == 1
    payloads = port.payloads()
    assert len(payloads) == 5
    assert not {
        key
        for payload in payloads
        for key in payload
        if key in {"protected_token", "signature", "proof", "transcript", "caller"}
    }


@pytest.mark.parametrize(
    ("first_kwargs", "second_kwargs"),
    (
        (
            {"nonce": "nonce-1", "approval": "approval", "binding": "binding-1"},
            {"nonce": "nonce-2", "approval": "approval", "binding": "binding-2"},
        ),
        ({"nonce": "nonce-1", "approval": "approval-1"}, {"nonce": "nonce-2", "approval": "approval-2"}),
    ),
)
def test_approval_and_binding_epoch_indexes_are_independently_one_use(
    first_kwargs,
    second_kwargs,
) -> None:
    port = _FakeTransactionPort()
    fixture = _verification_fixture(port)
    first, provenance_verifier = _verified_approval(fixture, **first_kwargs)
    second, _ = _verified_approval(fixture, **second_kwargs)
    control, _ = _control(port=port, provenance_verifier=provenance_verifier)

    assert control.admit(first, now_ms=6, activation_ttl_ms=20) is not None
    assert control.admit(second, now_ms=6, activation_ttl_ms=20) is None
    assert len(port.payloads()) == 5


def test_transaction_unavailable_or_unknown_commit_fails_closed_without_retry() -> None:
    unavailable_port = _FakeTransactionPort()
    verified, provenance_verifier = _verified_approval(
        _verification_fixture(unavailable_port)
    )
    control, _ = _control(port=unavailable_port, provenance_verifier=provenance_verifier)
    unavailable_port.fail_next(TransactionStatus.UNAVAILABLE)
    before = unavailable_port.payloads()

    assert control.admit(verified, now_ms=6, activation_ttl_ms=20) is None
    assert unavailable_port.payloads() == before

    unknown_port = _FakeTransactionPort()
    unknown, unknown_provenance = _verified_approval(
        _verification_fixture(unknown_port)
    )
    unknown_control, _ = _control(port=unknown_port, provenance_verifier=unknown_provenance)
    unknown_port.unknown_after_next_commit()

    assert unknown_control.admit(unknown, now_ms=6, activation_ttl_ms=20) is None
    assert len(unknown_port.payloads()) == 5
    assert unknown_control.admit(unknown, now_ms=6, activation_ttl_ms=20) is None


def test_late_transaction_abort_rolls_back_every_pending_reservation_index() -> None:
    port = _FakeTransactionPort()
    verified, provenance_verifier = _verified_approval(_verification_fixture(port))
    control, _ = _control(port=port, provenance_verifier=provenance_verifier)
    before = port.payloads()
    port.abort_after_next_mutation(2)

    assert control.admit(verified, now_ms=6, activation_ttl_ms=20) is None
    assert port.payloads() == before


def test_misrouted_terminal_binding_index_fails_closed_without_partial_rebind() -> None:
    port = _FakeTransactionPort()
    fixture = _verification_fixture(port)
    prior, provenance_verifier = _verified_approval(
        fixture,
        nonce="old-nonce",
        approval="old-approval",
        binding="old-binding",
    )
    requested, _ = _verified_approval(
        fixture,
        nonce="new-nonce",
        approval="new-approval",
        binding="new-binding",
    )
    control, _ = _control(port=port, provenance_verifier=provenance_verifier)
    old_bundle = control.admit(prior, now_ms=6, activation_ttl_ms=1)
    assert old_bundle is not None
    assert control.revoke(
        old_bundle.receipt.control_ref,
        reason=RevocationReason.TEARDOWN,
        now_ms=7,
    )
    binding_key = _scope().key(
        StoreNamespace.BINDING_EPOCH,
        f"{requested.binding_digest}:{requested.epoch}",
    )
    port.inject(binding_key, StoredRecord.create({"control_ref": old_bundle.receipt.control_ref}))
    before = port.payloads()

    assert control.admit(requested, now_ms=8, activation_ttl_ms=20) is None
    assert port.payloads() == before


def test_transactional_control_works_with_existing_separate_preauth_saga_and_revokes() -> None:
    port = _FakeTransactionPort()
    verified, provenance_verifier = _verified_approval(_verification_fixture(port))
    control, preauth = _control(port=port, provenance_verifier=provenance_verifier)
    saga = ExecutionSecuritySaga(control=control, preauth=preauth)

    session = saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    )

    assert session is not None
    assert control.authorizes_session(
        control_ref=session.control_ref,
        preauth_ref=session.preauth_ref,
        binding_digest=session.binding_digest,
        token_digest=session.issued_token.token_digest,
        now_ms=7,
    )
    assert control.revoke(
        session.control_ref,
        reason=RevocationReason.TEARDOWN,
        now_ms=8,
    )
    assert control.revoke(
        session.control_ref,
        reason=RevocationReason.TEARDOWN,
        now_ms=8,
    )
    assert not control.authorizes_session(
        control_ref=session.control_ref,
        preauth_ref=session.preauth_ref,
        binding_digest=session.binding_digest,
        token_digest=session.issued_token.token_digest,
        now_ms=8,
    )


def test_transactional_control_preserves_existing_saga_compensation() -> None:
    port = _FakeTransactionPort()
    verified, provenance_verifier = _verified_approval(_verification_fixture(port))
    control, preauth = _control(port=port, provenance_verifier=provenance_verifier)
    saga = ExecutionSecuritySaga(control=control, preauth=preauth)

    preauth.activate = lambda *args, **kwargs: None  # type: ignore[method-assign]

    assert saga.admit_and_activate(
        verified,
        now_ms=6,
        activation_ttl_ms=20,
        token_ttl_ms=10,
    ) is None
    reservations = [payload for payload in port.payloads() if "state" in payload]
    assert len(reservations) == 1
    assert reservations[0]["state"] == "revoked"
    assert reservations[0]["revocation_reason"] == "activation_failed"


def test_trust_pin_cas_is_shared_across_adapter_instances_and_rejects_root_swap() -> None:
    port = _FakeTransactionPort()
    first = TransactionalTrustGenerationPinStore(port=port, scope=_scope())
    second = TransactionalTrustGenerationPinStore(port=port, scope=_scope())
    snapshot, _ = _trust_material()
    initial = _pin(snapshot)
    assert first.bootstrap(initial)
    replacement = replace(
        initial,
        generation=2,
        snapshot_digest=_digest("new-snapshot"),
        cas_version_digest=_digest("cas:2"),
    )
    assert not first.compare_and_swap(
        expected=initial,
        replacement=replace(
            replacement,
            cas_version_digest=initial.cas_version_digest,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda store: store.compare_and_swap(expected=initial, replacement=replacement),
                (first, second),
            )
        )

    assert results.count(True) == 1
    assert second.read() == replacement
    assert not second.compare_and_swap(
        expected=replacement,
        replacement=replace(
            replacement,
            generation=3,
            root_key_fingerprint=_digest("forged-root"),
            cas_version_digest=_digest("cas:3"),
        ),
    )
    assert not first.compare_and_swap(expected=initial, replacement=replacement)


def test_module_is_offline_unwired_and_has_no_vendor_or_runtime_escape_hatch() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    imports = {
        (node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    } | {
        ("", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports == {
        ("__future__", "annotations"),
        ("abc", "ABC"),
        ("abc", "abstractmethod"),
        ("collections.abc", "Callable"),
        ("collections.abc", "Mapping"),
        ("", "dataclasses"),
        ("", "enum"),
        ("", "hashlib"),
        ("", "json"),
        ("", "secrets"),
        ("voice_bakeoff_security_contracts", "AdmissionBundle"),
        ("voice_bakeoff_security_contracts", "AdmissionReceipt"),
        ("voice_bakeoff_security_contracts", "ApprovalProvenanceVerifier"),
        ("voice_bakeoff_security_contracts", "ControlActivationProof"),
        ("voice_bakeoff_security_contracts", "ControlActivationSigner"),
        ("voice_bakeoff_security_contracts", "ControlGrantSigner"),
        ("voice_bakeoff_security_contracts", "ControlState"),
        ("voice_bakeoff_security_contracts", "ExecutionControlStore"),
        ("voice_bakeoff_security_contracts", "PreAuthActivationAcknowledgement"),
        ("voice_bakeoff_security_contracts", "PreAuthAcknowledgementVerifier"),
        ("voice_bakeoff_security_contracts", "RevocationReason"),
        ("voice_bakeoff_security_contracts", "TrustGenerationPin"),
        ("voice_bakeoff_security_contracts", "TrustGenerationPinStore"),
        ("voice_bakeoff_security_contracts", "VerifiedApproval"),
    }
    forbidden_calls = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "getenv",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "run",
        "Popen",
    }
    assert not {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } & forbidden_calls
    for path in (
        _ROOT / "app/main.py",
        _ROOT / "app/experiments/voice_bakeoff_app.py",
        _ROOT / "app/services/voice_pipeline.py",
        _ROOT / "app/webhooks/media_stream.py",
        _ROOT / "app/config.py",
        _ROOT / "scripts/run_voice_architecture_bakeoff.py",
    ):
        assert "voice_bakeoff_transactional_control_seam" not in path.read_text(
            encoding="utf-8"
        )
