"""Sealed, synchronous, synthetic-only driver for offline voice composition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock
from types import MappingProxyType

from app.services.caller_observation_extractor import (
    BackendOutcome,
    BackendResponse,
    ExtractionRequest,
    ObservationExtractor,
)
from app.services.receptionist_state import IntakeState
from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_materializer import FixedProposalMaterializer
from app.services.voice_bakeoff_turn_composition import (
    AdapterImplementationBinding,
    CompositionPolicy,
    CompositionResult,
    CompositionStatus,
    FinalTurnAdmissionAuthority,
    FinalTurnAdmissionReceipt,
    TurnCompositionTransaction,
    VersionedIntakeStore,
    final_turn_content_digest,
)
from app.services.voice_call_lifecycle import CallLifecycle
from app.services.voice_candidates import (
    AdapterResult,
    CandidateLimits,
    CandidateUsage,
    EventContext,
    OfflineCandidateAdapter,
)
from app.services.voice_candidates.chained_streaming import (
    ChainedSignal,
    ChainedSignalKind,
    ChainedStreamingAdapter,
)
from app.services.voice_candidates.conversation_relay import (
    ConversationRelayAdapter,
    RelaySignal,
    RelaySignalKind,
)
from app.services.voice_candidates.manual_native import (
    ManualNativeAdapter,
    ManualNativeSignal,
    ManualNativeSignalKind,
)
from app.services.voice_candidates.native_gemini import (
    NativeGeminiAdapter,
    NativeMode,
    NativeSignal,
    NativeSignalKind,
)
from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSessionBinding,
    VoiceSource,
)
from app.services.voice_session_auth import CandidateArm
from app.services.voice_speech_control import SpeechControl, SpeechPolicy

_DRIVER_DOMAIN = b"hey-kevin/offline-session-driver/v1\x00"
_LEASE_DOMAIN = b"hey-kevin/offline-session-lease/v1\x00"
_AUDIO_DOMAIN = b"hey-kevin/offline-synthetic-audio/v1\x00"
_EXTRACTOR_DIGEST = hashlib.sha256(
    b"hey-kevin/offline-fixed-observation-backend/v1"
).hexdigest()
_CODEC = "mulaw_8000_mono"
_FRAME_SCHEMA = "ordinal:u32,duration_ms:u16,payload:mutable-bytes"
_DRIVER_SOURCE_DIGEST = "78f364a8c29aceae72e405f1d6359c64dc7af2e93438771184b5256ab4e503a2"
_FACADE_CODE_DIGEST = "e5c523ecc88ff95cf173d6d4223173ef7d674814cfc1ec39dfbbd3422973a200"
_TRACE_LOCALES = frozenset({"en", "es", "pt", "zh"})
_FIXTURE_LOCALES = _TRACE_LOCALES | {"fr"}
_ASSEMBLY_CODE_DIGESTS = MappingProxyType({
    "caller_observation_extractor":
        "a3d79ce19f2c603f47dd370789773a707016a07e430c58fcd389a67065f9d364",
    "composition":
        "69d5427e6a8f38199833b93a0279d2c714f55324e5212d8a0f8cfcf5d5f67c2e",
    "dialogue_planner":
        "222f332a8756eec4144bfe9ede7bb5dbae71cdbfd1ca8505df58b862dc953457",
    "materializer":
        "4526aa31b979b544a5de761b9ea7f8ac4abfaa4d913044840629ef2f8a88d998",
    "receptionist_state":
        "8f26251f7d9e1e534acf6178d1d5bcefac2efb87016e3b34b881decea2294845",
    "voice_bakeoff_coordinator":
        "2a408b27c60c89091ef99261f3cd539ac1149ecc19102f55456da3d0ca2c6898",
    "voice_call_lifecycle":
        "875f0edddc5560510f906eb940bb9f1e38ed3ccf2dbfac722db59d85367aeda0",
    "voice_candidates_base":
        "1d96a31d07966f48ebb528cdd4618bc5a2c0cc7911323bef8d56431c839c7cfe",
    "voice_lifecycle":
        "bed3dfade722e448690b61feb369fdf3d7d587f14e94bcc56adc66cdc2782213",
    "voice_session_auth":
        "ecb56c931cfb8ddc2c8a70ef37d5e0b0834fece7382de5482aef8c7e914cf1ae",
    "voice_speech_control":
        "85eed871597b0e4463cfe0f1c5d4fd081fbe6d92c4089efc692a08a4a2b74311",
})
_ADAPTER_TYPES = MappingProxyType({
    CandidateArm.A: NativeGeminiAdapter,
    CandidateArm.B1: ChainedStreamingAdapter,
    CandidateArm.B2: ConversationRelayAdapter,
    CandidateArm.C: ManualNativeAdapter,
})
_ADAPTER_CODE_DIGESTS = MappingProxyType({
    CandidateArm.A:
        "d0fbf60762126fbc4c7675cd82544a9856e97b81d22567e03295c4cd827c6ea1",
    CandidateArm.B1:
        "cb54b6d79508469e4e26a96fb1a779bf0910031aacde25490137f08d469c0e9c",
    CandidateArm.B2:
        "e8adbcff7c0df53ac46666bf552bc48fb032f0eeb9b4df064bd57afd31e5612b",
    CandidateArm.C:
        "5c707edca3b9e9f59fea0a39d6b39a401bd8e0874506f21955f9f8c7598efc95",
})


class OfflineSessionState(str, Enum):
    CREATED = "created"
    LEASED = "leased"
    ACTIVE = "active"
    CLOSED = "closed"
    ABORTED = "aborted"


class SyntheticJourney(str, Enum):
    DIRECT_ANSWER = "direct_answer"
    QUESTION_ONLY = "question_only"
    LOW_CONFIDENCE_REPAIR = "low_confidence_repair"
    SAFETY_GUIDANCE = "safety_guidance"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    SUPERSEDING_TURN = "superseding_turn"
    BIDIRECTIONAL_CODE_SWITCH = "bidirectional_code_switch"
    REPAIR_EXHAUSTION = "repair_exhaustion"


class DriverFailure(str, Enum):
    INVALID_LEASE = "invalid_lease"
    EXPIRED_LEASE = "expired_lease"
    BUSY = "busy"
    RESOURCE_LIMIT = "resource_limit"
    ASSEMBLY = "assembly"
    COMPOSITION = "composition"
    DELIVERY = "delivery"
    INTERNAL = "internal"


class TraceKind(str, Enum):
    LEASE_ACCEPTED = "lease_accepted"
    INPUT_FINAL = "input_final"
    RESPONSE_PENDING = "response_pending"
    REPAIR_PENDING = "repair_pending"
    SUPERSEDED = "superseded"
    ACT_CONFIRMED = "act_confirmed"
    TRANSPORT_RESOLVED = "transport_resolved"
    PLAYBACK_OBSERVED = "playback_observed"
    RESPONSE_OBSERVED = "response_observed"
    TERMINAL = "terminal"
    BUFFERS_SCRUBBED = "buffers_scrubbed"


@dataclass(frozen=True, slots=True)
class OfflineSessionLimits:
    max_inbound_frame_bytes: int = 320
    max_outbound_frame_bytes: int = 320
    max_inbound_frames: int = 16
    max_outbound_frames: int = 16
    max_inbound_bytes: int = 5_120
    max_outbound_bytes: int = 5_120
    max_inbound_audio_ms: int = 320
    max_outbound_audio_ms: int = 320
    max_session_ms: int = 2_000
    max_queue_depth: int = 16
    max_concurrency: int = 1
    lease_ttl_ms: int = 1_000

    def __post_init__(self) -> None:
        values = (
            self.max_inbound_frame_bytes,
            self.max_outbound_frame_bytes,
            self.max_inbound_frames,
            self.max_outbound_frames,
            self.max_inbound_bytes,
            self.max_outbound_bytes,
            self.max_inbound_audio_ms,
            self.max_outbound_audio_ms,
            self.max_session_ms,
            self.max_queue_depth,
            self.max_concurrency,
            self.lease_ttl_ms,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("offline session limits must be positive exact integers")
        if self.max_concurrency != 1:
            raise ValueError("offline session concurrency must remain one")
        if (
            self.max_inbound_bytes
            < self.max_inbound_frame_bytes
            or self.max_outbound_bytes
            < self.max_outbound_frame_bytes
        ):
            raise ValueError("inbound byte limit cannot be smaller than one frame")

    @property
    def contract_digest(self) -> str:
        return _digest_json(
            {
                "lease_ttl_ms": self.lease_ttl_ms,
                "max_concurrency": self.max_concurrency,
                "max_inbound_audio_ms": self.max_inbound_audio_ms,
                "max_inbound_bytes": self.max_inbound_bytes,
                "max_inbound_frame_bytes":
                    self.max_inbound_frame_bytes,
                "max_inbound_frames": self.max_inbound_frames,
                "max_outbound_audio_ms":
                    self.max_outbound_audio_ms,
                "max_outbound_bytes": self.max_outbound_bytes,
                "max_outbound_frame_bytes":
                    self.max_outbound_frame_bytes,
                "max_outbound_frames": self.max_outbound_frames,
                "max_queue_depth": self.max_queue_depth,
                "max_session_ms": self.max_session_ms,
            }
        )


@dataclass(frozen=True, slots=True)
class OfflineTraceEvent:
    ordinal: int
    kind: TraceKind
    semantic_act_kind: VoiceSemanticActKind | None = None
    composition_status: CompositionStatus | None = None
    locale: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int
            or self.ordinal < 0
            or not isinstance(self.kind, TraceKind)
            or (
                self.semantic_act_kind is not None
                and not isinstance(
                    self.semantic_act_kind,
                    VoiceSemanticActKind,
                )
            )
            or (
                self.composition_status is not None
                and not isinstance(
                    self.composition_status,
                    CompositionStatus,
                )
            )
            or (
                self.locale is not None
                and self.locale not in _TRACE_LOCALES
            )
        ):
            raise ValueError("offline trace event is invalid")


@dataclass(frozen=True, slots=True)
class OfflineSessionResult:
    state: OfflineSessionState
    arm: CandidateArm
    journey: SyntheticJourney
    contract_digest: str
    trace: tuple[OfflineTraceEvent, ...]
    frame_count: int
    inbound_bytes: int
    inbound_audio_ms: int
    outbound_frame_count: int
    outbound_bytes: int
    outbound_audio_ms: int
    session_duration_ms: int
    buffers_scrubbed: bool
    failure: DriverFailure | None = None

    def __post_init__(self) -> None:
        if (
            self.state
            not in {
                OfflineSessionState.CLOSED,
                OfflineSessionState.ABORTED,
            }
            or not isinstance(self.arm, CandidateArm)
            or not isinstance(self.journey, SyntheticJourney)
            or not _digest(self.contract_digest)
            or not isinstance(self.trace, tuple)
            or any(not isinstance(item, OfflineTraceEvent) for item in self.trace)
            or type(self.frame_count) is not int
            or self.frame_count < 0
            or type(self.inbound_bytes) is not int
            or self.inbound_bytes < 0
            or type(self.inbound_audio_ms) is not int
            or self.inbound_audio_ms < 0
            or type(self.outbound_frame_count) is not int
            or self.outbound_frame_count < 0
            or type(self.outbound_bytes) is not int
            or self.outbound_bytes < 0
            or type(self.outbound_audio_ms) is not int
            or self.outbound_audio_ms < 0
            or type(self.session_duration_ms) is not int
            or self.session_duration_ms < 0
            or type(self.buffers_scrubbed) is not bool
            or (
                self.state is OfflineSessionState.CLOSED
                and self.failure is not None
            )
            or (
                self.state is OfflineSessionState.ABORTED
                and not isinstance(self.failure, DriverFailure)
            )
        ):
            raise ValueError("offline session result is invalid")


@dataclass(slots=True)
class _MutableFrame:
    ordinal: int
    duration_ms: int
    payload: bytearray


@dataclass(frozen=True, slots=True)
class _Fixture:
    locale: str
    content: str
    fields: tuple[tuple[str, object], ...]
    low_confidence: bool = False

    def __post_init__(self) -> None:
        if self.locale not in _FIXTURE_LOCALES:
            raise ValueError("synthetic fixture locale is invalid")


_FIXTURES = MappingProxyType({
    SyntheticJourney.DIRECT_ANSWER: _Fixture(
        locale="en",
        content="Fixture caller asks what a repair may cost.",
        fields=(
            ("language", "en"),
            ("intent", "pricing_question"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.QUESTION_ONLY: _Fixture(
        locale="en",
        content="Fixture caller needs help with a furnace.",
        fields=(
            ("language", "en"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.LOW_CONFIDENCE_REPAIR: _Fixture(
        locale="es",
        content="Fixture caller audio is intentionally ambiguous.",
        fields=(
            ("language", "es"),
            ("intent", "service_request"),
        ),
        low_confidence=True,
    ),
    SyntheticJourney.SAFETY_GUIDANCE: _Fixture(
        locale="pt",
        content="Fixture caller reports a synthetic immediate hazard.",
        fields=(
            ("language", "pt"),
            ("intent", "emergency"),
            ("urgency", "emergency"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.UNSUPPORTED_LANGUAGE: _Fixture(
        locale="fr",
        content="Fixture caller selects an unsupported locale.",
        fields=(
            ("language", "fr"),
            ("intent", "service_request"),
        ),
    ),
    SyntheticJourney.SUPERSEDING_TURN: _Fixture(
        locale="zh",
        content="Fixture caller first requests a furnace repair.",
        fields=(
            ("language", "zh"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.BIDIRECTIONAL_CODE_SWITCH: _Fixture(
        locale="es",
        content="Fixture caller requests a furnace repair in Spanish.",
        fields=(
            ("language", "es"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.REPAIR_EXHAUSTION: _Fixture(
        locale="es",
        content="Fixture caller audio is unclear on the first attempt.",
        fields=(
            ("language", "es"),
            ("intent", "service_request"),
        ),
        low_confidence=True,
    ),
})


class _FacadeSeal:
    __slots__ = ()


_FACADE_SEAL = _FacadeSeal()


class OfflineSessionFacade:
    """Single-use, payload-safe lease over internally generated fixture frames."""

    __slots__ = (
        "_arm",
        "_binding",
        "_contract_digest",
        "_driver",
        "_expires_at_ms",
        "_frames",
        "_journey",
        "_lease_id",
        "_outbound_frames",
        "_revoked",
        "_state",
    )

    def __init__(
        self,
        *,
        seal: _FacadeSeal,
        driver: OfflineSessionDriver,
        arm: CandidateArm,
        journey: SyntheticJourney,
        binding: VoiceSessionBinding,
        lease_id: str,
        expires_at_ms: int,
        contract_digest: str,
        frames: list[_MutableFrame],
    ) -> None:
        if seal is not _FACADE_SEAL:
            raise ValueError("offline facade construction is sealed")
        self._driver = driver
        self._arm = arm
        self._journey = journey
        self._binding = binding
        self._lease_id = lease_id
        self._expires_at_ms = expires_at_ms
        self._contract_digest = contract_digest
        self._frames = frames
        self._outbound_frames: list[_MutableFrame] = []
        self._state = OfflineSessionState.LEASED
        self._revoked = False

    @property
    def state(self) -> OfflineSessionState:
        return self._state

    @property
    def arm(self) -> CandidateArm:
        return self._arm

    @property
    def journey(self) -> SyntheticJourney:
        return self._journey

    @property
    def contract_digest(self) -> str:
        return self._contract_digest

    @property
    def revoked(self) -> bool:
        return self._revoked

    def __repr__(self) -> str:
        return (
            "OfflineSessionFacade("
            f"state={self._state.value!r}, "
            f"arm={self._arm.value!r}, "
            f"journey={self._journey.value!r})"
        )


@dataclass(frozen=True, slots=True)
class _LeaseGrant:
    facade: OfflineSessionFacade
    arm: CandidateArm
    journey: SyntheticJourney
    binding: VoiceSessionBinding
    lease_id: str
    expires_at_ms: int
    contract_digest: str
    state: OfflineSessionState
    revoked: bool


@dataclass(frozen=True, slots=True)
class _FrameSnapshot:
    frames: tuple[_MutableFrame, ...]
    payloads: tuple[bytearray, ...]
    frame_count: int
    byte_count: int
    audio_ms: int


@dataclass(slots=True)
class _Assembly:
    binding: VoiceSessionBinding
    adapter: OfflineCandidateAdapter
    lifecycle: VoiceLifecycle
    transaction: TurnCompositionTransaction
    receipts: FinalTurnAdmissionAuthority
    state: VersionedIntakeStore


@dataclass(frozen=True, slots=True)
class _SyntheticObservationBackend:
    fixture: _Fixture

    def __call__(self, request: ExtractionRequest) -> BackendResponse:
        fields = dict(self.fixture.fields)
        confidence = 0.1 if self.fixture.low_confidence else 0.99
        return BackendResponse(
            request_id=request.request_id,
            configuration_digest=request.configuration_digest,
            outcome=BackendOutcome.OK,
            fields=fields,
            confidences={
                field_name: (
                    0.99
                    if field_name == "language"
                    else confidence
                )
                for field_name in fields
            },
        )


class OfflineSessionDriver:
    """Own one synchronous fixture invocation and permanently close its facade."""

    def __init__(
        self,
        limits: OfflineSessionLimits = OfflineSessionLimits(),  # noqa: B008
    ) -> None:
        if type(limits) is not OfflineSessionLimits:
            raise ValueError("offline driver limits are invalid")
        self._limits = limits
        self._counter = 0
        self._leased: OfflineSessionFacade | None = None
        self._lease_grant: _LeaseGrant | None = None
        self._inbound_frames: list[_MutableFrame] = []
        self._outbound_frames: list[_MutableFrame] = []
        self._issued_inbound_payloads: tuple[bytearray, ...] = ()
        self._issued_outbound_payloads: list[bytearray] = []
        self._lock = RLock()

    @property
    def limits(self) -> OfflineSessionLimits:
        return self._limits

    @property
    def contract_digest(self) -> str:
        return _digest_json(
            {
                "adapter_code_digests": {
                    arm.value: digest
                    for arm, digest in _ADAPTER_CODE_DIGESTS.items()
                },
                "assembly_code_digests": dict(
                    _ASSEMBLY_CODE_DIGESTS
                ),
                "codec": _CODEC,
                "driver_source_digest": _DRIVER_SOURCE_DIGEST,
                "facade_code_digest": _FACADE_CODE_DIGEST,
                "frame_schema": _FRAME_SCHEMA,
                "limits_digest": self._limits.contract_digest,
            }
        )

    def lease(
        self,
        *,
        arm: CandidateArm,
        journey: SyntheticJourney,
        now_ms: int,
    ) -> OfflineSessionFacade | None:
        if (
            not isinstance(arm, CandidateArm)
            or not isinstance(journey, SyntheticJourney)
            or type(now_ms) is not int
            or now_ms < 0
        ):
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if (
                self._lease_grant is not None
                and self._lease_grant.state
                not in {
                    OfflineSessionState.CLOSED,
                    OfflineSessionState.ABORTED,
                }
            ):
                return None
            self._counter += 1
            binding = VoiceSessionBinding(
                environment="bakeoff_offline",
                contractor_binding="synthetic_tenant",
                call_binding=f"synthetic_call_{self._counter}",
                stream_binding=f"synthetic_stream_{self._counter}",
                epoch=self._counter,
            )
            expires_at_ms = now_ms + self._limits.lease_ttl_ms
            contract_digest = self.contract_digest
            lease_id = self._lease_identifier(
                arm=arm,
                journey=journey,
                binding=binding,
                expires_at_ms=expires_at_ms,
                contract_digest=contract_digest,
            )
            frames = self._fixture_frames(journey)
            if not self._frames_within_limits(
                frames,
                outbound=False,
            ):
                self._scrub_frames(frames)
                return None
            facade = OfflineSessionFacade(
                seal=_FACADE_SEAL,
                driver=self,
                arm=arm,
                journey=journey,
                binding=binding,
                lease_id=lease_id,
                expires_at_ms=expires_at_ms,
                contract_digest=contract_digest,
                frames=frames,
            )
            self._leased = facade
            self._inbound_frames = frames
            self._outbound_frames = facade._outbound_frames
            self._issued_inbound_payloads = tuple(
                frame.payload for frame in frames
            )
            self._issued_outbound_payloads = []
            self._lease_grant = _LeaseGrant(
                facade=facade,
                arm=arm,
                journey=journey,
                binding=binding,
                lease_id=lease_id,
                expires_at_ms=expires_at_ms,
                contract_digest=contract_digest,
                state=OfflineSessionState.LEASED,
                revoked=False,
            )
            return facade
        finally:
            self._lock.release()

    def revoke(
        self,
        facade: OfflineSessionFacade,
    ) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if not self._accepts_facade(
                facade,
                expected_state=OfflineSessionState.LEASED,
            ):
                return False
            grant = self._lease_grant
            if grant is None:
                return False
            terminal = self._transition_lease(
                grant,
                state=OfflineSessionState.ABORTED,
                revoked=True,
            )
            self._close_facade(
                facade,
                terminal,
            )
            return True
        finally:
            self._lock.release()

    def run(
        self,
        facade: OfflineSessionFacade,
        *,
        now_ms: int,
    ) -> OfflineSessionResult | None:
        if type(now_ms) is not int or now_ms < 0:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if not self._accepts_facade(
                facade,
                expected_state=OfflineSessionState.LEASED,
            ):
                return None
            grant = self._lease_grant
            if grant is None:
                return None
            if now_ms > grant.expires_at_ms:
                terminal = self._transition_lease(
                    grant,
                    state=OfflineSessionState.ABORTED,
                    revoked=True,
                )
                buffers_scrubbed = self._close_facade(
                    facade,
                    terminal,
                )
                return self._aborted_result(
                    terminal,
                    failure=DriverFailure.EXPIRED_LEASE,
                    buffers_scrubbed=buffers_scrubbed,
                )
            grant = self._transition_lease(
                grant,
                state=OfflineSessionState.ACTIVE,
                revoked=False,
            )
            frame_count = 0
            inbound_bytes = 0
            inbound_audio_ms = 0
            outbound_frame_count = 0
            outbound_bytes = 0
            outbound_audio_ms = 0
            session_duration_ms = 0
            captured_frames: tuple[_MutableFrame, ...] = ()
            captured_payloads: tuple[bytearray, ...] = ()
            captured_outbound: tuple[_MutableFrame, ...] = ()
            captured_outbound_payloads: tuple[bytearray, ...] = ()
            trace = [
                OfflineTraceEvent(
                    ordinal=0,
                    kind=TraceKind.LEASE_ACCEPTED,
                )
            ]
            failure: DriverFailure | None = None
            buffers_scrubbed = False
            try:
                inbound = self._frame_snapshot(
                    facade._frames,
                    outbound=False,
                    allow_empty=False,
                )
                if (
                    facade._outbound_frames
                    is not self._outbound_frames
                    or self._outbound_frames
                    or inbound is None
                ):
                    failure = DriverFailure.RESOURCE_LIMIT
                else:
                    captured_frames = inbound.frames
                    captured_payloads = inbound.payloads
                    frame_count = inbound.frame_count
                    inbound_bytes = inbound.byte_count
                    inbound_audio_ms = inbound.audio_ms
                    session_end_ms = self._execute_fixture(
                        grant=grant,
                        now_ms=now_ms,
                        trace=trace,
                    )
                    session_duration_ms = max(
                        0,
                        session_end_ms - now_ms,
                    )
                    if (
                        session_duration_ms
                        > self._limits.max_session_ms
                    ):
                        failure = DriverFailure.RESOURCE_LIMIT
            except _DriverAbort as error:
                failure = error.failure
            except Exception:  # noqa: BLE001
                failure = DriverFailure.INTERNAL
            finally:
                outbound = self._frame_snapshot(
                    self._outbound_frames,
                    outbound=True,
                    allow_empty=True,
                )
                if outbound is None:
                    if failure is None:
                        failure = DriverFailure.RESOURCE_LIMIT
                else:
                    captured_outbound = outbound.frames
                    captured_outbound_payloads = outbound.payloads
                    outbound_frame_count = outbound.frame_count
                    outbound_bytes = outbound.byte_count
                    outbound_audio_ms = outbound.audio_ms
                terminal = self._transition_lease(
                    grant,
                    state=(
                        OfflineSessionState.CLOSED
                        if failure is None
                        else OfflineSessionState.ABORTED
                    ),
                    revoked=True,
                )
                buffers_scrubbed = self._close_facade(
                    facade,
                    terminal,
                    extra_collections=(
                        captured_frames,
                        captured_outbound,
                    ),
                )
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=TraceKind.BUFFERS_SCRUBBED,
                    )
                )
            return OfflineSessionResult(
                state=terminal.state,
                arm=terminal.arm,
                journey=terminal.journey,
                contract_digest=terminal.contract_digest,
                trace=tuple(trace),
                frame_count=frame_count,
                inbound_bytes=inbound_bytes,
                inbound_audio_ms=inbound_audio_ms,
                outbound_frame_count=outbound_frame_count,
                outbound_bytes=outbound_bytes,
                outbound_audio_ms=outbound_audio_ms,
                session_duration_ms=session_duration_ms,
                buffers_scrubbed=(
                    buffers_scrubbed
                    and all(
                        not any(payload)
                        for payload in captured_payloads
                    )
                    and all(
                        not any(payload)
                        for payload in captured_outbound_payloads
                    )
                ),
                failure=failure,
            )
        finally:
            self._lock.release()

    def _execute_fixture(
        self,
        *,
        grant: _LeaseGrant,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        fixture = _FIXTURES[grant.journey]
        assembly = self._assembly(grant, fixture)
        receipt = self._admit_final_turn(
            assembly=assembly,
            fixture=fixture,
            turn_number=1,
            at_ms=now_ms + 1,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.INPUT_FINAL,
            )
        )
        first = assembly.transaction.execute(
            receipt,
            content=fixture.content,
            backend=_SyntheticObservationBackend(fixture),
            now_ms=max(now_ms + 2, receipt.at_ms),
        )
        trace.append(
            self._composition_trace(
                trace,
                first,
                locale=self._trace_locale(
                    assembly.state.current_state().language
                ),
            )
        )
        if grant.journey is SyntheticJourney.UNSUPPORTED_LANGUAGE:
            if (
                first.status is not CompositionStatus.TERMINAL_FAILURE
                or first.act_ids
            ):
                raise _DriverAbort(DriverFailure.COMPOSITION)
            return receipt.at_ms
        if grant.journey is SyntheticJourney.REPAIR_EXHAUSTION:
            return self._execute_repair_exhaustion(
                assembly=assembly,
                first=first,
                now_ms=now_ms,
                trace=trace,
            )
        delivery_at_ms = now_ms + 10
        if grant.journey is SyntheticJourney.SUPERSEDING_TURN:
            correction = _Fixture(
                locale="zh",
                content="Fixture caller corrects the request to an inspection.",
                fields=(
                    ("language", "zh"),
                    ("intent", "service_request"),
                    ("service_action", "inspect"),
                    ("service_object", "furnace"),
                ),
            )
            first, receipt = self._supersede_turn(
                assembly=assembly,
                stale=first,
                stale_receipt=receipt,
                stale_fixture=fixture,
                replacement=correction,
                turn_number=2,
                admit_at_ms=now_ms + 3,
                execute_at_ms=now_ms + 4,
                replay_at_ms=now_ms + 5,
                trace=trace,
            )
            fixture = correction
        elif (
            grant.journey
            is SyntheticJourney.BIDIRECTIONAL_CODE_SWITCH
        ):
            mandarin_correction = _Fixture(
                locale="zh",
                content="Fixture caller switches to Mandarin and requests an inspection.",
                fields=(
                    ("language", "zh"),
                    ("intent", "service_request"),
                    ("service_action", "inspect"),
                    ("service_object", "furnace"),
                ),
            )
            first, receipt = self._supersede_turn(
                assembly=assembly,
                stale=first,
                stale_receipt=receipt,
                stale_fixture=fixture,
                replacement=mandarin_correction,
                turn_number=2,
                admit_at_ms=now_ms + 3,
                execute_at_ms=now_ms + 4,
                replay_at_ms=now_ms + 5,
                trace=trace,
            )
            spanish_correction = _Fixture(
                locale="es",
                content="Fixture caller switches back to Spanish and confirms repair.",
                fields=(
                    ("language", "es"),
                    ("intent", "service_request"),
                    ("service_action", "repair"),
                    ("service_object", "furnace"),
                ),
            )
            first, receipt = self._supersede_turn(
                assembly=assembly,
                stale=first,
                stale_receipt=receipt,
                stale_fixture=mandarin_correction,
                replacement=spanish_correction,
                turn_number=3,
                admit_at_ms=now_ms + 6,
                execute_at_ms=now_ms + 7,
                replay_at_ms=now_ms + 8,
                trace=trace,
            )
            fixture = spanish_correction
            delivery_at_ms = now_ms + 12
        if first.status not in {
            CompositionStatus.RESPONSE_PENDING,
            CompositionStatus.REPAIR_PENDING,
        }:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        final, final_at_ms = self._deliver_pending(
            assembly=assembly,
            pending=first,
            at_ms=delivery_at_ms,
            trace=trace,
        )
        if final.status is not CompositionStatus.RESPONSE_OBSERVED:
            raise _DriverAbort(DriverFailure.DELIVERY)
        return final_at_ms

    def _execute_repair_exhaustion(
        self,
        *,
        assembly: _Assembly,
        first: CompositionResult,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        if first.status is not CompositionStatus.REPAIR_PENDING:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        observed, first_complete_ms = self._deliver_pending(
            assembly=assembly,
            pending=first,
            at_ms=now_ms + 5,
            trace=trace,
        )
        if observed.status is not CompositionStatus.RESPONSE_OBSERVED:
            raise _DriverAbort(DriverFailure.DELIVERY)
        second_fixture = _Fixture(
            locale="es",
            content="Fixture caller audio remains unclear on the second attempt.",
            fields=(
                ("language", "es"),
                ("intent", "service_request"),
            ),
            low_confidence=True,
        )
        second_receipt = self._admit_final_turn(
            assembly=assembly,
            fixture=second_fixture,
            turn_number=2,
            at_ms=first_complete_ms + 1,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.INPUT_FINAL,
            )
        )
        second_at_ms = max(
            first_complete_ms + 2,
            second_receipt.at_ms,
        )
        second = assembly.transaction.execute(
            second_receipt,
            content=second_fixture.content,
            backend=_SyntheticObservationBackend(
                second_fixture
            ),
            now_ms=second_at_ms,
        )
        trace.append(
            self._composition_trace(
                trace,
                second,
                locale=self._trace_locale(
                    assembly.state.current_state().language
                ),
            )
        )
        if (
            second.status is not CompositionStatus.CLOSURE_REQUIRED
            or second.act_ids
            or second.act_kinds
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        return second_at_ms

    def _supersede_turn(
        self,
        *,
        assembly: _Assembly,
        stale: CompositionResult,
        stale_receipt: FinalTurnAdmissionReceipt,
        stale_fixture: _Fixture,
        replacement: _Fixture,
        turn_number: int,
        admit_at_ms: int,
        execute_at_ms: int,
        replay_at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> tuple[CompositionResult, FinalTurnAdmissionReceipt]:
        if stale.status is not CompositionStatus.RESPONSE_PENDING:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        replacement_receipt = self._admit_final_turn(
            assembly=assembly,
            fixture=replacement,
            turn_number=turn_number,
            at_ms=admit_at_ms,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.INPUT_FINAL,
            )
        )
        replacement_result = assembly.transaction.execute(
            replacement_receipt,
            content=replacement.content,
            backend=_SyntheticObservationBackend(replacement),
            now_ms=max(
                execute_at_ms,
                replacement_receipt.at_ms,
            ),
        )
        replay = assembly.transaction.execute(
            stale_receipt,
            content=stale_fixture.content,
            backend=_SyntheticObservationBackend(stale_fixture),
            now_ms=replay_at_ms,
        )
        if replay.status is not CompositionStatus.SUPERSEDED:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.SUPERSEDED,
                composition_status=replay.status,
            )
        )
        trace.append(
            self._composition_trace(
                trace,
                replacement_result,
                locale=self._trace_locale(
                    assembly.state.current_state().language
                ),
            )
        )
        return replacement_result, replacement_receipt

    def _deliver_pending(
        self,
        *,
        assembly: _Assembly,
        pending: CompositionResult,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> tuple[CompositionResult, int]:
        result = pending
        for act_id, act_kind in zip(
            pending.act_ids,
            pending.act_kinds,
            strict=True,
        ):
            authorization = assembly.transaction.authorization_receipt(
                act_id
            )
            if authorization is None:
                raise _DriverAbort(DriverFailure.DELIVERY)
            confirmation = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
                payload=VoicePayload(),
                at_ms=at_ms,
            )
            if (
                not assembly.lifecycle.ingest(confirmation)
                or not assembly.transaction.accept_semantic_confirmation(
                    event=confirmation,
                    event_id=f"confirm_{confirmation.sequence}",
                    sequence=confirmation.sequence,
                )
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.ACT_CONFIRMED,
                    semantic_act_kind=act_kind,
                )
            )
            payload = self._playout_payload(
                authorization=authorization,
            )
            self._append_outbound_frame(
                authorization=authorization,
            )
            tts = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.TTS_BOUND,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
                payload=VoicePayload(
                    text_digest=payload.text_digest,
                    audio_id=payload.audio_id,
                ),
                at_ms=confirmation.at_ms + 1,
            )
            if not assembly.lifecycle.ingest(tts):
                raise _DriverAbort(DriverFailure.DELIVERY)
            playout = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.PLAYOUT_BOUND,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
                payload=payload,
                at_ms=tts.at_ms + 1,
            )
            if not assembly.lifecycle.ingest(playout):
                raise _DriverAbort(DriverFailure.DELIVERY)
            transport = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.TRANSPORT_RESOLVED,
                source=VoiceSource.TWILIO_AUTHENTICATED,
                payload=payload,
                at_ms=playout.at_ms + 1,
            )
            if not assembly.lifecycle.ingest(
                transport
            ) or not assembly.transaction.accept_transport_resolution(
                event=transport,
                event_id=f"transport_{transport.sequence}",
                sequence=transport.sequence,
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.TRANSPORT_RESOLVED,
                    semantic_act_kind=act_kind,
                )
            )
            playback = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
                payload=payload,
                at_ms=transport.at_ms + 1,
            )
            if not assembly.lifecycle.ingest(playback):
                raise _DriverAbort(DriverFailure.DELIVERY)
            observed = assembly.transaction.observe_playback(
                event=playback,
                event_id=f"playback_{playback.sequence}",
                sequence=playback.sequence,
            )
            if observed is None:
                raise _DriverAbort(DriverFailure.DELIVERY)
            result = observed
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.PLAYBACK_OBSERVED,
                    semantic_act_kind=act_kind,
                    composition_status=observed.status,
                )
            )
            at_ms = playback.at_ms + 1
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.RESPONSE_OBSERVED,
                composition_status=result.status,
            )
        )
        return result, at_ms

    def _assembly(
        self,
        grant: _LeaseGrant,
        fixture: _Fixture,
    ) -> _Assembly:
        binding = grant.binding
        adapter = self._adapter(grant.arm, binding)
        lifecycle = VoiceLifecycle(binding=binding)
        receipts = FinalTurnAdmissionAuthority(
            adapter=adapter,
            lifecycle=lifecycle,
            implementation_bindings=tuple(
                AdapterImplementationBinding(
                    adapter_type=adapter_type,
                    arm=arm,
                    implementation_digest=_ADAPTER_CODE_DIGESTS[arm],
                )
                for arm, adapter_type in _ADAPTER_TYPES.items()
            ),
            max_records=16,
            max_ttl_ms=self._limits.max_session_ms,
        )
        extractor = ObservationExtractor(
            binding=binding,
            configuration_digest=_EXTRACTOR_DIGEST,
            min_field_confidence=0.8,
            min_aggregate_confidence=0.85,
        )
        initial_state = IntakeState.new(
            call_sid=binding.call_binding,
        )
        initial_state.language = fixture.locale
        state = VersionedIntakeStore(
            binding=binding,
            initial_state=initial_state,
        )
        calls = CallLifecycle(
            binding=binding,
            voice_lifecycle=lifecycle,
            first_silence_ms=100,
            second_silence_ms=200,
        )
        speech = SpeechControl(
            SpeechPolicy(
                normal_word_budget=20,
                safety_word_budget=20,
                required_safety_fragments=(
                    "call emergency services",
                ),
                terminal_fragments=("goodbye",),
                localized_safety_fragments=(
                    ("en", ("call emergency services",)),
                    ("es", ("servicios de emergencia",)),
                    ("pt", ("serviços de emergência",)),
                    ("zh", ("紧急服务",)),
                ),
            )
        )
        transaction = TurnCompositionTransaction(
            binding=binding,
            adapter=adapter,
            lifecycle=lifecycle,
            extractor=extractor,
            receipts=receipts,
            state=state,
            coordinator=VoiceBakeoffCoordinator(
                speech=speech,
                calls=calls,
            ),
            materializer=FixedProposalMaterializer(),
            policy=CompositionPolicy(),
            max_outcomes=16,
        )
        return _Assembly(
            binding=binding,
            adapter=adapter,
            lifecycle=lifecycle,
            transaction=transaction,
            receipts=receipts,
            state=state,
        )

    def _admit_final_turn(
        self,
        *,
        assembly: _Assembly,
        fixture: _Fixture,
        turn_number: int,
        at_ms: int,
    ) -> FinalTurnAdmissionReceipt:
        sequence, canonical_at_ms = (
            assembly.lifecycle.next_position(at_ms=at_ms)
        )
        context = EventContext(
            binding=assembly.binding,
            sequence=sequence,
            at_ms=canonical_at_ms,
            input_turn_id=f"turn_{turn_number}",
            generation_id=f"input_generation_{turn_number}",
            semantic_act_id=f"input_act_{turn_number}",
            semantic_act_kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
        )
        results = self._final_results(
            assembly.adapter,
            context=context,
            content=fixture.content,
        )
        if not results:
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        for result in results:
            if (
                not result.accepted
                or len(result.events) != 1
                or not assembly.lifecycle.ingest(result.events[0])
            ):
                raise _DriverAbort(DriverFailure.ASSEMBLY)
        result = results[-1]
        receipt = assembly.receipts.mint(
            adapter=assembly.adapter,
            lifecycle=assembly.lifecycle,
            result=result,
            event=result.events[0],
            content=fixture.content,
            now_ms=result.events[0].at_ms,
            ttl_ms=min(1_000, self._limits.max_session_ms),
        )
        if receipt is None:
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        return receipt

    @staticmethod
    def _final_results(
        adapter: OfflineCandidateAdapter,
        *,
        context: EventContext,
        content: str,
    ) -> tuple[AdapterResult, ...]:
        payload = VoicePayload(
            text_digest=final_turn_content_digest(content)
        )
        if type(adapter) is NativeGeminiAdapter:
            return (
                adapter.handle(
                    NativeSignal(
                        NativeSignalKind.INPUT_FINAL,
                        context,
                        CandidateUsage(),
                        payload,
                    )
                ),
            )
        if type(adapter) is ChainedStreamingAdapter:
            return (
                adapter.handle(
                    ChainedSignal(
                        ChainedSignalKind.INPUT_FINAL,
                        context,
                        CandidateUsage(),
                        payload,
                    )
                ),
            )
        if type(adapter) is ConversationRelayAdapter:
            return (
                adapter.handle(
                    RelaySignal(
                        RelaySignalKind.PROMPT_FINAL,
                        context,
                        CandidateUsage(),
                        payload,
                    )
                ),
            )
        if type(adapter) is ManualNativeAdapter:
            started = adapter.handle(
                ManualNativeSignal(
                    ManualNativeSignalKind.ACTIVITY_STARTED,
                    context,
                )
            )
            ended = adapter.handle(
                ManualNativeSignal(
                    ManualNativeSignalKind.ACTIVITY_ENDED,
                    replace(
                        context,
                        sequence=context.sequence + 1,
                        at_ms=context.at_ms + 1,
                    ),
                )
            )
            if not started.accepted or not ended.accepted:
                return ()
            final = adapter.handle(
                ManualNativeSignal(
                    ManualNativeSignalKind.INPUT_FINAL,
                    replace(
                        context,
                        sequence=context.sequence + 2,
                        at_ms=context.at_ms + 2,
                    ),
                    payload=payload,
                )
            )
            return started, ended, final
        return ()

    def _adapter(
        self,
        arm: CandidateArm,
        binding: VoiceSessionBinding,
    ) -> OfflineCandidateAdapter:
        limits = CandidateLimits(
            output_tokens=128,
            audio_ms=self._limits.max_outbound_audio_ms,
            byte_count=self._limits.max_outbound_bytes,
            wall_clock_ms=self._limits.max_session_ms,
            cost_minor_units=100,
            request_count=self._limits.max_queue_depth,
        )
        if arm is CandidateArm.A:
            return NativeGeminiAdapter(
                binding=binding,
                mode=NativeMode.MANUAL_GATED,
                limits=limits,
            )
        if arm is CandidateArm.B1:
            return ChainedStreamingAdapter(
                binding=binding,
                limits=limits,
            )
        if arm is CandidateArm.B2:
            return ConversationRelayAdapter(
                binding=binding,
                limits=limits,
            )
        if arm is CandidateArm.C:
            return ManualNativeAdapter(
                binding=binding,
                limits=limits,
                generation_timeout_ms=100,
            )
        raise _DriverAbort(DriverFailure.ASSEMBLY)

    def _fixture_frames(
        self,
        journey: SyntheticJourney,
    ) -> list[_MutableFrame]:
        seed = hashlib.sha256(
            _AUDIO_DOMAIN + journey.value.encode("ascii")
        ).digest()
        frame_size = min(
            160,
            self._limits.max_inbound_frame_bytes,
        )
        payload = bytearray(
            seed[index % len(seed)] for index in range(frame_size)
        )
        return [
            _MutableFrame(
                ordinal=0,
                duration_ms=20,
                payload=payload,
            )
        ]

    def _frames_within_limits(
        self,
        frames: list[_MutableFrame],
        *,
        outbound: bool,
    ) -> bool:
        return self._frame_snapshot(
            frames,
            outbound=outbound,
            allow_empty=outbound,
        ) is not None

    def _frame_snapshot(
        self,
        frames: object,
        *,
        outbound: bool,
        allow_empty: bool,
    ) -> _FrameSnapshot | None:
        if type(frames) is not list:
            return None
        try:
            captured = tuple(frames)
        except Exception:  # noqa: BLE001
            return None
        max_frames = (
            self._limits.max_outbound_frames
            if outbound
            else self._limits.max_inbound_frames
        )
        max_frame_bytes = (
            self._limits.max_outbound_frame_bytes
            if outbound
            else self._limits.max_inbound_frame_bytes
        )
        max_bytes = (
            self._limits.max_outbound_bytes
            if outbound
            else self._limits.max_inbound_bytes
        )
        max_audio_ms = (
            self._limits.max_outbound_audio_ms
            if outbound
            else self._limits.max_inbound_audio_ms
        )
        if (
            (not captured and not allow_empty)
            or len(captured) > max_frames
            or len(captured) > self._limits.max_queue_depth
        ):
            return None
        byte_count = 0
        audio_ms = 0
        payloads: list[bytearray] = []
        validated: list[_MutableFrame] = []
        for index, frame in enumerate(captured):
            if type(frame) is not _MutableFrame:
                return None
            try:
                ordinal = frame.ordinal
                duration_ms = frame.duration_ms
                payload = frame.payload
            except Exception:  # noqa: BLE001
                return None
            if (
                type(ordinal) is not int
                or ordinal != index
                or type(duration_ms) is not int
                or duration_ms < 1
                or type(payload) is not bytearray
            ):
                return None
            payload_size = len(payload)
            if payload_size < 1 or payload_size > max_frame_bytes:
                return None
            byte_count += payload_size
            audio_ms += duration_ms
            if byte_count > max_bytes or audio_ms > max_audio_ms:
                return None
            validated.append(frame)
            payloads.append(payload)
        return _FrameSnapshot(
            frames=tuple(validated),
            payloads=tuple(payloads),
            frame_count=len(validated),
            byte_count=byte_count,
            audio_ms=audio_ms,
        )

    def _append_outbound_frame(
        self,
        *,
        authorization: VoiceEvent,
    ) -> None:
        seed = hashlib.sha256(
            _AUDIO_DOMAIN
            + authorization.semantic_act_id.encode("ascii")
        ).digest()
        frame_size = min(
            160,
            self._limits.max_outbound_frame_bytes,
        )
        payload = bytearray(
            seed[index % len(seed)]
            for index in range(frame_size)
        )
        self._issued_outbound_payloads.append(payload)
        self._outbound_frames.append(
            _MutableFrame(
                ordinal=len(self._outbound_frames),
                duration_ms=20,
                payload=payload,
            )
        )
        if not self._frames_within_limits(
            self._outbound_frames,
            outbound=True,
        ):
            raise _DriverAbort(DriverFailure.RESOURCE_LIMIT)

    @staticmethod
    def _lease_identifier(
        *,
        arm: CandidateArm,
        journey: SyntheticJourney,
        binding: VoiceSessionBinding,
        expires_at_ms: int,
        contract_digest: str,
    ) -> str:
        return hashlib.sha256(
            _LEASE_DOMAIN
            + _json_bytes(
                {
                    "arm": arm.value,
                    "binding": {
                        "call": binding.call_binding,
                        "contractor": binding.contractor_binding,
                        "environment": binding.environment,
                        "epoch": binding.epoch,
                        "stream": binding.stream_binding,
                    },
                    "contract_digest": contract_digest,
                    "expires_at_ms": expires_at_ms,
                    "journey": journey.value,
                }
            )
        ).hexdigest()

    def _accepts_facade(
        self,
        facade: object,
        *,
        expected_state: OfflineSessionState,
    ) -> bool:
        grant = self._lease_grant
        if (
            type(facade) is not OfflineSessionFacade
            or grant is None
        ):
            return False
        try:
            return (
                facade is self._leased
                and facade is grant.facade
                and facade._driver is self
                and grant.state is expected_state
                and facade._state is grant.state
                and facade._revoked is grant.revoked
                and not grant.revoked
                and facade._arm is grant.arm
                and facade._journey is grant.journey
                and facade._binding is grant.binding
                and facade._lease_id == grant.lease_id
                and facade._expires_at_ms == grant.expires_at_ms
                and facade._contract_digest
                == grant.contract_digest
                and grant.contract_digest == self.contract_digest
                and grant.lease_id
                == self._lease_identifier(
                    arm=grant.arm,
                    journey=grant.journey,
                    binding=grant.binding,
                    expires_at_ms=grant.expires_at_ms,
                    contract_digest=grant.contract_digest,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _transition_lease(
        self,
        grant: _LeaseGrant,
        *,
        state: OfflineSessionState,
        revoked: bool,
    ) -> _LeaseGrant:
        if self._lease_grant is not grant:
            raise _DriverAbort(DriverFailure.INTERNAL)
        updated = replace(
            grant,
            state=state,
            revoked=revoked,
        )
        self._lease_grant = updated
        grant.facade._state = state
        grant.facade._revoked = revoked
        return updated

    def _close_facade(
        self,
        facade: OfflineSessionFacade,
        terminal: _LeaseGrant,
        *,
        extra_collections: tuple[object, ...] = (),
    ) -> bool:
        collections: list[object] = [
            self._inbound_frames,
            self._outbound_frames,
            self._issued_inbound_payloads,
            tuple(self._issued_outbound_payloads),
            *extra_collections,
        ]
        try:
            collections.append(facade._frames)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            collections.append(facade._outbound_frames)
        except Exception:  # noqa: BLE001, S110
            pass
        scrubbed_payloads: list[bytearray] = []
        for collection in collections:
            scrubbed_payloads.extend(
                self._scrub_frames(collection)
            )
        for collection in collections:
            if type(collection) is list:
                try:
                    collection.clear()
                except Exception:  # noqa: BLE001, S110
                    pass
        self._inbound_frames = []
        self._outbound_frames = []
        self._issued_inbound_payloads = ()
        self._issued_outbound_payloads = []
        try:
            facade._driver = self
            facade._arm = terminal.arm
            facade._journey = terminal.journey
            facade._binding = terminal.binding
            facade._lease_id = terminal.lease_id
            facade._expires_at_ms = terminal.expires_at_ms
            facade._contract_digest = terminal.contract_digest
            facade._frames = []
            facade._outbound_frames = []
            facade._state = terminal.state
            facade._revoked = terminal.revoked
        except Exception:  # noqa: BLE001
            return False
        return all(
            not any(payload)
            for payload in scrubbed_payloads
        )

    @staticmethod
    def _scrub_frames(frames: object) -> tuple[bytearray, ...]:
        if type(frames) not in {list, tuple}:
            return ()
        scrubbed: list[bytearray] = []
        try:
            values = tuple(frames)
        except Exception:  # noqa: BLE001
            return ()
        for frame in values:
            if type(frame) is bytearray:
                payload = frame
            else:
                try:
                    payload = frame.payload
                except Exception:  # noqa: BLE001, S112
                    continue
            if type(payload) is not bytearray:
                continue
            try:
                payload[:] = b"\x00" * len(payload)
            except Exception:  # noqa: BLE001, S112
                continue
            scrubbed.append(payload)
        return tuple(scrubbed)

    def _aborted_result(
        self,
        grant: _LeaseGrant,
        *,
        failure: DriverFailure,
        buffers_scrubbed: bool,
    ) -> OfflineSessionResult:
        return OfflineSessionResult(
            state=OfflineSessionState.ABORTED,
            arm=grant.arm,
            journey=grant.journey,
            contract_digest=grant.contract_digest,
            trace=(
                OfflineTraceEvent(
                    ordinal=0,
                    kind=TraceKind.BUFFERS_SCRUBBED,
                ),
            ),
            frame_count=0,
            inbound_bytes=0,
            inbound_audio_ms=0,
            outbound_frame_count=0,
            outbound_bytes=0,
            outbound_audio_ms=0,
            session_duration_ms=0,
            buffers_scrubbed=buffers_scrubbed,
            failure=failure,
        )

    @staticmethod
    def _event_after(
        lifecycle: VoiceLifecycle,
        authorization: VoiceEvent,
        *,
        kind: VoiceEventKind,
        source: VoiceSource,
        payload: VoicePayload,
        at_ms: int,
    ) -> VoiceEvent:
        sequence, canonical_at_ms = lifecycle.next_position(
            at_ms=at_ms
        )
        return replace(
            authorization,
            kind=kind,
            source=source,
            sequence=sequence,
            at_ms=canonical_at_ms,
            payload=payload,
        )

    @staticmethod
    def _playout_payload(
        *,
        authorization: VoiceEvent,
    ) -> VoicePayload:
        digest = hashlib.sha256(
            _AUDIO_DOMAIN
            + authorization.semantic_act_id.encode("ascii")
        ).hexdigest()
        return VoicePayload(
            text_digest=digest,
            audio_id=f"audio_{digest[:24]}",
            playout_id=f"playout_{digest[24:48]}",
        )

    @staticmethod
    def _composition_trace(
        trace: list[OfflineTraceEvent],
        result: CompositionResult,
        *,
        locale: str | None,
    ) -> OfflineTraceEvent:
        if result.status is CompositionStatus.REPAIR_PENDING:
            kind = TraceKind.REPAIR_PENDING
        elif result.status is CompositionStatus.RESPONSE_PENDING:
            kind = TraceKind.RESPONSE_PENDING
        elif result.status is CompositionStatus.SUPERSEDED:
            kind = TraceKind.SUPERSEDED
        else:
            kind = TraceKind.TERMINAL
        return OfflineTraceEvent(
            ordinal=len(trace),
            kind=kind,
            composition_status=result.status,
            locale=locale,
        )

    @staticmethod
    def _trace_locale(locale: object) -> str | None:
        return (
            locale
            if isinstance(locale, str)
            and locale in _TRACE_LOCALES
            else None
        )


@dataclass(frozen=True, slots=True)
class _DriverAbort(Exception):
    failure: DriverFailure


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _digest_json(value: object) -> str:
    return hashlib.sha256(_DRIVER_DOMAIN + _json_bytes(value)).hexdigest()


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "DriverFailure",
    "OfflineSessionDriver",
    "OfflineSessionFacade",
    "OfflineSessionLimits",
    "OfflineSessionResult",
    "OfflineSessionState",
    "OfflineTraceEvent",
    "SyntheticJourney",
    "TraceKind",
]
