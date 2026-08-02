"""Offline-only security contracts for a future voice bakeoff execution gate.

This module deliberately contains no runtime, provider, cloud, filesystem, or
network integration.  The in-memory stores model the security invariants that
separate execution-control metadata from short-lived pre-authorization tokens.
"""

import dataclasses
import enum
import hashlib
import hmac
import json
import secrets
import threading

import cryptography.exceptions
import cryptography.hazmat.primitives.asymmetric.ed25519


_APPROVAL_DOMAIN = b"hey-kevin/voice-bakeoff/approval/v1\x00"
_APPROVAL_PROVENANCE_DOMAIN = (
    b"hey-kevin/voice-bakeoff/approval-provenance/v1\x00"
)
_TRUST_SNAPSHOT_ROOT_DOMAIN = (
    b"hey-kevin/voice-bakeoff/trust-snapshot-root/v1\x00"
)
_GRANT_DOMAIN = b"hey-kevin/voice-bakeoff/preauth-grant/v1\x00"
_ACK_DOMAIN = b"hey-kevin/voice-bakeoff/preauth-ack/v1\x00"
_CONTROL_PROOF_DOMAIN = b"hey-kevin/voice-bakeoff/control-proof/v1\x00"
_CUSTODY_ATTESTATION_DOMAIN = (
    b"hey-kevin/voice-bakeoff/custody-lock-attestation/v1\x00"
)
_HEX_DIGEST_LENGTH = 64
_MAX_OPAQUE_REF_LENGTH = 200
_OPAQUE_REF_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._:-"
)


class SignerRole(str, enum.Enum):
    """The sole owner role allowed to authorize a connected bakeoff."""

    OWNER = "owner"


class ApprovalArm(str, enum.Enum):
    """Closed set of bakeoff candidate arms."""

    A = "A"
    B1 = "B1"
    B2 = "B2"
    C = "C"


class ControlState(str, enum.Enum):
    """Execution-control reservation state."""

    PENDING_PREAUTH = "pending_preauth"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PreAuthState(str, enum.Enum):
    """State held only by the physically separate pre-auth token store."""

    PENDING_CONTROL = "pending_control"
    ACTIVE = "active"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RevocationReason(str, enum.Enum):
    """Payload-free reason codes for compensating and terminal revocation."""

    ACTIVATION_FAILED = "activation_failed"
    CONTROL_FINALIZATION_FAILED = "control_finalization_failed"
    CONTROL_CONFIRMATION_FAILED = "control_confirmation_failed"
    TEARDOWN = "teardown"


class EvidenceKind(str, enum.Enum):
    """Closed, payload-safe evidence categories."""

    CAPABILITY_FACT = "capability_fact"
    SAFETY_FACT = "safety_fact"
    LATENCY_FACT = "latency_fact"
    LIFECYCLE_FACT = "lifecycle_fact"


