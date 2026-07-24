"""Adversarial tests for the offline-only voice ingress authentication contract."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.voice_session_auth import (
    AttestedCall,
    AuthFailure,
    AuthState,
    CallbackPurpose,
    CandidateArm,
    EvidenceTier,
    ExecutionBinding,
    InMemoryVoiceAuthStore,
    IngressKind,
    PreAuthFrame,
    PreAuthFrameKind,
    SetupEnvelope,
    VerifiedTelephonyRequest,
    VoiceSessionAuthenticator,
)


def _digest(value: str) -> str:
    return value * 64


def _binding(epoch: int = 1, **changes: object) -> ExecutionBinding:
    values: dict[str, object] = {
        "environment": "bakeoff", "candidate_arm": CandidateArm.B1,
        "evidence_tier": EvidenceTier.BOUNDED_CONNECTED_PROBE,
        "window_digest": _digest("a"), "approval_id_digest": _digest("b"),
        "approval_self_digest": _digest("c"), "manifest_digest": _digest("d"),
        "configuration_digest": _digest("e"), "contractor_binding_digest": _digest("f"),
        "provider_account_digest": _digest("0"), "approved_source_pstn_digest": _digest("3"),
        "approved_destination_pstn_digest": _digest("4"), "expected_call_digest": _digest("1"),
        "epoch": epoch, "expires_at_ms": 1_000, "max_capability_ttl_ms": 100,
        "max_setup_wait_ms": 20, "max_setup_bytes": 64, "max_callback_uses": 1,
    }
    values.update(changes)
    return ExecutionBinding(**values)


def _request(ingress: IngressKind = IngressKind.MEDIA_STREAM, **changes: object) -> VerifiedTelephonyRequest:
    values: dict[str, object] = {
        "ingress": ingress, "provider_account_digest": _digest("0"),
        "call_digest": _digest("1"), "canonical_endpoint_id": "bakeoff_wss",
    }
    values.update(changes)
    return VerifiedTelephonyRequest(**values)


def _call(binding: ExecutionBinding, **changes: object) -> AttestedCall:
    values: dict[str, object] = {
        "call_digest": binding.expected_call_digest,
        "source_pstn_digest": binding.approved_source_pstn_digest,
        "destination_pstn_digest": binding.approved_destination_pstn_digest,
    }
    values.update(changes)
    return AttestedCall(**values)


class _EnvelopeVerifier:
    def __init__(self, binding: ExecutionBinding | None) -> None:
        self.binding = binding
        self.calls = 0

    def verify_and_consume(self, envelope: object) -> ExecutionBinding | None:
        self.calls += 1
        return self.binding if envelope == "approved" else None


class _SignatureVerifier:
    def __init__(
        self,
        callback_request: VerifiedTelephonyRequest | None = None,
        issuer_request: VerifiedTelephonyRequest | None = None,
    ) -> None:
        self.callback_request = callback_request
        self.issuer_request = issuer_request
        self.urls: list[str] = []

    def verify(self, *, canonical_url: str, request: object) -> VerifiedTelephonyRequest | None:
        self.urls.append(canonical_url)
        if request != "signed":
            return None
        if canonical_url.endswith("status"):
            return self.callback_request or _request(
                IngressKind.STATUS_CALLBACK,
                canonical_endpoint_id="bakeoff_https",
            )
        if canonical_url.endswith("evidence"):
            return self.callback_request or _request(
                IngressKind.EVIDENCE_CALLBACK,
                canonical_endpoint_id="bakeoff_https",
            )
        if canonical_url.endswith("issuer"):
            return self.issuer_request or _request(
                IngressKind.TOKEN_ISSUER, canonical_endpoint_id="bakeoff_https"
            )
        return _request()


class _CallAttestor:
    def __init__(self, call: AttestedCall | None) -> None:
        self.call = call

    def attest(self, result: object) -> AttestedCall | None:
        return self.call if result == "created" else None


def _facade(
    binding: ExecutionBinding,
    request: VerifiedTelephonyRequest | None = None,
    issuer_request: VerifiedTelephonyRequest | None = None,
):
    store = InMemoryVoiceAuthStore()
    envelope = _EnvelopeVerifier(binding)
    signature = _SignatureVerifier(request, issuer_request)
    app = VoiceSessionAuthenticator(
        store=store, envelope_verifier=envelope, signature_verifier=signature,
        call_attestor=_CallAttestor(_call(binding)),
        canonical_urls={
            IngressKind.MEDIA_STREAM: "wss://configured.example/media",
            IngressKind.CONVERSATION_RELAY: "wss://configured.example/relay",
            IngressKind.CAPABILITY_PROBE: "wss://configured.example/probe",
            IngressKind.RECONNECT: "wss://configured.example/reconnect",
            IngressKind.TOKEN_ISSUER: "https://configured.example/issuer",
            IngressKind.STATUS_CALLBACK: "https://configured.example/status",
            IngressKind.EVIDENCE_CALLBACK: "https://configured.example/evidence",
        },
    )
    return app, store, envelope, signature


def _authenticated(app: VoiceSessionAuthenticator, binding: ExecutionBinding) -> str:
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    capability = app.issue_verified_capability(
        binding, untrusted_request="signed", now_ms=3, ttl_ms=10
    )
    assert capability is not None
    assert app.begin_verified_handshake(binding, ingress=IngressKind.MEDIA_STREAM, untrusted_request="signed", now_ms=4).state is AuthState.AUTH_PENDING
    assert app.consume_setup(binding, SetupEnvelope(stream_digest=_digest("2"), protected_token=capability.protected_token, byte_count=10), now_ms=5).authenticated
    return capability.protected_token


def test_verifier_facade_blocks_forged_envelope_and_request():
    binding = _binding()
    app, store, envelope, signature = _facade(binding)
    assert app.register_verified_execution("forged", now_ms=1) is None
    assert envelope.calls == 1 and not store._records
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    assert app.issue_verified_capability(
        binding, untrusted_request="signed", now_ms=3, ttl_ms=10
    ) is not None
    result = app.begin_verified_handshake(binding, ingress=IngressKind.MEDIA_STREAM, untrusted_request="forged", now_ms=4)
    assert result.failure is AuthFailure.SIGNATURE_INVALID
    assert signature.urls == ["https://configured.example/issuer", "wss://configured.example/media"]


def test_attested_pstn_and_approval_bound_execution_fail_closed():
    binding = _binding()
    app, _, _, _ = _facade(binding)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    app_bad, _, _, _ = _facade(binding)
    app_bad._call_attestor = _CallAttestor(_call(binding, source_pstn_digest=_digest("5")))
    assert app_bad.register_verified_execution("approved", now_ms=1) == binding
    assert not app_bad.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)


def test_setup_deadline_and_media_before_auth_reject_and_erase_token():
    binding = _binding(max_setup_wait_ms=2)
    app, _, _, _ = _facade(binding)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    token = app.issue_verified_capability(
        binding, untrusted_request="signed", now_ms=3, ttl_ms=10
    )
    assert token is not None
    assert app.begin_verified_handshake(binding, ingress=IngressKind.MEDIA_STREAM, untrusted_request="signed", now_ms=4).state is AuthState.AUTH_PENDING
    assert app.consume_setup(binding, SetupEnvelope(stream_digest=_digest("2"), protected_token=token.protected_token), now_ms=6).failure is AuthFailure.SETUP_TIMEOUT

    app, _, _, _ = _facade(binding)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    token = app.issue_verified_capability(
        binding, untrusted_request="signed", now_ms=3, ttl_ms=10
    )
    assert token is not None
    assert app.begin_verified_handshake(binding, ingress=IngressKind.MEDIA_STREAM, untrusted_request="signed", now_ms=4).state is AuthState.AUTH_PENDING
    assert app.reject_pre_auth_frame(binding, PreAuthFrame(PreAuthFrameKind.MEDIA), now_ms=5).failure is AuthFailure.PREAUTH_INPUT
    assert app.consume_setup(binding, SetupEnvelope(stream_digest=_digest("2"), protected_token=token.protected_token), now_ms=6).state is AuthState.REJECTED


def test_concurrent_setup_has_exactly_one_authenticated_winner():
    binding = _binding()
    app, _, _, _ = _facade(binding)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    capability = app.issue_verified_capability(
        binding, untrusted_request="signed", now_ms=3, ttl_ms=10
    )
    assert capability is not None
    assert app.begin_verified_handshake(binding, ingress=IngressKind.MEDIA_STREAM, untrusted_request="signed", now_ms=4).state is AuthState.AUTH_PENDING
    setup = SetupEnvelope(stream_digest=_digest("2"), protected_token=capability.protected_token)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: app.consume_setup(binding, setup, now_ms=5), range(2)))
    assert sum(result.authenticated for result in results) == 1


def test_callback_capability_is_bound_one_time_and_revoked():
    binding = _binding()
    callback_request = _request(
        IngressKind.STATUS_CALLBACK,
        canonical_endpoint_id="bakeoff_https",
    )
    app, _, _, _ = _facade(binding, callback_request)
    _authenticated(app, binding)
    capability = app.issue_callback_capability(
        binding,
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        now_ms=6,
        ttl_ms=10,
    )
    assert capability is not None
    assert app.authorize_verified_callback(binding, untrusted_request="signed", stream_digest=_digest("3"), purpose=CallbackPurpose.STATUS, protected_capability=capability.protected_token, now_ms=7).failure is AuthFailure.BINDING_MISMATCH
    replacement = app.issue_callback_capability(
        binding,
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        now_ms=7,
        ttl_ms=10,
    )
    assert replacement is not None
    assert app.authorize_verified_callback(binding, untrusted_request="signed", stream_digest=_digest("2"), purpose=CallbackPurpose.STATUS, protected_capability=replacement.protected_token, now_ms=7).authenticated
    assert app.issue_callback_capability(binding, stream_digest=_digest("2"), purpose=CallbackPurpose.STATUS, now_ms=7, ttl_ms=10) is None
    app.revoke(binding)
    assert app.authorize_verified_callback(binding, untrusted_request="signed", stream_digest=_digest("2"), purpose=CallbackPurpose.STATUS, protected_capability=capability.protected_token, now_ms=8).state is AuthState.REJECTED


def test_closed_schema_caps_and_token_redaction():
    with pytest.raises(ValueError, match="environment"):
        _binding(environment="production")
    with pytest.raises(ValueError, match="SHA-256"):
        _binding(manifest_digest="bad")
    binding = _binding()
    app, store, _, _ = _facade(binding)
    token = _authenticated(app, binding)
    assert token not in repr(SetupEnvelope(stream_digest=_digest("2"), protected_token=token))
    assert token not in repr(store._records[binding])


def test_facade_reconnect_rotates_epoch_and_requires_fresh_attestation():
    binding = _binding()
    app, _, _, _ = _facade(binding)
    _authenticated(app, binding)
    replacement = _binding(epoch=2)

    assert app.advance_epoch(binding, replacement, now_ms=6)
    assert app.begin_verified_handshake(binding, ingress=IngressKind.MEDIA_STREAM, untrusted_request="signed", now_ms=7).state is AuthState.REJECTED
    assert app.issue_verified_capability(replacement, untrusted_request="signed", now_ms=7, ttl_ms=10) is None
    assert app.bind_attested_call(replacement, control_plane_result="created", now_ms=7)
    assert app.issue_verified_capability(replacement, untrusted_request="signed", now_ms=7, ttl_ms=10) is not None


def test_facade_rejects_wrong_canonical_scheme():
    binding = _binding()
    with pytest.raises(ValueError, match="canonical URLs"):
        VoiceSessionAuthenticator(
            store=InMemoryVoiceAuthStore(), envelope_verifier=_EnvelopeVerifier(binding),
            signature_verifier=_SignatureVerifier(), call_attestor=_CallAttestor(_call(binding)),
            canonical_urls={IngressKind.MEDIA_STREAM: "https://wrong.example/media"},
        )


def test_issuer_rejects_cross_account_call_and_concurrent_replay():
    binding = _binding()
    cross_account = _request(
        IngressKind.TOKEN_ISSUER,
        provider_account_digest=_digest("9"),
        canonical_endpoint_id="bakeoff_https",
    )
    app, _, _, _ = _facade(binding, issuer_request=cross_account)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    assert app.issue_verified_capability(binding, untrusted_request="signed", now_ms=3, ttl_ms=10) is None

    wrong_call = _request(
        IngressKind.TOKEN_ISSUER,
        call_digest=_digest("8"),
        canonical_endpoint_id="bakeoff_https",
    )
    app, _, _, _ = _facade(binding, issuer_request=wrong_call)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    assert app.issue_verified_capability(binding, untrusted_request="signed", now_ms=3, ttl_ms=10) is None

    wrong_endpoint = _request(
        IngressKind.TOKEN_ISSUER, canonical_endpoint_id="bakeoff_wss"
    )
    app, _, _, _ = _facade(binding, issuer_request=wrong_endpoint)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    assert app.issue_verified_capability(binding, untrusted_request="signed", now_ms=3, ttl_ms=10) is None

    app, _, _, _ = _facade(binding)
    assert app.register_verified_execution("approved", now_ms=1) == binding
    assert app.bind_attested_call(binding, control_plane_result="created", now_ms=2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        issued = list(
            executor.map(
                lambda _: app.issue_verified_capability(
                    binding, untrusted_request="signed", now_ms=3, ttl_ms=10
                ),
                range(2),
            )
        )
    assert sum(capability is not None for capability in issued) == 1
