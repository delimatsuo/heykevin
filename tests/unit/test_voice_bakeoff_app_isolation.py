"""Offline isolation and authentication tests for the bakeoff application."""

import ast
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.experiments.voice_bakeoff_app import (
    BakeoffAppConfig,
    BakeoffDependency,
    BakeoffIngressRuntime,
    MonotonicClock,
    RuntimeMode,
    ServerBoundedIngressReader,
    VerifiedServerIngressLimits,
    connected_configuration_digest,
    create_voice_bakeoff_app,
    execution_binding_digest,
)
from app.services.voice_session_auth import (
    AttestedCall,
    AuthState,
    CallbackPurpose,
    CandidateArm,
    EvidenceTier,
    ExecutionBinding,
    InMemoryVoiceAuthStore,
    IngressKind,
    SetupEnvelope,
    VerifiedTelephonyRequest,
    VoiceSessionAuthenticator,
)

_DISABLED_FEATURES = frozenset(
    {
        "tools",
        "writes",
        "notifications",
        "terminal_actions",
        "transfers",
        "recording",
        "tracing",
        "data_sharing",
        "request_response_logging",
        "session_resumption",
        "provider_cache",
    }
)
_IDENTITY_PERMISSIONS = frozenset(
    {"bakeoff_auth_store", "bakeoff_evidence_store"}
)


def _ingress_reader() -> ServerBoundedIngressReader:
    return ServerBoundedIngressReader(
        VerifiedServerIngressLimits(
            max_websocket_message_bytes=512,
            max_http_body_bytes=4_096,
            reader_code_digest=ServerBoundedIngressReader.code_digest(),
        )
    )


_INGRESS_CONTRACT_DIGEST = _ingress_reader().limits.contract_digest


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _imported_modules(source: str, *, module_name: str) -> set[str]:
    tree = ast.parse(source)
    package = module_name.split(".")[:-1]
    imported: set[str] = set()
    importlib_names = {"importlib"}
    import_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            import_module_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "import_module"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                parts = package[: max(keep, 0)]
                if node.module:
                    parts.extend(node.module.split("."))
                base = ".".join(parts)
            else:
                base = node.module or ""
            if base:
                imported.add(base)
            imported.update(
                ".".join(part for part in (base, alias.name) if part)
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, ast.Call) and node.args:
            target = _literal_string(node.args[0])
            if target is None:
                continue
            if isinstance(node.func, ast.Name) and (
                node.func.id == "__import__" or node.func.id in import_module_names
            ):
                imported.add(target)
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_names
            ):
                imported.add(target)
    return imported


def _targets(module: str, target: str) -> bool:
    return module == target or module.startswith(f"{target}.")


def _module_path(module: str) -> Path | None:
    parts = module.split(".")
    module_file = Path(*parts).with_suffix(".py")
    package_file = Path(*parts) / "__init__.py"
    if module_file.is_file():
        return module_file
    if package_file.is_file():
        return package_file
    return None


def _reachable_local_modules(root: str) -> set[str]:
    pending = [root]
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        path = _module_path(module)
        if path is None:
            continue
        visited.add(module)
        imported = _imported_modules(
            path.read_text(encoding="utf-8"),
            module_name=module,
        )
        pending.extend(
            dependency
            for dependency in imported
            if dependency.startswith("app.") and dependency not in visited
        )
    return visited


def _digest(character: str) -> str:
    return character * 64


def _binding(arm: CandidateArm = CandidateArm.B1) -> ExecutionBinding:
    return ExecutionBinding(
        environment="bakeoff",
        candidate_arm=arm,
        evidence_tier=EvidenceTier.BOUNDED_CONNECTED_PROBE,
        window_digest=_digest("a"),
        approval_id_digest=_digest("b"),
        approval_self_digest=_digest("c"),
        manifest_digest=_digest("d"),
        configuration_digest=connected_configuration_digest(
            dependencies=_dependencies(arm),
            execution_identity="bakeoff_runner",
            identity_permissions=_IDENTITY_PERMISSIONS,
            ingress_contract_digest=_INGRESS_CONTRACT_DIGEST,
            disabled_features=_DISABLED_FEATURES,
            capability_ttl_ms=10,
        ),
        contractor_binding_digest=_digest("f"),
        provider_account_digest=_digest("0"),
        approved_source_pstn_digest=_digest("3"),
        approved_destination_pstn_digest=_digest("4"),
        expected_call_digest=_digest("1"),
        epoch=1,
        expires_at_ms=1_000,
        max_capability_ttl_ms=100,
        max_setup_wait_ms=20,
        max_setup_bytes=512,
        max_callback_uses=2,
    )


