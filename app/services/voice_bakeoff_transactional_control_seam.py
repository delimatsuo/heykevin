"""Unwired transaction contract for future isolated bakeoff control storage.

This module is a source-only conformance seam.  It neither imports a cloud SDK
nor constructs a client, reads configuration, resolves credentials, contacts a
provider, or authorizes a Task 4.8 execution.  A future adapter must bind the
``TransactionPort`` to one dedicated nonproduction store and independently
prove its transaction, IAM, retry, and recovery semantics.

Only the execution-control store is modelled here.  The pre-auth store remains
separate, and ``ExecutionSecuritySaga`` continues to use compensation rather
than pretending the two stores have a distributed transaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
import dataclasses
import enum
import hashlib
import json
import secrets

from .voice_bakeoff_security_contracts import (
    AdmissionBundle,
    AdmissionReceipt,
    ApprovalProvenanceVerifier,
    ControlActivationProof,
    ControlActivationSigner,
    ControlGrantSigner,
    ControlState,
    ExecutionControlStore,
    PreAuthActivationAcknowledgement,
    PreAuthAcknowledgementVerifier,
    RevocationReason,
    TrustGenerationPin,
    TrustGenerationPinStore,
    VerifiedApproval,
)


_HEX = frozenset("0123456789abcdef")


def _digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _is_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith("ref_") and len(value) > 4


def _is_exact_int(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


class StoreRole(str, enum.Enum):
    """Closed roles for separately administered control stores."""

    EXECUTION_CONTROL = "execution_control"
    PREAUTH = "preauth"


class StoreNamespace(str, enum.Enum):
    """Fixed payload-safe key spaces for one transactional control store."""

    TRUST_PIN = "trust_pin"
    CONSUMED_NONCE = "consumed_nonce"
    CONSUMED_APPROVAL = "consumed_approval"
    BINDING_EPOCH = "binding_epoch"
    CONTROL_RESERVATION = "control_reservation"


@dataclasses.dataclass(frozen=True, slots=True)
class TransactionScope:
    """Opaque binding to a dedicated store, never a credential or endpoint."""

    role: StoreRole
    project_ref: str
    database_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, StoreRole):
            raise ValueError("store role must be closed")
        if not _is_ref(self.project_ref) or not _is_ref(self.database_ref):
            raise ValueError("transaction scope requires opaque references")

    @property
    def scope_digest(self) -> str:
        return _digest_json(
            {
                "database_ref": self.database_ref,
                "project_ref": self.project_ref,
                "role": self.role.value,
            }
        )

    def key(self, namespace: StoreNamespace, identifier: str) -> str:
        if not isinstance(namespace, StoreNamespace) or not isinstance(identifier, str):
            raise ValueError("transaction key inputs must be closed")
        return f"{self.scope_digest}:{namespace.value}:{identifier}"


@dataclasses.dataclass(frozen=True, slots=True)
class StoredRecord:
    """A versioned, scalar-only record returned by a transaction adapter."""

    version: int
    fields: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        if not _is_exact_int(self.version, minimum=1):
            raise ValueError("stored record version must be positive")
        if (
            not isinstance(self.fields, tuple)
            or not self.fields
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or type(item[1]) not in {str, int, bool, type(None)}
                for item in self.fields
            )
            or tuple(sorted(self.fields, key=lambda item: item[0])) != self.fields
            or len({field for field, _ in self.fields}) != len(self.fields)
        ):
            raise ValueError("stored record must be canonical scalar-only data")

    @classmethod
    def create(cls, fields: Mapping[str, object]) -> StoredRecord:
        if not isinstance(fields, Mapping):
            raise ValueError("stored fields must be a mapping")
        return cls(
            version=1,
            fields=tuple(sorted(dict(fields).items(), key=lambda item: item[0])),
        )

    def values(self) -> dict[str, object]:
        return dict(self.fields)


class TransactionStatus(str, enum.Enum):
    """The only result classes an injected transaction port may expose."""

    COMMITTED = "committed"
    ABORTED = "aborted"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True, slots=True)
class TransactionResult:
    """Payload-safe transaction completion result; non-commit states fail closed."""

    status: TransactionStatus
    value: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TransactionStatus):
            raise ValueError("transaction status must be closed")
        if self.status is not TransactionStatus.COMMITTED and self.value is not None:
            raise ValueError("non-committed transactions cannot return a value")


class TransactionAborted(RuntimeError):
    """Signal a required all-or-nothing transaction rollback to the port."""


class TransactionView(ABC):
    """Narrow serializable mutation view; no vendor client escapes this port."""

    @abstractmethod
    def read(self, key: str) -> StoredRecord | None:
        raise NotImplementedError

    @abstractmethod
    def create(self, key: str, record: StoredRecord) -> bool:
        raise NotImplementedError

    @abstractmethod
    def replace(self, key: str, *, expected_version: int, record: StoredRecord) -> bool:
        raise NotImplementedError


class TransactionPort(ABC):
    """Injectable single-store serializable transaction boundary."""

    @abstractmethod
    def transact(self, callback: Callable[[TransactionView], object]) -> TransactionResult:
        """Run one callback atomically or return a non-committed outcome.

        The port must roll back all writes if ``TransactionAborted`` escapes the
        callback.  A port must never return a value for conflict, unavailable,
        or unknown outcomes.  A port may retry a callback after a conflict, so
        callbacks must have no externally visible side effects beyond this view.
        """

        raise NotImplementedError


class DurableStoreUnavailable(RuntimeError):
    """Raised when a mandatory trust pin cannot be read from its store."""


def _committed(port: TransactionPort, callback: Callable[[TransactionView], object]) -> object | None:
    try:
        result = port.transact(callback)
    except Exception:
        return None
    if not isinstance(result, TransactionResult) or result.status is not TransactionStatus.COMMITTED:
        return None
    return result.value


def _pin_record(pin: TrustGenerationPin) -> StoredRecord:
    return StoredRecord.create(
        {
            "cas_version_digest": pin.cas_version_digest,
            "generation": pin.generation,
            "persistence_ref": pin.persistence_ref,
            "root_key_fingerprint": pin.root_key_fingerprint,
            "snapshot_digest": pin.snapshot_digest,
        }
    )


def _decode_pin(record: StoredRecord) -> TrustGenerationPin | None:
    values = record.values()
    if set(values) != {
        "cas_version_digest",
        "generation",
        "persistence_ref",
        "root_key_fingerprint",
        "snapshot_digest",
    }:
        return None
    try:
        return TrustGenerationPin(
            generation=values["generation"],
            snapshot_digest=values["snapshot_digest"],
            root_key_fingerprint=values["root_key_fingerprint"],
            persistence_ref=values["persistence_ref"],
            cas_version_digest=values["cas_version_digest"],
        )
    except ValueError:
        return None


class TransactionalTrustGenerationPinStore(TrustGenerationPinStore):
    """Reference trust-pin adapter over one injected atomic store boundary."""

    def __init__(self, *, port: TransactionPort, scope: TransactionScope) -> None:
        if not isinstance(port, TransactionPort) or not isinstance(scope, TransactionScope):
            raise ValueError("transactional pin store requires a closed port and scope")
        if scope.role is not StoreRole.EXECUTION_CONTROL:
            raise ValueError("trust pins belong only to the execution-control store")
        self._port = port
        self._scope = scope

    @property
    def scope(self) -> TransactionScope:
        return self._scope

    def _key(self) -> str:
        return self._scope.key(StoreNamespace.TRUST_PIN, "current")

    def bootstrap(self, initial_pin: TrustGenerationPin) -> bool:
        """Create the initial pin once; an existing pin is never overwritten."""
        if not isinstance(initial_pin, TrustGenerationPin):
            return False

        def operation(view: TransactionView) -> object:
            if view.read(self._key()) is not None:
                return False
            if not view.create(self._key(), _pin_record(initial_pin)):
                raise TransactionAborted("initial pin create conflict")
            return True

        return _committed(self._port, operation) is True

    def read(self) -> TrustGenerationPin:
        def operation(view: TransactionView) -> object:
            record = view.read(self._key())
            if record is None:
                return None
            return _decode_pin(record)

        pin = _committed(self._port, operation)
        if not isinstance(pin, TrustGenerationPin):
            raise DurableStoreUnavailable("trust pin was unavailable or malformed")
        return pin

    def compare_and_swap(
        self,
        *,
        expected: TrustGenerationPin,
        replacement: TrustGenerationPin,
    ) -> bool:
        if (
            not isinstance(expected, TrustGenerationPin)
            or not isinstance(replacement, TrustGenerationPin)
            or replacement.persistence_ref != expected.persistence_ref
            or replacement.root_key_fingerprint != expected.root_key_fingerprint
            or replacement.generation <= expected.generation
            or replacement.cas_version_digest == expected.cas_version_digest
        ):
            return False

        def operation(view: TransactionView) -> object:
            record = view.read(self._key())
            if record is None or _decode_pin(record) != expected:
                return False
            if not view.replace(
                self._key(),
                expected_version=record.version,
                record=_pin_record(replacement),
            ):
                raise TransactionAborted("trust pin CAS conflict")
            return True

        return _committed(self._port, operation) is True


@dataclasses.dataclass(frozen=True, slots=True)
class _ControlReservation:
    control_ref: str
    control_version: int
    state: ControlState
    binding_digest: str
    approval_id_digest: str
    nonce_digest: str
    epoch: int
    activation_deadline_ms: int
    expires_at_ms: int
    grant_id_digest: str
    receipt_digest: str
    acknowledgement_digest: str | None = None
    preauth_ref: str | None = None
    token_digest: str | None = None
    activated_at_ms: int | None = None
    revocation_reason: RevocationReason | None = None

    def __post_init__(self) -> None:
        if not _is_ref(self.control_ref) or not _is_exact_int(self.control_version, minimum=1):
            raise ValueError("control reservation reference or version is invalid")
        if not isinstance(self.state, ControlState):
            raise ValueError("control reservation state must be closed")
        if any(
            not _is_digest(value)
            for value in (
                self.binding_digest,
                self.approval_id_digest,
                self.nonce_digest,
                self.grant_id_digest,
                self.receipt_digest,
            )
        ):
            raise ValueError("control reservation digests are invalid")
        if not _is_exact_int(self.epoch, minimum=1) or not _is_exact_int(
            self.activation_deadline_ms, minimum=1
        ) or not _is_exact_int(self.expires_at_ms, minimum=1):
            raise ValueError("control reservation lifetime is invalid")
        if self.activation_deadline_ms > self.expires_at_ms:
            raise ValueError("control activation deadline exceeds expiry")
        if self.acknowledgement_digest is not None and not _is_digest(
            self.acknowledgement_digest
        ):
            raise ValueError("acknowledgement digest is invalid")
        if self.preauth_ref is not None and not _is_ref(self.preauth_ref):
            raise ValueError("preauth reference is invalid")
        if self.token_digest is not None and not _is_digest(self.token_digest):
            raise ValueError("token digest is invalid")
        if self.activated_at_ms is not None and not _is_exact_int(
            self.activated_at_ms, minimum=1
        ):
            raise ValueError("activated time is invalid")
        if self.revocation_reason is not None and not isinstance(
            self.revocation_reason, RevocationReason
        ):
            raise ValueError("revocation reason must be closed")

    def record(self) -> StoredRecord:
        return StoredRecord.create(
            {
                "acknowledgement_digest": self.acknowledgement_digest,
                "activated_at_ms": self.activated_at_ms,
                "activation_deadline_ms": self.activation_deadline_ms,
                "approval_id_digest": self.approval_id_digest,
                "binding_digest": self.binding_digest,
                "control_ref": self.control_ref,
                "control_version": self.control_version,
                "epoch": self.epoch,
                "expires_at_ms": self.expires_at_ms,
                "grant_id_digest": self.grant_id_digest,
                "nonce_digest": self.nonce_digest,
                "preauth_ref": self.preauth_ref,
                "receipt_digest": self.receipt_digest,
                "revocation_reason": (
                    self.revocation_reason.value
                    if self.revocation_reason is not None
                    else None
                ),
                "state": self.state.value,
                "token_digest": self.token_digest,
            }
        )

    @classmethod
    def from_record(cls, record: StoredRecord) -> _ControlReservation | None:
        values = record.values()
        if set(values) != {
            "acknowledgement_digest",
            "activated_at_ms",
            "activation_deadline_ms",
            "approval_id_digest",
            "binding_digest",
            "control_ref",
            "control_version",
            "epoch",
            "expires_at_ms",
            "grant_id_digest",
            "nonce_digest",
            "preauth_ref",
            "receipt_digest",
            "revocation_reason",
            "state",
            "token_digest",
        }:
            return None
        try:
            return cls(
                control_ref=values["control_ref"],
                control_version=values["control_version"],
                state=ControlState(values["state"]),
                binding_digest=values["binding_digest"],
                approval_id_digest=values["approval_id_digest"],
                nonce_digest=values["nonce_digest"],
                epoch=values["epoch"],
                activation_deadline_ms=values["activation_deadline_ms"],
                expires_at_ms=values["expires_at_ms"],
                grant_id_digest=values["grant_id_digest"],
                receipt_digest=values["receipt_digest"],
                acknowledgement_digest=values["acknowledgement_digest"],
                preauth_ref=values["preauth_ref"],
                token_digest=values["token_digest"],
                activated_at_ms=values["activated_at_ms"],
                revocation_reason=(
                    RevocationReason(values["revocation_reason"])
                    if values["revocation_reason"] is not None
                    else None
                ),
            )
        except (TypeError, ValueError):
            return None

    def expired(self, *, now_ms: int) -> _ControlReservation:
        if self.state is ControlState.PENDING_PREAUTH and (
            now_ms >= self.activation_deadline_ms or now_ms >= self.expires_at_ms
        ):
            return dataclasses.replace(
                self,
                control_version=self.control_version + 1,
                state=ControlState.EXPIRED,
            )
        if self.state is ControlState.ACTIVE and now_ms >= self.expires_at_ms:
            return dataclasses.replace(
                self,
                control_version=self.control_version + 1,
                state=ControlState.EXPIRED,
            )
        return self


def _index_record(control_ref: str) -> StoredRecord:
    if not _is_ref(control_ref):
        raise ValueError("index record requires a control reference")
    return StoredRecord.create({"control_ref": control_ref})


def _index_control_ref(record: StoredRecord) -> str | None:
    values = record.values()
    control_ref = values.get("control_ref")
    if set(values) != {"control_ref"} or not _is_ref(control_ref):
        return None
    return control_ref


class TransactionalExecutionControlStore(ExecutionControlStore):
    """Reference execution-control adapter over one injected atomic store.

    This is deliberately not a Firestore implementation.  It defines the
    single-store atomic operations a future isolated adapter must preserve.
    """

    def __init__(
        self,
        *,
        port: TransactionPort,
        scope: TransactionScope,
        approval_provenance_verifier: ApprovalProvenanceVerifier,
        grant_signer: ControlGrantSigner,
        acknowledgement_verifier: PreAuthAcknowledgementVerifier,
        activation_signer: ControlActivationSigner,
        issuer_ref: str,
        preauth_audience_ref: str,
    ) -> None:
        if not isinstance(port, TransactionPort) or not isinstance(scope, TransactionScope):
            raise ValueError("transactional control store requires a closed port and scope")
        if scope.role is not StoreRole.EXECUTION_CONTROL:
            raise ValueError("control reservations belong only to execution-control")
        if not _is_ref(issuer_ref) or not _is_ref(preauth_audience_ref):
            raise ValueError("control store references must be opaque")
        if not isinstance(approval_provenance_verifier, ApprovalProvenanceVerifier) or not isinstance(
            grant_signer, ControlGrantSigner
        ):
            raise ValueError("control approval and grant authorities are closed")
        if not isinstance(acknowledgement_verifier, PreAuthAcknowledgementVerifier) or not isinstance(
            activation_signer, ControlActivationSigner
        ):
            raise ValueError("control handoff authorities are closed")
        fingerprints = {
            grant_signer.key_fingerprint,
            acknowledgement_verifier.key_fingerprint,
            activation_signer.key_fingerprint,
        }
        if len(fingerprints) != 3:
            raise ValueError("directional message keys must be distinct")
        self._port = port
        self._scope = scope
        self._approval_provenance_verifier = approval_provenance_verifier
        self._grant_signer = grant_signer
        self._acknowledgement_verifier = acknowledgement_verifier
        self._activation_signer = activation_signer
        self._issuer_ref = issuer_ref
        self._preauth_audience_ref = preauth_audience_ref

    @property
    def scope(self) -> TransactionScope:
        return self._scope

    def _key(self, namespace: StoreNamespace, identifier: str) -> str:
        return self._scope.key(namespace, identifier)

    def _reservation_key(self, control_ref: str) -> str:
        return self._key(StoreNamespace.CONTROL_RESERVATION, control_ref)

    def _load_reservation(
        self,
        view: TransactionView,
        control_ref: str,
    ) -> tuple[StoredRecord, _ControlReservation] | None:
        record = view.read(self._reservation_key(control_ref))
        if record is None:
            return None
        reservation = _ControlReservation.from_record(record)
        if reservation is None or reservation.control_ref != control_ref:
            raise TransactionAborted("malformed control reservation")
        return record, reservation

    def _replace_reservation(
        self,
        view: TransactionView,
        record: StoredRecord,
        reservation: _ControlReservation,
    ) -> None:
        if not view.replace(
            self._reservation_key(reservation.control_ref),
            expected_version=record.version,
            record=reservation.record(),
        ):
            raise TransactionAborted("control reservation write conflict")

    @staticmethod
    def _acknowledgement_digest(
        acknowledgement: PreAuthActivationAcknowledgement,
    ) -> str:
        return _digest_json(
            {
                "proof": acknowledgement.proof.hex(),
                "value": acknowledgement.unsigned_value(),
            }
        )

    def _activation_proof(
        self,
        reservation: _ControlReservation,
        acknowledgement: PreAuthActivationAcknowledgement,
    ) -> ControlActivationProof:
        if reservation.activated_at_ms is None:
            raise ValueError("active control reservation requires activation time")
        return self._activation_signer.issue(
            schema_version=1,
            audience_ref=self._preauth_audience_ref,
            control_ref=reservation.control_ref,
            control_version=reservation.control_version,
            preauth_ref=acknowledgement.preauth_ref,
            preauth_version=acknowledgement.preauth_version,
            binding_digest=reservation.binding_digest,
            grant_id_digest=reservation.grant_id_digest,
            token_digest=acknowledgement.token_digest,
            activated_at_ms=reservation.activated_at_ms,
        )

    def _acknowledgement_matches(
        self,
        reservation: _ControlReservation,
        acknowledgement: PreAuthActivationAcknowledgement,
    ) -> bool:
        return (
            acknowledgement.audience_ref == self._issuer_ref
            and acknowledgement.control_ref == reservation.control_ref
            and acknowledgement.control_version == reservation.control_version - 1
            and acknowledgement.preauth_ref == reservation.preauth_ref
            and acknowledgement.binding_digest == reservation.binding_digest
            and acknowledgement.receipt_digest == reservation.receipt_digest
            and acknowledgement.grant_id_digest == reservation.grant_id_digest
            and acknowledgement.token_digest == reservation.token_digest
            and self._acknowledgement_digest(acknowledgement)
            == reservation.acknowledgement_digest
        )

    def admit(
        self,
        approval: VerifiedApproval,
        *,
        now_ms: int,
        activation_ttl_ms: int,
    ) -> AdmissionBundle | None:
        if (
            not isinstance(approval, VerifiedApproval)
            or not self._approval_provenance_verifier.verify(approval)
            or approval.caps_digest != approval.caps.caps_digest
            or not _is_exact_int(now_ms, minimum=1)
            or not _is_exact_int(activation_ttl_ms, minimum=1)
            or now_ms < approval.issued_at_ms
            or now_ms >= approval.expires_at_ms
        ):
            return None
        control_ref = f"ref_control_{secrets.token_hex(16)}"
        grant_id_digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        activation_deadline_ms = min(now_ms + activation_ttl_ms, approval.expires_at_ms)
        receipt = AdmissionReceipt(
            schema_version=1,
            control_ref=control_ref,
            control_version=1,
            binding_digest=approval.binding_digest,
            approval_id_digest=approval.approval_id_digest,
            nonce_digest=approval.nonce_digest,
            activation_deadline_ms=activation_deadline_ms,
            expires_at_ms=approval.expires_at_ms,
        )
        reservation = _ControlReservation(
            control_ref=control_ref,
            control_version=1,
            state=ControlState.PENDING_PREAUTH,
            binding_digest=approval.binding_digest,
            approval_id_digest=approval.approval_id_digest,
            nonce_digest=approval.nonce_digest,
            epoch=approval.epoch,
            activation_deadline_ms=activation_deadline_ms,
            expires_at_ms=approval.expires_at_ms,
            grant_id_digest=grant_id_digest,
            receipt_digest=receipt.receipt_digest,
        )
        grant = self._grant_signer.issue(
            issuer_ref=self._issuer_ref,
            audience_ref=self._preauth_audience_ref,
            control_ref=control_ref,
            control_version=reservation.control_version,
            binding_digest=reservation.binding_digest,
            receipt_digest=reservation.receipt_digest,
            grant_id_digest=reservation.grant_id_digest,
            issued_at_ms=now_ms,
            expires_at_ms=activation_deadline_ms,
        )
        bundle = AdmissionBundle(receipt=receipt, grant=grant)
        nonce_key = self._key(StoreNamespace.CONSUMED_NONCE, approval.nonce_digest)
        approval_key = self._key(
            StoreNamespace.CONSUMED_APPROVAL,
            approval.approval_id_digest,
        )
        binding_key = self._key(
            StoreNamespace.BINDING_EPOCH,
            f"{approval.binding_digest}:{approval.epoch}",
        )

        def operation(view: TransactionView) -> object:
            if view.read(nonce_key) is not None or view.read(approval_key) is not None:
                return None
            prior_binding = view.read(binding_key)
            if prior_binding is not None:
                prior_ref = _index_control_ref(prior_binding)
                if prior_ref is None:
                    raise TransactionAborted("malformed binding index")
                loaded = self._load_reservation(view, prior_ref)
                if loaded is None:
                    raise TransactionAborted("binding index points to no control reservation")
                prior_record, prior = loaded
                if (
                    prior.binding_digest != approval.binding_digest
                    or prior.epoch != approval.epoch
                ):
                    raise TransactionAborted("binding index scope mismatch")
                terminal = prior.expired(now_ms=now_ms)
                if terminal.state in {ControlState.PENDING_PREAUTH, ControlState.ACTIVE}:
                    return None
                if terminal != prior:
                    self._replace_reservation(view, prior_record, terminal)
                if not view.replace(
                    binding_key,
                    expected_version=prior_binding.version,
                    record=_index_record(control_ref),
                ):
                    raise TransactionAborted("binding epoch replacement conflict")
            elif not view.create(binding_key, _index_record(control_ref)):
                raise TransactionAborted("binding epoch create conflict")
            if not view.create(nonce_key, _index_record(control_ref)):
                raise TransactionAborted("nonce create conflict")
            if not view.create(approval_key, _index_record(control_ref)):
                raise TransactionAborted("approval create conflict")
            if not view.create(self._reservation_key(control_ref), reservation.record()):
                raise TransactionAborted("control reservation create conflict")
            return bundle

        result = _committed(self._port, operation)
        return result if isinstance(result, AdmissionBundle) else None

    def finalize(
        self,
        acknowledgement: PreAuthActivationAcknowledgement,
        *,
        now_ms: int,
    ) -> ControlActivationProof | None:
        if not isinstance(acknowledgement, PreAuthActivationAcknowledgement) or not _is_exact_int(
            now_ms, minimum=1
        ) or not self._acknowledgement_verifier.verify(acknowledgement):
            return None

        def operation(view: TransactionView) -> object:
            loaded = self._load_reservation(view, acknowledgement.control_ref)
            if loaded is None:
                return None
            record, prior = loaded
            reservation = prior.expired(now_ms=now_ms)
            if reservation != prior:
                self._replace_reservation(view, record, reservation)
            if reservation.state is ControlState.ACTIVE:
                return (
                    self._activation_proof(reservation, acknowledgement)
                    if self._acknowledgement_matches(reservation, acknowledgement)
                    else None
                )
            if (
                reservation.state is not ControlState.PENDING_PREAUTH
                or acknowledgement.audience_ref != self._issuer_ref
                or acknowledgement.control_version != reservation.control_version
                or acknowledgement.binding_digest != reservation.binding_digest
                or acknowledgement.receipt_digest != reservation.receipt_digest
                or acknowledgement.grant_id_digest != reservation.grant_id_digest
                or acknowledgement.acknowledged_at_ms > now_ms
                or acknowledgement.acknowledged_at_ms >= reservation.activation_deadline_ms
            ):
                return None
            active = dataclasses.replace(
                reservation,
                acknowledgement_digest=self._acknowledgement_digest(acknowledgement),
                activated_at_ms=now_ms,
                control_version=reservation.control_version + 1,
                preauth_ref=acknowledgement.preauth_ref,
                state=ControlState.ACTIVE,
                token_digest=acknowledgement.token_digest,
            )
            self._replace_reservation(view, record, active)
            return self._activation_proof(active, acknowledgement)

        result = _committed(self._port, operation)
        return result if isinstance(result, ControlActivationProof) else None

    def authorizes_session(
        self,
        *,
        control_ref: str,
        preauth_ref: str,
        binding_digest: str,
        token_digest: str,
        now_ms: int,
    ) -> bool:
        if not all(
            (
                _is_ref(control_ref),
                _is_ref(preauth_ref),
                _is_digest(binding_digest),
                _is_digest(token_digest),
                _is_exact_int(now_ms, minimum=1),
            )
        ):
            return False

        def operation(view: TransactionView) -> object:
            loaded = self._load_reservation(view, control_ref)
            if loaded is None:
                return False
            record, prior = loaded
            reservation = prior.expired(now_ms=now_ms)
            if reservation != prior:
                self._replace_reservation(view, record, reservation)
            return (
                reservation.state is ControlState.ACTIVE
                and reservation.preauth_ref == preauth_ref
                and reservation.binding_digest == binding_digest
                and reservation.token_digest == token_digest
            )

        return _committed(self._port, operation) is True

    def revoke(
        self,
        control_ref: str,
        *,
        reason: RevocationReason,
        now_ms: int,
    ) -> bool:
        if not _is_ref(control_ref) or not isinstance(reason, RevocationReason) or not _is_exact_int(
            now_ms, minimum=1
        ):
            return False

        def operation(view: TransactionView) -> object:
            loaded = self._load_reservation(view, control_ref)
            if loaded is None:
                return False
            record, prior = loaded
            reservation = prior.expired(now_ms=now_ms)
            if reservation.state is ControlState.REVOKED:
                return True
            revoked = dataclasses.replace(
                reservation,
                control_version=reservation.control_version + 1,
                revocation_reason=reason,
                state=ControlState.REVOKED,
            )
            self._replace_reservation(view, record, revoked)
            return True

        return _committed(self._port, operation) is True
