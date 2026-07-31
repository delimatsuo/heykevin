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
from app.services.voice_bakeoff_closure import (
    OfflineAuthorityInventory,
    OfflineClosureCommitReceipt,
    OfflineClosurePhase,
    OfflineLocalClosureAuthority,
    ScriptedOptOutConfirmationReceipt,
)
from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_materializer import FixedProposalMaterializer
from app.services.voice_bakeoff_silence import (
    LifecycleActResult,
    LifecycleActStatus,
    SilenceLifecycleController,
)
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
from app.services.voice_call_lifecycle import (
    CallIntent,
    CallIntentKind,
    CallLifecycle,
    SilencePhase,
)
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
from app.services.voice_speech_control import (
    CancellationReason,
    ReplayMode,
    SpeechControl,
    SpeechPolicy,
)

_DRIVER_DOMAIN = b"hey-kevin/offline-session-driver/v1\x00"
_LEASE_DOMAIN = b"hey-kevin/offline-session-lease/v1\x00"
_AUDIO_DOMAIN = b"hey-kevin/offline-synthetic-audio/v1\x00"
_EXTRACTOR_DIGEST = hashlib.sha256(
    b"hey-kevin/offline-fixed-observation-backend/v1"
).hexdigest()
_CODEC = "mulaw_8000_mono"
_FRAME_SCHEMA = "ordinal:u32,duration_ms:u16,payload:mutable-bytes"
_DRIVER_SOURCE_DIGEST = "654afa6601e985d43ee123c333f70aa17ef86686c43ab76e9e389b9bc2c59a5f"
_FACADE_CODE_DIGEST = "620e13028fceec909f40b5f10f75cf500209de661c8cd63bc8350d3397e9f5b5"
_TRACE_LOCALES = frozenset({"en", "es", "pt", "zh"})
_FIXTURE_LOCALES = _TRACE_LOCALES | {"fr"}
_ASSEMBLY_CODE_DIGESTS = MappingProxyType({
    "caller_observation_extractor":
        "a3d79ce19f2c603f47dd370789773a707016a07e430c58fcd389a67065f9d364",
    "voice_bakeoff_closure":
        "301999acd5aac7143ff1cabf85a9f508b194acc44afc91887c70b52731a68111",
    "composition":
        "36945f2911538aef4ce433cc9ed063dfeebe3dbcc094ccdac57735fe2cb24a6c",
    "dialogue_planner":
        "222f332a8756eec4144bfe9ede7bb5dbae71cdbfd1ca8505df58b862dc953457",
    "materializer":
        "a9f7eb4458bc6f52fd4e4836d1c8eca0e50ca8bf8b636fd3fab88e63aa0fa9c7",
    "receptionist_state":
        "8f26251f7d9e1e534acf6178d1d5bcefac2efb87016e3b34b881decea2294845",
    "voice_bakeoff_coordinator":
        "c4397ea6e88830be82cb9531f5f84518a22b54d7ef2a64b4166d1590024f5ee7",
    "voice_bakeoff_silence":
        "5ddb3ad7c0d29838d1062e61f4f15b2d9b8ebc6340b4b73c4ac4b0cbff1c9fee",
    "voice_call_lifecycle":
        "87a45a63d355be415864e739f1ec71397b0d7e4c5d366446c848a17884af8233",
    "voice_candidates_base":
        "1d96a31d07966f48ebb528cdd4618bc5a2c0cc7911323bef8d56431c839c7cfe",
    "voice_lifecycle":
        "45879f2d5247f37f5671536b8dd52947eeef77625691f442e5980cc1c96556a2",
    "voice_session_auth":
        "ecb56c931cfb8ddc2c8a70ef37d5e0b0834fece7382de5482aef8c7e914cf1ae",
    "voice_speech_control":
        "2cb20a33235f11dff59effb6e1d95b0635d22dcc8a07d0973aa5f85c40ef7e1f",
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
        "e7baa4dc48873c6f3504a77a0e705f423c01e2b704d221d0a4e78890a4c44a2a",
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
    UNOBSERVED_QUESTION_OUTCOMES = "unobserved_question_outcomes"
    INTERRUPTION_RECONNECT = "interruption_reconnect"
    SILENCE_BOUNDARY_MORE_TIME = "silence_boundary_more_time"
    FIXED_NONTERMINAL_FALLBACKS = "fixed_nonterminal_fallbacks"
    REPEAT_SLOWER = "repeat_slower"
    OPT_OUT_WITHDRAWAL = "opt_out_withdrawal"


class OfflineStopPosture(str, Enum):
    RESPONSE_PENDING = "response_pending"
    PLAYOUT_BOUND_QUEUED = "playout_bound_queued"
    TRANSPORT_RESOLVED_UNOBSERVED = (
        "transport_resolved_unobserved"
    )
    PLAYOUT_PARTIAL_UNOBSERVED = "playout_partial_unobserved"
    REPLAY_PENDING = "replay_pending"
    SILENCE_TIMER_ARMED = "silence_timer_armed"


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
    PLAYOUT_PARTIAL = "playout_partial"
    PLAYOUT_CLEARED = "playout_cleared"
    PLAYOUT_INTERRUPTED = "playout_interrupted"
    ACT_FAILED = "act_failed"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    SILENCE_TIMER_ARMED = "silence_timer_armed"
    CALLER_ACTIVITY_AT_BOUNDARY = "caller_activity_at_boundary"
    MORE_TIME_ACCEPTED = "more_time_accepted"
    MORE_TIME_IGNORED = "more_time_ignored"
    LOCAL_TERMINAL_ELIGIBLE = "local_terminal_eligible"
    LOCAL_TERMINATED = "local_terminated"
    FIXED_FALLBACK_REQUESTED = "fixed_fallback_requested"
    FIXED_FALLBACK_OBSERVED = "fixed_fallback_observed"
    REPLAY_REQUESTED = "replay_requested"
    REPLAY_PENDING = "replay_pending"
    REPLAY_OBSERVED = "replay_observed"
    PARTICIPANT_ACTIVITY = "participant_activity"
    WITHDRAWAL_CLASSIFIED = "withdrawal_classified"
    SCRIPTED_OPT_OUT_AUTHORIZED = "scripted_opt_out_authorized"
    GENERAL_AUTHORITY_CANCELLED = "general_authority_cancelled"
    GENERAL_OUTBOUND_CLEARED = "general_outbound_cleared"
    CLOSURE_CAPABILITY_RETAINED = "closure_capability_retained"
    CLOSURE_STAGED = "closure_staged"
    CLOSURE_PREDICATE_REJECTED = "closure_predicate_rejected"
    OFFLINE_OUTBOUND_COMMITTED = "offline_outbound_committed"
    SYNTHETIC_PLAYBACK_OBSERVED = "synthetic_playback_observed"
    CLOSURE_TEARDOWN_COMPLETE = "closure_teardown_complete"


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
    max_session_ms: int = 120_000
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
    replay_mode: ReplayMode | None = None
    content_digest: str | None = None

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
            or (
                self.replay_mode is not None
                and not isinstance(self.replay_mode, ReplayMode)
            )
            or (
                self.content_digest is not None
                and not _digest(self.content_digest)
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
    pre_stop_outbound_frame_count: int
    post_stop_outbound_frame_delta: int
    post_stop_outbound_ordinals: tuple[int, ...]
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
            or type(self.pre_stop_outbound_frame_count) is not int
            or self.pre_stop_outbound_frame_count < 0
            or type(self.post_stop_outbound_frame_delta) is not int
            or self.post_stop_outbound_frame_delta < 0
            or not isinstance(
                self.post_stop_outbound_ordinals,
                tuple,
            )
            or any(
                type(ordinal) is not int
                for ordinal in self.post_stop_outbound_ordinals
            )
            or (
                self.journey
                is not SyntheticJourney.OPT_OUT_WITHDRAWAL
                and (
                    self.pre_stop_outbound_frame_count != 0
                    or self.post_stop_outbound_frame_delta != 0
                    or self.post_stop_outbound_ordinals
                )
            )
            or (
                self.journey
                is SyntheticJourney.OPT_OUT_WITHDRAWAL
                and (
                    self.post_stop_outbound_frame_delta
                    != self.outbound_frame_count
                    or self.post_stop_outbound_ordinals
                    != tuple(
                        range(self.post_stop_outbound_frame_delta)
                    )
                )
            )
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
    outcome: BackendOutcome = BackendOutcome.OK

    def __post_init__(self) -> None:
        if (
            self.locale not in _FIXTURE_LOCALES
            or not isinstance(self.outcome, BackendOutcome)
            or (
                self.outcome is not BackendOutcome.OK
                and self.fields
            )
        ):
            raise ValueError("synthetic fixture is invalid")


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
    SyntheticJourney.UNOBSERVED_QUESTION_OUTCOMES: _Fixture(
        locale="en",
        content="Fixture caller requests a furnace repair.",
        fields=(
            ("language", "en"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.INTERRUPTION_RECONNECT: _Fixture(
        locale="en",
        content="Fixture caller requests a furnace repair before interruption.",
        fields=(
            ("language", "en"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.SILENCE_BOUNDARY_MORE_TIME: _Fixture(
        locale="en",
        content="Fixture caller requests a furnace repair before waiting.",
        fields=(
            ("language", "en"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.FIXED_NONTERMINAL_FALLBACKS: _Fixture(
        locale="pt",
        content="Fixture caller requests a furnace repair before asking for another access mode.",
        fields=(
            ("language", "pt"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.REPEAT_SLOWER: _Fixture(
        locale="en",
        content="Fixture caller requests a furnace repair before asking for repeat and slower speech.",
        fields=(
            ("language", "en"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
    ),
    SyntheticJourney.OPT_OUT_WITHDRAWAL: _Fixture(
        locale="en",
        content="Fixture caller requests a furnace repair.",
        fields=(
            ("language", "en"),
            ("intent", "service_request"),
            ("service_action", "repair"),
            ("service_object", "furnace"),
        ),
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
        "_scripted_locale",
        "_state",
        "_stop_posture",
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
        scripted_locale: str,
        stop_posture: OfflineStopPosture,
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
        self._scripted_locale = scripted_locale
        self._stop_posture = stop_posture
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
    scripted_locale: str
    stop_posture: OfflineStopPosture
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
    calls: CallLifecycle
    lifecycle: VoiceLifecycle
    speech: SpeechControl
    transaction: TurnCompositionTransaction
    silence: SilenceLifecycleController
    receipts: FinalTurnAdmissionAuthority
    state: VersionedIntakeStore


@dataclass(frozen=True, slots=True)
class _StopCheckpoint:
    at_ms: int
    state_version: int
    state_snapshot: dict[str, object]
    act_ids: tuple[str, ...]
    timer: CallIntent | None
    baseline_frame_count: int
    baseline_last_ordinal: int | None


@dataclass(frozen=True, slots=True)
class _SyntheticObservationBackend:
    fixture: _Fixture

    def __call__(self, request: ExtractionRequest) -> BackendResponse:
        fields = (
            dict(self.fixture.fields)
            if self.fixture.outcome is BackendOutcome.OK
            else {}
        )
        confidence = 0.1 if self.fixture.low_confidence else 0.99
        return BackendResponse(
            request_id=request.request_id,
            configuration_digest=request.configuration_digest,
            outcome=self.fixture.outcome,
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


@dataclass(frozen=True, slots=True)
class _ForbiddenReplayBackend:
    def __call__(
        self,
        request: ExtractionRequest,
    ) -> BackendResponse:
        raise _DriverAbort(DriverFailure.COMPOSITION)


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
        self._closure_authority = OfflineLocalClosureAuthority()
        self._scripted_confirmation: (
            ScriptedOptOutConfirmationReceipt | None
        ) = None
        self._participant_surrogate: object | None = None
        self._pre_stop_outbound_frame_count = 0
        self._post_stop_outbound_frame_delta = 0
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
        scripted_locale: str | None = None,
        stop_posture: OfflineStopPosture = (
            OfflineStopPosture.RESPONSE_PENDING
        ),
    ) -> OfflineSessionFacade | None:
        selected_locale = (
            scripted_locale
            if scripted_locale is not None
            else _FIXTURES[journey].locale
            if isinstance(journey, SyntheticJourney)
            else None
        )
        if (
            not isinstance(arm, CandidateArm)
            or not isinstance(journey, SyntheticJourney)
            or type(now_ms) is not int
            or now_ms < 0
            or not isinstance(stop_posture, OfflineStopPosture)
            or (
                journey is SyntheticJourney.OPT_OUT_WITHDRAWAL
                and selected_locale not in _TRACE_LOCALES
            )
            or (
                journey is not SyntheticJourney.OPT_OUT_WITHDRAWAL
                and scripted_locale is not None
            )
            or (
                journey is not SyntheticJourney.OPT_OUT_WITHDRAWAL
                and stop_posture
                is not OfflineStopPosture.RESPONSE_PENDING
            )
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
                scripted_locale=selected_locale,
                stop_posture=stop_posture,
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
                scripted_locale=selected_locale,
                stop_posture=stop_posture,
                frames=frames,
            )
            grant = _LeaseGrant(
                facade=facade,
                arm=arm,
                journey=journey,
                binding=binding,
                lease_id=lease_id,
                expires_at_ms=expires_at_ms,
                contract_digest=contract_digest,
                scripted_locale=selected_locale,
                stop_posture=stop_posture,
                state=OfflineSessionState.LEASED,
                revoked=False,
            )
            participant_surrogate = object()
            if (
                journey is SyntheticJourney.OPT_OUT_WITHDRAWAL
                and not self._closure_authority.register_leased(
                    facade=facade,
                    leased_record=grant,
                    driver_identity=self,
                    participant_surrogate=participant_surrogate,
                    lease_revision=0,
                    expires_at_ms=expires_at_ms,
                    arm=arm.value,
                    journey=journey.value,
                    contract_digest=contract_digest,
                    binding=binding,
                    locale=selected_locale,
                )
            ):
                self._scrub_frames(frames)
                return None
            self._leased = facade
            self._inbound_frames = frames
            self._outbound_frames = facade._outbound_frames
            self._issued_inbound_payloads = tuple(
                frame.payload for frame in frames
            )
            self._issued_outbound_payloads = []
            self._lease_grant = grant
            self._scripted_confirmation = None
            self._pre_stop_outbound_frame_count = 0
            self._post_stop_outbound_frame_delta = 0
            self._participant_surrogate = (
                participant_surrogate
                if journey is SyntheticJourney.OPT_OUT_WITHDRAWAL
                else None
            )
            return facade
        finally:
            self._lock.release()

    def confirm_scripted_opt_out(
        self,
        facade: OfflineSessionFacade,
        *,
        now_ms: int,
    ) -> ScriptedOptOutConfirmationReceipt | None:
        """Mint fixture-only pre-activation role-play confirmation."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if (
                type(now_ms) is not int
                or now_ms < 0
                or not self._accepts_facade(
                    facade,
                    expected_state=OfflineSessionState.LEASED,
                )
            ):
                return None
            grant = self._lease_grant
            participant = self._participant_surrogate
            if (
                grant is None
                or grant.journey
                is not SyntheticJourney.OPT_OUT_WITHDRAWAL
                or participant is None
            ):
                return None
            confirmation = (
                self._closure_authority.confirm_scripted_step(
                    facade=facade,
                    leased_record=grant,
                    driver_identity=self,
                    participant_surrogate=participant,
                    now_ms=now_ms,
                )
            )
            self._scripted_confirmation = confirmation
            return confirmation
        finally:
            self._lock.release()

    def withdraw(
        self,
        facade: OfflineSessionFacade,
    ) -> bool:
        """Latch participant withdrawal without acquiring the driver lock."""
        return self._closure_authority.withdraw(facade=facade)

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
            if (
                grant.journey
                is SyntheticJourney.OPT_OUT_WITHDRAWAL
            ):
                self._closure_authority.terminate(
                    facade=facade,
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
                if (
                    grant.journey
                    is SyntheticJourney.OPT_OUT_WITHDRAWAL
                ):
                    self._closure_authority.terminate(
                        facade=facade,
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
            if (
                grant.journey
                is SyntheticJourney.OPT_OUT_WITHDRAWAL
            ):
                active_grant = replace(
                    grant,
                    state=OfflineSessionState.ACTIVE,
                    revoked=False,
                )
                if not self._closure_authority.activate(
                    facade=facade,
                    leased_record=grant,
                    active_record=active_grant,
                    driver_identity=self,
                    active_revision=1,
                ):
                    terminal = self._transition_lease(
                        grant,
                        state=OfflineSessionState.ABORTED,
                        revoked=True,
                    )
                    self._closure_authority.terminate(
                        facade=facade,
                    )
                    buffers_scrubbed = self._close_facade(
                        facade,
                        terminal,
                    )
                    return self._aborted_result(
                        terminal,
                        failure=DriverFailure.INTERNAL,
                        buffers_scrubbed=buffers_scrubbed,
                    )
                self._lease_grant = active_grant
                facade._state = OfflineSessionState.ACTIVE
                facade._revoked = False
                grant = active_grant
            else:
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
            closure_cleanup_ok = (
                grant.journey
                is not SyntheticJourney.OPT_OUT_WITHDRAWAL
            )
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
                outbound_source = self._outbound_frames
                if (
                    grant.journey
                    is SyntheticJourney.OPT_OUT_WITHDRAWAL
                ):
                    closure_frame = (
                        self._closure_authority.committed_frame(
                            facade=facade,
                            active_record=grant,
                        )
                    )
                    if closure_frame is None:
                        outbound_source = []
                    else:
                        outbound_source = [
                            _MutableFrame(
                                ordinal=closure_frame.ordinal,
                                duration_ms=(
                                    closure_frame.duration_ms
                                ),
                                payload=bytearray(
                                    closure_frame.payload
                                ),
                            )
                        ]
                outbound = self._frame_snapshot(
                    outbound_source,
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
                if (
                    grant.journey
                    is SyntheticJourney.OPT_OUT_WITHDRAWAL
                ):
                    closure_termination = (
                        self._closure_authority.terminate(
                            facade=facade,
                            active_record=grant,
                        )
                    )
                    closure_cleanup_ok = (
                        closure_termination is not None
                        and closure_termination[0].phase
                        is OfflineClosurePhase.TERMINATED
                        and all(
                            not any(payload)
                            for payload
                            in closure_termination[1]
                        )
                    )
                    if not closure_cleanup_ok and failure is None:
                        failure = DriverFailure.DELIVERY
                    if closure_cleanup_ok:
                        trace.append(
                            OfflineTraceEvent(
                                ordinal=len(trace),
                                kind=(
                                    TraceKind
                                    .CLOSURE_TEARDOWN_COMPLETE
                                ),
                            )
                        )
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
                buffers_scrubbed = (
                    buffers_scrubbed and closure_cleanup_ok
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
                pre_stop_outbound_frame_count=(
                    self._pre_stop_outbound_frame_count
                ),
                post_stop_outbound_frame_delta=(
                    self._post_stop_outbound_frame_delta
                ),
                post_stop_outbound_ordinals=(
                    tuple(
                        frame.ordinal
                        for frame in captured_outbound
                    )
                    if grant.journey
                    is SyntheticJourney.OPT_OUT_WITHDRAWAL
                    else ()
                ),
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
        if (
            grant.journey
            is SyntheticJourney.OPT_OUT_WITHDRAWAL
        ):
            return self._execute_opt_out_withdrawal(
                grant=grant,
                now_ms=now_ms,
                trace=trace,
            )
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
        if (
            grant.journey
            is SyntheticJourney.UNOBSERVED_QUESTION_OUTCOMES
        ):
            return self._execute_unobserved_question_outcomes(
                assembly=assembly,
                pending=first,
                receipt=receipt,
                fixture=fixture,
                now_ms=now_ms,
                trace=trace,
            )
        if grant.journey is SyntheticJourney.INTERRUPTION_RECONNECT:
            return self._execute_interruption_reconnect(
                grant=grant,
                assembly=assembly,
                pending=first,
                receipt=receipt,
                fixture=fixture,
                now_ms=now_ms,
                trace=trace,
            )
        if (
            grant.journey
            is SyntheticJourney.SILENCE_BOUNDARY_MORE_TIME
        ):
            return self._execute_silence_boundary_more_time(
                grant=grant,
                assembly=assembly,
                pending=first,
                fixture=fixture,
                now_ms=now_ms,
                trace=trace,
            )
        if (
            grant.journey
            is SyntheticJourney.FIXED_NONTERMINAL_FALLBACKS
        ):
            return self._execute_fixed_nonterminal_fallbacks(
                grant=grant,
                assembly=assembly,
                pending=first,
                fixture=fixture,
                now_ms=now_ms,
                trace=trace,
            )
        if grant.journey is SyntheticJourney.REPEAT_SLOWER:
            return self._execute_repeat_slower(
                grant=grant,
                assembly=assembly,
                pending=first,
                fixture=fixture,
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

    def _execute_opt_out_withdrawal(
        self,
        *,
        grant: _LeaseGrant,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        """Arbitrate typed withdrawal before any stop-turn extraction."""
        stop_at_ms = now_ms + 3
        facade = grant.facade
        if self._closure_authority.is_withdrawn(
            facade=facade,
            active_record=grant,
        ):
            trace.extend((
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.PARTICIPANT_ACTIVITY,
                ),
                OfflineTraceEvent(
                    ordinal=len(trace) + 1,
                    kind=TraceKind.WITHDRAWAL_CLASSIFIED,
                ),
                OfflineTraceEvent(
                    ordinal=len(trace) + 2,
                    kind=TraceKind.GENERAL_AUTHORITY_CANCELLED,
                ),
            ))
            return stop_at_ms
        fixture = _Fixture(
            locale=grant.scripted_locale,
            content="Fixture caller requests a furnace repair.",
            fields=(
                ("language", grant.scripted_locale),
                ("intent", "service_request"),
                ("service_action", "repair"),
                ("service_object", "furnace"),
            ),
        )
        assembly = self._assembly(grant, fixture)
        cleanup_at_ms = now_ms
        assembly_sealed = False
        try:
            if self._closure_authority.is_withdrawn(
                facade=facade,
                active_record=grant,
            ):
                self._seal_latched_withdrawal(
                    assembly=assembly,
                    at_ms=now_ms,
                    trace=trace,
                )
                assembly_sealed = True
                return now_ms
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
            if self._closure_authority.is_withdrawn(
                facade=facade,
                active_record=grant,
            ):
                self._seal_latched_withdrawal(
                    assembly=assembly,
                    at_ms=receipt.at_ms,
                    trace=trace,
                )
                assembly_sealed = True
                return receipt.at_ms
            pending = assembly.transaction.execute(
                receipt,
                content=fixture.content,
                backend=_SyntheticObservationBackend(fixture),
                now_ms=max(now_ms + 2, receipt.at_ms),
            )
            trace.append(
                self._composition_trace(
                    trace,
                    pending,
                    locale=grant.scripted_locale,
                )
            )
            if (
                pending.status
                is not CompositionStatus.RESPONSE_PENDING
                or pending.act_kinds
                != (VoiceSemanticActKind.QUESTION,)
            ):
                raise _DriverAbort(DriverFailure.COMPOSITION)
            checkpoint = self._prepare_stop_checkpoint(
                grant=grant,
                assembly=assembly,
                pending=pending,
                at_ms=max(now_ms + 3, receipt.at_ms + 2),
                trace=trace,
            )
            stop_at_ms = max(stop_at_ms, checkpoint.at_ms + 1)
            cleanup_at_ms = stop_at_ms
            self._pre_stop_outbound_frame_count = (
                checkpoint.baseline_frame_count
            )
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.PARTICIPANT_ACTIVITY,
                )
            )
            confirmation = self._scripted_confirmation
            if confirmation is None:
                self._closure_authority.withdraw(
                    facade=facade,
                )
            if not self._seal_composition_assembly(
                assembly=assembly,
                at_ms=stop_at_ms,
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            assembly_sealed = True
            if not self._clear_general_outbound(
                checkpoint=checkpoint,
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            inventory = self._closure_inventory(
                assembly=assembly,
                queued_outbound_frames=len(
                    self._outbound_frames
                ),
            )
            if (
                not inventory.is_sealed
                or not self._stop_checkpoint_invalidated(
                    assembly=assembly,
                    checkpoint=checkpoint,
                    at_ms=stop_at_ms,
                )
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            trace.extend((
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.GENERAL_AUTHORITY_CANCELLED,
                ),
                OfflineTraceEvent(
                    ordinal=len(trace) + 1,
                    kind=TraceKind.GENERAL_OUTBOUND_CLEARED,
                ),
            ))
            if (
                confirmation is None
                or self._closure_authority.is_withdrawn(
                    facade=facade,
                    active_record=grant,
                )
            ):
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=TraceKind.WITHDRAWAL_CLASSIFIED,
                    )
                )
                return stop_at_ms
            capability = self._closure_authority.mint_capability(
                facade=facade,
                active_record=grant,
                confirmation=confirmation,
                inventory=inventory,
                now_ms=stop_at_ms,
            )
            if capability is None:
                withdrawn = (
                    self._closure_authority.is_withdrawn(
                        facade=facade,
                        active_record=grant,
                    )
                )
                if withdrawn:
                    trace.append(
                        OfflineTraceEvent(
                            ordinal=len(trace),
                            kind=TraceKind.PARTICIPANT_ACTIVITY,
                        )
                    )
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=(
                            TraceKind.WITHDRAWAL_CLASSIFIED
                            if withdrawn
                            else (
                                TraceKind
                                .CLOSURE_PREDICATE_REJECTED
                            )
                        ),
                    )
                )
                return stop_at_ms
            trace.extend((
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.SCRIPTED_OPT_OUT_AUTHORIZED,
                    locale=grant.scripted_locale,
                ),
                OfflineTraceEvent(
                    ordinal=len(trace) + 1,
                    kind=TraceKind.CLOSURE_CAPABILITY_RETAINED,
                    locale=grant.scripted_locale,
                ),
            ))
            stage = self._closure_authority.stage(
                facade=facade,
                active_record=grant,
                capability=capability,
                now_ms=stop_at_ms + 1,
                max_frame_bytes=(
                    self._limits.max_outbound_frame_bytes
                ),
                max_outbound_frames=(
                    self._limits.max_outbound_frames
                ),
                max_outbound_bytes=(
                    self._limits.max_outbound_bytes
                ),
                max_outbound_audio_ms=(
                    self._limits.max_outbound_audio_ms
                ),
            )
            if stage is None:
                withdrawn = (
                    self._closure_authority.is_withdrawn(
                        facade=facade,
                        active_record=grant,
                    )
                )
                if withdrawn:
                    trace.append(
                        OfflineTraceEvent(
                            ordinal=len(trace),
                            kind=TraceKind.PARTICIPANT_ACTIVITY,
                        )
                    )
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=(
                            TraceKind.WITHDRAWAL_CLASSIFIED
                            if withdrawn
                            else (
                                TraceKind
                                .CLOSURE_PREDICATE_REJECTED
                            )
                        ),
                    )
                )
                return stop_at_ms + 1
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.CLOSURE_STAGED,
                    semantic_act_kind=VoiceSemanticActKind.OPT_OUT,
                    locale=stage.locale,
                    content_digest=stage.text_digest,
                )
            )
            commit = self._closure_authority.commit(
                facade=facade,
                active_record=grant,
                capability=capability,
                stage=stage,
                now_ms=stop_at_ms + 2,
            )
            if commit is None:
                withdrawn = (
                    self._closure_authority.is_withdrawn(
                        facade=facade,
                        active_record=grant,
                    )
                )
                if withdrawn:
                    trace.append(
                        OfflineTraceEvent(
                            ordinal=len(trace),
                            kind=TraceKind.PARTICIPANT_ACTIVITY,
                        )
                    )
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=(
                            TraceKind.WITHDRAWAL_CLASSIFIED
                            if withdrawn
                            else (
                                TraceKind
                                .CLOSURE_PREDICATE_REJECTED
                            )
                        ),
                    )
                )
                return stop_at_ms + 2
            self._post_stop_outbound_frame_delta = (
                commit.frame_count
            )
            self._trace_committed_closure(
                trace=trace,
                commit=commit,
            )
            if self._closure_authority.mark_synthetic_playback(
                facade=facade,
                active_record=grant,
                commit=commit,
            ):
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=TraceKind.SYNTHETIC_PLAYBACK_OBSERVED,
                        semantic_act_kind=(
                            VoiceSemanticActKind.OPT_OUT
                        ),
                        locale=commit.locale,
                        content_digest=commit.text_digest,
                    )
                )
            return stop_at_ms + 3
        finally:
            if (
                not assembly_sealed
                and not self._seal_composition_assembly(
                    assembly=assembly,
                    at_ms=cleanup_at_ms,
                )
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)

    def _prepare_stop_checkpoint(
        self,
        *,
        grant: _LeaseGrant,
        assembly: _Assembly,
        pending: CompositionResult,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> _StopCheckpoint:
        """Enter one exact pre-stop posture without caller observation."""
        posture = grant.stop_posture
        timer: CallIntent | None = None
        checkpoint_at_ms = at_ms
        if posture is OfflineStopPosture.RESPONSE_PENDING:
            pass
        elif posture is OfflineStopPosture.PLAYOUT_BOUND_QUEUED:
            checkpoint_at_ms = (
                self._bind_stop_question(
                    assembly=assembly,
                    pending=pending,
                    at_ms=at_ms,
                    trace=trace,
                    resolve_transport=False,
                )
            )
        elif (
            posture
            is OfflineStopPosture.TRANSPORT_RESOLVED_UNOBSERVED
        ):
            checkpoint_at_ms = self._bind_stop_question(
                assembly=assembly,
                pending=pending,
                at_ms=at_ms,
                trace=trace,
                resolve_transport=True,
            )
        elif (
            posture
            is OfflineStopPosture.PLAYOUT_PARTIAL_UNOBSERVED
        ):
            checkpoint_at_ms, _ = (
                self._stage_unobserved_question(
                    assembly=assembly,
                    pending=pending,
                    event_kind=VoiceEventKind.PLAYOUT_PARTIAL,
                    trace_kind=TraceKind.PLAYOUT_PARTIAL,
                    resolve_transport=False,
                    at_ms=at_ms,
                    trace=trace,
                )
            )
        elif posture is OfflineStopPosture.REPLAY_PENDING:
            checkpoint_at_ms, timer = (
                self._deliver_question_for_silence(
                    assembly=assembly,
                    pending=pending,
                    at_ms=at_ms,
                    trace=trace,
                )
            )
            replay_fixture = _Fixture(
                locale=grant.scripted_locale,
                content="repeat",
                fields=(),
                outcome=BackendOutcome.CANCELLED,
            )
            replay_receipt = self._admit_final_turn(
                assembly=assembly,
                fixture=replay_fixture,
                turn_number=2,
                at_ms=checkpoint_at_ms + 1,
                semantic_act_kind=VoiceSemanticActKind.REPEAT,
            )
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.REPLAY_REQUESTED,
                    semantic_act_kind=VoiceSemanticActKind.REPEAT,
                    locale=grant.scripted_locale,
                    replay_mode=ReplayMode.EXACT,
                )
            )
            replay = assembly.transaction.execute_replay(
                replay_receipt,
                content=replay_fixture.content,
                command=VoiceSemanticActKind.REPEAT,
                now_ms=replay_receipt.at_ms,
            )
            trace.append(
                self._composition_trace(
                    trace,
                    replay,
                    locale=grant.scripted_locale,
                )
            )
            if (
                replay.status is not CompositionStatus.REPLAY_PENDING
                or replay.replay_mode is not ReplayMode.EXACT
                or replay.act_kinds
                != (VoiceSemanticActKind.QUESTION,)
                or assembly.calls.phase
                is not SilencePhase.QUESTION_RESERVED
            ):
                raise _DriverAbort(DriverFailure.COMPOSITION)
            checkpoint_at_ms = replay_receipt.at_ms
        elif posture is OfflineStopPosture.SILENCE_TIMER_ARMED:
            checkpoint_at_ms, timer = (
                self._deliver_question_for_silence(
                    assembly=assembly,
                    pending=pending,
                    at_ms=at_ms,
                    trace=trace,
                )
            )
        else:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        frames = tuple(self._outbound_frames)
        if (
            any(
                frame.ordinal != index
                for index, frame in enumerate(frames)
            )
            or (
                posture is OfflineStopPosture.RESPONSE_PENDING
                and frames
            )
            or (
                posture is not OfflineStopPosture.RESPONSE_PENDING
                and len(frames) != 1
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        return _StopCheckpoint(
            at_ms=checkpoint_at_ms,
            state_version=assembly.state.version,
            state_snapshot=(
                assembly.state.current_state().to_dict()
            ),
            act_ids=assembly.speech.act_ids_for_binding(
                assembly.binding
            ),
            timer=timer,
            baseline_frame_count=len(frames),
            baseline_last_ordinal=(
                frames[-1].ordinal if frames else None
            ),
        )

    def _bind_stop_question(
        self,
        *,
        assembly: _Assembly,
        pending: CompositionResult,
        at_ms: int,
        trace: list[OfflineTraceEvent],
        resolve_transport: bool,
    ) -> int:
        if (
            pending.status is not CompositionStatus.RESPONSE_PENDING
            or pending.act_kinds
            != (VoiceSemanticActKind.QUESTION,)
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        authorization = assembly.transaction.authorization_receipt(
            pending.act_ids[0]
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
                semantic_act_kind=VoiceSemanticActKind.QUESTION,
            )
        )
        payload = self._playout_payload(
            authorization=authorization,
            text_digest=assembly.speech.authorized_text_digest(
                authorization.semantic_act_id
            ),
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
        if (
            not assembly.lifecycle.ingest(tts)
            or not assembly.transaction.accept_tts_binding(event=tts)
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        playout = self._event_after(
            assembly.lifecycle,
            authorization,
            kind=VoiceEventKind.PLAYOUT_BOUND,
            source=VoiceSource.LOCAL_AUTHORITATIVE,
            payload=payload,
            at_ms=tts.at_ms + 1,
        )
        if (
            not assembly.lifecycle.ingest(playout)
            or not assembly.transaction.accept_playout_binding(
                event=playout
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        self._append_outbound_frame(
            authorization=authorization,
        )
        if not resolve_transport:
            return playout.at_ms
        transport = self._event_after(
            assembly.lifecycle,
            authorization,
            kind=VoiceEventKind.TRANSPORT_RESOLVED,
            source=VoiceSource.TWILIO_AUTHENTICATED,
            payload=payload,
            at_ms=playout.at_ms + 1,
        )
        if (
            not assembly.lifecycle.ingest(transport)
            or not assembly.transaction.accept_transport_resolution(
                event=transport,
                event_id=f"transport_{transport.sequence}",
                sequence=transport.sequence,
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.TRANSPORT_RESOLVED,
                semantic_act_kind=VoiceSemanticActKind.QUESTION,
            )
        )
        return transport.at_ms

    def _clear_general_outbound(
        self,
        *,
        checkpoint: _StopCheckpoint,
    ) -> bool:
        frames = tuple(self._outbound_frames)
        if (
            len(frames) != checkpoint.baseline_frame_count
            or (
                frames[-1].ordinal if frames else None
            )
            != checkpoint.baseline_last_ordinal
        ):
            return False
        payloads = self._scrub_frames(frames)
        self._outbound_frames.clear()
        return (
            len(payloads) == len(frames)
            and all(not any(payload) for payload in payloads)
            and not self._outbound_frames
        )

    @staticmethod
    def _stop_checkpoint_invalidated(
        *,
        assembly: _Assembly,
        checkpoint: _StopCheckpoint,
        at_ms: int,
    ) -> bool:
        if (
            assembly.state.version != checkpoint.state_version
            or assembly.state.current_state().to_dict()
            != checkpoint.state_snapshot
            or assembly.transaction.pending_response_count
            or assembly.receipts.unconsumed_receipt_count
            or assembly.silence.pending_count
            or assembly.calls.phase is not SilencePhase.TERMINATED
            or not assembly.calls.is_quiescent
            or not assembly.adapter.terminally_closed
            or any(
                assembly.speech.is_live(act_id)
                or assembly.transaction.authorization_receipt(
                    act_id
                )
                is not None
                for act_id in checkpoint.act_ids
            )
        ):
            return False
        timer = checkpoint.timer
        if timer is None:
            return True
        sequence, canonical_at_ms = assembly.calls.next_position(
            at_ms=max(at_ms, timer.deadline_ms or at_ms)
        )
        return not assembly.calls.timer_fired(
            binding=assembly.binding,
            event_id=f"stale_stop_timer_{sequence}",
            sequence=sequence,
            action_id=timer.action_id,
            revision=timer.revision,
            now_ms=canonical_at_ms,
        )

    @staticmethod
    def _seal_latched_withdrawal(
        *,
        assembly: _Assembly,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> None:
        trace.extend((
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.PARTICIPANT_ACTIVITY,
            ),
            OfflineTraceEvent(
                ordinal=len(trace) + 1,
                kind=TraceKind.WITHDRAWAL_CLASSIFIED,
            ),
        ))
        if not OfflineSessionDriver._seal_composition_assembly(
            assembly=assembly,
            at_ms=at_ms,
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        inventory = OfflineSessionDriver._closure_inventory(
            assembly=assembly,
            queued_outbound_frames=0,
        )
        if not inventory.is_sealed:
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.GENERAL_AUTHORITY_CANCELLED,
            )
        )

    @staticmethod
    def _closure_inventory(
        *,
        assembly: _Assembly,
        queued_outbound_frames: int,
    ) -> OfflineAuthorityInventory:
        act_ids = assembly.speech.act_ids_for_binding(
            assembly.binding
        )
        return OfflineAuthorityInventory(
            transaction_pending=(
                assembly.transaction.pending_response_count
            ),
            admission_receipts=(
                assembly.receipts.unconsumed_receipt_count
            ),
            silence_pending=assembly.silence.pending_count,
            speech_batches=(
                assembly.speech.reservation_batch_count(
                    assembly.binding
                )
            ),
            live_speech_acts=sum(
                assembly.speech.is_live(act_id)
                for act_id in act_ids
            ),
            queued_outbound_frames=queued_outbound_frames,
            call_quiescent=assembly.calls.is_quiescent,
            call_terminated=(
                assembly.calls.phase is SilencePhase.TERMINATED
            ),
            adapter_terminally_closed=(
                assembly.adapter.terminally_closed
            ),
        )

    @staticmethod
    def _trace_committed_closure(
        *,
        trace: list[OfflineTraceEvent],
        commit: OfflineClosureCommitReceipt,
    ) -> None:
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.OFFLINE_OUTBOUND_COMMITTED,
                semantic_act_kind=VoiceSemanticActKind.OPT_OUT,
                locale=commit.locale,
                content_digest=commit.text_digest,
            )
        )

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

    def _execute_repeat_slower(
        self,
        *,
        grant: _LeaseGrant,
        assembly: _Assembly,
        pending: CompositionResult,
        fixture: _Fixture,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        """Qualify exact repeat and sealed slower render in four locales."""
        end_ms = self._run_repeat_slower_locale(
            assembly=assembly,
            pending=pending,
            locale=fixture.locale,
            at_ms=now_ms + 5,
            trace=trace,
        )
        for offset, locale in enumerate(
            ("es", "pt", "zh"),
            start=1,
        ):
            localized = _Fixture(
                locale=locale,
                content=(
                    "Fixture caller requests a furnace repair in "
                    f"locale {locale} before replay commands."
                ),
                fields=(
                    ("language", locale),
                    ("intent", "service_request"),
                    ("service_action", "repair"),
                    ("service_object", "furnace"),
                ),
            )
            isolated = self._fresh_silence_assembly(
                grant=grant,
                fixture=localized,
                suffix=f"replay_{locale}",
                epoch_offset=offset,
            )
            cleanup_at_ms = end_ms + 1
            try:
                localized_pending, start_ms = (
                    self._start_silence_question(
                        assembly=isolated,
                        fixture=localized,
                        turn_number=1,
                        at_ms=cleanup_at_ms,
                        trace=trace,
                    )
                )
                cleanup_at_ms = start_ms + 1
                end_ms = self._run_repeat_slower_locale(
                    assembly=isolated,
                    pending=localized_pending,
                    locale=locale,
                    at_ms=cleanup_at_ms,
                    trace=trace,
                )
            except Exception:
                if not self._seal_composition_assembly(
                    assembly=isolated,
                    at_ms=cleanup_at_ms,
                ):
                    raise _DriverAbort(
                        DriverFailure.DELIVERY
                    ) from None
                raise
        return end_ms

    def _run_repeat_slower_locale(
        self,
        *,
        assembly: _Assembly,
        pending: CompositionResult,
        locale: str,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        cleanup_at_ms = at_ms
        try:
            if (
                locale not in _TRACE_LOCALES
                or pending.status
                is not CompositionStatus.RESPONSE_PENDING
                or pending.act_kinds
                != (VoiceSemanticActKind.QUESTION,)
            ):
                raise _DriverAbort(DriverFailure.COMPOSITION)
            observed, end_ms = self._deliver_pending(
                assembly=assembly,
                pending=pending,
                at_ms=at_ms,
                trace=trace,
            )
            cleanup_at_ms = end_ms
            if (
                observed.status
                is not CompositionStatus.RESPONSE_OBSERVED
                or assembly.calls.phase
                is not SilencePhase.FIRST_ARMED
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            original_act_id = observed.act_ids[0]
            exact_digest = (
                assembly.speech.authorized_text_digest(
                    original_act_id
                )
            )
            if exact_digest is None:
                raise _DriverAbort(DriverFailure.DELIVERY)
            state_version = assembly.state.version
            state_snapshot = (
                assembly.state.current_state().to_dict()
            )
            source_act_id = original_act_id
            for turn_number, (
                command,
                mode,
                content,
            ) in enumerate(
                (
                    (
                        VoiceSemanticActKind.REPEAT,
                        ReplayMode.EXACT,
                        "repeat",
                    ),
                    (
                        VoiceSemanticActKind.SLOWER_SPEECH,
                        ReplayMode.SLOWER,
                        "slower",
                    ),
                ),
                start=2,
            ):
                command_fixture = _Fixture(
                    locale=locale,
                    content=content,
                    fields=(),
                    outcome=BackendOutcome.CANCELLED,
                )
                receipt = self._admit_final_turn(
                    assembly=assembly,
                    fixture=command_fixture,
                    turn_number=turn_number,
                    at_ms=end_ms + 1,
                    semantic_act_kind=command,
                )
                trace.append(
                    OfflineTraceEvent(
                        ordinal=len(trace),
                        kind=TraceKind.REPLAY_REQUESTED,
                        semantic_act_kind=command,
                        locale=locale,
                        replay_mode=mode,
                    )
                )
                replay = assembly.transaction.execute_replay(
                    receipt,
                    content=content,
                    command=command,
                    now_ms=receipt.at_ms,
                )
                trace.append(
                    self._composition_trace(
                        trace,
                        replay,
                        locale=locale,
                    )
                )
                if (
                    replay.status
                    is not CompositionStatus.REPLAY_PENDING
                    or replay.replay_mode is not mode
                    or replay.replay_source_act_id
                    != source_act_id
                    or replay.act_kinds
                    != (VoiceSemanticActKind.QUESTION,)
                    or assembly.state.version != state_version
                    or assembly.state.current_state().to_dict()
                    != state_snapshot
                    or assembly.calls.phase
                    is not SilencePhase.QUESTION_RESERVED
                ):
                    raise _DriverAbort(DriverFailure.COMPOSITION)
                replay_act_id = replay.act_ids[0]
                replay_binding = (
                    assembly.speech.replay_binding(
                        replay_act_id
                    )
                )
                if (
                    replay_binding is None
                    or replay_binding.source_act_id
                    != source_act_id
                    or replay_binding.request_id
                    != receipt.receipt_id
                    or replay_binding.mode is not mode
                    or replay_binding.text_digest
                    != exact_digest
                    or (
                        assembly.speech
                        .authorized_text_digest(replay_act_id)
                    )
                    != exact_digest
                ):
                    raise _DriverAbort(DriverFailure.DELIVERY)
                replay_observed, end_ms = (
                    self._deliver_pending(
                        assembly=assembly,
                        pending=replay,
                        at_ms=max(
                            end_ms + 2,
                            receipt.at_ms + 1,
                        ),
                        trace=trace,
                    )
                )
                cleanup_at_ms = end_ms
                if (
                    replay_observed.status
                    is not CompositionStatus.REPLAY_OBSERVED
                    or replay_observed.replay_mode is not mode
                    or assembly.state.version != state_version
                    or assembly.state.current_state().to_dict()
                    != state_snapshot
                    or assembly.calls.phase
                    is not SilencePhase.FIRST_ARMED
                ):
                    raise _DriverAbort(DriverFailure.DELIVERY)
                source_act_id = replay_act_id
            return end_ms
        finally:
            if not self._seal_composition_assembly(
                assembly=assembly,
                at_ms=cleanup_at_ms + 1,
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)

    @staticmethod
    def _seal_composition_assembly(
        *,
        assembly: _Assembly,
        at_ms: int,
    ) -> bool:
        """Seal transaction and shared lifecycle inventories exactly once."""
        try:
            transaction_sealed = (
                assembly.transaction.abort(at_ms=at_ms)
            )
        except Exception:  # noqa: BLE001
            transaction_sealed = False
        try:
            silence_sealed = assembly.silence.abort(
                at_ms=at_ms
            )
        except Exception:  # noqa: BLE001
            silence_sealed = False
        return (
            transaction_sealed
            and silence_sealed
            and assembly.transaction.pending_response_count == 0
            and assembly.receipts.unconsumed_receipt_count == 0
            and assembly.calls.is_quiescent
            and assembly.calls.phase is SilencePhase.TERMINATED
            and assembly.adapter.terminally_closed
            and assembly.speech.reservation_batch_count(
                assembly.binding
            )
            == 0
            and all(
                not assembly.speech.is_live(act_id)
                for act_id in (
                    assembly.speech.act_ids_for_binding(
                        assembly.binding
                    )
                )
            )
        )

    def _execute_silence_boundary_more_time(
        self,
        *,
        grant: _LeaseGrant,
        assembly: _Assembly,
        pending: CompositionResult,
        fixture: _Fixture,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        """Qualify exact boundary and both more-time branches in isolation."""
        boundary_end_ms, boundary_timer = (
            self._deliver_question_for_silence(
                assembly=assembly,
                pending=pending,
                at_ms=now_ms + 5,
                trace=trace,
            )
        )
        boundary_sequence, boundary_at_ms = (
            assembly.calls.next_position(
                at_ms=boundary_timer.deadline_ms or 0
            )
        )
        cancelled = assembly.calls.cancel(
            binding=assembly.binding,
            event_id="boundary_caller_activity",
            sequence=boundary_sequence,
            at_ms=boundary_at_ms,
        )
        assembly.adapter.terminalize_permit_admission()
        if (
            tuple(intent.kind for intent in cancelled)
            != (
                CallIntentKind.CANCEL_TIMER,
                CallIntentKind.CANCEL_ACT,
            )
            or cancelled[1].act_id is None
            or not assembly.speech.cancel(
                cancelled[1].act_id,
                reason=CancellationReason.CALLER_ACTIVITY,
            )
            or assembly.calls.timer_fired(
                binding=assembly.binding,
                event_id="stale_boundary_timer",
                sequence=boundary_sequence + 1,
                action_id=boundary_timer.action_id,
                revision=boundary_timer.revision,
                now_ms=boundary_at_ms,
            )
            or not assembly.calls.is_quiescent
            or assembly.speech.is_live(cancelled[1].act_id)
            or assembly.transaction.pending_response_count
            or assembly.receipts.unconsumed_receipt_count
            or not assembly.adapter.terminally_closed
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.CALLER_ACTIVITY_AT_BOUNDARY,
            )
        )

        before = self._fresh_silence_assembly(
            grant=grant,
            fixture=fixture,
            suffix="before_presence",
            epoch_offset=1,
        )
        before_end_ms = self._run_more_time_before_presence(
            assembly=before,
            fixture=fixture,
            at_ms=max(boundary_end_ms, boundary_at_ms) + 1,
            trace=trace,
        )

        after = self._fresh_silence_assembly(
            grant=grant,
            fixture=fixture,
            suffix="after_presence",
            epoch_offset=2,
        )
        after_end_ms = self._run_more_time_after_presence(
            assembly=after,
            fixture=fixture,
            at_ms=before_end_ms + 1,
            trace=trace,
        )
        return after_end_ms

    def _execute_fixed_nonterminal_fallbacks(
        self,
        *,
        grant: _LeaseGrant,
        assembly: _Assembly,
        pending: CompositionResult,
        fixture: _Fixture,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        """Qualify reviewed nonterminal fallbacks in isolated sessions."""
        end_ms, timer = self._deliver_question_for_silence(
            assembly=assembly,
            pending=pending,
            at_ms=now_ms + 5,
            trace=trace,
        )
        end_ms = self._run_fixed_nonterminal_fallback(
            assembly=assembly,
            timer=timer,
            kind=CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
            event_id="unsupported_access_at_boundary",
            at_ms=timer.deadline_ms or end_ms,
            trace=trace,
        )

        voicemail = self._fresh_silence_assembly(
            grant=grant,
            fixture=fixture,
            suffix="simulated_voicemail",
            epoch_offset=1,
        )
        voicemail_pending, start_ms = self._start_silence_question(
            assembly=voicemail,
            fixture=fixture,
            turn_number=2,
            at_ms=end_ms + 1,
            trace=trace,
        )
        end_ms, voicemail_timer = (
            self._deliver_question_for_silence(
                assembly=voicemail,
                pending=voicemail_pending,
                at_ms=start_ms + 1,
                trace=trace,
            )
        )
        return self._run_fixed_nonterminal_fallback(
            assembly=voicemail,
            timer=voicemail_timer,
            kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
            event_id="simulated_voicemail_before_deadline",
            at_ms=end_ms + 1,
            trace=trace,
        )

    def _run_fixed_nonterminal_fallback(
        self,
        *,
        assembly: _Assembly,
        timer: CallIntent,
        kind: CallIntentKind,
        event_id: str,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        cleanup_at_ms = at_ms if type(at_ms) is int else 0
        try:
            if (
                not isinstance(kind, CallIntentKind)
                or kind
                not in {
                    CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
                    CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
                }
                or not isinstance(timer, CallIntent)
                or timer.kind is not CallIntentKind.ARM_TIMER
                or timer.deadline_ms is None
                or timer.turn_id is None
                or timer.turn_sequence is None
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            sequence, canonical_at_ms = (
                assembly.calls.next_position(at_ms=at_ms)
            )
            intents = assembly.calls.request_fixed_fallback(
                kind=kind,
                binding=assembly.binding,
                event_id=event_id,
                sequence=sequence,
                at_ms=canonical_at_ms,
                turn_id=timer.turn_id,
                turn_sequence=timer.turn_sequence,
            )
            if (
                tuple(intent.kind for intent in intents)
                != (
                    CallIntentKind.CANCEL_TIMER,
                    CallIntentKind.CANCEL_ACT,
                    kind,
                )
                or intents[1].act_id is None
                or intents[2].act_id is None
                or not assembly.speech.cancel(
                    intents[1].act_id,
                    reason=CancellationReason.CALLER_ACTIVITY,
                )
                or assembly.calls.timer_receipt() is not None
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.FIXED_FALLBACK_REQUESTED,
                    semantic_act_kind=(
                        VoiceSemanticActKind.ACKNOWLEDGEMENT
                    ),
                )
            )
            observed, end_ms = self._deliver_lifecycle_act(
                assembly=assembly,
                intent=intents[2],
                at_ms=canonical_at_ms + 1,
                trace=trace,
            )
            cleanup_at_ms = max(cleanup_at_ms, end_ms)
            stale_sequence, stale_at_ms = (
                assembly.calls.next_position(
                    at_ms=max(end_ms, timer.deadline_ms)
                )
            )
            if (
                observed.status
                is not LifecycleActStatus.OBSERVED
                or observed.request_kind is not kind
                or observed.emitted_intents
                or assembly.calls.timer_fired(
                    binding=assembly.binding,
                    event_id=f"stale_{event_id}",
                    sequence=stale_sequence,
                    action_id=timer.action_id,
                    revision=timer.revision,
                    now_ms=stale_at_ms,
                )
                or not assembly.calls.is_quiescent
                or assembly.silence.pending_count
                or assembly.silence.is_terminal
                or assembly.transaction.pending_response_count
                or assembly.receipts.unconsumed_receipt_count
                or assembly.speech.is_live(intents[1].act_id)
                or not assembly.speech.is_live(observed.act_id)
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            return end_ms
        finally:
            try:
                sealed = assembly.silence.abort(
                    at_ms=cleanup_at_ms
                )
            except Exception:  # noqa: BLE001
                sealed = False
            if not sealed:
                raise _DriverAbort(DriverFailure.DELIVERY)

    def _run_more_time_before_presence(
        self,
        *,
        assembly: _Assembly,
        fixture: _Fixture,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        pending, start_ms = self._start_silence_question(
            assembly=assembly,
            fixture=fixture,
            turn_number=2,
            at_ms=at_ms,
            trace=trace,
        )
        end_ms, original_timer = self._deliver_question_for_silence(
            assembly=assembly,
            pending=pending,
            at_ms=start_ms + 1,
            trace=trace,
        )
        acknowledgement, request_at_ms = self._request_more_time(
            assembly=assembly,
            at_ms=end_ms,
            event_id="more_time_before_presence",
            trace=trace,
        )
        observed, end_ms = self._deliver_lifecycle_act(
            assembly=assembly,
            intent=acknowledgement,
            at_ms=request_at_ms + 1,
            trace=trace,
        )
        extension = self._single_emitted(
            observed,
            CallIntentKind.ARM_TIMER,
        )
        if (
            assembly.calls.timer_fired(
                binding=assembly.binding,
                event_id="stale_pre_extension_timer",
                sequence=(
                    assembly.calls.next_position(
                        at_ms=end_ms
                    )[0]
                ),
                action_id=original_timer.action_id,
                revision=original_timer.revision,
                now_ms=end_ms,
            )
            or assembly.calls.timer_receipt() is not extension
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        immutable = (
            extension.action_id,
            extension.revision,
            extension.deadline_ms,
        )
        repeat_sequence, repeat_at_ms = (
            assembly.calls.next_position(at_ms=end_ms + 1)
        )
        if assembly.calls.request_more_time(
            binding=assembly.binding,
            event_id="repeat_more_time_before_presence",
            sequence=repeat_sequence,
            at_ms=repeat_at_ms,
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        current = assembly.calls.timer_receipt()
        if (
            current is not extension
            or (
                current.action_id,
                current.revision,
                current.deadline_ms,
            )
            != immutable
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.MORE_TIME_IGNORED,
            )
        )
        presence = self._fire_timer(
            assembly=assembly,
            timer=extension,
            event_id="extension_before_presence",
            expected=CallIntentKind.REQUEST_PRESENCE_CHECK,
        )
        presence_observed, end_ms = self._deliver_lifecycle_act(
            assembly=assembly,
            intent=presence,
            at_ms=extension.deadline_ms or end_ms,
            trace=trace,
        )
        second_timer = self._single_emitted(
            presence_observed,
            CallIntentKind.ARM_TIMER,
        )
        closing = self._fire_timer(
            assembly=assembly,
            timer=second_timer,
            event_id="second_silence_before_presence",
            expected=CallIntentKind.REQUEST_CLOSING,
        )
        terminal, end_ms = self._deliver_lifecycle_act(
            assembly=assembly,
            intent=closing,
            at_ms=second_timer.deadline_ms or end_ms,
            trace=trace,
        )
        return self._finish_local_terminal(
            assembly=assembly,
            result=terminal,
            at_ms=end_ms,
            trace=trace,
        )

    def _run_more_time_after_presence(
        self,
        *,
        assembly: _Assembly,
        fixture: _Fixture,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        pending, start_ms = self._start_silence_question(
            assembly=assembly,
            fixture=fixture,
            turn_number=3,
            at_ms=at_ms,
            trace=trace,
        )
        end_ms, first_timer = self._deliver_question_for_silence(
            assembly=assembly,
            pending=pending,
            at_ms=start_ms + 1,
            trace=trace,
        )
        presence = self._fire_timer(
            assembly=assembly,
            timer=first_timer,
            event_id="first_silence_after_presence",
            expected=CallIntentKind.REQUEST_PRESENCE_CHECK,
        )
        presence_observed, end_ms = self._deliver_lifecycle_act(
            assembly=assembly,
            intent=presence,
            at_ms=first_timer.deadline_ms or end_ms,
            trace=trace,
        )
        second_timer = self._single_emitted(
            presence_observed,
            CallIntentKind.ARM_TIMER,
        )
        if assembly.calls.timer_receipt() is not second_timer:
            raise _DriverAbort(DriverFailure.DELIVERY)
        acknowledgement, request_at_ms = self._request_more_time(
            assembly=assembly,
            at_ms=end_ms,
            event_id="more_time_after_presence",
            trace=trace,
        )
        observed, end_ms = self._deliver_lifecycle_act(
            assembly=assembly,
            intent=acknowledgement,
            at_ms=request_at_ms + 1,
            trace=trace,
        )
        extension = self._single_emitted(
            observed,
            CallIntentKind.ARM_TIMER,
        )
        closing = self._fire_timer(
            assembly=assembly,
            timer=extension,
            event_id="extension_after_presence",
            expected=CallIntentKind.REQUEST_CLOSING,
        )
        terminal, end_ms = self._deliver_lifecycle_act(
            assembly=assembly,
            intent=closing,
            at_ms=extension.deadline_ms or end_ms,
            trace=trace,
        )
        return self._finish_local_terminal(
            assembly=assembly,
            result=terminal,
            at_ms=end_ms,
            trace=trace,
        )

    def _start_silence_question(
        self,
        *,
        assembly: _Assembly,
        fixture: _Fixture,
        turn_number: int,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> tuple[CompositionResult, int]:
        receipt = self._admit_final_turn(
            assembly=assembly,
            fixture=fixture,
            turn_number=turn_number,
            at_ms=at_ms,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.INPUT_FINAL,
            )
        )
        execute_at_ms = max(at_ms + 1, receipt.at_ms)
        pending = assembly.transaction.execute(
            receipt,
            content=fixture.content,
            backend=_SyntheticObservationBackend(fixture),
            now_ms=execute_at_ms,
        )
        trace.append(
            self._composition_trace(
                trace,
                pending,
                locale=self._trace_locale(
                    assembly.state.current_state().language
                ),
            )
        )
        if (
            pending.status is not CompositionStatus.RESPONSE_PENDING
            or pending.act_kinds
            != (VoiceSemanticActKind.QUESTION,)
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        return pending, execute_at_ms

    def _deliver_question_for_silence(
        self,
        *,
        assembly: _Assembly,
        pending: CompositionResult,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> tuple[int, CallIntent]:
        if pending.act_kinds != (VoiceSemanticActKind.QUESTION,):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        observed, end_ms = self._deliver_pending(
            assembly=assembly,
            pending=pending,
            at_ms=at_ms,
            trace=trace,
        )
        timer = assembly.calls.timer_receipt()
        if (
            observed.status is not CompositionStatus.RESPONSE_OBSERVED
            or timer is None
            or timer.kind is not CallIntentKind.ARM_TIMER
            or timer.deadline_ms is None
            or timer.deadline_ms
            != at_ms + 4 + 10_000
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.SILENCE_TIMER_ARMED,
            )
        )
        return end_ms, timer

    def _request_more_time(
        self,
        *,
        assembly: _Assembly,
        at_ms: int,
        event_id: str,
        trace: list[OfflineTraceEvent],
    ) -> tuple[CallIntent, int]:
        sequence, canonical_at_ms = assembly.calls.next_position(
            at_ms=at_ms
        )
        intents = assembly.calls.request_more_time(
            binding=assembly.binding,
            event_id=event_id,
            sequence=sequence,
            at_ms=canonical_at_ms,
        )
        if (
            tuple(intent.kind for intent in intents)
            != (
                CallIntentKind.CANCEL_TIMER,
                CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT,
            )
            or assembly.calls.timer_receipt() is not None
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.MORE_TIME_ACCEPTED,
            )
        )
        return intents[1], canonical_at_ms

    def _deliver_lifecycle_act(
        self,
        *,
        assembly: _Assembly,
        intent: CallIntent,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> tuple[LifecycleActResult, int]:
        pending = assembly.silence.prepare(intent, at_ms=at_ms)
        if (
            pending is None
            or pending.status is not LifecycleActStatus.PENDING
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        authorization = assembly.silence.authorization_receipt(
            pending.act_id
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
            or not assembly.silence.accept_semantic_confirmation(
                event=confirmation
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.ACT_CONFIRMED,
                semantic_act_kind=pending.semantic_act_kind,
            )
        )
        payload = self._playout_payload(
            authorization=authorization,
            text_digest=pending.text_digest,
        )
        self._append_outbound_frame(authorization=authorization)
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
        if not assembly.lifecycle.ingest(tts) or not assembly.silence.accept_tts_binding(event=tts):
            raise _DriverAbort(DriverFailure.DELIVERY)
        playout = self._event_after(
            assembly.lifecycle,
            authorization,
            kind=VoiceEventKind.PLAYOUT_BOUND,
            source=VoiceSource.LOCAL_AUTHORITATIVE,
            payload=payload,
            at_ms=tts.at_ms + 1,
        )
        if not assembly.lifecycle.ingest(playout) or not assembly.silence.accept_playout_binding(
            event=playout
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        transport = self._event_after(
            assembly.lifecycle,
            authorization,
            kind=VoiceEventKind.TRANSPORT_RESOLVED,
            source=VoiceSource.TWILIO_AUTHENTICATED,
            payload=payload,
            at_ms=playout.at_ms + 1,
        )
        if (
            not assembly.lifecycle.ingest(transport)
            or not assembly.silence.accept_transport_resolution(
                event=transport,
                event_id=f"lifecycle_transport_{transport.sequence}",
                sequence=transport.sequence,
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.TRANSPORT_RESOLVED,
                semantic_act_kind=pending.semantic_act_kind,
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
        observed = assembly.silence.observe_playback(
            event=playback,
            event_id=f"lifecycle_playback_{playback.sequence}",
            sequence=playback.sequence,
        )
        if observed is None:
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.PLAYBACK_OBSERVED,
                semantic_act_kind=pending.semantic_act_kind,
            )
        )
        if (
            observed.status is LifecycleActStatus.OBSERVED
            and len(observed.emitted_intents) == 1
            and observed.emitted_intents[0].kind
            is CallIntentKind.ARM_TIMER
        ):
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.SILENCE_TIMER_ARMED,
                )
            )
        elif (
            observed.status is LifecycleActStatus.OBSERVED
            and not observed.emitted_intents
            and observed.request_kind
            in {
                CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
                CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
            }
        ):
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.FIXED_FALLBACK_OBSERVED,
                    semantic_act_kind=pending.semantic_act_kind,
                    locale=pending.locale,
                )
            )
        elif (
            observed.status
            is LifecycleActStatus.TERMINAL_ELIGIBLE
        ):
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.LOCAL_TERMINAL_ELIGIBLE,
                )
            )
        else:
            raise _DriverAbort(DriverFailure.DELIVERY)
        return observed, playback.at_ms + 1

    @staticmethod
    def _single_emitted(
        result: LifecycleActResult,
        expected: CallIntentKind,
    ) -> CallIntent:
        if (
            len(result.emitted_intents) != 1
            or result.emitted_intents[0].kind is not expected
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        return result.emitted_intents[0]

    @staticmethod
    def _fire_timer(
        *,
        assembly: _Assembly,
        timer: CallIntent,
        event_id: str,
        expected: CallIntentKind,
    ) -> CallIntent:
        if (
            timer.kind is not CallIntentKind.ARM_TIMER
            or timer.deadline_ms is None
            or assembly.calls.timer_receipt() is not timer
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        sequence, at_ms = assembly.calls.next_position(
            at_ms=timer.deadline_ms
        )
        intents = assembly.calls.timer_fired(
            binding=assembly.binding,
            event_id=event_id,
            sequence=sequence,
            action_id=timer.action_id,
            revision=timer.revision,
            now_ms=at_ms,
        )
        if len(intents) != 1 or intents[0].kind is not expected:
            raise _DriverAbort(DriverFailure.DELIVERY)
        return intents[0]

    @staticmethod
    def _finish_local_terminal(
        *,
        assembly: _Assembly,
        result: LifecycleActResult,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        terminal = OfflineSessionDriver._single_emitted(
            result,
            CallIntentKind.TERMINAL_ELIGIBLE,
        )
        if (
            not assembly.silence.terminalize(terminal)
            or not assembly.silence.is_terminal
            or not assembly.calls.is_quiescent
            or assembly.silence.pending_count
            or any(
                assembly.speech.is_live(act_id)
                for act_id
                in assembly.speech.act_ids_for_binding(
                    assembly.binding
                )
            )
            or assembly.transaction.pending_response_count
            or assembly.receipts.unconsumed_receipt_count
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.LOCAL_TERMINATED,
            )
        )
        return at_ms

    def _fresh_silence_assembly(
        self,
        *,
        grant: _LeaseGrant,
        fixture: _Fixture,
        suffix: str,
        epoch_offset: int,
    ) -> _Assembly:
        if (
            not suffix.replace("_", "").isalnum()
            or type(epoch_offset) is not int
            or epoch_offset < 1
        ):
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        binding = VoiceSessionBinding(
            environment=grant.binding.environment,
            contractor_binding=grant.binding.contractor_binding,
            call_binding=(
                f"{grant.binding.call_binding}_{suffix}"
            ),
            stream_binding=(
                f"{grant.binding.stream_binding}_{suffix}"
            ),
            epoch=grant.binding.epoch + epoch_offset,
        )
        return self._assembly(
            grant,
            fixture,
            binding=binding,
        )

    def _execute_unobserved_question_outcomes(
        self,
        *,
        assembly: _Assembly,
        pending: CompositionResult,
        receipt: FinalTurnAdmissionReceipt,
        fixture: _Fixture,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        variants = (
            (
                VoiceEventKind.PLAYOUT_PARTIAL,
                TraceKind.PLAYOUT_PARTIAL,
                False,
            ),
            (
                VoiceEventKind.PLAYOUT_CLEARED,
                TraceKind.PLAYOUT_CLEARED,
                False,
            ),
            (
                VoiceEventKind.ACT_FAILED,
                TraceKind.ACT_FAILED,
                True,
            ),
            (
                VoiceEventKind.PLAYOUT_INTERRUPTED,
                TraceKind.PLAYOUT_INTERRUPTED,
                False,
            ),
        )
        cursor_ms = now_ms + 5
        authorizations: list[VoiceEvent] = []
        for index, (
            event_kind,
            trace_kind,
            resolve_transport,
        ) in enumerate(
            variants,
            start=1,
        ):
            cursor_ms, authorization = self._stage_unobserved_question(
                assembly=assembly,
                pending=pending,
                event_kind=event_kind,
                trace_kind=trace_kind,
                resolve_transport=resolve_transport,
                at_ms=cursor_ms,
                trace=trace,
            )
            authorizations.append(authorization)
            if index < len(variants):
                replacement = _Fixture(
                    locale="en",
                    content=(
                        "Fixture caller supplies a new final turn "
                        f"after unobserved question {index}."
                    ),
                    fields=(
                        ("language", "en"),
                        ("intent", "service_request"),
                        ("service_action", "repair"),
                        ("service_object", "furnace"),
                    ),
                )
                pending, receipt = self._supersede_turn(
                    assembly=assembly,
                    stale=pending,
                    stale_receipt=receipt,
                    stale_fixture=fixture,
                    replacement=replacement,
                    turn_number=index + 1,
                    admit_at_ms=cursor_ms + 1,
                    execute_at_ms=cursor_ms + 2,
                    replay_at_ms=cursor_ms + 3,
                    trace=trace,
                )
                if (
                    assembly.adapter.has_permit(authorization)
                    or assembly.speech.is_live(
                        authorization.semantic_act_id
                    )
                    or assembly.transaction.authorization_receipt(
                        authorization.semantic_act_id
                    )
                    is not None
                ):
                    raise _DriverAbort(DriverFailure.COMPOSITION)
                fixture = replacement
                cursor_ms += 5
        if pending.status is not CompositionStatus.RESPONSE_PENDING:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        final_fixture = _Fixture(
            locale="en",
            content="Fixture caller final turn is cancelled.",
            fields=(),
            outcome=BackendOutcome.CANCELLED,
        )
        final_receipt = self._admit_final_turn(
            assembly=assembly,
            fixture=final_fixture,
            turn_number=len(variants) + 1,
            at_ms=cursor_ms + 1,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.INPUT_FINAL,
            )
        )
        final_at_ms = max(cursor_ms + 2, final_receipt.at_ms)
        final = assembly.transaction.execute(
            final_receipt,
            content=final_fixture.content,
            backend=_SyntheticObservationBackend(final_fixture),
            now_ms=final_at_ms,
        )
        replay = assembly.transaction.execute(
            receipt,
            content=fixture.content,
            backend=_SyntheticObservationBackend(fixture),
            now_ms=final_at_ms + 1,
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
                final,
                locale=self._trace_locale(
                    assembly.state.current_state().language
                ),
            )
        )
        if (
            final.status is not CompositionStatus.SILENT
            or final.act_ids
            or final.act_kinds
            or assembly.state.current_state().asked_slots
            or assembly.transaction.pending_response_count
            or assembly.receipts.unconsumed_receipt_count
            or any(
                assembly.adapter.has_permit(authorization)
                or assembly.speech.is_live(
                    authorization.semantic_act_id
                )
                for authorization in authorizations
            )
            or not assembly.calls.is_quiescent
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        return final_at_ms + 1

    def _execute_interruption_reconnect(
        self,
        *,
        grant: _LeaseGrant,
        assembly: _Assembly,
        pending: CompositionResult,
        receipt: FinalTurnAdmissionReceipt,
        fixture: _Fixture,
        now_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> int:
        interrupted_at_ms, stale_authorization = (
            self._stage_unobserved_question(
                assembly=assembly,
                pending=pending,
                event_kind=VoiceEventKind.PLAYOUT_INTERRUPTED,
                trace_kind=TraceKind.PLAYOUT_INTERRUPTED,
                resolve_transport=False,
                at_ms=now_ms + 5,
                trace=trace,
            )
        )
        disconnected = self._session_transition(
            assembly=assembly,
            kind=VoiceEventKind.SESSION_DISCONNECTED,
            at_ms=interrupted_at_ms + 1,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.SESSION_DISCONNECTED,
            )
        )
        if (
            not assembly.transaction.terminalize_disconnected_session(
                event=disconnected
            )
            or assembly.transaction.pending_response_count
            or assembly.receipts.unconsumed_receipt_count
            or assembly.adapter.has_permit(stale_authorization)
            or assembly.speech.is_live(
                stale_authorization.semantic_act_id
            )
            or not assembly.calls.is_quiescent
            or assembly.state.current_state().asked_slots
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        reestablished = self._session_transition(
            assembly=assembly,
            kind=VoiceEventKind.SESSION_REESTABLISHED,
            at_ms=disconnected.at_ms + 1,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.SESSION_REESTABLISHED,
            )
        )
        assembly.adapter.terminalize_permit_admission()
        if not assembly.adapter.terminally_closed:
            raise _DriverAbort(DriverFailure.COMPOSITION)
        confirmed_state = assembly.state.current_state()
        confirmed_version = assembly.state.version
        fresh_binding = VoiceSessionBinding(
            environment=assembly.binding.environment,
            contractor_binding=(
                assembly.binding.contractor_binding
            ),
            call_binding=assembly.binding.call_binding,
            stream_binding=(
                f"{assembly.binding.stream_binding}_reconnected"
            ),
            epoch=assembly.binding.epoch + 1,
        )
        reconnect_fixture = _Fixture(
            locale=fixture.locale,
            content=(
                "Fixture caller resumes after a proven "
                "synthetic reconnect."
            ),
            fields=(
                ("language", fixture.locale),
                ("intent", "service_request"),
            ),
            low_confidence=True,
        )
        fresh = self._assembly(
            grant,
            reconnect_fixture,
            binding=fresh_binding,
            initial_state=confirmed_state,
            initial_state_version=confirmed_version,
        )
        fresh_receipt = self._admit_final_turn(
            assembly=fresh,
            fixture=reconnect_fixture,
            turn_number=2,
            at_ms=reestablished.at_ms + 1,
        )
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=TraceKind.INPUT_FINAL,
            )
        )
        fresh_result = fresh.transaction.execute(
            fresh_receipt,
            content=reconnect_fixture.content,
            backend=_SyntheticObservationBackend(
                reconnect_fixture
            ),
            now_ms=max(
                reestablished.at_ms + 2,
                fresh_receipt.at_ms,
            ),
        )
        trace.append(
            self._composition_trace(
                trace,
                fresh_result,
                locale=self._trace_locale(
                    fresh.state.current_state().language
                ),
            )
        )
        stale_replay = assembly.transaction.execute(
            receipt,
            content=fixture.content,
            backend=_ForbiddenReplayBackend(),
            now_ms=reestablished.at_ms + 3,
        )
        if (
            stale_replay.status
            is not CompositionStatus.TERMINAL_FAILURE
            or stale_replay.reason != "session_disconnected"
            or fresh_result.status
            is not CompositionStatus.REPAIR_PENDING
            or fresh_result.act_kinds
            != (VoiceSemanticActKind.REPAIR,)
            or fresh.binding.call_binding
            != assembly.binding.call_binding
            or fresh.binding.contractor_binding
            != assembly.binding.contractor_binding
            or fresh.binding.environment
            != assembly.binding.environment
            or fresh.binding.stream_binding
            == assembly.binding.stream_binding
            or fresh.binding.epoch
            != assembly.binding.epoch + 1
            or fresh.state.version != confirmed_version
            or fresh.state.current_state() != confirmed_state
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        fresh_authorization = (
            fresh.transaction.authorization_receipt(
                fresh_result.act_ids[0]
            )
        )
        if (
            fresh_authorization is None
            or assembly.lifecycle.act_state(
                stale_authorization
            )
            is not VoiceEventKind.PLAYOUT_INTERRUPTED
            or assembly.adapter.accept_permit(
                fresh_authorization,
                lifecycle=fresh.lifecycle,
            )
            or fresh.adapter.accept_permit(
                stale_authorization,
                lifecycle=assembly.lifecycle,
            )
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        observed, end_at_ms = self._deliver_pending(
            assembly=fresh,
            pending=fresh_result,
            at_ms=reestablished.at_ms + 4,
            trace=trace,
        )
        if (
            observed.status
            is not CompositionStatus.RESPONSE_OBSERVED
            or fresh.state.current_state() != confirmed_state
            or fresh.state.current_state().asked_slots
            or fresh.transaction.pending_response_count
            or fresh.receipts.unconsumed_receipt_count
            or not fresh.calls.is_quiescent
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        return end_at_ms

    def _stage_unobserved_question(
        self,
        *,
        assembly: _Assembly,
        pending: CompositionResult,
        event_kind: VoiceEventKind,
        trace_kind: TraceKind,
        resolve_transport: bool,
        at_ms: int,
        trace: list[OfflineTraceEvent],
    ) -> tuple[int, VoiceEvent]:
        if (
            pending.status is not CompositionStatus.RESPONSE_PENDING
            or pending.act_kinds
            != (VoiceSemanticActKind.QUESTION,)
        ):
            raise _DriverAbort(DriverFailure.COMPOSITION)
        authorization = assembly.transaction.authorization_receipt(
            pending.act_ids[0]
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
                semantic_act_kind=VoiceSemanticActKind.QUESTION,
            )
        )
        payload = self._playout_payload(
            authorization=authorization,
            text_digest=(
                assembly.speech.authorized_text_digest(
                    authorization.semantic_act_id
                )
            ),
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
        if (
            not assembly.lifecycle.ingest(tts)
            or not assembly.transaction.accept_tts_binding(
                event=tts
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        playout = self._event_after(
            assembly.lifecycle,
            authorization,
            kind=VoiceEventKind.PLAYOUT_BOUND,
            source=VoiceSource.LOCAL_AUTHORITATIVE,
            payload=payload,
            at_ms=tts.at_ms + 1,
        )
        if (
            not assembly.lifecycle.ingest(playout)
            or not assembly.transaction.accept_playout_binding(
                event=playout
            )
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        prior = playout
        if resolve_transport:
            transport = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.TRANSPORT_RESOLVED,
                source=VoiceSource.TWILIO_AUTHENTICATED,
                payload=payload,
                at_ms=playout.at_ms + 1,
            )
            if (
                not assembly.lifecycle.ingest(transport)
                or not assembly.transaction.accept_transport_resolution(
                    event=transport,
                    event_id=f"transport_{transport.sequence}",
                    sequence=transport.sequence,
                )
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            trace.append(
                OfflineTraceEvent(
                    ordinal=len(trace),
                    kind=TraceKind.TRANSPORT_RESOLVED,
                    semantic_act_kind=VoiceSemanticActKind.QUESTION,
                )
            )
            prior = transport
        terminal = self._event_after(
            assembly.lifecycle,
            authorization,
            kind=event_kind,
            source=(
                VoiceSource.LOCAL_AUTHORITATIVE
                if event_kind is VoiceEventKind.ACT_FAILED
                else VoiceSource.TWILIO_AUTHENTICATED
            ),
            payload=payload,
            at_ms=prior.at_ms + 1,
        )
        if (
            not assembly.lifecycle.ingest(terminal)
            or assembly.state.current_state().asked_slots
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        if resolve_transport and (
            assembly.transaction.infer_playback(
                act_id=authorization.semantic_act_id,
                event_id=f"inference_{terminal.sequence}",
                sequence=terminal.sequence + 1,
                at_ms=terminal.at_ms + 1,
                inference_id=f"inference_{terminal.sequence}",
                transport_id=payload.playout_id or "",
            )
            is not None
            or assembly.state.current_state().asked_slots
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=trace_kind,
                semantic_act_kind=VoiceSemanticActKind.QUESTION,
            )
        )
        return terminal.at_ms, authorization

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
                text_digest=(
                    assembly.speech.authorized_text_digest(
                        authorization.semantic_act_id
                    )
                ),
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
            if (
                not assembly.lifecycle.ingest(tts)
                or not assembly.transaction.accept_tts_binding(
                    event=tts
                )
            ):
                raise _DriverAbort(DriverFailure.DELIVERY)
            playout = self._event_after(
                assembly.lifecycle,
                authorization,
                kind=VoiceEventKind.PLAYOUT_BOUND,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
                payload=payload,
                at_ms=tts.at_ms + 1,
            )
            if (
                not assembly.lifecycle.ingest(playout)
                or not assembly.transaction.accept_playout_binding(
                    event=playout
                )
            ):
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
            self._append_outbound_frame(
                authorization=authorization,
                replay_mode=pending.replay_mode,
            )
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
                    replay_mode=observed.replay_mode,
                )
            )
            at_ms = playback.at_ms + 1
        trace.append(
            OfflineTraceEvent(
                ordinal=len(trace),
                kind=(
                    TraceKind.REPLAY_OBSERVED
                    if result.status
                    is CompositionStatus.REPLAY_OBSERVED
                    else TraceKind.RESPONSE_OBSERVED
                ),
                composition_status=result.status,
                replay_mode=result.replay_mode,
            )
        )
        return result, at_ms

    def _assembly(
        self,
        grant: _LeaseGrant,
        fixture: _Fixture,
        *,
        binding: VoiceSessionBinding | None = None,
        initial_state: IntakeState | None = None,
        initial_state_version: int = 0,
    ) -> _Assembly:
        if binding is None:
            binding = grant.binding
        if (
            not isinstance(binding, VoiceSessionBinding)
            or type(initial_state_version) is not int
            or initial_state_version < 0
        ):
            raise _DriverAbort(DriverFailure.ASSEMBLY)
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
        if initial_state is None:
            if initial_state_version != 0:
                raise _DriverAbort(DriverFailure.ASSEMBLY)
            initial_state = IntakeState.new(
                call_sid=binding.call_binding,
            )
            initial_state.language = fixture.locale
        elif (
            not isinstance(initial_state, IntakeState)
            or initial_state.side_effects_allowed
            or initial_state.call_sid != binding.call_binding
            or initial_state.language != fixture.locale
        ):
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        state = VersionedIntakeStore(
            binding=binding,
            initial_state=initial_state,
            initial_version=initial_state_version,
        )
        calls = CallLifecycle(
            binding=binding,
            voice_lifecycle=lifecycle,
            first_silence_ms=10_000,
            second_silence_ms=10_000,
            more_time_extension_ms=20_000,
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
        coordinator = VoiceBakeoffCoordinator(
            speech=speech,
            calls=calls,
        )
        materializer = FixedProposalMaterializer()
        policy = CompositionPolicy()
        transaction = TurnCompositionTransaction(
            binding=binding,
            adapter=adapter,
            lifecycle=lifecycle,
            extractor=extractor,
            receipts=receipts,
            state=state,
            coordinator=coordinator,
            materializer=materializer,
            policy=policy,
            max_outcomes=16,
        )
        silence = SilenceLifecycleController(
            binding=binding,
            adapter=adapter,
            lifecycle=lifecycle,
            state=state,
            coordinator=coordinator,
            materializer=materializer,
            policy=policy,
        )
        return _Assembly(
            binding=binding,
            adapter=adapter,
            calls=calls,
            lifecycle=lifecycle,
            speech=speech,
            transaction=transaction,
            silence=silence,
            receipts=receipts,
            state=state,
        )

    def _session_transition(
        self,
        *,
        assembly: _Assembly,
        kind: VoiceEventKind,
        at_ms: int,
    ) -> VoiceEvent:
        if kind not in {
            VoiceEventKind.SESSION_DISCONNECTED,
            VoiceEventKind.SESSION_REESTABLISHED,
        }:
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        sequence, canonical_at_ms = (
            assembly.lifecycle.next_position(at_ms=at_ms)
        )
        context = EventContext(
            binding=assembly.binding,
            sequence=sequence,
            at_ms=canonical_at_ms,
            input_turn_id=f"session_turn_{sequence}",
            generation_id=f"session_generation_{sequence}",
            semantic_act_id=f"session_act_{sequence}",
            semantic_act_kind=(
                VoiceSemanticActKind.ACKNOWLEDGEMENT
            ),
        )
        disconnected = (
            kind is VoiceEventKind.SESSION_DISCONNECTED
        )
        adapter = assembly.adapter
        if type(adapter) is NativeGeminiAdapter:
            result = adapter.handle(
                NativeSignal(
                    (
                        NativeSignalKind.SESSION_DISCONNECTED
                        if disconnected
                        else NativeSignalKind.SESSION_REESTABLISHED
                    ),
                    context,
                )
            )
        elif type(adapter) is ChainedStreamingAdapter:
            result = adapter.handle(
                ChainedSignal(
                    (
                        ChainedSignalKind.SESSION_DISCONNECTED
                        if disconnected
                        else ChainedSignalKind.SESSION_REESTABLISHED
                    ),
                    context,
                )
            )
        elif type(adapter) is ConversationRelayAdapter:
            result = adapter.handle(
                RelaySignal(
                    (
                        RelaySignalKind.SESSION_DISCONNECTED
                        if disconnected
                        else RelaySignalKind.SESSION_REESTABLISHED
                    ),
                    context,
                )
            )
        elif type(adapter) is ManualNativeAdapter:
            result = adapter.handle(
                ManualNativeSignal(
                    (
                        ManualNativeSignalKind.SESSION_DISCONNECTED
                        if disconnected
                        else ManualNativeSignalKind.SESSION_REESTABLISHED
                    ),
                    context,
                )
            )
        else:
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        if (
            not result.accepted
            or len(result.events) != 1
            or result.events[0].kind is not kind
            or not assembly.lifecycle.ingest(result.events[0])
        ):
            raise _DriverAbort(DriverFailure.ASSEMBLY)
        return result.events[0]

    def _admit_final_turn(
        self,
        *,
        assembly: _Assembly,
        fixture: _Fixture,
        turn_number: int,
        at_ms: int,
        semantic_act_kind: VoiceSemanticActKind = (
            VoiceSemanticActKind.ACKNOWLEDGEMENT
        ),
    ) -> FinalTurnAdmissionReceipt:
        if not isinstance(
            semantic_act_kind,
            VoiceSemanticActKind,
        ):
            raise _DriverAbort(DriverFailure.ASSEMBLY)
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
            semantic_act_kind=semantic_act_kind,
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
        replay_mode: ReplayMode | None = None,
    ) -> None:
        if (
            replay_mode is not None
            and not isinstance(replay_mode, ReplayMode)
        ):
            raise _DriverAbort(DriverFailure.DELIVERY)
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
                duration_ms=(
                    40
                    if replay_mode is ReplayMode.SLOWER
                    else 20
                ),
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
        scripted_locale: str,
        stop_posture: OfflineStopPosture,
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
                    "scripted_locale": scripted_locale,
                    "stop_posture": stop_posture.value,
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
                and facade._scripted_locale
                == grant.scripted_locale
                and facade._stop_posture is grant.stop_posture
                and grant.contract_digest == self.contract_digest
                and grant.lease_id
                == self._lease_identifier(
                    arm=grant.arm,
                    journey=grant.journey,
                    binding=grant.binding,
                    expires_at_ms=grant.expires_at_ms,
                    contract_digest=grant.contract_digest,
                    scripted_locale=grant.scripted_locale,
                    stop_posture=grant.stop_posture,
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
        if (
            terminal.journey
            is SyntheticJourney.OPT_OUT_WITHDRAWAL
        ):
            self._scripted_confirmation = None
            self._participant_surrogate = None
        try:
            facade._driver = self
            facade._arm = terminal.arm
            facade._journey = terminal.journey
            facade._binding = terminal.binding
            facade._lease_id = terminal.lease_id
            facade._expires_at_ms = terminal.expires_at_ms
            facade._contract_digest = terminal.contract_digest
            facade._scripted_locale = terminal.scripted_locale
            facade._stop_posture = terminal.stop_posture
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
            pre_stop_outbound_frame_count=0,
            post_stop_outbound_frame_delta=0,
            post_stop_outbound_ordinals=(),
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
        text_digest: str | None = None,
    ) -> VoicePayload:
        if text_digest is None:
            raise _DriverAbort(DriverFailure.DELIVERY)
        digest = hashlib.sha256(
            _AUDIO_DOMAIN
            + authorization.semantic_act_id.encode("ascii")
        ).hexdigest()
        return VoicePayload(
            text_digest=text_digest,
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
        elif result.status is CompositionStatus.REPLAY_PENDING:
            kind = TraceKind.REPLAY_PENDING
        elif result.status is CompositionStatus.SUPERSEDED:
            kind = TraceKind.SUPERSEDED
        else:
            kind = TraceKind.TERMINAL
        return OfflineTraceEvent(
            ordinal=len(trace),
            kind=kind,
            composition_status=result.status,
            locale=locale,
            replay_mode=result.replay_mode,
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
    "OfflineStopPosture",
    "OfflineTraceEvent",
    "SyntheticJourney",
    "TraceKind",
]
