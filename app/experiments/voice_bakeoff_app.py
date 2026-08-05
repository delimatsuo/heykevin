"""Factory-built, offline-qualified application boundary for the voice bakeoff.

Importing this module constructs no application, provider, route, client, task, or
network connection. A future approved runner must inject the verified Task-2.1
authentication facade and its exact nonproduction execution binding.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import threading
from dataclasses import dataclass
from enum import Enum

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from app.services.voice_session_auth import (
    AuthFailure,
    AuthResult,
    AuthState,
    CallbackPurpose,
    CandidateArm,
    ExecutionBinding,
    IngressKind,
    IssuedCapability,
    PreAuthFrame,
    PreAuthFrameKind,
    SetupEnvelope,
    VoiceSessionAuthenticator,
)


_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_]{1,128}")
_MAX_CALLBACK_BODY_BYTES = 4_096
_CALLBACK_READ_TIMEOUT_SECONDS = 1.0
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
_DEPENDENCY_ROLES = {
    CandidateArm.A: frozenset({"telephony", "native_voice"}),
    CandidateArm.B1: frozenset(
        {"telephony", "speech_to_text", "text_generation", "text_to_speech"}
    ),
    CandidateArm.B2: frozenset(
        {"telephony", "conversation_relay", "text_generation"}
    ),
    CandidateArm.C: frozenset({"telephony", "native_voice"}),
}
_NONPRODUCTION_PERMISSIONS = frozenset(
    {"bakeoff_auth_store", "bakeoff_evidence_store"}
)


class RuntimeMode(str, Enum):
    DRY_RUN = "dry_run"
    CONNECTED = "connected"


@dataclass(frozen=True, slots=True)
class BakeoffDependency:
    role: str
    provider_ref: str
    version_ref: str
    endpoint_ref: str
    destination_allowlist_ref: str
    credential_ref: str
    account_region_ref: str
    nonproduction_identity_ref: str
    privacy_posture_ref: str

    def __post_init__(self) -> None:
        for name in (
            "role",
            "provider_ref",
            "version_ref",
            "endpoint_ref",
            "destination_allowlist_ref",
            "credential_ref",
            "account_region_ref",
            "nonproduction_identity_ref",
            "privacy_posture_ref",
        ):
            if not isinstance(getattr(self, name), str) or not _IDENTIFIER_PATTERN.fullmatch(
                getattr(self, name)
            ):
                raise ValueError(f"{name} must be an opaque identifier")


def connected_configuration_digest(
    *,
    dependencies: tuple[BakeoffDependency, ...],
    execution_identity: str,
    identity_permissions: frozenset[str],
    ingress_contract_digest: str,
    disabled_features: frozenset[str],
    capability_ttl_ms: int,
) -> str:
    """Digest the complete connected resource and identity inventory."""
    material = {
        "dependencies": [
            {
                name: getattr(dependency, name)
                for name in dependency.__dataclass_fields__
            }
            for dependency in sorted(dependencies, key=lambda item: item.role)
        ],
        "execution_identity": execution_identity,
        "identity_permissions": sorted(identity_permissions),
        "ingress_contract_digest": ingress_contract_digest,
        "disabled_features": sorted(disabled_features),
        "capability_ttl_ms": capability_ttl_ms,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BakeoffAppConfig:
    mode: RuntimeMode
    environment: str
    disabled_features: frozenset[str]
    dependencies: tuple[BakeoffDependency, ...] = ()
    execution_record_digest: str | None = None
    manifest_digest: str | None = None
    configuration_digest: str | None = None
    execution_identity: str | None = None
    identity_permissions: frozenset[str] = frozenset()
    ingress_contract_digest: str | None = None
    capability_ttl_ms: int = 1_000

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode) or self.environment != "bakeoff":
            raise ValueError("voice bakeoff environment must be bakeoff")
        if self.disabled_features != _DISABLED_FEATURES:
            raise ValueError("all risky features must remain disabled")
        if (
            isinstance(self.capability_ttl_ms, bool)
            or not isinstance(self.capability_ttl_ms, int)
            or self.capability_ttl_ms < 1
        ):
            raise ValueError("capability TTL must be positive")
        if self.mode is RuntimeMode.DRY_RUN:
            if (
                self.dependencies
                or self.execution_record_digest is not None
                or self.manifest_digest is not None
                or self.configuration_digest is not None
                or self.execution_identity is not None
                or self.identity_permissions
                or self.ingress_contract_digest is not None
            ):
                raise ValueError("dry-run configuration cannot enable external resources")
            return
        for name in (
            "execution_record_digest",
            "manifest_digest",
            "configuration_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
                raise ValueError(f"{name} is invalid")
        if (
            not isinstance(self.execution_identity, str)
            or not _IDENTIFIER_PATTERN.fullmatch(self.execution_identity)
            or self.identity_permissions != _NONPRODUCTION_PERMISSIONS
            or not isinstance(self.ingress_contract_digest, str)
            or not _DIGEST_PATTERN.fullmatch(self.ingress_contract_digest)
        ):
            raise ValueError("execution identity is not nonproduction-bounded")
        roles = [dependency.role for dependency in self.dependencies]
        if len(roles) != len(set(roles)):
            raise ValueError("dependency roles must be unique")
        if self.configuration_digest != connected_configuration_digest(
            dependencies=self.dependencies,
            execution_identity=self.execution_identity,
            identity_permissions=self.identity_permissions,
            ingress_contract_digest=self.ingress_contract_digest,
            disabled_features=self.disabled_features,
            capability_ttl_ms=self.capability_ttl_ms,
        ):
            raise ValueError("connected configuration is not approval-bound")

    @classmethod
    def dry_run(cls) -> "BakeoffAppConfig":
        return cls(
            mode=RuntimeMode.DRY_RUN,
            environment="bakeoff",
            disabled_features=_DISABLED_FEATURES,
        )


def execution_binding_digest(binding: ExecutionBinding) -> str:
    material = {
        name: (
            getattr(binding, name).value
            if isinstance(getattr(binding, name), Enum)
            else getattr(binding, name)
        )
        for name in binding.__dataclass_fields__
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BakeoffIngressRuntime:
    """Auth-only route runtime; it owns no provider or application datastore."""

    def __init__(
        self,
        *,
        config: BakeoffAppConfig,
        authenticator: VoiceSessionAuthenticator | None = None,
        binding: ExecutionBinding | None = None,
        startup_now_ms: int = 0,
    ) -> None:
        if not isinstance(config, BakeoffAppConfig):
            raise ValueError("bakeoff app config is invalid")
        self.config = config
        self._authenticator = authenticator
        self._binding = binding
        self._binding_lock = threading.Lock()
        if config.mode is RuntimeMode.DRY_RUN:
            if authenticator is not None or binding is not None or startup_now_ms != 0:
                raise ValueError("dry-run runtime cannot receive connected authority")
            return
        if (
            not isinstance(authenticator, VoiceSessionAuthenticator)
            or not isinstance(binding, ExecutionBinding)
            or binding.environment != "bakeoff"
            or config.execution_record_digest != execution_binding_digest(binding)
            or config.manifest_digest != binding.manifest_digest
            or config.configuration_digest != binding.configuration_digest
            or frozenset(dependency.role for dependency in config.dependencies)
            != _DEPENDENCY_ROLES[binding.candidate_arm]
            or not authenticator.claim_active_execution(
                binding,
                runtime_claim_digest=config.execution_record_digest,
                now_ms=startup_now_ms,
            )
        ):
            raise ValueError("connected runtime binding is not exact")

    @property
    def connected(self) -> bool:
        return self.config.mode is RuntimeMode.CONNECTED

    @property
    def binding(self) -> ExecutionBinding | None:
        with self._binding_lock:
            return self._binding

    def rotate_for_reconnect(
        self,
        *,
        new_binding: ExecutionBinding,
        control_plane_result: object,
        now_ms: int,
    ) -> bool:
        """Fail-closed epoch rotation used before issuing a reconnect capability."""
        if self._authenticator is None:
            return False
        with self._binding_lock:
            old_binding = self._binding
            if (
                not isinstance(old_binding, ExecutionBinding)
                or not isinstance(new_binding, ExecutionBinding)
                or new_binding.configuration_digest
                != self.config.configuration_digest
                or new_binding.manifest_digest != self.config.manifest_digest
                or not self._authenticator.advance_epoch(
                    old_binding,
                    new_binding,
                    now_ms=now_ms,
                )
            ):
                return False
            if not self._authenticator.bind_attested_call(
                new_binding,
                control_plane_result=control_plane_result,
                now_ms=now_ms,
            ):
                self._authenticator.revoke(new_binding)
                return False
            self._binding = new_binding
            return True

    def issue_capability(
        self,
        *,
        arm: CandidateArm,
        untrusted_request: object,
        now_ms: int,
    ) -> IssuedCapability | None:
        binding = self.binding
        if (
            self._authenticator is None
            or binding is None
            or binding.candidate_arm is not arm
        ):
            return None
        return self._authenticator.issue_verified_capability(
            binding,
            untrusted_request=untrusted_request,
            now_ms=now_ms,
            ttl_ms=self.config.capability_ttl_ms,
        )

    def begin_handshake(
        self,
        *,
        arm: CandidateArm,
        ingress: IngressKind,
        untrusted_request: object,
        now_ms: int,
    ) -> AuthResult:
        binding = self.binding
        if (
            self._authenticator is None
            or binding is None
            or binding.candidate_arm is not arm
        ):
            return AuthResult(AuthState.REJECTED, AuthFailure.BINDING_MISMATCH)
        return self._authenticator.begin_verified_handshake(
            binding,
            ingress=ingress,
            untrusted_request=untrusted_request,
            now_ms=now_ms,
        )

    def consume_setup(self, setup: SetupEnvelope, *, now_ms: int) -> AuthResult:
        binding = self.binding
        if self._authenticator is None or binding is None:
            return AuthResult(AuthState.REJECTED, AuthFailure.MISSING_EXECUTION)
        return self._authenticator.consume_setup(
            binding,
            setup,
            now_ms=now_ms,
        )

    def reject_pre_auth(
        self,
        frame: PreAuthFrame,
        *,
        now_ms: int,
    ) -> AuthResult:
        binding = self.binding
        if self._authenticator is None or binding is None:
            return AuthResult(AuthState.REJECTED, AuthFailure.MISSING_EXECUTION)
        return self._authenticator.reject_pre_auth_frame(
            binding,
            frame,
            now_ms=now_ms,
        )

    def abort_pending(self, *, now_ms: int) -> None:
        binding = self.binding
        if self._authenticator is None or binding is None:
            return
        self._authenticator.reject_pre_auth_frame(
            binding,
            PreAuthFrame(PreAuthFrameKind.OTHER),
            now_ms=now_ms,
        )
        self._authenticator.revoke(binding)

    def authorize_callback(
        self,
        *,
        untrusted_request: object,
        stream_digest: str,
        purpose: CallbackPurpose,
        protected_capability: str,
        now_ms: int,
    ) -> AuthResult:
        binding = self.binding
        if self._authenticator is None or binding is None:
            return AuthResult(AuthState.REJECTED, AuthFailure.MISSING_EXECUTION)
        if (
            not isinstance(stream_digest, str)
            or not _DIGEST_PATTERN.fullmatch(stream_digest)
            or not isinstance(protected_capability, str)
            or len(protected_capability) < 16
        ):
            return AuthResult(AuthState.REJECTED, AuthFailure.CALLBACK_INVALID)
        return self._authenticator.authorize_verified_callback(
            binding,
            untrusted_request=untrusted_request,
            stream_digest=stream_digest,
            purpose=purpose,
            protected_capability=protected_capability,
            now_ms=now_ms,
        )

    def issue_callback_capability(
        self,
        *,
        stream_digest: str,
        purpose: CallbackPurpose,
        now_ms: int,
    ) -> IssuedCapability | None:
        binding = self.binding
        if (
            self._authenticator is None
            or binding is None
            or not isinstance(stream_digest, str)
            or not _DIGEST_PATTERN.fullmatch(stream_digest)
        ):
            return None
        return self._authenticator.issue_callback_capability(
            binding,
            stream_digest=stream_digest,
            purpose=purpose,
            now_ms=now_ms,
            ttl_ms=self.config.capability_ttl_ms,
        )


class MonotonicClock:
    def __init__(self, *, initial_ms: int = 0) -> None:
        if isinstance(initial_ms, bool) or not isinstance(initial_ms, int) or initial_ms < 0:
            raise ValueError("initial clock value is invalid")
        self._now_ms = initial_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, delta_ms: int) -> int:
        if isinstance(delta_ms, bool) or not isinstance(delta_ms, int) or delta_ms < 0:
            raise ValueError("clock delta is invalid")
        self._now_ms += delta_ms
        return self._now_ms


@dataclass(frozen=True, slots=True)
class VerifiedServerIngressLimits:
    """Exact limits shared by the concrete reader and future server launcher."""

    max_websocket_message_bytes: int
    max_http_body_bytes: int
    reader_code_digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_websocket_message_bytes, bool)
            or not isinstance(self.max_websocket_message_bytes, int)
            or self.max_websocket_message_bytes < 1
            or isinstance(self.max_http_body_bytes, bool)
            or not isinstance(self.max_http_body_bytes, int)
            or self.max_http_body_bytes < 1
            or not _DIGEST_PATTERN.fullmatch(self.reader_code_digest)
        ):
            raise ValueError("verified server ingress limits are invalid")

    @property
    def contract_digest(self) -> str:
        encoded = json.dumps(
            {
                "max_http_body_bytes": self.max_http_body_bytes,
                "max_websocket_message_bytes": self.max_websocket_message_bytes,
                "reader_code_digest": self.reader_code_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ServerBoundedIngressReader:
    """Concrete ASGI reader whose code and server limits are approval-bound."""

    def __init__(self, limits: VerifiedServerIngressLimits) -> None:
        if (
            not isinstance(limits, VerifiedServerIngressLimits)
            or limits.reader_code_digest != self.code_digest()
        ):
            raise ValueError("server reader code is not verified")
        self._limits = limits

    @classmethod
    def code_digest(cls) -> str:
        return hashlib.sha256(inspect.getsource(cls).encode("utf-8")).hexdigest()

    @property
    def limits(self) -> VerifiedServerIngressLimits:
        return self._limits

    async def receive_setup_bytes(self, websocket: WebSocket) -> bytearray:
        message = await websocket.receive()
        if not isinstance(message, dict):
            raise ValueError("unexpected WebSocket setup message")
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if message.get("type") != "websocket.receive":
            raise ValueError("unexpected WebSocket setup message")
        text = message.get("text")
        binary = message.get("bytes")
        if (text is None) == (binary is None):
            raise ValueError("setup must contain exactly one payload")
        raw = text.encode("utf-8") if isinstance(text, str) else binary
        if (
            not isinstance(raw, bytes)
            or len(raw) > self._limits.max_websocket_message_bytes
        ):
            raise ValueError("setup exceeds verified server limit")
        return bytearray(raw)

    async def receive_callback_bytes(self, request: Request) -> bytearray:
        body = bytearray()
        async for chunk in request.stream():
            if (
                not isinstance(chunk, bytes)
                or len(body) + len(chunk) > self._limits.max_http_body_bytes
            ):
                raise HTTPException(status_code=413)
            body.extend(chunk)
        return body


def create_voice_bakeoff_app(
    *,
    runtime: BakeoffIngressRuntime,
    clock: MonotonicClock,
    ingress_reader: ServerBoundedIngressReader,
) -> FastAPI:
    """Create the isolated route table without starting a server."""
    if (
        not isinstance(runtime, BakeoffIngressRuntime)
        or not isinstance(clock, MonotonicClock)
        or type(ingress_reader) is not ServerBoundedIngressReader
    ):
        raise ValueError("bakeoff runtime, clock, and bounded ingress are required")
    if not runtime.connected:
        raise ValueError("dry-run cannot construct a network route table")
    binding = runtime.binding
    limits = ingress_reader.limits
    if (
        binding is None
        or limits.contract_digest != runtime.config.ingress_contract_digest
        or limits.reader_code_digest != ServerBoundedIngressReader.code_digest()
        or limits.max_websocket_message_bytes != binding.max_setup_bytes
        or limits.max_http_body_bytes != _MAX_CALLBACK_BODY_BYTES
    ):
        raise ValueError("server-level ingress limits are not exact")
    app = FastAPI(
        title="Hey Kevin Voice Bakeoff",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bakeoff_runtime = runtime

    def register_token_issuer(arm: CandidateArm) -> None:
        async def issue_token(request: Request) -> Response:
            capability = runtime.issue_capability(
                arm=arm,
                untrusted_request=request,
                now_ms=clock.now_ms(),
            )
            if capability is None:
                raise HTTPException(status_code=403)
            connector = (
                "ConversationRelay"
                if arm is CandidateArm.B2
                else "Stream"
            )
            body = (
                f"<Response><Connect><{connector}>"
                f'<Parameter name="capability" value="{capability.protected_token}"/>'
                f"</{connector}></Connect></Response>"
            )
            return Response(content=body, media_type="application/xml")

        app.post(
            f"/bakeoff/twiml/{arm.value}",
            name=f"bakeoff_token_{arm.value}",
        )(issue_token)

    for candidate_arm in CandidateArm:
        register_token_issuer(candidate_arm)

    async def websocket_endpoint(
        websocket: WebSocket,
        *,
        arm: CandidateArm,
        ingress: IngressKind,
    ) -> None:
        pending = runtime.begin_handshake(
            arm=arm,
            ingress=ingress,
            untrusted_request=websocket,
            now_ms=clock.now_ms(),
        )
        if pending.state is not AuthState.AUTH_PENDING:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        raw_bytes = bytearray()
        try:
            current_binding = runtime.binding
            if current_binding is None:
                raise ValueError("active binding is missing")
            async with asyncio.timeout(
                current_binding.max_setup_wait_ms / 1_000
            ):
                raw_bytes = await ingress_reader.receive_setup_bytes(websocket)
            if not isinstance(raw_bytes, bytearray):
                raise TypeError("bounded ingress must return an erasable buffer")
            if (
                runtime.binding != current_binding
                or len(raw_bytes) > current_binding.max_setup_bytes
            ):
                runtime.abort_pending(now_ms=clock.now_ms())
                await websocket.close(code=4400)
                return
            raw = json.loads(bytes(raw_bytes))
            if not isinstance(raw, dict) or set(raw) != {
                "stream_digest",
                "protected_token",
            }:
                runtime.reject_pre_auth(
                    PreAuthFrame(
                        PreAuthFrameKind.MEDIA
                        if isinstance(raw, dict) and raw.get("kind") == "media"
                        else PreAuthFrameKind.OTHER
                    ),
                    now_ms=clock.now_ms(),
                )
                await websocket.close(code=4400)
                return
            setup = SetupEnvelope(
                stream_digest=raw["stream_digest"],
                protected_token=raw["protected_token"],
                frame_count=1,
                byte_count=len(raw_bytes),
            )
        except TimeoutError:
            runtime.abort_pending(now_ms=clock.now_ms())
            await websocket.close(code=4408)
            return
        except asyncio.CancelledError:
            runtime.abort_pending(now_ms=clock.now_ms())
            await asyncio.shield(websocket.close(code=4410))
            raise
        except WebSocketDisconnect:
            runtime.abort_pending(now_ms=clock.now_ms())
            return
        except (TypeError, ValueError):
            runtime.reject_pre_auth(
                PreAuthFrame(PreAuthFrameKind.OTHER),
                now_ms=clock.now_ms(),
            )
            await websocket.close(code=4400)
            return
        finally:
            for index in range(len(raw_bytes)):
                raw_bytes[index] = 0
        result = runtime.consume_setup(setup, now_ms=clock.now_ms())
        if not result.authenticated:
            await websocket.close(code=4403)
            return
        await websocket.send_json({"state": AuthState.AUTHENTICATED.value})
        await websocket.close(code=1000)

    def register_websocket(
        path: str,
        arm: CandidateArm,
        ingress: IngressKind,
    ) -> None:
        async def endpoint(websocket: WebSocket) -> None:
            await websocket_endpoint(websocket, arm=arm, ingress=ingress)

        app.websocket(path, name=f"bakeoff_{ingress.value}_{path}")(endpoint)

    for arm in (CandidateArm.A, CandidateArm.B1, CandidateArm.C):
        register_websocket(
            f"/bakeoff/ws/media/{arm.value}",
            arm,
            IngressKind.MEDIA_STREAM,
        )
    register_websocket(
        "/bakeoff/ws/relay/B2",
        CandidateArm.B2,
        IngressKind.CONVERSATION_RELAY,
    )
    for arm in CandidateArm:
        register_websocket(
            f"/bakeoff/ws/probe/{arm.value}",
            arm,
            IngressKind.CAPABILITY_PROBE,
        )
        register_websocket(
            f"/bakeoff/ws/reconnect/{arm.value}",
            arm,
            IngressKind.RECONNECT,
        )

    def register_callback(path: str, purpose: CallbackPurpose) -> None:
        async def callback_endpoint(request: Request) -> dict[str, str]:
            body = bytearray()
            try:
                async with asyncio.timeout(_CALLBACK_READ_TIMEOUT_SECONDS):
                    body = await ingress_reader.receive_callback_bytes(request)
                if not isinstance(body, bytearray):
                    raise TypeError("bounded ingress must return an erasable buffer")
                if len(body) > _MAX_CALLBACK_BODY_BYTES:
                    raise HTTPException(status_code=413)
                raw = json.loads(bytes(body))
            except TimeoutError as exc:
                raise HTTPException(status_code=408) from exc
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(status_code=400) from exc
            finally:
                for index in range(len(body)):
                    body[index] = 0
            if not isinstance(raw, dict) or set(raw) != {
                "stream_digest",
                "protected_capability",
            }:
                raise HTTPException(status_code=400)
            result = runtime.authorize_callback(
                untrusted_request=request,
                stream_digest=raw["stream_digest"],
                purpose=purpose,
                protected_capability=raw["protected_capability"],
                now_ms=clock.now_ms(),
            )
            if not result.authenticated:
                raise HTTPException(status_code=403)
            return {"state": AuthState.AUTHENTICATED.value}

        app.post(path, name=f"bakeoff_{purpose.value}_callback")(
            callback_endpoint
        )

    register_callback("/bakeoff/callback/status", CallbackPurpose.STATUS)
    register_callback("/bakeoff/callback/evidence", CallbackPurpose.EVIDENCE)
    return app


__all__ = [
    "BakeoffAppConfig",
    "BakeoffDependency",
    "BakeoffIngressRuntime",
    "MonotonicClock",
    "RuntimeMode",
    "ServerBoundedIngressReader",
    "VerifiedServerIngressLimits",
    "connected_configuration_digest",
    "create_voice_bakeoff_app",
    "execution_binding_digest",
]