class EvidenceResult(str, enum.Enum):
    """Closed evidence outcomes."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


def _require_ref(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("ref_")
        or len(value) <= len("ref_")
        or len(value) > _MAX_OPAQUE_REF_LENGTH
        or any(character not in _OPAQUE_REF_CHARACTERS for character in value)
    ):
        raise ValueError(f"{field_name} must be an opaque reference")


def _require_exact_int(
    value: int,
    field_name: str,
    *,
    minimum: int = 0,
) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")


def _require_digest(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _HEX_DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_window(
    effective_at_ms: int,
    expires_at_ms: int,
    *,
    field_name: str,
) -> None:
    if type(effective_at_ms) is not int or type(expires_at_ms) is not int:
        raise ValueError(f"{field_name} must use exact integer timestamps")
    if effective_at_ms < 0 or expires_at_ms <= effective_at_ms:
        raise ValueError(f"{field_name} must be a positive bounded window")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _digest_json(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sign_ed25519(
    private_key: object,
    domain: bytes,
    value: object,
) -> bytes:
    if not isinstance(
        private_key,
        cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
    ):
        raise ValueError("signer requires an Ed25519 private key")
    return private_key.sign(domain + _json_bytes(value))


def _verify_ed25519(
    public_key: bytes,
    domain: bytes,
    value: object,
    signature: bytes,
) -> bool:
    if (
        not isinstance(public_key, bytes)
        or len(public_key) != 32
        or not isinstance(signature, bytes)
        or len(signature) != 64
    ):
        return False
    try:
        verifier = (
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey.from_public_bytes(
                public_key
            )
        )
        verifier.verify(signature, domain + _json_bytes(value))
    except (ValueError, cryptography.exceptions.InvalidSignature):
        return False
    return True


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalCaps:
    """Hard upper bounds carried by an approval."""

    requests: int
    attempts: int
    calls: int
    concurrency: int
    duration_ms: int
    bytes: int
    audio_ms: int
    retries: int
    tokens: int
    cost_minor_units: int
    artifact_ttl_ms: int

    def __post_init__(self) -> None:
        values = dataclasses.astuple(self)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("approval caps must be non-negative integers")
        if self.concurrency < 1 or self.duration_ms < 1 or self.artifact_ttl_ms < 1:
            raise ValueError("approval caps require positive bounded lifetimes")

    def canonical_value(self) -> dict[str, int]:
        return {
            field.name: getattr(self, field.name)
            for field in dataclasses.fields(self)
        }

    @property
    def caps_digest(self) -> str:
        return _digest_json(self.canonical_value())


@dataclasses.dataclass(frozen=True, slots=True)
class TrustedSignerKey:
    """One effective signer key in an immutable trust snapshot."""

    role: SignerRole
    identity_ref: str
    key_id: str
    public_key: bytes = dataclasses.field(repr=False)
    effective_at_ms: int
    expires_at_ms: int
    revoked_at_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, SignerRole):
            raise ValueError("role must be a closed SignerRole")
        _require_ref(self.identity_ref, "identity_ref")
        _require_ref(self.key_id, "key_id")
        if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
            raise ValueError("public_key must be a raw Ed25519 public key")
        _require_window(
            self.effective_at_ms,
            self.expires_at_ms,
            field_name="signer window",
        )
        if self.revoked_at_ms is not None and (
            type(self.revoked_at_ms) is not int
            or self.revoked_at_ms < self.effective_at_ms
        ):
            raise ValueError("revoked_at_ms must fall within the signer lifetime")

    def canonical_value(self) -> dict[str, object]:
        return {
            "effective_at_ms": self.effective_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "identity_ref": self.identity_ref,
            "key_id": self.key_id,
            "public_key_digest": hashlib.sha256(self.public_key).hexdigest(),
            "revoked_at_ms": self.revoked_at_ms,
            "role": self.role.value,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class TrustSnapshot:
    """Immutable, generation-bound trust material for offline verification."""

    generation: int
    version_ref: str
    policy_digest: str
    immutable_custody_ref: str
    effective_at_ms: int
    expires_at_ms: int
    signers: tuple[TrustedSignerKey, ...]
    sole_owner_authorization: bool
    no_break_glass: bool
    root_signature: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        _require_exact_int(self.generation, "generation", minimum=1)
        _require_ref(self.version_ref, "version_ref")
        _require_digest(self.policy_digest, "policy_digest")
        _require_ref(self.immutable_custody_ref, "immutable_custody_ref")
        _require_window(
            self.effective_at_ms,
            self.expires_at_ms,
            field_name="snapshot window",
        )
        required_roles = frozenset(SignerRole)
        roles = [signer.role for signer in self.signers]
        if len(self.signers) != len(required_roles) or frozenset(roles) != required_roles:
            raise ValueError("trust snapshot requires exactly one owner signer")
        identities = {signer.identity_ref for signer in self.signers}
        key_ids = {signer.key_id for signer in self.signers}
        public_keys = {signer.public_key for signer in self.signers}
        if (
            len(identities) != len(required_roles)
            or len(key_ids) != len(required_roles)
            or len(public_keys) != len(required_roles)
        ):
            raise ValueError("trust owner signer must use one identity and key")
        if not self.sole_owner_authorization or not self.no_break_glass:
            raise ValueError("trust policy must require sole owner authorization and forbid break glass")
        if not isinstance(self.root_signature, bytes) or len(
            self.root_signature
        ) != 64:
            raise ValueError("root_signature must be an Ed25519 signature")

    @property
    def signer_set_digest(self) -> str:
        signers = sorted(
            (signer.canonical_value() for signer in self.signers),
            key=lambda value: str(value["role"]),
        )
        return _digest_json(signers)

    @property
    def snapshot_digest(self) -> str:
        return _digest_json(self.unsigned_value())

    def unsigned_value(self) -> dict[str, object]:
        return {
            "effective_at_ms": self.effective_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "generation": self.generation,
            "immutable_custody_ref": self.immutable_custody_ref,
            "no_break_glass": self.no_break_glass,
            "sole_owner_authorization": self.sole_owner_authorization,
            "policy_digest": self.policy_digest,
            "signer_set_digest": self.signer_set_digest,
            "version_ref": self.version_ref,
        }


class TrustSnapshotRootSigner:
    """Stable offline authority that authenticates trust-snapshot transitions."""

    def __init__(self, private_key: object) -> None:
        if not isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            raise ValueError("trust root signer requires an Ed25519 private key")
        self._private_key = private_key

    def issue(self, snapshot: TrustSnapshot) -> TrustSnapshot:
        signature = _sign_ed25519(
            self._private_key,
            _TRUST_SNAPSHOT_ROOT_DOMAIN,
            snapshot.unsigned_value(),
        )
        return dataclasses.replace(snapshot, root_signature=signature)


class TrustSnapshotRootVerifier:
    """Verifier for the stable trust root, with no transition-minting ability."""

    def __init__(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("trust root verifier requires an Ed25519 public key")
        self._public_key = public_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()

    def verify(self, snapshot: TrustSnapshot) -> bool:
        return (
            isinstance(snapshot, TrustSnapshot)
            and _verify_ed25519(
                self._public_key,
                _TRUST_SNAPSHOT_ROOT_DOMAIN,
                snapshot.unsigned_value(),
                snapshot.root_signature,
            )
        )


@dataclasses.dataclass(frozen=True, slots=True)
class DetachedApprovalSignature:
    """Detached owner/key binding and Ed25519 signature."""

    role: SignerRole
    identity_ref: str
    key_id: str
    signature: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.role, SignerRole):
            raise ValueError("signature role must be a closed SignerRole")
        _require_ref(self.identity_ref, "identity_ref")
        _require_ref(self.key_id, "key_id")
        if not isinstance(self.signature, bytes) or len(self.signature) != 64:
            raise ValueError("signature must be a raw Ed25519 signature")


@dataclasses.dataclass(frozen=True, slots=True)
class TechnicalReviewReceipt:
    """Mandatory advisory review bound into an owner authorization."""

    review_digest: str
    provenance_ref: str
    reviewed_payload_digest: str
    reviewed_binding_digest: str
    unresolved_p1_count: int
    advisory_only: bool

    def __post_init__(self) -> None:
        _require_digest(self.review_digest, "review_digest")
        _require_ref(self.provenance_ref, "provenance_ref")
        _require_digest(self.reviewed_payload_digest, "reviewed_payload_digest")
        _require_digest(self.reviewed_binding_digest, "reviewed_binding_digest")
        if (
            type(self.unresolved_p1_count) is not int
            or self.unresolved_p1_count != 0
        ):
            raise ValueError("technical review must have no unresolved P1")
        if self.advisory_only is not True:
            raise ValueError("technical review must remain advisory-only")

    def canonical_value(self) -> dict[str, object]:
        return {
            "advisory_only": self.advisory_only,
            "provenance_ref": self.provenance_ref,
            "review_digest": self.review_digest,
            "reviewed_binding_digest": self.reviewed_binding_digest,
            "reviewed_payload_digest": self.reviewed_payload_digest,
            "unresolved_p1_count": self.unresolved_p1_count,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class SignedApproval:
    """Closed, detached-signature approval envelope."""

    payload_digest: str
    approval_id_digest: str
    nonce_digest: str
    binding_digest: str
    trust_snapshot_digest: str
    signer_set_digest: str
    environment: str
    arm: ApprovalArm
    epoch: int
    issued_at_ms: int
    expires_at_ms: int
    caps: ApprovalCaps
    technical_review: TechnicalReviewReceipt
    signatures: tuple[DetachedApprovalSignature, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "payload_digest",
            "approval_id_digest",
            "nonce_digest",
            "binding_digest",
            "trust_snapshot_digest",
            "signer_set_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        if self.environment != "bakeoff":
            raise ValueError("environment must be the isolated bakeoff environment")
        if not isinstance(self.arm, ApprovalArm):
            raise ValueError("arm must be a closed ApprovalArm")
        _require_exact_int(self.epoch, "epoch", minimum=1)
        _require_window(
            self.issued_at_ms,
            self.expires_at_ms,
            field_name="approval window",
        )
        if not isinstance(self.caps, ApprovalCaps):
            raise ValueError("caps must be ApprovalCaps")
        if not isinstance(self.technical_review, TechnicalReviewReceipt):
            raise ValueError("technical_review must be a closed receipt")
        if (
            self.technical_review.reviewed_payload_digest != self.payload_digest
            or self.technical_review.reviewed_binding_digest != self.binding_digest
        ):
            raise ValueError("technical review must bind the approval payload and source manifest")
        if not isinstance(self.signatures, tuple) or any(
            not isinstance(signature, DetachedApprovalSignature)
            for signature in self.signatures
        ):
            raise ValueError("signatures must be a closed signature tuple")


def _approval_message_value(approval: SignedApproval) -> dict[str, object]:
    return {
        "approval_id_digest": approval.approval_id_digest,
        "arm": approval.arm.value,
        "binding_digest": approval.binding_digest,
        "caps": approval.caps.canonical_value(),
        "environment": approval.environment,
        "epoch": approval.epoch,
        "expires_at_ms": approval.expires_at_ms,
        "issued_at_ms": approval.issued_at_ms,
        "nonce_digest": approval.nonce_digest,
        "payload_digest": approval.payload_digest,
        "signer_set_digest": approval.signer_set_digest,
        "technical_review": approval.technical_review.canonical_value(),
        "trust_snapshot_digest": approval.trust_snapshot_digest,
    }


def approval_signature_payload(approval: SignedApproval) -> dict[str, object]:
    """Return the exact JSON-able payload `approval_signature_message` signs.

    This is everything `OfflineApprovalVerifier.verify()` authenticates a
    signature against, except the domain prefix — i.e. the same dict
    `approval_signature_message` domain-separates and hashes-into-a-signature
    below. Exposed publicly (not just as the private `_approval_message_value`
    helper) so external tooling can produce a `--payload` file for
    `scripts/sign_voice_bakeoff_approval.py` that reproduces the exact bytes
    this module's verifier checks, without re-deriving this shape by hand and
    risking silent drift from the real one. See
    `scripts/run_voice_architecture_bakeoff.py`'s `--emit-signing-payload`
    mode, the intended caller.
    """

    return _approval_message_value(approval)


def approval_signature_message(approval: SignedApproval) -> bytes:
    """Return the domain-separated canonical approval message."""

    return _APPROVAL_DOMAIN + _json_bytes(approval_signature_payload(approval))


@dataclasses.dataclass(frozen=True, slots=True)
class VerifiedApproval:
    """An approval whose complete signer set was verified offline."""

    payload_digest: str
    approval_id_digest: str
    nonce_digest: str
    binding_digest: str
    trust_snapshot_digest: str
    signer_set_digest: str
    caps_digest: str
    environment: str
    arm: ApprovalArm
    epoch: int
    issued_at_ms: int
    expires_at_ms: int
    caps: ApprovalCaps
    technical_review: TechnicalReviewReceipt
    verification_proof: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "payload_digest",
            "approval_id_digest",
            "nonce_digest",
            "binding_digest",
            "trust_snapshot_digest",
            "signer_set_digest",
            "caps_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        if self.caps_digest != self.caps.caps_digest:
            raise ValueError("caps_digest must match the canonical caps")
        if not isinstance(self.technical_review, TechnicalReviewReceipt):
            raise ValueError("verified technical_review must be a closed receipt")
        if (
            self.technical_review.reviewed_payload_digest != self.payload_digest
            or self.technical_review.reviewed_binding_digest != self.binding_digest
        ):
            raise ValueError("verified technical review must bind the approval payload and source manifest")
        if self.environment != "bakeoff":
            raise ValueError("verified environment must be bakeoff")
        if not isinstance(self.arm, ApprovalArm):
            raise ValueError("verified arm must be a closed ApprovalArm")
        _require_exact_int(self.epoch, "epoch", minimum=1)
        _require_window(
            self.issued_at_ms,
            self.expires_at_ms,
            field_name="verified approval window",
        )
        if not isinstance(self.verification_proof, bytes) or len(
            self.verification_proof
        ) != 64:
            raise ValueError("verification_proof must be an Ed25519 signature")

    def unsigned_value(self) -> dict[str, object]:
        return {
            "approval_id_digest": self.approval_id_digest,
            "arm": self.arm.value,
            "binding_digest": self.binding_digest,
            "caps": self.caps.canonical_value(),
            "caps_digest": self.caps_digest,
            "environment": self.environment,
            "epoch": self.epoch,
            "expires_at_ms": self.expires_at_ms,
            "issued_at_ms": self.issued_at_ms,
            "nonce_digest": self.nonce_digest,
            "payload_digest": self.payload_digest,
            "signer_set_digest": self.signer_set_digest,
            "technical_review": self.technical_review.canonical_value(),
            "trust_snapshot_digest": self.trust_snapshot_digest,
        }


class ApprovalProvenanceSigner:
    """Verifier-only authority for minting admission provenance proofs."""

    def __init__(self, private_key: object) -> None:
        if not isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            raise ValueError("provenance signer requires an Ed25519 private key")
        self._private_key = private_key

    def issue(self, approval: VerifiedApproval) -> VerifiedApproval:
        proof = _sign_ed25519(
            self._private_key,
            _APPROVAL_PROVENANCE_DOMAIN,
            approval.unsigned_value(),
        )
        return dataclasses.replace(approval, verification_proof=proof)


class ApprovalProvenanceVerifier:
    """Admission-side verifier with no ability to mint provenance."""

    def __init__(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("provenance verifier requires an Ed25519 public key")
        self._public_key = public_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()

    def verify(self, approval: VerifiedApproval) -> bool:
        if not isinstance(approval, VerifiedApproval):
            return False
        if approval.caps_digest != approval.caps.caps_digest:
            return False
        return _verify_ed25519(
            self._public_key,
            _APPROVAL_PROVENANCE_DOMAIN,
            approval.unsigned_value(),
            approval.verification_proof,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class TrustGenerationPin:
    """Read-back value that a runtime adapter must persist with atomic CAS."""

    generation: int
    snapshot_digest: str
    root_key_fingerprint: str
    persistence_ref: str
    cas_version_digest: str

    def __post_init__(self) -> None:
        _require_exact_int(self.generation, "generation", minimum=1)
        _require_digest(self.snapshot_digest, "snapshot_digest")
        _require_digest(
            self.root_key_fingerprint,
            "root_key_fingerprint",
        )
        _require_ref(self.persistence_ref, "persistence_ref")
        _require_digest(self.cas_version_digest, "cas_version_digest")


class TrustGenerationPinStore:
    """Closed CAS interface; runtime adapters must durably implement it."""

    def read(self) -> TrustGenerationPin:
        raise NotImplementedError

    def compare_and_swap(
        self,
        *,
        expected: TrustGenerationPin,
        replacement: TrustGenerationPin,
    ) -> bool:
        raise NotImplementedError


class InMemoryTrustGenerationPinStore(TrustGenerationPinStore):
    """Offline implementation of the trust-generation CAS interface."""

    def __init__(self, initial_pin: TrustGenerationPin) -> None:
        if not isinstance(initial_pin, TrustGenerationPin):
            raise ValueError("initial_pin must be a TrustGenerationPin")
        self._lock = threading.Lock()
        self._pin = initial_pin

    def read(self) -> TrustGenerationPin:
        with self._lock:
            return self._pin

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
            or replacement.root_key_fingerprint
            != expected.root_key_fingerprint
            or replacement.generation <= expected.generation
        ):
            return False
        with self._lock:
            if self._pin != expected:
                return False
            self._pin = replacement
            return True


class OfflineApprovalVerifier:
    """Stateful verifier that rejects replay of an older trust generation."""

    def __init__(
        self,
        *,
        provenance_signer: ApprovalProvenanceSigner,
        snapshot_root_verifier: TrustSnapshotRootVerifier,
        generation_pin_store: TrustGenerationPinStore,
    ) -> None:
        if not isinstance(provenance_signer, ApprovalProvenanceSigner):
            raise ValueError("provenance_signer must be verifier-only authority")
        if not isinstance(snapshot_root_verifier, TrustSnapshotRootVerifier):
            raise ValueError("snapshot_root_verifier must be a stable trust root")
        if not isinstance(
            generation_pin_store,
            TrustGenerationPinStore,
        ):
            raise ValueError(
                "generation_pin_store must implement the closed CAS contract"
            )
        self._provenance_signer = provenance_signer
        self._snapshot_root_verifier = snapshot_root_verifier
        self._generation_pin_store = generation_pin_store
        self._lock = threading.Lock()
        if (
            generation_pin_store.read().root_key_fingerprint
            != snapshot_root_verifier.key_fingerprint
        ):
            raise ValueError("generation pin must bind the stable trust root")

    def verify(
        self,
        approval: SignedApproval,
        snapshot: TrustSnapshot,
        *,
        now_ms: int,
    ) -> VerifiedApproval | None:
        if not isinstance(approval, SignedApproval) or not isinstance(
            snapshot,
            TrustSnapshot,
        ):
            return None
        if not self._snapshot_root_verifier.verify(snapshot):
            return None
        if (
            type(now_ms) is not int
            or now_ms < snapshot.effective_at_ms
            or now_ms >= snapshot.expires_at_ms
            or approval.issued_at_ms < snapshot.effective_at_ms
            or approval.issued_at_ms >= snapshot.expires_at_ms
            or approval.expires_at_ms > snapshot.expires_at_ms
            or now_ms < approval.issued_at_ms
            or now_ms >= approval.expires_at_ms
            or approval.trust_snapshot_digest != snapshot.snapshot_digest
            or approval.signer_set_digest != snapshot.signer_set_digest
        ):
            return None

        signers_by_role = {signer.role: signer for signer in snapshot.signers}
        signatures_by_role: dict[SignerRole, DetachedApprovalSignature] = {}
        if len(approval.signatures) != len(signers_by_role):
            return None
        for signature in approval.signatures:
            if signature.role in signatures_by_role:
                return None
            signatures_by_role[signature.role] = signature
        if set(signatures_by_role) != set(signers_by_role):
            return None

        message = approval_signature_message(approval)
        for role, signer in signers_by_role.items():
            signature = signatures_by_role[role]
            if (
                signature.identity_ref != signer.identity_ref
                or signature.key_id != signer.key_id
                or approval.issued_at_ms < signer.effective_at_ms
                or approval.issued_at_ms >= signer.expires_at_ms
                or approval.expires_at_ms > signer.expires_at_ms
                or (
                    signer.revoked_at_ms is not None
                    and approval.expires_at_ms > signer.revoked_at_ms
                )
                or now_ms < signer.effective_at_ms
                or now_ms >= signer.expires_at_ms
                or (
                    signer.revoked_at_ms is not None
                    and now_ms >= signer.revoked_at_ms
                )
            ):
                return None
            try:
                public_key = (
                    cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey.from_public_bytes(
                        signer.public_key
                    )
                )
                public_key.verify(signature.signature, message)
            except (ValueError, cryptography.exceptions.InvalidSignature):
                return None

        snapshot_digest = snapshot.snapshot_digest
        with self._lock:
            persisted_pin = self._generation_pin_store.read()
            if snapshot.generation < persisted_pin.generation:
                return None
            if (
                snapshot.generation == persisted_pin.generation
                and snapshot_digest != persisted_pin.snapshot_digest
            ):
                return None
            if snapshot.generation > persisted_pin.generation:
                replacement = TrustGenerationPin(
                    generation=snapshot.generation,
                    snapshot_digest=snapshot_digest,
                    root_key_fingerprint=(
                        persisted_pin.root_key_fingerprint
                    ),
                    persistence_ref=persisted_pin.persistence_ref,
                    cas_version_digest=_digest_json(
                        {
                            "prior_cas_version_digest": (
                                persisted_pin.cas_version_digest
                            ),
                            "snapshot_digest": snapshot_digest,
                            "generation": snapshot.generation,
                        }
                    ),
                )
                if not self._generation_pin_store.compare_and_swap(
                    expected=persisted_pin,
                    replacement=replacement,
                ):
                    return None

        unsigned_verified = VerifiedApproval(
            payload_digest=approval.payload_digest,
            approval_id_digest=approval.approval_id_digest,
            nonce_digest=approval.nonce_digest,
            binding_digest=approval.binding_digest,
            trust_snapshot_digest=approval.trust_snapshot_digest,
            signer_set_digest=approval.signer_set_digest,
            caps_digest=approval.caps.caps_digest,
            environment=approval.environment,
            arm=approval.arm,
            epoch=approval.epoch,
            issued_at_ms=approval.issued_at_ms,
            expires_at_ms=approval.expires_at_ms,
            caps=approval.caps,
            technical_review=approval.technical_review,
            verification_proof=b"\x00" * 64,
        )
        return self._provenance_signer.issue(unsigned_verified)


@dataclasses.dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """Passive reservation receipt; it carries no activation authority."""

    schema_version: int
    control_ref: str
    control_version: int
    binding_digest: str
    approval_id_digest: str
    nonce_digest: str
    activation_deadline_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("receipt schema_version must be 1")
        _require_ref(self.control_ref, "control_ref")
        _require_exact_int(self.control_version, "control_version", minimum=1)
        for field_name in (
            "binding_digest",
            "approval_id_digest",
            "nonce_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        _require_exact_int(
            self.activation_deadline_ms,
            "activation_deadline_ms",
            minimum=1,
        )
        _require_exact_int(self.expires_at_ms, "expires_at_ms", minimum=1)
        if self.activation_deadline_ms > self.expires_at_ms:
            raise ValueError("activation deadline must not exceed approval expiry")

    @property
    def receipt_digest(self) -> str:
        return _digest_json(
            {
                field.name: getattr(self, field.name)
                for field in dataclasses.fields(self)
            }
        )


@dataclasses.dataclass(frozen=True, slots=True)
class PreAuthActivationGrant:
    """Short-lived, audience-bound authority to create one pre-auth record."""

    schema_version: int
    issuer_ref: str
    audience_ref: str
    purpose: str
    control_ref: str
    control_version: int
    binding_digest: str
    receipt_digest: str
    grant_id_digest: str
    issued_at_ms: int
    expires_at_ms: int
    proof: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("grant schema_version must be 1")
        for field_name in ("issuer_ref", "audience_ref", "control_ref"):
            _require_ref(getattr(self, field_name), field_name)
        if self.purpose != "activate_voice_bakeoff_preauth":
            raise ValueError("grant purpose is closed")
        _require_exact_int(self.control_version, "control_version", minimum=1)
        for field_name in (
            "binding_digest",
            "receipt_digest",
            "grant_id_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        _require_window(
            self.issued_at_ms,
            self.expires_at_ms,
            field_name="grant window",
        )
        if not isinstance(self.proof, bytes) or len(self.proof) != 64:
            raise ValueError("grant proof must be an Ed25519 signature")

    def unsigned_value(self) -> dict[str, object]:
        return {
            field.name: getattr(self, field.name)
            for field in dataclasses.fields(self)
            if field.name != "proof"
        }


@dataclasses.dataclass(frozen=True, slots=True)
class PreAuthActivationAcknowledgement:
    """Authenticated digest-only acknowledgement from the pre-auth store."""

    schema_version: int
    audience_ref: str
    control_ref: str
    control_version: int
    preauth_ref: str
    preauth_version: int
    binding_digest: str
    receipt_digest: str
    grant_id_digest: str
    token_digest: str
    acknowledged_at_ms: int
    proof: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("acknowledgement schema_version must be 1")
        for field_name in ("audience_ref", "control_ref", "preauth_ref"):
            _require_ref(getattr(self, field_name), field_name)
        _require_exact_int(self.control_version, "control_version", minimum=1)
        _require_exact_int(self.preauth_version, "preauth_version", minimum=1)
        for field_name in (
            "binding_digest",
            "receipt_digest",
            "grant_id_digest",
            "token_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        _require_exact_int(
            self.acknowledged_at_ms,
            "acknowledged_at_ms",
            minimum=0,
        )
        if not isinstance(self.proof, bytes) or len(self.proof) != 64:
            raise ValueError("acknowledgement proof must be an Ed25519 signature")

    def unsigned_value(self) -> dict[str, object]:
        return {
            field.name: getattr(self, field.name)
            for field in dataclasses.fields(self)
            if field.name != "proof"
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ControlActivationProof:
    """Authenticated proof that control finalized the matching acknowledgement."""

    schema_version: int
    audience_ref: str
    control_ref: str
    control_version: int
    preauth_ref: str
    preauth_version: int
    binding_digest: str
    grant_id_digest: str
    token_digest: str
    activated_at_ms: int
    proof: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("control proof schema_version must be 1")
        for field_name in ("audience_ref", "control_ref", "preauth_ref"):
            _require_ref(getattr(self, field_name), field_name)
        _require_exact_int(self.control_version, "control_version", minimum=1)
        _require_exact_int(self.preauth_version, "preauth_version", minimum=1)
        for field_name in (
            "binding_digest",
            "grant_id_digest",
            "token_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        _require_exact_int(
            self.activated_at_ms,
            "activated_at_ms",
            minimum=0,
        )
        if not isinstance(self.proof, bytes) or len(self.proof) != 64:
            raise ValueError("control proof must be an Ed25519 signature")

    def unsigned_value(self) -> dict[str, object]:
        return {
            field.name: getattr(self, field.name)
            for field in dataclasses.fields(self)
            if field.name != "proof"
        }


class ControlGrantSigner:
    """Control-side authority that can issue only pre-auth activation grants."""

    def __init__(self, private_key: object) -> None:
        if not isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            raise ValueError("grant signer requires an Ed25519 private key")
        self._private_key = private_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(
            self._private_key.public_key().public_bytes_raw()
        ).hexdigest()

    def issue(
        self,
        *,
        issuer_ref: str,
        audience_ref: str,
        control_ref: str,
        control_version: int,
        binding_digest: str,
        receipt_digest: str,
        grant_id_digest: str,
        issued_at_ms: int,
        expires_at_ms: int,
    ) -> PreAuthActivationGrant:
        grant = PreAuthActivationGrant(
            schema_version=1,
            issuer_ref=issuer_ref,
            audience_ref=audience_ref,
            purpose="activate_voice_bakeoff_preauth",
            control_ref=control_ref,
            control_version=control_version,
            binding_digest=binding_digest,
            receipt_digest=receipt_digest,
            grant_id_digest=grant_id_digest,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            proof=b"\x00" * 64,
        )
        return dataclasses.replace(
            grant,
            proof=_sign_ed25519(
                self._private_key,
                _GRANT_DOMAIN,
                grant.unsigned_value(),
            ),
        )


class ControlGrantVerifier:
    """Pre-auth-side grant verifier with no grant-minting capability."""

    def __init__(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("grant verifier requires an Ed25519 public key")
        self._public_key = public_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()

    def verify(self, grant: PreAuthActivationGrant) -> bool:
        if not isinstance(grant, PreAuthActivationGrant):
            return False
        return _verify_ed25519(
            self._public_key,
            _GRANT_DOMAIN,
            grant.unsigned_value(),
            grant.proof,
        )


class PreAuthAcknowledgementSigner:
    """Pre-auth-side authority that can issue only activation acknowledgements."""

    def __init__(self, private_key: object) -> None:
        if not isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            raise ValueError("ack signer requires an Ed25519 private key")
        self._private_key = private_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(
            self._private_key.public_key().public_bytes_raw()
        ).hexdigest()

    def issue(
        self,
        **values: object,
    ) -> PreAuthActivationAcknowledgement:
        acknowledgement = PreAuthActivationAcknowledgement(
            proof=b"\x00" * 64,
            **values,
        )
        return dataclasses.replace(
            acknowledgement,
            proof=_sign_ed25519(
                self._private_key,
                _ACK_DOMAIN,
                acknowledgement.unsigned_value(),
            ),
        )


class PreAuthAcknowledgementVerifier:
    """Control-side acknowledgement verifier with no ack-minting capability."""

    def __init__(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("ack verifier requires an Ed25519 public key")
        self._public_key = public_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()

    def verify(
        self,
        acknowledgement: PreAuthActivationAcknowledgement,
    ) -> bool:
        if not isinstance(
            acknowledgement,
            PreAuthActivationAcknowledgement,
        ):
            return False
        return _verify_ed25519(
            self._public_key,
            _ACK_DOMAIN,
            acknowledgement.unsigned_value(),
            acknowledgement.proof,
        )


class ControlActivationSigner:
    """Control-side authority that can issue only final activation proofs."""

    def __init__(self, private_key: object) -> None:
        if not isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            raise ValueError("control signer requires an Ed25519 private key")
        self._private_key = private_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(
            self._private_key.public_key().public_bytes_raw()
        ).hexdigest()

    def issue(
        self,
        **values: object,
    ) -> ControlActivationProof:
        proof = ControlActivationProof(proof=b"\x00" * 64, **values)
        return dataclasses.replace(
            proof,
            proof=_sign_ed25519(
                self._private_key,
                _CONTROL_PROOF_DOMAIN,
                proof.unsigned_value(),
            ),
        )


class ControlActivationVerifier:
    """Pre-auth-side control verifier with no proof-minting capability."""

    def __init__(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("control verifier requires an Ed25519 public key")
        self._public_key = public_key

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()

    def verify(self, proof: ControlActivationProof) -> bool:
        if not isinstance(proof, ControlActivationProof):
            return False
        return _verify_ed25519(
            self._public_key,
            _CONTROL_PROOF_DOMAIN,
            proof.unsigned_value(),
            proof.proof,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class AdmissionBundle:
    """Control reservation plus its separately authenticated grant."""

    receipt: AdmissionReceipt
    grant: PreAuthActivationGrant

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, AdmissionReceipt) or not isinstance(
            self.grant,
            PreAuthActivationGrant,
        ):
            raise ValueError("admission bundle types are closed")
        if (
            self.receipt.control_ref != self.grant.control_ref
            or self.receipt.control_version != self.grant.control_version
            or self.receipt.binding_digest != self.grant.binding_digest
            or self.receipt.receipt_digest != self.grant.receipt_digest
        ):
            raise ValueError("admission bundle must bind receipt and grant")


@dataclasses.dataclass
class _ControlRecord:
    control_ref: str
    version: int
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


class ExecutionControlStore:
    """Narrow nominal interface for offline control-store adapter readiness.

    Adapters must atomically consume one approval/nonce/binding epoch, enforce
    expiry and bindings, keep finalization/revocation idempotent, and fail closed
    on unavailable storage. This interface does not prove durable storage,
    distinct principals, or Task 4.8 execution readiness.
    """

    def admit(
        self,
        approval: VerifiedApproval,
        *,
        now_ms: int,
        activation_ttl_ms: int,
    ) -> AdmissionBundle | None:
        raise NotImplementedError

    def finalize(
        self,
        acknowledgement: PreAuthActivationAcknowledgement,
        *,
        now_ms: int,
    ) -> ControlActivationProof | None:
        raise NotImplementedError

    def authorizes_session(
        self,
        *,
        control_ref: str,
        preauth_ref: str,
        binding_digest: str,
        token_digest: str,
        now_ms: int,
    ) -> bool:
        raise NotImplementedError

    def revoke(
        self,
        control_ref: str,
        *,
        reason: RevocationReason,
        now_ms: int,
    ) -> bool:
        raise NotImplementedError


class InMemoryExecutionControlStore(ExecutionControlStore):
    """Atomic, offline model of nonce and active-execution control metadata."""

    def __init__(
        self,
        *,
        approval_provenance_verifier: ApprovalProvenanceVerifier,
        grant_signer: ControlGrantSigner,
        acknowledgement_verifier: PreAuthAcknowledgementVerifier,
        activation_signer: ControlActivationSigner,
        issuer_ref: str,
        preauth_audience_ref: str,
        commit_authorizer=None,
    ) -> None:
        _require_ref(issuer_ref, "issuer_ref")
        _require_ref(preauth_audience_ref, "preauth_audience_ref")
        if not isinstance(
            approval_provenance_verifier,
            ApprovalProvenanceVerifier,
        ) or not isinstance(grant_signer, ControlGrantSigner):
            raise ValueError("control approval and grant authorities are closed")
        if not isinstance(
            acknowledgement_verifier,
            PreAuthAcknowledgementVerifier,
        ) or not isinstance(activation_signer, ControlActivationSigner):
            raise ValueError("control handoff authorities are closed")
        directional_fingerprints = {
            grant_signer.key_fingerprint,
            acknowledgement_verifier.key_fingerprint,
            activation_signer.key_fingerprint,
        }
        if len(directional_fingerprints) != 3:
            raise ValueError("directional message keys must be distinct")
        self._approval_provenance_verifier = approval_provenance_verifier
        self._grant_signer = grant_signer
        self._acknowledgement_verifier = acknowledgement_verifier
        self._activation_signer = activation_signer
        self._issuer_ref = issuer_ref
        self._preauth_audience_ref = preauth_audience_ref
        self._commit_authorizer = commit_authorizer or (lambda: True)
        self._lock = threading.RLock()
        self._consumed_nonces: set[str] = set()
        self._consumed_approval_ids: set[str] = set()
        self._binding_epochs: dict[tuple[str, int], str] = {}
        self._records: dict[str, _ControlRecord] = {}

    @property
    def consumed_nonce_count(self) -> int:
        with self._lock:
            return len(self._consumed_nonces)

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

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
            or type(now_ms) is not int
            or type(activation_ttl_ms) is not int
            or activation_ttl_ms < 1
            or now_ms < approval.issued_at_ms
            or now_ms >= approval.expires_at_ms
        ):
            return None
        with self._lock:
            binding_epoch = (approval.binding_digest, approval.epoch)
            prior_binding_ref = self._binding_epochs.get(binding_epoch)
            if prior_binding_ref is not None:
                prior_binding = self._records[prior_binding_ref]
                self._expire(prior_binding, now_ms)
                if prior_binding.state in {
                    ControlState.PENDING_PREAUTH,
                    ControlState.ACTIVE,
                }:
                    return None
            if (
                approval.nonce_digest in self._consumed_nonces
                or approval.approval_id_digest in self._consumed_approval_ids
            ):
                return None
            try:
                commit_authorized = self._commit_authorizer()
            except Exception:
                return None
            if not commit_authorized:
                return None

            control_ref = f"ref_control_{secrets.token_hex(16)}"
            grant_id_digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            activation_deadline_ms = min(
                now_ms + activation_ttl_ms,
                approval.expires_at_ms,
            )
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
            record = _ControlRecord(
                control_ref=control_ref,
                version=1,
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
                control_version=record.version,
                binding_digest=record.binding_digest,
                receipt_digest=record.receipt_digest,
                grant_id_digest=record.grant_id_digest,
                issued_at_ms=now_ms,
                expires_at_ms=activation_deadline_ms,
            )
            self._consumed_nonces.add(approval.nonce_digest)
            self._consumed_approval_ids.add(approval.approval_id_digest)
            self._binding_epochs[binding_epoch] = control_ref
            self._records[control_ref] = record
            return AdmissionBundle(receipt=receipt, grant=grant)

    def state(self, control_ref: str, *, now_ms: int) -> ControlState | None:
        with self._lock:
            record = self._records.get(control_ref)
            if record is None:
                return None
            self._expire(record, now_ms)
            return record.state

    def only_record_state(self, *, now_ms: int) -> ControlState | None:
        with self._lock:
            if len(self._records) != 1:
                return None
            record = next(iter(self._records.values()))
            self._expire(record, now_ms)
            return record.state

    def is_active(self, control_ref: str, *, now_ms: int) -> bool:
        return self.state(control_ref, now_ms=now_ms) is ControlState.ACTIVE

    def authorizes_session(
        self,
        *,
        control_ref: str,
        preauth_ref: str,
        binding_digest: str,
        token_digest: str,
        now_ms: int,
    ) -> bool:
        with self._lock:
            record = self._records.get(control_ref)
            if record is None:
                return False
            self._expire(record, now_ms)
            return (
                record.state is ControlState.ACTIVE
                and record.preauth_ref == preauth_ref
                and record.binding_digest == binding_digest
                and record.token_digest == token_digest
            )

    def finalize(
        self,
        acknowledgement: PreAuthActivationAcknowledgement,
        *,
        now_ms: int,
    ) -> ControlActivationProof | None:
        if not self._acknowledgement_verifier.verify(acknowledgement):
            return None
        with self._lock:
            record = self._records.get(acknowledgement.control_ref)
            if record is None:
                return None
            self._expire(record, now_ms)
            if record.state is ControlState.ACTIVE:
                if not self._acknowledgement_matches(record, acknowledgement):
                    return None
                return self._activation_proof(record, acknowledgement)
            if (
                record.state is not ControlState.PENDING_PREAUTH
                or acknowledgement.audience_ref != self._issuer_ref
                or acknowledgement.control_version != record.version
                or acknowledgement.binding_digest != record.binding_digest
                or acknowledgement.receipt_digest != record.receipt_digest
                or acknowledgement.grant_id_digest != record.grant_id_digest
                or acknowledgement.acknowledged_at_ms > now_ms
                or acknowledgement.acknowledged_at_ms
                >= record.activation_deadline_ms
            ):
                return None
            record.state = ControlState.ACTIVE
            record.version += 1
            record.preauth_ref = acknowledgement.preauth_ref
            record.token_digest = acknowledgement.token_digest
            record.acknowledgement_digest = self._acknowledgement_digest(
                acknowledgement
            )
            record.activated_at_ms = now_ms
            return self._activation_proof(record, acknowledgement)

    def _activation_proof(
        self,
        record: _ControlRecord,
        acknowledgement: PreAuthActivationAcknowledgement,
    ) -> ControlActivationProof:
        if record.activated_at_ms is None:
            raise ValueError("active control record requires activated_at_ms")
        return self._activation_signer.issue(
            schema_version=1,
            audience_ref=self._preauth_audience_ref,
            control_ref=record.control_ref,
            control_version=record.version,
            preauth_ref=acknowledgement.preauth_ref,
            preauth_version=acknowledgement.preauth_version,
            binding_digest=record.binding_digest,
            grant_id_digest=record.grant_id_digest,
            token_digest=acknowledgement.token_digest,
            activated_at_ms=record.activated_at_ms,
        )

    def _acknowledgement_matches(
        self,
        record: _ControlRecord,
        acknowledgement: PreAuthActivationAcknowledgement,
    ) -> bool:
        return (
            acknowledgement.audience_ref == self._issuer_ref
            and acknowledgement.control_ref == record.control_ref
            and acknowledgement.control_version == record.version - 1
            and acknowledgement.preauth_ref == record.preauth_ref
            and acknowledgement.binding_digest == record.binding_digest
            and acknowledgement.receipt_digest == record.receipt_digest
            and acknowledgement.grant_id_digest == record.grant_id_digest
            and acknowledgement.token_digest == record.token_digest
            and self._acknowledgement_digest(acknowledgement)
            == record.acknowledgement_digest
        )

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

    def revoke(
        self,
        control_ref: str,
        *,
        reason: RevocationReason,
        now_ms: int,
    ) -> bool:
        if not isinstance(reason, RevocationReason):
            return False
        with self._lock:
            record = self._records.get(control_ref)
            if record is None:
                return False
            self._expire(record, now_ms)
            if record.state is ControlState.REVOKED:
                return True
            record.state = ControlState.REVOKED
            record.version += 1
            record.revocation_reason = reason
            return True

    @staticmethod
    def _expire(record: _ControlRecord, now_ms: int) -> None:
        if record.state is ControlState.PENDING_PREAUTH and (
            now_ms >= record.activation_deadline_ms
            or now_ms >= record.expires_at_ms
        ):
            record.state = ControlState.EXPIRED
            record.version += 1
        elif record.state is ControlState.ACTIVE and now_ms >= record.expires_at_ms:
            record.state = ControlState.EXPIRED
            record.version += 1


@dataclasses.dataclass(frozen=True, slots=True)
class IssuedPreAuthToken:
    """Ephemeral token returned only to the caller of activation."""

    protected_token: str = dataclasses.field(repr=False)
    token_digest: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.protected_token, str) or len(self.protected_token) < 32:
            raise ValueError("protected_token must contain at least 32 characters")
        _require_digest(self.token_digest, "token_digest")
        if (
            hashlib.sha256(self.protected_token.encode("utf-8")).hexdigest()
            != self.token_digest
        ):
            raise ValueError("token_digest must bind the protected token")
        _require_exact_int(self.expires_at_ms, "expires_at_ms", minimum=1)


@dataclasses.dataclass(frozen=True, slots=True)
class PreAuthActivation:
    """Idempotent activation result; retries never return the raw token again."""

    issued_token: IssuedPreAuthToken | None
    acknowledgement: PreAuthActivationAcknowledgement

    def __post_init__(self) -> None:
        if self.issued_token is not None and not isinstance(
            self.issued_token,
            IssuedPreAuthToken,
        ):
            raise ValueError("issued_token type is closed")
        if not isinstance(
            self.acknowledgement,
            PreAuthActivationAcknowledgement,
        ):
            raise ValueError("acknowledgement type is closed")
        if self.issued_token is not None and (
            self.issued_token.token_digest
            != self.acknowledgement.token_digest
        ):
            raise ValueError("activation token must match its acknowledgement")


@dataclasses.dataclass
class _PreAuthRecord:
    control_ref: str
    control_version: int
    preauth_ref: str
    binding_digest: str
    receipt_digest: str
    grant_id_digest: str
    token_digest: str
    state: PreAuthState
    version: int
    created_at_ms: int
    expires_at_ms: int
    revocation_reason: RevocationReason | None = None


class PreAuthTokenStore:
    """Narrow nominal interface for offline pre-auth adapter readiness.

    Adapters must preserve grant/proof bindings, one-use consumption, expiry,
    idempotent confirmation/revocation, and fail-closed storage errors. They
    must reconcile a committed activation by grant ID without returning a token
    and linearize consume versus revoke across all saga instances. This interface
    does not prove physical separation, distinct principals, durable custody, or
    Task 4.8 execution readiness.
    """

    def activate(
        self,
        grant: PreAuthActivationGrant,
        *,
        now_ms: int,
        token_ttl_ms: int,
    ) -> PreAuthActivation | None:
        raise NotImplementedError

    def confirm_control_active(
        self,
        proof: ControlActivationProof,
        *,
        now_ms: int,
    ) -> bool:
        raise NotImplementedError

    def recover_activation(
        self,
        grant: PreAuthActivationGrant,
    ) -> PreAuthActivationAcknowledgement | None:
        """Return only a prior activation acknowledgement for this exact grant."""
        raise NotImplementedError

    def consume(
        self,
        preauth_ref: str,
        *,
        control_ref: str,
        binding_digest: str,
        protected_token: str,
        now_ms: int,
    ) -> bool:
        raise NotImplementedError

    def revoke(
        self,
        preauth_ref: str,
        *,
        reason: RevocationReason,
        now_ms: int,
    ) -> bool:
        raise NotImplementedError


class InMemoryPreAuthTokenStore(PreAuthTokenStore):
    """Digest-only offline model of a separately owned pre-auth store."""

    def __init__(
        self,
        *,
        grant_verifier: ControlGrantVerifier,
        acknowledgement_signer: PreAuthAcknowledgementSigner,
        activation_verifier: ControlActivationVerifier,
        audience_ref: str,
        trusted_issuer_ref: str,
        token_factory=None,
    ) -> None:
        _require_ref(audience_ref, "audience_ref")
        _require_ref(trusted_issuer_ref, "trusted_issuer_ref")
        if not isinstance(
            grant_verifier,
            ControlGrantVerifier,
        ) or not isinstance(
            acknowledgement_signer,
            PreAuthAcknowledgementSigner,
        ):
            raise ValueError("preauth grant and acknowledgement authorities are closed")
        if not isinstance(activation_verifier, ControlActivationVerifier):
            raise ValueError("preauth activation verifier is closed")
        directional_fingerprints = {
            grant_verifier.key_fingerprint,
            acknowledgement_signer.key_fingerprint,
            activation_verifier.key_fingerprint,
        }
        if len(directional_fingerprints) != 3:
            raise ValueError("directional message keys must be distinct")
        self._grant_verifier = grant_verifier
        self._acknowledgement_signer = acknowledgement_signer
        self._activation_verifier = activation_verifier
        self._audience_ref = audience_ref
        self._trusted_issuer_ref = trusted_issuer_ref
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._lock = threading.RLock()
        self._records: dict[str, _PreAuthRecord] = {}
        self._grant_index: dict[str, str] = {}
        self._acknowledgements: dict[str, PreAuthActivationAcknowledgement] = {}

    def __repr__(self) -> str:
        with self._lock:
            return (
                "InMemoryPreAuthTokenStore("
                f"audience_ref={self._audience_ref!r}, "
                f"record_count={len(self._records)})"
            )

    @classmethod
    def record_type(cls):
        return _PreAuthRecord

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

    def activate(
        self,
        grant: PreAuthActivationGrant,
        *,
        now_ms: int,
        token_ttl_ms: int,
    ) -> PreAuthActivation | None:
        if (
            not isinstance(grant, PreAuthActivationGrant)
            or not self._grant_verifier.verify(grant)
            or grant.schema_version != 1
            or grant.audience_ref != self._audience_ref
            or grant.issuer_ref != self._trusted_issuer_ref
            or grant.purpose != "activate_voice_bakeoff_preauth"
            or type(now_ms) is not int
            or now_ms < grant.issued_at_ms
            or now_ms >= grant.expires_at_ms
            or type(token_ttl_ms) is not int
            or token_ttl_ms < 1
        ):
            return None
        with self._lock:
            prior_ref = self._grant_index.get(grant.grant_id_digest)
            if prior_ref is not None:
                prior = self._records[prior_ref]
                if not self._record_matches_grant(prior, grant):
                    return None
                return PreAuthActivation(
                    issued_token=None,
                    acknowledgement=self._acknowledgements[prior_ref],
                )

            protected_token = self._token_factory()
            if not isinstance(protected_token, str) or len(protected_token) < 32:
                return None
            token_digest = hashlib.sha256(protected_token.encode("utf-8")).hexdigest()
            preauth_ref = f"ref_preauth_{secrets.token_hex(16)}"
            expires_at_ms = min(now_ms + token_ttl_ms, grant.expires_at_ms)
            record = _PreAuthRecord(
                control_ref=grant.control_ref,
                control_version=grant.control_version,
                preauth_ref=preauth_ref,
                binding_digest=grant.binding_digest,
                receipt_digest=grant.receipt_digest,
                grant_id_digest=grant.grant_id_digest,
                token_digest=token_digest,
                state=PreAuthState.PENDING_CONTROL,
                version=1,
                created_at_ms=now_ms,
                expires_at_ms=expires_at_ms,
            )
            acknowledgement = self._acknowledgement_signer.issue(
                schema_version=1,
                audience_ref=grant.issuer_ref,
                control_ref=grant.control_ref,
                control_version=grant.control_version,
                preauth_ref=preauth_ref,
                preauth_version=record.version,
                binding_digest=grant.binding_digest,
                receipt_digest=grant.receipt_digest,
                grant_id_digest=grant.grant_id_digest,
                token_digest=token_digest,
                acknowledged_at_ms=now_ms,
            )
            self._records[preauth_ref] = record
            self._grant_index[grant.grant_id_digest] = preauth_ref
            self._acknowledgements[preauth_ref] = acknowledgement
            return PreAuthActivation(
                issued_token=IssuedPreAuthToken(
                    protected_token=protected_token,
                    token_digest=token_digest,
                    expires_at_ms=expires_at_ms,
                ),
                acknowledgement=acknowledgement,
            )

    def confirm_control_active(
        self,
        proof: ControlActivationProof,
        *,
        now_ms: int,
    ) -> bool:
        if (
            not self._activation_verifier.verify(proof)
            or proof.schema_version != 1
            or proof.audience_ref != self._audience_ref
        ):
            return False
        with self._lock:
            record = self._records.get(proof.preauth_ref)
            if record is None:
                return False
            self._expire(record, now_ms)
            if record.state is PreAuthState.ACTIVE:
                return (
                    proof.control_ref == record.control_ref
                    and proof.control_version == record.control_version + 1
                    and proof.preauth_version == record.version - 1
                    and proof.binding_digest == record.binding_digest
                    and proof.grant_id_digest == record.grant_id_digest
                    and proof.token_digest == record.token_digest
                )
            if (
                record.state is not PreAuthState.PENDING_CONTROL
                or proof.control_ref != record.control_ref
                or proof.control_version != record.control_version + 1
                or proof.preauth_version != record.version
                or proof.binding_digest != record.binding_digest
                or proof.grant_id_digest != record.grant_id_digest
                or proof.token_digest != record.token_digest
                or now_ms < proof.activated_at_ms
            ):
                return False
            record.state = PreAuthState.ACTIVE
            record.version += 1
            return True

    def recover_activation(
        self,
        grant: PreAuthActivationGrant,
    ) -> PreAuthActivationAcknowledgement | None:
        """Reconcile a committed activation without repeating token disclosure."""
        if not isinstance(grant, PreAuthActivationGrant):
            return None
        with self._lock:
            preauth_ref = self._grant_index.get(grant.grant_id_digest)
            if preauth_ref is None:
                return None
            record = self._records.get(preauth_ref)
            if record is None or not self._record_matches_grant(record, grant):
                return None
            return self._acknowledgements.get(preauth_ref)

    def state(self, preauth_ref: str, *, now_ms: int) -> PreAuthState | None:
        with self._lock:
            record = self._records.get(preauth_ref)
            if record is None:
                return None
            self._expire(record, now_ms)
            return record.state

    def only_record_state(self, *, now_ms: int) -> PreAuthState | None:
        with self._lock:
            if len(self._records) != 1:
                return None
            record = next(iter(self._records.values()))
            self._expire(record, now_ms)
            return record.state

    def consume(
        self,
        preauth_ref: str,
        *,
        control_ref: str,
        binding_digest: str,
        protected_token: str,
        now_ms: int,
    ) -> bool:
        if not isinstance(protected_token, str):
            return False
        with self._lock:
            record = self._records.get(preauth_ref)
            if record is None:
                return False
            self._expire(record, now_ms)
            candidate_digest = hashlib.sha256(
                protected_token.encode("utf-8")
            ).hexdigest()
            if (
                record.state is not PreAuthState.ACTIVE
                or record.control_ref != control_ref
                or record.binding_digest != binding_digest
                or not hmac.compare_digest(record.token_digest, candidate_digest)
            ):
                return False
            record.state = PreAuthState.CONSUMED
            record.version += 1
            return True

    def revoke(
        self,
        preauth_ref: str,
        *,
        reason: RevocationReason,
        now_ms: int,
    ) -> bool:
        if not isinstance(reason, RevocationReason):
            return False
        with self._lock:
            record = self._records.get(preauth_ref)
            if record is None:
                return False
            self._expire(record, now_ms)
            if record.state is PreAuthState.REVOKED:
                return True
            record.state = PreAuthState.REVOKED
            record.version += 1
            record.revocation_reason = reason
            return True

    @staticmethod
    def _record_matches_grant(
        record: _PreAuthRecord,
        grant: PreAuthActivationGrant,
    ) -> bool:
        return (
            record.control_ref == grant.control_ref
            and record.control_version == grant.control_version
            and record.binding_digest == grant.binding_digest
            and record.receipt_digest == grant.receipt_digest
            and record.grant_id_digest == grant.grant_id_digest
        )

    @staticmethod
    def _expire(record: _PreAuthRecord, now_ms: int) -> None:
        if record.state in {PreAuthState.PENDING_CONTROL, PreAuthState.ACTIVE} and (
            now_ms >= record.expires_at_ms
        ):
            record.state = PreAuthState.EXPIRED
            record.version += 1


@dataclasses.dataclass(frozen=True, slots=True)
class ActiveSecuritySession:
    """Opaque references plus the ephemeral token returned to the caller."""

    control_ref: str
    preauth_ref: str
    binding_digest: str
    issued_token: IssuedPreAuthToken

    def __post_init__(self) -> None:
        _require_ref(self.control_ref, "control_ref")
        _require_ref(self.preauth_ref, "preauth_ref")
        _require_digest(self.binding_digest, "binding_digest")
        if not isinstance(self.issued_token, IssuedPreAuthToken):
            raise ValueError("issued_token must be an IssuedPreAuthToken")


@dataclasses.dataclass(frozen=True, slots=True)
class TeardownResult:
    """Independent outcomes from the two idempotent teardown attempts."""

    preauth_revoked: bool
    control_revoked: bool

    def __post_init__(self) -> None:
        if type(self.preauth_revoked) is not bool or type(
            self.control_revoked
        ) is not bool:
            raise ValueError("teardown outcomes must be exact booleans")


class ExecutionSecuritySaga:
    """Fail-closed handoff between non-transactional control and pre-auth stores."""

    def __init__(
        self,
        *,
        control: ExecutionControlStore,
        preauth: PreAuthTokenStore,
    ) -> None:
        if not isinstance(control, ExecutionControlStore):
            raise ValueError("control store must implement the closed interface")
        if not isinstance(preauth, PreAuthTokenStore):
            raise ValueError("preauth store must implement the closed interface")
        if control is preauth:
            raise ValueError("control and preauth stores must be distinct")
        self._control = control
        self._preauth = preauth
        self._lock = threading.RLock()

    def admit_and_activate(
        self,
        approval: VerifiedApproval,
        *,
        now_ms: int,
        activation_ttl_ms: int,
        token_ttl_ms: int,
    ) -> ActiveSecuritySession | None:
        try:
            bundle = self._control.admit(
                approval,
                now_ms=now_ms,
                activation_ttl_ms=activation_ttl_ms,
            )
        except Exception:
            return None
        if bundle is None:
            return None
        try:
            activation = self._preauth.activate(
                bundle.grant,
                now_ms=now_ms,
                token_ttl_ms=token_ttl_ms,
            )
        except Exception:
            activation = None
        if activation is None or activation.issued_token is None:
            preauth_ref = (
                activation.acknowledgement.preauth_ref
                if activation is not None
                else self._recover_preauth_ref(bundle.grant)
            )
            self._compensate(
                control_ref=bundle.receipt.control_ref,
                preauth_ref=preauth_ref,
                reason=RevocationReason.ACTIVATION_FAILED,
                now_ms=now_ms,
            )
            return None
        try:
            proof = self._control.finalize(
                activation.acknowledgement,
                now_ms=now_ms,
            )
        except Exception:
            proof = None
        if proof is None:
            self._compensate(
                control_ref=bundle.receipt.control_ref,
                preauth_ref=activation.acknowledgement.preauth_ref,
                reason=RevocationReason.CONTROL_FINALIZATION_FAILED,
                now_ms=now_ms,
            )
            return None
        try:
            control_confirmed = self._preauth.confirm_control_active(
                proof,
                now_ms=now_ms,
            )
        except Exception:
            control_confirmed = False
        if not control_confirmed:
            self._compensate(
                control_ref=bundle.receipt.control_ref,
                preauth_ref=activation.acknowledgement.preauth_ref,
                reason=RevocationReason.CONTROL_CONFIRMATION_FAILED,
                now_ms=now_ms,
            )
            return None
        return ActiveSecuritySession(
            control_ref=bundle.receipt.control_ref,
            preauth_ref=activation.acknowledgement.preauth_ref,
            binding_digest=bundle.receipt.binding_digest,
            issued_token=activation.issued_token,
        )

    def _recover_preauth_ref(
        self,
        grant: PreAuthActivationGrant,
    ) -> str | None:
        try:
            acknowledgement = self._preauth.recover_activation(grant)
        except Exception:
            return None
        if not isinstance(acknowledgement, PreAuthActivationAcknowledgement):
            return None
        return acknowledgement.preauth_ref

    def _compensate(
        self,
        *,
        control_ref: str,
        preauth_ref: str | None,
        reason: RevocationReason,
        now_ms: int,
    ) -> None:
        if preauth_ref is not None:
            try:
                self._preauth.revoke(
                    preauth_ref,
                    reason=reason,
                    now_ms=now_ms,
                )
            except Exception:
                pass
        try:
            self._control.revoke(
                control_ref,
                reason=reason,
                now_ms=now_ms,
            )
        except Exception:
            pass

    def consume(
        self,
        session: ActiveSecuritySession,
        *,
        protected_token: str,
        now_ms: int,
    ) -> bool:
        if not isinstance(session, ActiveSecuritySession) or not isinstance(
            protected_token,
            str,
        ):
            return False
        token_digest = hashlib.sha256(protected_token.encode("utf-8")).hexdigest()
        with self._lock:
            if not self._control.authorizes_session(
                control_ref=session.control_ref,
                preauth_ref=session.preauth_ref,
                binding_digest=session.binding_digest,
                token_digest=token_digest,
                now_ms=now_ms,
            ):
                return False
            return self._preauth.consume(
                session.preauth_ref,
                control_ref=session.control_ref,
                binding_digest=session.binding_digest,
                protected_token=protected_token,
                now_ms=now_ms,
            )

    def teardown(
        self,
        session: ActiveSecuritySession,
        *,
        reason: RevocationReason,
        now_ms: int,
    ) -> TeardownResult:
        with self._lock:
            try:
                preauth_revoked = self._preauth.revoke(
                    session.preauth_ref,
                    reason=reason,
                    now_ms=now_ms,
                )
            except Exception:
                preauth_revoked = False
            try:
                control_revoked = self._control.revoke(
                    session.control_ref,
                    reason=reason,
                    now_ms=now_ms,
                )
            except Exception:
                control_revoked = False
        return TeardownResult(
            preauth_revoked=preauth_revoked,
            control_revoked=control_revoked,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class PayloadSafeReceipt:
    """Closed receipt schema that cannot carry transcript or audio payloads."""

    schema_version: int
    kind: EvidenceKind
    result: EvidenceResult
    arm: ApprovalArm
    event_digest: str
    correlation_digest: str
    occurred_at_ms: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("kind must be a closed EvidenceKind")
        if not isinstance(self.result, EvidenceResult):
            raise ValueError("result must be a closed EvidenceResult")
        if not isinstance(self.arm, ApprovalArm):
            raise ValueError("arm must be a closed ApprovalArm")
        _require_digest(self.event_digest, "event_digest")
        _require_digest(self.correlation_digest, "correlation_digest")
        if type(self.occurred_at_ms) is not int or self.occurred_at_ms < 0:
            raise ValueError("occurred_at_ms must be non-negative")


@dataclasses.dataclass(frozen=True, slots=True)
class KmsKeyVersionRef:
    """Typed reference to one exact KMS key version."""

    project_id: str
    location: str
    key_ring: str
    key_name: str
    version: int

    def __post_init__(self) -> None:
        for field_name in ("project_id", "location", "key_ring", "key_name"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or not value
                or "/" in value
                or value.strip() != value
            ):
                raise ValueError(f"{field_name} must be one exact path segment")
        _require_exact_int(self.version, "version", minimum=1)

    @property
    def resource_name(self) -> str:
        return (
            f"projects/{self.project_id}/locations/{self.location}/"
            f"keyRings/{self.key_ring}/cryptoKeys/{self.key_name}/"
            f"cryptoKeyVersions/{self.version}"
        )

    def canonical_value(self) -> dict[str, object]:
        return {
            field.name: getattr(self, field.name)
            for field in dataclasses.fields(self)
        }


@dataclasses.dataclass(frozen=True, slots=True)
class SignedCustodyLockAttestation:
    """Signed proof that distinct evidence/residue custody is retention locked."""

    schema_version: int
    evidence_destination_ref: str
    residue_destination_ref: str
    kms_key_version_ref: KmsKeyVersionRef
    immutable_custody_ref: str
    retention_policy_digest: str
    locked_at_ms: int
    expires_at_ms: int
    proof: bytes = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("custody schema_version must be 1")
        for field_name in (
            "evidence_destination_ref",
            "residue_destination_ref",
            "immutable_custody_ref",
        ):
            _require_ref(getattr(self, field_name), field_name)
        if self.evidence_destination_ref == self.residue_destination_ref:
            raise ValueError("evidence and residue destinations must be distinct")
        if not isinstance(self.kms_key_version_ref, KmsKeyVersionRef):
            raise ValueError("kms_key_version_ref must be typed")
        _require_digest(
            self.retention_policy_digest,
            "retention_policy_digest",
        )
        _require_window(
            self.locked_at_ms,
            self.expires_at_ms,
            field_name="custody attestation window",
        )
        if not isinstance(self.proof, bytes) or len(self.proof) != 64:
            raise ValueError("custody proof must be an Ed25519 signature")

    def unsigned_value(self) -> dict[str, object]:
        return {
            "evidence_destination_ref": self.evidence_destination_ref,
            "expires_at_ms": self.expires_at_ms,
            "immutable_custody_ref": self.immutable_custody_ref,
            "kms_key_version_ref": self.kms_key_version_ref.canonical_value(),
            "locked_at_ms": self.locked_at_ms,
            "residue_destination_ref": self.residue_destination_ref,
            "retention_policy_digest": self.retention_policy_digest,
            "schema_version": self.schema_version,
        }

    @property
    def attestation_digest(self) -> str:
        return _digest_json(
            {
                "proof": self.proof.hex(),
                "value": self.unsigned_value(),
            }
        )


class CustodyLockAttestationSigner:
    """Custody-control authority that signs only lock attestations."""

    def __init__(self, private_key: object) -> None:
        if not isinstance(
            private_key,
            cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey,
        ):
            raise ValueError("custody signer requires an Ed25519 private key")
        self._private_key = private_key

    def issue(
        self,
        attestation: SignedCustodyLockAttestation,
    ) -> SignedCustodyLockAttestation:
        proof = _sign_ed25519(
            self._private_key,
            _CUSTODY_ATTESTATION_DOMAIN,
            attestation.unsigned_value(),
        )
        return dataclasses.replace(attestation, proof=proof)


class CustodyLockAttestationVerifier:
    """Evidence-side custody verifier with no attestation-minting capability."""

    def __init__(self, public_key: bytes) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("custody verifier requires an Ed25519 public key")
        self._public_key = public_key

    def verify(
        self,
        attestation: SignedCustodyLockAttestation,
        *,
        now_ms: int,
    ) -> bool:
        return (
            isinstance(attestation, SignedCustodyLockAttestation)
            and type(now_ms) is int
            and attestation.locked_at_ms <= now_ms < attestation.expires_at_ms
            and _verify_ed25519(
                self._public_key,
                _CUSTODY_ATTESTATION_DOMAIN,
                attestation.unsigned_value(),
                attestation.proof,
            )
        )


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceRoutingContract:
    """Approval-bound, attested evidence and residue custody contract."""

    approval_id_digest: str
    binding_digest: str
    correlation_digest: str
    custody_attestation: SignedCustodyLockAttestation
    artifact_ttl_ms: int
    max_receipts: int

    def __post_init__(self) -> None:
        _require_digest(self.approval_id_digest, "approval_id_digest")
        _require_digest(self.binding_digest, "binding_digest")
        _require_digest(self.correlation_digest, "correlation_digest")
        if not isinstance(
            self.custody_attestation,
            SignedCustodyLockAttestation,
        ):
            raise ValueError("custody_attestation must be signed")
        _require_exact_int(
            self.artifact_ttl_ms,
            "artifact_ttl_ms",
            minimum=1,
        )
        _require_exact_int(self.max_receipts, "max_receipts", minimum=1)


@dataclasses.dataclass(frozen=True, slots=True)
class RoutedEvidenceReceipt:
    """Bounded routing decision; no write or encryption is performed here."""

    receipt_digest: str
    evidence_destination_ref: str
    residue_destination_ref: str
    kms_key_version_ref: KmsKeyVersionRef
    immutable_custody_ref: str
    custody_attestation_digest: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        _require_digest(self.receipt_digest, "receipt_digest")
        for field_name in (
            "evidence_destination_ref",
            "residue_destination_ref",
            "immutable_custody_ref",
        ):
            _require_ref(getattr(self, field_name), field_name)
        if self.evidence_destination_ref == self.residue_destination_ref:
            raise ValueError("routed destinations must be distinct")
        if not isinstance(self.kms_key_version_ref, KmsKeyVersionRef):
            raise ValueError("routed KMS reference must be typed")
        _require_digest(
            self.custody_attestation_digest,
            "custody_attestation_digest",
        )
        _require_exact_int(self.expires_at_ms, "expires_at_ms", minimum=1)


class InMemoryPayloadSafeRouter:
    """Offline-only bounded evidence-routing policy emulator."""

    def __init__(
        self,
        contract: EvidenceRoutingContract,
        *,
        approval: VerifiedApproval,
        approval_provenance_verifier: ApprovalProvenanceVerifier,
        custody_verifier: CustodyLockAttestationVerifier,
    ) -> None:
        self._contract = contract
        self._approval = approval
        self._approval_provenance_verifier = approval_provenance_verifier
        self._custody_verifier = custody_verifier
        self._lock = threading.Lock()
        self._routed_count = 0
        self._authorized = (
            approval_provenance_verifier.verify(approval)
            and contract.approval_id_digest == approval.approval_id_digest
            and contract.binding_digest == approval.binding_digest
            and contract.artifact_ttl_ms <= approval.caps.artifact_ttl_ms
            and contract.max_receipts <= approval.caps.requests
        )

    def route(
        self,
        receipt: PayloadSafeReceipt,
        *,
        now_ms: int,
    ) -> RoutedEvidenceReceipt | None:
        if (
            not isinstance(receipt, PayloadSafeReceipt)
            or not self._authorized
            or type(now_ms) is not int
            or now_ms < receipt.occurred_at_ms
            or receipt.occurred_at_ms < self._approval.issued_at_ms
            or now_ms >= self._approval.expires_at_ms
            or receipt.arm is not self._approval.arm
            or receipt.correlation_digest
            != self._contract.correlation_digest
            or not self._custody_verifier.verify(
                self._contract.custody_attestation,
                now_ms=now_ms,
            )
        ):
            return None
        with self._lock:
            if self._routed_count >= self._contract.max_receipts:
                return None
            self._routed_count += 1
        receipt_digest = _digest_json(
            {
                "arm": receipt.arm.value,
                "correlation_digest": receipt.correlation_digest,
                "event_digest": receipt.event_digest,
                "kind": receipt.kind.value,
                "occurred_at_ms": receipt.occurred_at_ms,
                "result": receipt.result.value,
                "schema_version": receipt.schema_version,
            }
        )
        custody = self._contract.custody_attestation
        return RoutedEvidenceReceipt(
            receipt_digest=receipt_digest,
            evidence_destination_ref=custody.evidence_destination_ref,
            residue_destination_ref=custody.residue_destination_ref,
            kms_key_version_ref=custody.kms_key_version_ref,
            immutable_custody_ref=custody.immutable_custody_ref,
            custody_attestation_digest=custody.attestation_digest,
            expires_at_ms=min(
                now_ms + self._contract.artifact_ttl_ms,
                self._approval.expires_at_ms,
                custody.expires_at_ms,
            ),
        )
