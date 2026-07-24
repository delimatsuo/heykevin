"""Offline Arm A interface stub for native Gemini lifecycle mapping.

No Gemini SDK, credential, endpoint, socket, or provider construction is present.
The provider/model registry remains intentionally unselected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.voice_candidates import (
    AdapterRejectReason,
    AdapterResult,
    CandidateArm,
    CandidateLimits,
    CandidateUsage,
    EventContext,
    OfflineCandidateAdapter,
)
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSource,
)


BASELINE_128_LIMITS = CandidateLimits(
    output_tokens=128,
    audio_ms=6_000,
    byte_count=1_000_000,
    wall_clock_ms=15_000,
    cost_minor_units=100,
    request_count=100,
)
RUNAWAY_ONLY_LIMITS = CandidateLimits(
    output_tokens=1_024,
    audio_ms=15_000,
    byte_count=2_000_000,
    wall_clock_ms=30_000,
    cost_minor_units=200,
    request_count=200,
)


class NativeMode(str, Enum):
    AUTOMATIC_CONTROL = "automatic_control"
    MANUAL_GATED = "manual_gated"


class NativeSignalKind(str, Enum):
    GENERATION_STARTED = "generation_started"
    AUDIO_FRAME = "audio_frame"
    GENERATION_COMPLETED = "generation_completed"
    TURN_COMPLETED = "turn_completed"
    PROVIDER_INTERRUPTED = "provider_interrupted"
    TRANSPORT_RESOLVED = "transport_resolved"
    PLAYOUT_CLEARED = "playout_cleared"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    SESSION_RESUMED = "session_resumed"
    SESSION_GO_AWAY = "session_go_away"
    UNEXPECTED_TOOL_CALL = "unexpected_tool_call"
    TERMINAL_REQUESTED = "terminal_requested"


@dataclass(frozen=True, slots=True)
class NativeSignal:
    kind: NativeSignalKind
    context: EventContext
    usage: CandidateUsage = CandidateUsage()
    payload: VoicePayload = VoicePayload()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, NativeSignalKind)
            or not isinstance(self.context, EventContext)
            or not isinstance(self.usage, CandidateUsage)
            or not isinstance(self.payload, VoicePayload)
        ):
            raise ValueError("native signal is invalid")


class NativeGeminiAdapter(OfflineCandidateAdapter):
    """Pure event mapper; Arm A remains non-selectable pending Task 4.8."""

    arm = CandidateArm.A

    def __init__(
        self,
        *,
        binding,
        mode: NativeMode,
        limits: CandidateLimits,
    ) -> None:
        super().__init__(binding=binding, limits=limits)
        if not isinstance(mode, NativeMode):
            raise ValueError("native mode is invalid")
        self.mode = mode
        self._fresh_epoch_required = False
        self._generation_state: dict[
            tuple[str, str, str, VoiceSemanticActKind], NativeSignalKind
        ] = {}

    @property
    def selectable_offline(self) -> bool:
        return False

    def provider_configuration(self) -> dict[str, object]:
        return {
            "adapter_only": True,
            "automatic_activity_detection": (
                self.mode is NativeMode.AUTOMATIC_CONTROL
            ),
            "max_output_tokens": self.limits.output_tokens,
            "max_audio_ms": self.limits.audio_ms,
            "max_bytes": self.limits.byte_count,
            "max_wall_clock_ms": self.limits.wall_clock_ms,
            "max_cost_minor_units": self.limits.cost_minor_units,
            "max_requests": self.limits.request_count,
        }

    def handle(self, signal: NativeSignal) -> AdapterResult:
        if not isinstance(signal, NativeSignal):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if self._fresh_epoch_required:
            return self.rejected(AdapterRejectReason.STALE_EPOCH)
        if signal.context.binding != self.binding:
            preflight = self.preflight(
                context=signal.context,
                usage=signal.usage,
                permit_required=False,
            )
            assert preflight is not None
            return preflight
        mapping = {
            NativeSignalKind.GENERATION_STARTED: (
                VoiceEventKind.GENERATION_STARTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.AUDIO_FRAME: (
                VoiceEventKind.AUDIO_FRAME_GENERATED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.GENERATION_COMPLETED: (
                VoiceEventKind.GENERATION_COMPLETED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.TURN_COMPLETED: (
                VoiceEventKind.PROVIDER_TURN_COMPLETED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.PROVIDER_INTERRUPTED: (
                VoiceEventKind.GENERATION_CANCELLED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.TRANSPORT_RESOLVED: (
                VoiceEventKind.TRANSPORT_RESOLVED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            NativeSignalKind.PLAYOUT_CLEARED: (
                VoiceEventKind.PLAYOUT_CLEARED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            NativeSignalKind.SESSION_DISCONNECTED: (
                VoiceEventKind.SESSION_DISCONNECTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.SESSION_REESTABLISHED: (
                VoiceEventKind.SESSION_REESTABLISHED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.SESSION_RESUMED: (
                VoiceEventKind.SESSION_RESUMED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.SESSION_GO_AWAY: (
                VoiceEventKind.SESSION_GO_AWAY,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
        }
        key = self.permit_key(signal.context)
        prior = self._generation_state.get(key)
        payload = signal.payload
        empty_payload = payload == VoicePayload()
        allowed_prior = {
            NativeSignalKind.GENERATION_STARTED: {None},
            NativeSignalKind.AUDIO_FRAME: {
                NativeSignalKind.GENERATION_STARTED,
                NativeSignalKind.AUDIO_FRAME,
            },
            NativeSignalKind.GENERATION_COMPLETED: {
                NativeSignalKind.GENERATION_STARTED,
                NativeSignalKind.AUDIO_FRAME,
            },
            NativeSignalKind.TURN_COMPLETED: {
                NativeSignalKind.GENERATION_COMPLETED,
            },
            NativeSignalKind.PROVIDER_INTERRUPTED: {
                NativeSignalKind.GENERATION_STARTED,
                NativeSignalKind.AUDIO_FRAME,
            },
        }
        if (
            signal.kind is NativeSignalKind.AUDIO_FRAME
            and (
                payload.audio_id is None
                or payload.playout_id is not None
            )
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind in {
            NativeSignalKind.GENERATION_STARTED,
            NativeSignalKind.GENERATION_COMPLETED,
            NativeSignalKind.TURN_COMPLETED,
            NativeSignalKind.PROVIDER_INTERRUPTED,
            NativeSignalKind.SESSION_DISCONNECTED,
            NativeSignalKind.SESSION_REESTABLISHED,
            NativeSignalKind.SESSION_RESUMED,
            NativeSignalKind.SESSION_GO_AWAY,
            NativeSignalKind.UNEXPECTED_TOOL_CALL,
            NativeSignalKind.TERMINAL_REQUESTED,
        } and not empty_payload:
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind in {
            NativeSignalKind.TRANSPORT_RESOLVED,
            NativeSignalKind.PLAYOUT_CLEARED,
        } and (
            payload.text_digest is None
            or payload.audio_id is None
            or payload.playout_id is None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        output_signal = signal.kind in {
            NativeSignalKind.GENERATION_STARTED,
            NativeSignalKind.AUDIO_FRAME,
            NativeSignalKind.GENERATION_COMPLETED,
            NativeSignalKind.TURN_COMPLETED,
            NativeSignalKind.PROVIDER_INTERRUPTED,
            NativeSignalKind.TRANSPORT_RESOLVED,
            NativeSignalKind.PLAYOUT_CLEARED,
        }
        if (
            output_signal
            and not self.permitted(signal.context)
        ):
            return self.rejected(AdapterRejectReason.PERMIT_REQUIRED)
        if signal.kind in allowed_prior and prior not in allowed_prior[signal.kind]:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        preflight = self.preflight(
            context=signal.context,
            usage=signal.usage,
            permit_required=output_signal,
            count_request=signal.kind is NativeSignalKind.GENERATION_STARTED,
        )
        if preflight is not None:
            return preflight
        if signal.kind is NativeSignalKind.UNEXPECTED_TOOL_CALL:
            return self.fail(
                signal.context,
                reason=AdapterRejectReason.TOOL_DENIED,
            )
        if signal.kind is NativeSignalKind.TERMINAL_REQUESTED:
            return self.fail(
                signal.context,
                reason=AdapterRejectReason.TERMINAL_DENIED,
            )
        if signal.kind in allowed_prior:
            self._generation_state[key] = signal.kind
        kind, source = mapping[signal.kind]
        if signal.kind is NativeSignalKind.SESSION_REESTABLISHED:
            self._fresh_epoch_required = True
            self._permitted.clear()
        return self.accepted(
            signal.context.event(
                kind,
                source=source,
                payload=payload,
            )
        )


__all__ = [
    "BASELINE_128_LIMITS",
    "RUNAWAY_ONLY_LIMITS",
    "NativeGeminiAdapter",
    "NativeMode",
    "NativeSignal",
    "NativeSignalKind",
]