class _EnvelopeVerifier:
    def __init__(self, binding: ExecutionBinding) -> None:
        self.binding = binding

    def verify_and_consume(self, envelope: object) -> ExecutionBinding | None:
        return self.binding if envelope == "approved" else None


class _SignatureVerifier:
    def __init__(self) -> None:
        self.ingress = IngressKind.MEDIA_STREAM

    def verify(
        self,
        *,
        canonical_url: str,
        request: object,
    ) -> VerifiedTelephonyRequest | None:
        if request == "forged":
            return None
        endpoint = (
            "bakeoff_https"
            if self.ingress
            in {
                IngressKind.TOKEN_ISSUER,
                IngressKind.STATUS_CALLBACK,
                IngressKind.EVIDENCE_CALLBACK,
            }
            else "bakeoff_wss"
        )
        return VerifiedTelephonyRequest(
            ingress=self.ingress,
            provider_account_digest=_digest("0"),
            call_digest=_digest("1"),
            canonical_endpoint_id=endpoint,
        )


class _CallAttestor:
    def __init__(self, binding: ExecutionBinding) -> None:
        self.binding = binding

    def attest(self, result: object) -> AttestedCall | None:
        if result != "created":
            return None
        return AttestedCall(
            call_digest=self.binding.expected_call_digest,
            source_pstn_digest=self.binding.approved_source_pstn_digest,
            destination_pstn_digest=self.binding.approved_destination_pstn_digest,
        )


class _FakeRequest:
    def __init__(
        self,
        *,
        body: bytes = b"{}",
    ) -> None:
        self.body = body

    async def stream(self):
        yield self.body


class _FakeWebSocket:
    def __init__(
        self,
        *,
        setup: bytes = b"{}",
        setup_delay_seconds: float = 0,
        disconnect: bool = False,
    ) -> None:
        self.accepted = False
        self.closed: list[int] = []
        self.sent: list[dict[str, str]] = []
        self.setup = setup
        self.setup_delay_seconds = setup_delay_seconds
        self.disconnect = disconnect

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.closed.append(code)

    async def send_json(self, value: dict[str, str]) -> None:
        self.sent.append(value)

    async def receive(self) -> dict[str, object]:
        if self.setup_delay_seconds:
            await asyncio.sleep(self.setup_delay_seconds)
        if self.disconnect:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "bytes": self.setup}


def _dependencies(arm: CandidateArm) -> tuple[BakeoffDependency, ...]:
    roles = {
        CandidateArm.A: ("telephony", "native_voice"),
        CandidateArm.B1: (
            "telephony",
            "speech_to_text",
            "text_generation",
            "text_to_speech",
        ),
        CandidateArm.B2: ("telephony", "conversation_relay", "text_generation"),
        CandidateArm.C: ("telephony", "native_voice"),
    }[arm]
    return tuple(
        BakeoffDependency(
            role=role,
            provider_ref=f"provider_{role}",
            version_ref=f"version_{role}",
            endpoint_ref=f"endpoint_{role}",
            destination_allowlist_ref=f"allowlist_{role}",
            credential_ref=f"credential_{role}",
            account_region_ref=f"region_{role}",
            nonproduction_identity_ref=f"identity_{role}",
            privacy_posture_ref=f"privacy_{role}",
        )
        for role in roles
    )


