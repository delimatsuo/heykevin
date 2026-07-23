"""Offline-only authentication contract for future isolated voice bakeoff ingress.

This module deliberately does not construct provider clients, verify a provider
signature, mount a route, or read media. A future isolated adapter must perform
provider-specific canonical-signature verification and pass only the normalized,
verified facts defined here.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


_DIGEST_LENGTH = 64


class IngressKind(str, Enum):
    TOKEN_ISSUER = "token_issuer"
    MEDIA_STREAM = "media_stream"
    CONVERSATION_RELAY = "conversation_relay"
    CAPABILITY_PROBE = "capability_probe"
    CALLBACK = "callback"
    RECONNECT = "reconnect"


class AuthState(str, Enum):
    UNTRUSTED_HANDSHAKE = "untrusted_handshake"
    SIGNATURE_VALIDATED = "signature_validated"
    AUTH_PENDING = "auth_pending"
    AUTHENTICATED = "authenticated"
    REJECTED = "rejected"


class AuthFailure(str, Enum):
    MISSING_EXECUTION = "missing_execution"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CALL_UNBOUND = "call_unbound"
    BINDING_MISMATCH = "binding_mismatch"
    DISALLOWED_INGRESS = "disallowed_ingress"
    DISALLOWED_ACCOUNT = "disallowed_account"
    NONCANONICAL_ENDPOINT = "noncanonical_endpoint"
    DUPLICATE = "duplicate"
    SETUP_LIMIT = "setup_limit"
    PREAUTH_INPUT = "preauth_input"
    CAPABILITY_MISSING = "capability_missing"
    SIGNATURE_INVALID = "signature_invalid"
    ENVELOPE_INVALID = "envelope_invalid"
    SETUP_TIMEOUT = "setup_timeout"
    CALLBACK_MISSING = "callback_missing"
    CALLBACK_INVALID = "callback_invalid"
    CALLBACK_EXPIRED = "callback_expired"
    TOKEN_INVALID = "token_invalid"
    TOKEN_EXPIRED = "token_expired"
    CAP_EXHAUSTED = "cap_exhausted"
    STALE_EPOCH = "stale_epoch"


class CandidateArm(str, Enum):
    A = "A"
    B1 = "B1"
    B2 = "B2"
    C = "C"


class EvidenceTier(str, Enum):
    BOUNDED_CONNECTED_PROBE = "bounded_connected_probe"
    SEALED_SELECTION = "sealed_selection"


class PreAuthFrameKind(str, Enum):
    SETUP = "setup"
    MEDIA = "media"
    OTHER = "other"


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
        raise ValueError(f"{name} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 digest") from exc
    return value.lower()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    """Opaque execution identity established before any ingress is accepted."""

    environment: str
    candidate_arm: CandidateArm
    evidence_tier: EvidenceTier
    window_digest: str
    approval_id_digest: str
    approval_self_digest: str
    manifest_digest: str
    configuration_digest: str
    contractor_binding_digest: str
    provider_account_digest: str
    approved_source_pstn_digest: str
    approved_destination_pstn_digest: str
    expected_call_digest: str
    epoch: int
    expires_at_ms: int
    max_capability_ttl_ms: int
    max_setup_wait_ms: int
    max_setup_bytes: int
    max_callback_uses: int

    def __post_init__(self) -> None:
        if self.environment != "bakeoff":
            raise ValueError("environment must be bakeoff")
        if not isinstance(self.candidate_arm, CandidateArm):
            raise ValueError("candidate arm is invalid")
        if not isinstance(self.evidence_tier, EvidenceTier):
            raise ValueError("evidence tier is invalid")
        for name in (
            "window_digest",
            "approval_id_digest",
            "approval_self_digest",
            "manifest_digest",
            "configuration_digest",
            "contractor_binding_digest",
            "provider_account_digest",
            "approved_source_pstn_digest",
            "approved_destination_pstn_digest",
            "expected_call_digest",
        ):
            _digest(getattr(self, name), name)
        _positive_int(self.epoch, "epoch")
        _positive_int(self.expires_at_ms, "expires_at_ms")
        _positive_int(self.max_capability_ttl_ms, "max_capability_ttl_ms")
        _positive_int(self.max_setup_wait_ms, "max_setup_wait_ms")
        _positive_int(self.max_setup_bytes, "max_setup_bytes")
        _positive_int(self.max_callback_uses, "max_callback_uses")


@dataclass(frozen=True, slots=True)
class VerifiedTelephonyRequest:
    """Provider-verifier output; it intentionally excludes headers and raw IDs."""

    ingress: IngressKind
    provider_account_digest: str
    call_digest: str
    canonical_endpoint_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.ingress, IngressKind):
            raise ValueError("ingress is invalid")
        _digest(self.provider_account_digest, "provider_account_digest")
        _digest(self.call_digest, "call_digest")
        if self.canonical_endpoint_id not in {"bakeoff_https", "bakeoff_wss"}:
            raise ValueError("canonical endpoint is invalid")


@dataclass(frozen=True, slots=True)
class SetupEnvelope:
    """One normalized setup frame; the token is never retained by the store."""

    stream_digest: str
    protected_token: str = field(repr=False)
    kind: PreAuthFrameKind = PreAuthFrameKind.SETUP
    frame_count: int = 1
    byte_count: int = 1

    def __post_init__(self) -> None:
        _digest(self.stream_digest, "stream_digest")
        if self.kind is not PreAuthFrameKind.SETUP:
            raise ValueError("setup envelope kind is invalid")
        if not isinstance(self.protected_token, str) or len(self.protected_token) < 16:
            raise ValueError("protected token is invalid")
        _positive_int(self.frame_count, "frame_count")
        _positive_int(self.byte_count, "byte_count")


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    """Ephemeral token return value with redacted representation."""

    protected_token: str = field(repr=False)
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class AuthResult:
    state: AuthState
    failure: AuthFailure | None = None

    @property
    def authenticated(self) -> bool:
        return self.state is AuthState.AUTHENTICATED


@dataclass(frozen=True, slots=True)
class AttestedCall:
    """Opaque control-plane result bound before any ingress can authenticate."""

    call_digest: str
    source_pstn_digest: str
    destination_pstn_digest: str

    def __post_init__(self) -> None:
        _digest(self.call_digest, "call_digest")
        _digest(self.source_pstn_digest, "source_pstn_digest")
        _digest(self.destination_pstn_digest, "destination_pstn_digest")


@dataclass(frozen=True, slots=True)
class PreAuthFrame:
    """A closed type marker for the first frame before authentication."""

    kind: PreAuthFrameKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PreAuthFrameKind):
            raise ValueError("pre-auth frame kind is invalid")


class SignatureVerifier(Protocol):
    """Future provider adapter verifies a canonical URL before returning a request."""

    def verify(
        self, *, canonical_url: str, request: object
    ) -> VerifiedTelephonyRequest | None: ...


class ExecutionEnvelopeVerifier(Protocol):
    """Future approval verifier consumes its nonce before returning a binding."""

    def verify_and_consume(self, envelope: object) -> ExecutionBinding | None: ...


class ControlPlaneCallAttestor(Protocol):
    """Future runner attests the returned isolated control-plane call result."""

    def attest(self, result: object) -> AttestedCall | None: ...


class VoiceAuthStore(Protocol):
    """Atomic-store surface required by future isolated ingress adapters."""

    def register_execution(self, binding: ExecutionBinding, *, now_ms: int) -> bool: ...

    def bind_attested_call(
        self, binding: ExecutionBinding, *, attested_call: AttestedCall, now_ms: int
    ) -> bool: ...

    def issue_capability(
        self, binding: ExecutionBinding, *, now_ms: int, ttl_ms: int
    ) -> IssuedCapability | None: ...

    def begin_handshake(
        self, binding: ExecutionBinding, request: VerifiedTelephonyRequest, *, now_ms: int
    ) -> AuthResult: ...

    def consume_setup(
        self,
        binding: ExecutionBinding,
        setup: SetupEnvelope,
        *,
        now_ms: int,
    ) -> AuthResult: ...

    def revoke(self, binding: ExecutionBinding) -> None: ...

    def reject_pre_auth_frame(
        self, binding: ExecutionBinding, frame: PreAuthFrame, *, now_ms: int
    ) -> AuthResult: ...

    def issue_callback_capability(
        self, binding: ExecutionBinding, *, stream_digest: str, now_ms: int, ttl_ms: int
    ) -> IssuedCapability | None: ...

    def authorize_callback(
        self,
        binding: ExecutionBinding,
        request: VerifiedTelephonyRequest,
        *,
        stream_digest: str,
        protected_capability: str,
        now_ms: int,
    ) -> AuthResult: ...

    def advance_epoch(
        self, old_binding: ExecutionBinding, new_binding: ExecutionBinding, *, now_ms: int
    ) -> bool: ...


@dataclass(slots=True)
class _Record:
    binding: ExecutionBinding
    attested_call_bound: bool = False
    token_digest: str | None = None
    token_expires_at_ms: int | None = None
    setup_deadline_ms: int | None = None
    capability_issued: bool = False
    state: AuthState = AuthState.UNTRUSTED_HANDSHAKE
    request: VerifiedTelephonyRequest | None = None
    stream_digest: str | None = None
    callback_uses: int = 0
    callback_digest: str | None = None
    callback_expires_at_ms: int | None = None
    revoked: bool = False


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InMemoryVoiceAuthStore:
    """Thread-safe deterministic store for offline contract tests only."""

    def __init__(self) -> None:
        self._records: dict[ExecutionBinding, _Record] = {}
        self._lock = threading.Lock()

    def register_execution(self, binding: ExecutionBinding, *, now_ms: int) -> bool:
        with self._lock:
            if now_ms >= binding.expires_at_ms or binding in self._records:
                return False
            self._records[binding] = _Record(binding=binding)
            return True

    def bind_attested_call(
        self, binding: ExecutionBinding, *, attested_call: AttestedCall, now_ms: int
    ) -> bool:
        with self._lock:
            record, failure = self._active_record(binding, now_ms)
            if record is None or failure is not None:
                return False
            if (
                attested_call.call_digest != binding.expected_call_digest
                or attested_call.source_pstn_digest != binding.approved_source_pstn_digest
                or attested_call.destination_pstn_digest
                != binding.approved_destination_pstn_digest
            ):
                return False
            if record.attested_call_bound:
                return True
            record.attested_call_bound = True
            return True

    def issue_capability(
        self, binding: ExecutionBinding, *, now_ms: int, ttl_ms: int
    ) -> IssuedCapability | None:
        _positive_int(ttl_ms, "ttl_ms")
        with self._lock:
            record, failure = self._active_record(binding, now_ms)
            if (
                record is None
                or failure is not None
                or not record.attested_call_bound
                or record.capability_issued
            ):
                return None
            expires_at_ms = now_ms + ttl_ms
            if (
                ttl_ms > binding.max_capability_ttl_ms
                or expires_at_ms > binding.expires_at_ms
            ):
                return None
            token = secrets.token_urlsafe(32)
            record.token_digest = _token_digest(token)
            record.token_expires_at_ms = expires_at_ms
            record.capability_issued = True
            return IssuedCapability(protected_token=token, expires_at_ms=expires_at_ms)

    def begin_handshake(
        self, binding: ExecutionBinding, request: VerifiedTelephonyRequest, *, now_ms: int
    ) -> AuthResult:
        with self._lock:
            record, lifecycle_failure = self._active_record(binding, now_ms)
            if lifecycle_failure is not None:
                return AuthResult(AuthState.REJECTED, lifecycle_failure)
            failure = self._request_failure(record, binding, request)
            if failure is not None:
                return AuthResult(AuthState.REJECTED, failure)
            if request.ingress not in {
                IngressKind.MEDIA_STREAM,
                IngressKind.CONVERSATION_RELAY,
                IngressKind.CAPABILITY_PROBE,
                IngressKind.RECONNECT,
            }:
                return AuthResult(AuthState.REJECTED, AuthFailure.DISALLOWED_INGRESS)
            if record.state is not AuthState.UNTRUSTED_HANDSHAKE:
                return AuthResult(AuthState.REJECTED, AuthFailure.DUPLICATE)
            if record.token_digest is None or record.token_expires_at_ms is None:
                return AuthResult(AuthState.REJECTED, AuthFailure.CAPABILITY_MISSING)
            record.state = AuthState.SIGNATURE_VALIDATED
            record.request = request
            record.setup_deadline_ms = min(
                now_ms + binding.max_setup_wait_ms, record.token_expires_at_ms
            )
            record.state = AuthState.AUTH_PENDING
            return AuthResult(AuthState.AUTH_PENDING)

    def reject_pre_auth_frame(
        self, binding: ExecutionBinding, frame: PreAuthFrame, *, now_ms: int
    ) -> AuthResult:
        with self._lock:
            record, lifecycle_failure = self._active_record(binding, now_ms)
            if lifecycle_failure is not None:
                return AuthResult(AuthState.REJECTED, lifecycle_failure)
            if record.state is not AuthState.AUTH_PENDING or frame.kind is PreAuthFrameKind.SETUP:
                return AuthResult(AuthState.REJECTED, AuthFailure.PREAUTH_INPUT)
            self._reject(record)
            return AuthResult(AuthState.REJECTED, AuthFailure.PREAUTH_INPUT)

    def consume_setup(
        self,
        binding: ExecutionBinding,
        setup: SetupEnvelope,
        *,
        now_ms: int,
    ) -> AuthResult:
        with self._lock:
            record, lifecycle_failure = self._active_record(binding, now_ms)
            if lifecycle_failure is not None:
                return AuthResult(AuthState.REJECTED, lifecycle_failure)
            if record.state is not AuthState.AUTH_PENDING or record.request is None:
                return AuthResult(AuthState.REJECTED, AuthFailure.DUPLICATE)
            if record.setup_deadline_ms is None or now_ms >= record.setup_deadline_ms:
                self._reject(record)
                return AuthResult(AuthState.REJECTED, AuthFailure.SETUP_TIMEOUT)
            if setup.frame_count != 1 or setup.byte_count > binding.max_setup_bytes:
                self._reject(record)
                return AuthResult(AuthState.REJECTED, AuthFailure.SETUP_LIMIT)
            if record.token_expires_at_ms is None or now_ms >= record.token_expires_at_ms:
                self._reject(record)
                return AuthResult(AuthState.REJECTED, AuthFailure.TOKEN_EXPIRED)
            if record.token_digest is None or not secrets.compare_digest(
                record.token_digest, _token_digest(setup.protected_token)
            ):
                self._reject(record)
                return AuthResult(AuthState.REJECTED, AuthFailure.TOKEN_INVALID)
            record.token_digest = None
            record.token_expires_at_ms = None
            record.setup_deadline_ms = None
            record.stream_digest = setup.stream_digest
            record.state = AuthState.AUTHENTICATED
            return AuthResult(AuthState.AUTHENTICATED)

    def authorize_callback(
        self,
        binding: ExecutionBinding,
        request: VerifiedTelephonyRequest,
        *,
        stream_digest: str,
        protected_capability: str,
        now_ms: int,
    ) -> AuthResult:
        with self._lock:
            record, lifecycle_failure = self._active_record(binding, now_ms)
            if lifecycle_failure is not None:
                return AuthResult(AuthState.REJECTED, lifecycle_failure)
            failure = self._request_failure(record, binding, request)
            if failure is not None:
                return AuthResult(AuthState.REJECTED, failure)
            if request.ingress is not IngressKind.CALLBACK or record.state is not AuthState.AUTHENTICATED:
                return AuthResult(AuthState.REJECTED, AuthFailure.DISALLOWED_INGRESS)
            try:
                stream_digest = _digest(stream_digest, "stream_digest")
            except ValueError:
                return AuthResult(AuthState.REJECTED, AuthFailure.BINDING_MISMATCH)
            if record.callback_digest is None or record.callback_expires_at_ms is None:
                return AuthResult(AuthState.REJECTED, AuthFailure.CALLBACK_MISSING)
            if now_ms >= record.callback_expires_at_ms:
                self._clear_callback(record)
                return AuthResult(AuthState.REJECTED, AuthFailure.CALLBACK_EXPIRED)
            if not isinstance(protected_capability, str) or not secrets.compare_digest(
                record.callback_digest, _token_digest(protected_capability)
            ):
                return AuthResult(AuthState.REJECTED, AuthFailure.CALLBACK_INVALID)
            self._clear_callback(record)
            if record.stream_digest != stream_digest:
                return AuthResult(AuthState.REJECTED, AuthFailure.BINDING_MISMATCH)
            if record.callback_uses >= binding.max_callback_uses:
                self._reject(record)
                record.revoked = True
                return AuthResult(AuthState.REJECTED, AuthFailure.CAP_EXHAUSTED)
            record.callback_uses += 1
            return AuthResult(AuthState.AUTHENTICATED)

    def issue_callback_capability(
        self, binding: ExecutionBinding, *, stream_digest: str, now_ms: int, ttl_ms: int
    ) -> IssuedCapability | None:
        _positive_int(ttl_ms, "ttl_ms")
        with self._lock:
            record, failure = self._active_record(binding, now_ms)
            if (
                record is None
                or failure is not None
                or record.state is not AuthState.AUTHENTICATED
                or record.stream_digest != stream_digest
                or record.callback_digest is not None
                or record.callback_uses >= binding.max_callback_uses
                or ttl_ms > binding.max_capability_ttl_ms
                or now_ms + ttl_ms > binding.expires_at_ms
            ):
                return None
            token = secrets.token_urlsafe(32)
            record.callback_digest = _token_digest(token)
            record.callback_expires_at_ms = now_ms + ttl_ms
            return IssuedCapability(protected_token=token, expires_at_ms=now_ms + ttl_ms)

    def revoke(self, binding: ExecutionBinding) -> None:
        with self._lock:
            record = self._records.get(binding)
            if record is not None:
                self._reject(record)
                record.revoked = True

    def advance_epoch(
        self, old_binding: ExecutionBinding, new_binding: ExecutionBinding, *, now_ms: int
    ) -> bool:
        with self._lock:
            old_record, failure = self._active_record(old_binding, now_ms)
            if (
                old_record is None
                or failure is not None
                or new_binding.epoch != old_binding.epoch + 1
                or new_binding in self._records
            ):
                return False
            if any(
                getattr(old_binding, field_name) != getattr(new_binding, field_name)
                for field_name in (
                    "environment", "candidate_arm", "evidence_tier", "window_digest",
                    "approval_id_digest", "approval_self_digest", "manifest_digest",
                    "configuration_digest",
                    "contractor_binding_digest", "provider_account_digest",
                    "approved_source_pstn_digest", "approved_destination_pstn_digest",
                    "expected_call_digest", "expires_at_ms", "max_capability_ttl_ms",
                    "max_setup_wait_ms", "max_setup_bytes",
                    "max_callback_uses",
                )
            ):
                return False
            self._reject(old_record)
            old_record.revoked = True
            self._records[new_binding] = _Record(binding=new_binding)
            return True

    def _active_record(
        self, binding: ExecutionBinding, now_ms: int
    ) -> tuple[_Record | None, AuthFailure | None]:
        record = self._records.get(binding)
        if record is None:
            return None, AuthFailure.MISSING_EXECUTION
        if record.revoked:
            return None, AuthFailure.REVOKED
        if now_ms >= binding.expires_at_ms:
            self._reject(record)
            return None, AuthFailure.EXPIRED
        return record, None

    @staticmethod
    def _request_failure(
        record: _Record | None,
        binding: ExecutionBinding,
        request: VerifiedTelephonyRequest,
    ) -> AuthFailure | None:
        if record is None:
            return AuthFailure.MISSING_EXECUTION
        if not record.attested_call_bound:
            return AuthFailure.CALL_UNBOUND
        if request.provider_account_digest != binding.provider_account_digest:
            return AuthFailure.DISALLOWED_ACCOUNT
        if request.call_digest != binding.expected_call_digest:
            return AuthFailure.BINDING_MISMATCH
        expected_endpoint = (
            "bakeoff_https"
            if request.ingress in {IngressKind.CALLBACK, IngressKind.TOKEN_ISSUER}
            else "bakeoff_wss"
        )
        if request.canonical_endpoint_id != expected_endpoint:
            return AuthFailure.NONCANONICAL_ENDPOINT
        return None

    @staticmethod
    def _reject(record: _Record) -> None:
        record.token_digest = None
        record.token_expires_at_ms = None
        record.setup_deadline_ms = None
        InMemoryVoiceAuthStore._clear_callback(record)
        record.state = AuthState.REJECTED

    @staticmethod
    def _clear_callback(record: _Record) -> None:
        record.callback_digest = None
        record.callback_expires_at_ms = None


class VoiceSessionAuthenticator:
    """Public facade that requires verifier output before store transitions.

    The concrete store is deliberately replaceable by a dedicated distributed
    auth-token store in the future isolated application. This facade does not
    retain raw envelopes, requests, URLs, or capabilities.
    """

    def __init__(
        self,
        *,
        store: VoiceAuthStore,
        envelope_verifier: ExecutionEnvelopeVerifier,
        signature_verifier: SignatureVerifier,
        call_attestor: ControlPlaneCallAttestor,
        canonical_urls: dict[IngressKind, str],
    ) -> None:
        wss_ingress = {
            IngressKind.MEDIA_STREAM,
            IngressKind.CONVERSATION_RELAY,
            IngressKind.CAPABILITY_PROBE,
            IngressKind.RECONNECT,
        }
        required = wss_ingress | {IngressKind.TOKEN_ISSUER, IngressKind.CALLBACK}
        if set(canonical_urls) != required or any(
            not isinstance(url, str)
            or not url.startswith("wss://" if ingress in wss_ingress else "https://")
            for ingress, url in canonical_urls.items()
        ):
            raise ValueError("canonical URLs are required")
        self._store = store
        self._envelope_verifier = envelope_verifier
        self._signature_verifier = signature_verifier
        self._call_attestor = call_attestor
        self._canonical_urls = dict(canonical_urls)

    def register_verified_execution(self, envelope: object, *, now_ms: int) -> ExecutionBinding | None:
        binding = self._envelope_verifier.verify_and_consume(envelope)
        if binding is None or not self._store.register_execution(binding, now_ms=now_ms):
            return None
        return binding

    def bind_attested_call(
        self, binding: ExecutionBinding, *, control_plane_result: object, now_ms: int
    ) -> bool:
        attested_call = self._call_attestor.attest(control_plane_result)
        if attested_call is None:
            return False
        return self._store.bind_attested_call(
            binding, attested_call=attested_call, now_ms=now_ms
        )

    def issue_verified_capability(
        self, binding: ExecutionBinding, *, untrusted_request: object, now_ms: int, ttl_ms: int
    ) -> IssuedCapability | None:
        canonical_url = self._canonical_urls[IngressKind.TOKEN_ISSUER]
        verified = self._signature_verifier.verify(
            canonical_url=canonical_url, request=untrusted_request
        )
        if (
            verified is None
            or verified.ingress is not IngressKind.TOKEN_ISSUER
            or verified.canonical_endpoint_id != "bakeoff_https"
            or verified.provider_account_digest != binding.provider_account_digest
            or verified.call_digest != binding.expected_call_digest
        ):
            return None
        return self._store.issue_capability(binding, now_ms=now_ms, ttl_ms=ttl_ms)

    def begin_verified_handshake(
        self,
        binding: ExecutionBinding,
        *,
        ingress: IngressKind,
        untrusted_request: object,
        now_ms: int,
    ) -> AuthResult:
        canonical_url = self._canonical_urls.get(ingress)
        if canonical_url is None:
            return AuthResult(AuthState.REJECTED, AuthFailure.NONCANONICAL_ENDPOINT)
        verified = self._signature_verifier.verify(
            canonical_url=canonical_url, request=untrusted_request
        )
        if verified is None:
            return AuthResult(AuthState.REJECTED, AuthFailure.SIGNATURE_INVALID)
        if verified.ingress is not ingress:
            return AuthResult(AuthState.REJECTED, AuthFailure.BINDING_MISMATCH)
        return self._store.begin_handshake(binding, verified, now_ms=now_ms)

    def reject_pre_auth_frame(
        self, binding: ExecutionBinding, frame: PreAuthFrame, *, now_ms: int
    ) -> AuthResult:
        return self._store.reject_pre_auth_frame(binding, frame, now_ms=now_ms)

    def consume_setup(
        self,
        binding: ExecutionBinding,
        setup: SetupEnvelope,
        *,
        now_ms: int,
    ) -> AuthResult:
        return self._store.consume_setup(binding, setup, now_ms=now_ms)

    def issue_callback_capability(
        self, binding: ExecutionBinding, *, stream_digest: str, now_ms: int, ttl_ms: int
    ) -> IssuedCapability | None:
        return self._store.issue_callback_capability(
            binding, stream_digest=stream_digest, now_ms=now_ms, ttl_ms=ttl_ms
        )

    def authorize_verified_callback(
        self,
        binding: ExecutionBinding,
        *,
        untrusted_request: object,
        stream_digest: str,
        protected_capability: str,
        now_ms: int,
    ) -> AuthResult:
        canonical_url = self._canonical_urls.get(IngressKind.CALLBACK)
        if canonical_url is None:
            return AuthResult(AuthState.REJECTED, AuthFailure.NONCANONICAL_ENDPOINT)
        verified = self._signature_verifier.verify(
            canonical_url=canonical_url, request=untrusted_request
        )
        if verified is None or verified.ingress is not IngressKind.CALLBACK:
            return AuthResult(AuthState.REJECTED, AuthFailure.SIGNATURE_INVALID)
        return self._store.authorize_callback(
            binding,
            verified,
            stream_digest=stream_digest,
            protected_capability=protected_capability,
            now_ms=now_ms,
        )

    def revoke(self, binding: ExecutionBinding) -> None:
        self._store.revoke(binding)

    def advance_epoch(
        self, old_binding: ExecutionBinding, new_binding: ExecutionBinding, *, now_ms: int
    ) -> bool:
        return self._store.advance_epoch(old_binding, new_binding, now_ms=now_ms)