def _connected(
    arm: CandidateArm = CandidateArm.B1,
) -> tuple[BakeoffIngressRuntime, VoiceSessionAuthenticator, _SignatureVerifier]:
    binding = _binding(arm)
    signature = _SignatureVerifier()
    authenticator = VoiceSessionAuthenticator(
        store=InMemoryVoiceAuthStore(),
        envelope_verifier=_EnvelopeVerifier(binding),
        signature_verifier=signature,
        call_attestor=_CallAttestor(binding),
        canonical_urls={
            IngressKind.MEDIA_STREAM: "wss://bakeoff.invalid/media",
            IngressKind.CONVERSATION_RELAY: "wss://bakeoff.invalid/relay",
            IngressKind.CAPABILITY_PROBE: "wss://bakeoff.invalid/probe",
            IngressKind.RECONNECT: "wss://bakeoff.invalid/reconnect",
            IngressKind.TOKEN_ISSUER: "https://bakeoff.invalid/token",
            IngressKind.STATUS_CALLBACK: "https://bakeoff.invalid/status",
            IngressKind.EVIDENCE_CALLBACK: "https://bakeoff.invalid/evidence",
        },
    )
    config = BakeoffAppConfig(
        mode=RuntimeMode.CONNECTED,
        environment="bakeoff",
        disabled_features=_DISABLED_FEATURES,
        dependencies=_dependencies(arm),
        execution_record_digest=execution_binding_digest(binding),
        manifest_digest=binding.manifest_digest,
        configuration_digest=binding.configuration_digest,
        execution_identity="bakeoff_runner",
        identity_permissions=_IDENTITY_PERMISSIONS,
        ingress_contract_digest=_INGRESS_CONTRACT_DIGEST,
        capability_ttl_ms=10,
    )
    assert authenticator.register_verified_execution("approved", now_ms=1) == binding
    return (
        BakeoffIngressRuntime(
            config=config,
            authenticator=authenticator,
            binding=binding,
            startup_now_ms=2,
        ),
        authenticator,
        signature,
    )


def test_dry_run_cannot_construct_routes_and_connected_table_is_isolated():
    runtime = BakeoffIngressRuntime(config=BakeoffAppConfig.dry_run())
    with pytest.raises(ValueError, match="dry-run"):
        create_voice_bakeoff_app(
            runtime=runtime,
            clock=MonotonicClock(),
            ingress_reader=_ingress_reader(),
        )
    connected, _, _ = _connected()
    app = create_voice_bakeoff_app(
        runtime=connected,
        clock=MonotonicClock(initial_ms=2),
        ingress_reader=_ingress_reader(),
    )
    paths = {route.path for route in app.routes}
    assert paths == {
        "/bakeoff/twiml/A",
        "/bakeoff/twiml/B1",
        "/bakeoff/twiml/B2",
        "/bakeoff/twiml/C",
        "/bakeoff/ws/media/A",
        "/bakeoff/ws/media/B1",
        "/bakeoff/ws/media/C",
        "/bakeoff/ws/relay/B2",
        "/bakeoff/ws/probe/A",
        "/bakeoff/ws/probe/B1",
        "/bakeoff/ws/probe/B2",
        "/bakeoff/ws/probe/C",
        "/bakeoff/ws/reconnect/A",
        "/bakeoff/ws/reconnect/B1",
        "/bakeoff/ws/reconnect/B2",
        "/bakeoff/ws/reconnect/C",
        "/bakeoff/callback/status",
        "/bakeoff/callback/evidence",
    }
    assert runtime.issue_capability(
        arm=CandidateArm.A,
        untrusted_request="signed",
        now_ms=0,
    ) is None
    assert not runtime.begin_handshake(
        arm=CandidateArm.A,
        ingress=IngressKind.MEDIA_STREAM,
        untrusted_request="signed",
        now_ms=0,
    ).authenticated


def test_connected_runtime_requires_exact_binding_dependencies_and_permissions():
    binding = _binding()
    with pytest.raises(ValueError, match="environment"):
        BakeoffAppConfig(
            mode=RuntimeMode.DRY_RUN,
            environment="production",
            disabled_features=frozenset(),
        )
    runtime, authenticator, _ = _connected()
    assert runtime.connected and runtime.binding == binding
    with pytest.raises(ValueError, match="binding is not exact"):
        BakeoffIngressRuntime(
            config=runtime.config,
            authenticator=authenticator,
            binding=binding,
            startup_now_ms=2,
        )
    changed_dependencies = list(runtime.config.dependencies)
    changed_dependencies[0] = replace(
        changed_dependencies[0],
        credential_ref="production_credential_ref",
    )
    with pytest.raises(ValueError, match="approval-bound"):
        replace(runtime.config, dependencies=tuple(changed_dependencies))
    with pytest.raises(ValueError, match="approval-bound"):
        replace(runtime.config, execution_identity="alternate_runner")
    with pytest.raises(ValueError, match="approval-bound"):
        BakeoffAppConfig(
            mode=RuntimeMode.CONNECTED,
            environment="bakeoff",
            disabled_features=runtime.config.disabled_features,
            dependencies=_dependencies(CandidateArm.A),
            execution_record_digest=runtime.config.execution_record_digest,
            manifest_digest=runtime.config.manifest_digest,
            configuration_digest=runtime.config.configuration_digest,
            execution_identity="bakeoff_runner",
            identity_permissions=_IDENTITY_PERMISSIONS,
            ingress_contract_digest=_INGRESS_CONTRACT_DIGEST,
        )
    with pytest.raises(ValueError, match="identity"):
        BakeoffAppConfig(
            mode=RuntimeMode.CONNECTED,
            environment="bakeoff",
            disabled_features=runtime.config.disabled_features,
            dependencies=_dependencies(CandidateArm.B1),
            execution_record_digest=_digest("a"),
            manifest_digest=_digest("b"),
            configuration_digest=_digest("c"),
            execution_identity="production_admin",
            identity_permissions=frozenset({"production_firestore"}),
            ingress_contract_digest=_INGRESS_CONTRACT_DIGEST,
        )
    assert "provider_url" not in BakeoffAppConfig.__dataclass_fields__

    store = InMemoryVoiceAuthStore()
    missing_authenticator = VoiceSessionAuthenticator(
        store=store,
        envelope_verifier=_EnvelopeVerifier(binding),
        signature_verifier=_SignatureVerifier(),
        call_attestor=_CallAttestor(binding),
        canonical_urls={
            IngressKind.MEDIA_STREAM: "wss://bakeoff.invalid/media",
            IngressKind.CONVERSATION_RELAY: "wss://bakeoff.invalid/relay",
            IngressKind.CAPABILITY_PROBE: "wss://bakeoff.invalid/probe",
            IngressKind.RECONNECT: "wss://bakeoff.invalid/reconnect",
            IngressKind.TOKEN_ISSUER: "https://bakeoff.invalid/token",
            IngressKind.STATUS_CALLBACK: "https://bakeoff.invalid/status",
            IngressKind.EVIDENCE_CALLBACK: "https://bakeoff.invalid/evidence",
        },
    )
    with pytest.raises(ValueError, match="binding is not exact"):
        BakeoffIngressRuntime(
            config=runtime.config,
            authenticator=missing_authenticator,
            binding=binding,
            startup_now_ms=2,
        )


@pytest.mark.parametrize(
    ("arm", "connector"),
    (
        (CandidateArm.A, "Stream"),
        (CandidateArm.B2, "ConversationRelay"),
    ),
)
def test_token_issuer_binds_arm_and_renders_the_correct_isolated_twiml(
    arm: CandidateArm,
    connector: str,
):
    runtime, authenticator, signature = _connected(arm)
    binding = runtime.binding
    assert binding is not None
    assert authenticator.bind_attested_call(
        binding,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    app = create_voice_bakeoff_app(
        runtime=runtime,
        clock=MonotonicClock(initial_ms=3),
        ingress_reader=_ingress_reader(),
    )
    wrong = CandidateArm.C if arm is not CandidateArm.C else CandidateArm.A
    routes = {route.path: route for route in app.routes}
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(routes[f"/bakeoff/twiml/{wrong.value}"].endpoint(object()))
    assert rejected.value.status_code == 403
    response = asyncio.run(routes[f"/bakeoff/twiml/{arm.value}"].endpoint(object()))
    body = response.body.decode("utf-8")
    assert f"<{connector}>" in body
    assert "capability" in body


@pytest.mark.parametrize(
    "ingress",
    (
        IngressKind.MEDIA_STREAM,
        IngressKind.CONVERSATION_RELAY,
        IngressKind.CAPABILITY_PROBE,
        IngressKind.RECONNECT,
    ),
)
def test_every_websocket_ingress_uses_auth_pending_and_one_setup_only(
    ingress: IngressKind,
):
    arm = (
        CandidateArm.B2
        if ingress is IngressKind.CONVERSATION_RELAY
        else CandidateArm.B1
    )
    runtime, authenticator, signature = _connected(arm)
    binding = runtime.binding
    assert binding is not None
    assert authenticator.bind_attested_call(
        binding,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    capability = runtime.issue_capability(
        arm=arm,
        untrusted_request="signed",
        now_ms=3,
    )
    assert capability is not None
    signature.ingress = ingress
    wrong_arm = CandidateArm.A if arm is not CandidateArm.A else CandidateArm.C
    assert runtime.begin_handshake(
        arm=wrong_arm,
        ingress=ingress,
        untrusted_request="signed",
        now_ms=4,
    ).state is AuthState.REJECTED
    pending = runtime.begin_handshake(
        arm=arm,
        ingress=ingress,
        untrusted_request="signed",
        now_ms=4,
    )
    assert pending.state is AuthState.AUTH_PENDING
    setup = SetupEnvelope(
        stream_digest=_digest("2"),
        protected_token=capability.protected_token,
        byte_count=10,
    )
    assert runtime.consume_setup(setup, now_ms=5).authenticated
    assert runtime.consume_setup(setup, now_ms=6).state is AuthState.REJECTED


def test_callbacks_require_store_issued_bound_capability():
    runtime, authenticator, signature = _connected()
    binding = runtime.binding
    assert binding is not None
    assert authenticator.bind_attested_call(
        binding,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    capability = runtime.issue_capability(
        arm=CandidateArm.B1,
        untrusted_request="signed",
        now_ms=3,
    )
    assert capability is not None
    signature.ingress = IngressKind.MEDIA_STREAM
    assert runtime.begin_handshake(
        arm=CandidateArm.B1,
        ingress=IngressKind.MEDIA_STREAM,
        untrusted_request="signed",
        now_ms=4,
    ).state is AuthState.AUTH_PENDING
    assert runtime.consume_setup(
        SetupEnvelope(
            stream_digest=_digest("2"),
            protected_token=capability.protected_token,
        ),
        now_ms=5,
    ).authenticated
    callback = runtime.issue_callback_capability(
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        now_ms=6,
    )
    assert callback is not None
    signature.ingress = IngressKind.STATUS_CALLBACK
    assert not runtime.authorize_callback(
        untrusted_request="signed",
        stream_digest=_digest("3"),
        purpose=CallbackPurpose.STATUS,
        protected_capability=callback.protected_token,
        now_ms=7,
    ).authenticated
    replacement = runtime.issue_callback_capability(
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        now_ms=7,
    )
    assert replacement is not None
    accepted = runtime.authorize_callback(
        untrusted_request="signed",
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        protected_capability=replacement.protected_token,
        now_ms=8,
    )
    assert accepted.authenticated
    assert not runtime.authorize_callback(
        untrusted_request="signed",
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        protected_capability=replacement.protected_token,
        now_ms=9,
    ).authenticated


def test_normal_session_rotates_epoch_before_reconnect_capability():
    runtime, authenticator, signature = _connected()
    original = runtime.binding
    assert original is not None
    assert authenticator.bind_attested_call(
        original,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    capability = runtime.issue_capability(
        arm=CandidateArm.B1,
        untrusted_request="signed",
        now_ms=3,
    )
    assert capability is not None
    signature.ingress = IngressKind.MEDIA_STREAM
    assert runtime.begin_handshake(
        arm=CandidateArm.B1,
        ingress=IngressKind.MEDIA_STREAM,
        untrusted_request="signed",
        now_ms=4,
    ).state is AuthState.AUTH_PENDING
    assert runtime.consume_setup(
        SetupEnvelope(
            stream_digest=_digest("2"),
            protected_token=capability.protected_token,
        ),
        now_ms=5,
    ).authenticated

    replacement = replace(original, epoch=2)
    assert runtime.rotate_for_reconnect(
        new_binding=replacement,
        control_plane_result="created",
        now_ms=6,
    )
    assert runtime.binding == replacement
    assert not authenticator.accepts_active_execution(original, now_ms=7)
    signature.ingress = IngressKind.TOKEN_ISSUER
    reconnect_capability = runtime.issue_capability(
        arm=CandidateArm.B1,
        untrusted_request="signed",
        now_ms=7,
    )
    assert reconnect_capability is not None
    signature.ingress = IngressKind.RECONNECT
    reconnect_setup = (
        b'{"stream_digest":"'
        + _digest("5").encode("ascii")
        + b'","protected_token":"'
        + reconnect_capability.protected_token.encode("ascii")
        + b'"}'
    )
    app = create_voice_bakeoff_app(
        runtime=runtime,
        clock=MonotonicClock(initial_ms=8),
        ingress_reader=_ingress_reader(),
    )
    socket = _FakeWebSocket(setup=reconnect_setup)
    routes = {route.path: route for route in app.routes}
    asyncio.run(routes["/bakeoff/ws/reconnect/B1"].endpoint(socket))
    assert socket.sent == [{"state": AuthState.AUTHENTICATED.value}]
    assert socket.closed == [1000]
    assert not runtime.rotate_for_reconnect(
        new_binding=replacement,
        control_plane_result="created",
        now_ms=10,
    )


def test_websocket_route_enforces_real_deadline_and_revokes_pending_execution():
    runtime, authenticator, signature = _connected()
    binding = runtime.binding
    assert binding is not None
    assert authenticator.bind_attested_call(
        binding,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    assert runtime.issue_capability(
        arm=CandidateArm.B1,
        untrusted_request="signed",
        now_ms=3,
    )
    signature.ingress = IngressKind.MEDIA_STREAM
    socket = _FakeWebSocket(setup_delay_seconds=0.05)
    app = create_voice_bakeoff_app(
        runtime=runtime,
        clock=MonotonicClock(initial_ms=4),
        ingress_reader=_ingress_reader(),
    )
    routes = {route.path: route for route in app.routes}
    asyncio.run(routes["/bakeoff/ws/media/B1"].endpoint(socket))
    assert socket.accepted
    assert socket.closed == [4408]
    assert not authenticator.accepts_active_execution(binding, now_ms=5)


def test_websocket_route_cancellation_revokes_closes_and_reraises():
    runtime, authenticator, signature = _connected()
    binding = runtime.binding
    assert binding is not None
    assert authenticator.bind_attested_call(
        binding,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    assert runtime.issue_capability(
        arm=CandidateArm.B1,
        untrusted_request="signed",
        now_ms=3,
    )
    signature.ingress = IngressKind.MEDIA_STREAM
    socket = _FakeWebSocket(setup_delay_seconds=10)
    app = create_voice_bakeoff_app(
        runtime=runtime,
        clock=MonotonicClock(initial_ms=4),
        ingress_reader=_ingress_reader(),
    )
    routes = {route.path: route for route in app.routes}

    async def cancel_pending() -> None:
        task = asyncio.create_task(
            routes["/bakeoff/ws/media/B1"].endpoint(socket)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_pending())
    assert socket.accepted
    assert socket.closed == [4410]
    assert not authenticator.accepts_active_execution(binding, now_ms=5)


def test_actual_websocket_and_callback_routes_authenticate_only_bounded_payloads():
    runtime, authenticator, signature = _connected()
    binding = runtime.binding
    assert binding is not None
    assert authenticator.bind_attested_call(
        binding,
        control_plane_result="created",
        now_ms=2,
    )
    signature.ingress = IngressKind.TOKEN_ISSUER
    capability = runtime.issue_capability(
        arm=CandidateArm.B1,
        untrusted_request="signed",
        now_ms=3,
    )
    assert capability is not None
    setup = (
        b'{"stream_digest":"'
        + _digest("2").encode("ascii")
        + b'","protected_token":"'
        + capability.protected_token.encode("ascii")
        + b'"}'
    )
    reader = _ingress_reader()
    app = create_voice_bakeoff_app(
        runtime=runtime,
        clock=MonotonicClock(initial_ms=4),
        ingress_reader=reader,
    )
    signature.ingress = IngressKind.MEDIA_STREAM
    routes = {route.path: route for route in app.routes}
    socket = _FakeWebSocket(setup=setup)
    asyncio.run(routes["/bakeoff/ws/media/B1"].endpoint(socket))
    assert socket.sent == [{"state": AuthState.AUTHENTICATED.value}]
    assert socket.closed == [1000]

    callback = runtime.issue_callback_capability(
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.STATUS,
        now_ms=5,
    )
    assert callback is not None
    status_request = _FakeRequest(body=(
        b'{"stream_digest":"'
        + _digest("2").encode("ascii")
        + b'","protected_capability":"'
        + callback.protected_token.encode("ascii")
        + b'"}'
    ))
    signature.ingress = IngressKind.STATUS_CALLBACK
    assert asyncio.run(
        routes["/bakeoff/callback/status"].endpoint(status_request)
    ) == {"state": AuthState.AUTHENTICATED.value}

    evidence_capability = runtime.issue_callback_capability(
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.EVIDENCE,
        now_ms=6,
    )
    assert evidence_capability is not None
    wrong_purpose_request = _FakeRequest(body=(
        b'{"stream_digest":"'
        + _digest("2").encode("ascii")
        + b'","protected_capability":"'
        + evidence_capability.protected_token.encode("ascii")
        + b'"}'
    ))
    signature.ingress = IngressKind.STATUS_CALLBACK
    with pytest.raises(HTTPException) as wrong_purpose:
        asyncio.run(
            routes["/bakeoff/callback/status"].endpoint(
                wrong_purpose_request
            )
        )
    assert wrong_purpose.value.status_code == 403

    evidence_replacement = runtime.issue_callback_capability(
        stream_digest=_digest("2"),
        purpose=CallbackPurpose.EVIDENCE,
        now_ms=7,
    )
    assert evidence_replacement is not None
    evidence_request = _FakeRequest(body=(
        b'{"stream_digest":"'
        + _digest("2").encode("ascii")
        + b'","protected_capability":"'
        + evidence_replacement.protected_token.encode("ascii")
        + b'"}'
    ))
    signature.ingress = IngressKind.EVIDENCE_CALLBACK
    assert asyncio.run(
        routes["/bakeoff/callback/evidence"].endpoint(evidence_request)
    ) == {"state": AuthState.AUTHENTICATED.value}


def test_callback_route_defensively_rejects_a_broken_oversize_reader():
    runtime, _, _ = _connected()
    reader = _ingress_reader()
    app = create_voice_bakeoff_app(
        runtime=runtime,
        clock=MonotonicClock(),
        ingress_reader=reader,
    )
    routes = {route.path: route for route in app.routes}
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            routes["/bakeoff/callback/status"].endpoint(
                _FakeRequest(body=b"x" * 4_097)
            )
        )
    assert rejected.value.status_code == 413


def test_factory_requires_exact_server_preallocation_limits():
    runtime, _, _ = _connected()
    wrong_limits = VerifiedServerIngressLimits(
        max_websocket_message_bytes=513,
        max_http_body_bytes=4_096,
        reader_code_digest=ServerBoundedIngressReader.code_digest(),
    )
    reader = ServerBoundedIngressReader(wrong_limits)
    with pytest.raises(ValueError, match="server-level"):
        create_voice_bakeoff_app(
            runtime=runtime,
            clock=MonotonicClock(),
            ingress_reader=reader,
        )
    with pytest.raises(ValueError, match="reader code"):
        ServerBoundedIngressReader(
            replace(
                wrong_limits,
                max_websocket_message_bytes=512,
                reader_code_digest="0" * 64,
            )
        )

    class StructuralImpostor:
        limits = _ingress_reader().limits

        async def receive_setup_bytes(self, websocket: object) -> bytearray:
            return bytearray()

        async def receive_callback_bytes(self, request: object) -> bytearray:
            return bytearray()

    with pytest.raises(ValueError, match="bounded ingress"):
        create_voice_bakeoff_app(
            runtime=runtime,
            clock=MonotonicClock(),
            ingress_reader=StructuralImpostor(),
        )


def test_production_import_graph_cannot_discover_bakeoff_entrypoint():
    target = "app.experiments.voice_bakeoff_app"
    reachable = _reachable_local_modules("app.main")
    assert "app.main" in reachable
    assert target not in reachable
    for module in reachable:
        path = _module_path(module)
        assert path is not None
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = _imported_modules(
            source,
            module_name=module,
        )
        strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not any(_targets(module, target) for module in imported)
        assert target not in strings


def test_isolation_resolver_catches_relative_from_and_dynamic_imports():
    source = """
from app.experiments import voice_bakeoff_app
from ..experiments.voice_bakeoff_app import create_voice_bakeoff_app
import importlib as il
from importlib import import_module as load
il.import_module("app.experiments.voice_bakeoff_app")
load("app.webhooks.media_stream")
__import__("twilio.rest")
"""
    modules = _imported_modules(source, module_name="app.services.fixture")
    assert {
        "app.experiments.voice_bakeoff_app",
        "app.webhooks.media_stream",
        "twilio.rest",
    } <= modules


def test_app_and_harness_have_no_provider_network_or_live_route_imports():
    forbidden = (
        "google",
        "twilio",
        "deepgram",
        "elevenlabs",
        "socket",
        "requests",
        "httpx",
        "websockets",
        "subprocess",
        "app.main",
        "app.webhooks",
        "app.services.gemini_pipeline",
        "app.services.voice_pipeline",
    )
    for path in (
        Path("app/experiments/voice_bakeoff_app.py"),
        Path("scripts/voice_bakeoff_caller.py"),
    ):
        modules = _imported_modules(
            path.read_text(encoding="utf-8"),
            module_name=".".join(path.with_suffix("").parts),
        )
        assert not {
            module
            for module in modules
            if any(_targets(module, target) for target in forbidden)
        }
