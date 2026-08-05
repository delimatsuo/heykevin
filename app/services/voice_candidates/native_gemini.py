"""Offline Arm A interface stub for native Gemini lifecycle mapping.

No Gemini SDK, credential, endpoint, socket, or provider construction is present.
The provider/model registry remains intentionally unselected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    INPUT_FINAL = "input_final"
    GENERATION_STARTED = "generation_started"
    AUDIO_FRAME = "audio_frame"
    AUDIO_BOUND = "audio_bound"
    GENERATION_COMPLETED = "generation_completed"
    TURN_COMPLETED = "turn_completed"
    PROVIDER_INTERRUPTED = "provider_interrupted"
    PLAYOUT_BOUND = "playout_bound"
    TRANSPORT_RESOLVED = "transport_resolved"
    PLAYOUT_CLEARED = "playout_cleared"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    SESSION_RESUMED = "session_resumed"
    SESSION_GO_AWAY = "session_go_away"
    UNEXPECTED_TOOL_CALL = "unexpected_tool_call"
    TERMINAL_REQUESTED = "terminal_requested"


class _NativePlayoutState(str, Enum):
    BOUND = "bound"
    RESOLVED = "resolved"
    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class NativeSignal:
    kind: NativeSignalKind
    context: EventContext
    usage: CandidateUsage = field(default_factory=CandidateUsage)
    payload: VoicePayload = field(default_factory=VoicePayload)
    frame_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NativeSignalKind) or not isinstance(self.context, EventContext) or not isinstance(self.usage, CandidateUsage) or not isinstance(self.payload, VoicePayload):
            raise TypeError("native signal is invalid")
        if self.kind is NativeSignalKind.AUDIO_FRAME:
            # This is an upstream-supplied opaque identity used only for local
            # duplicate detection. The adapter neither computes it from audio
            # bytes nor authenticates the bytes or the identity.
            if not isinstance(self.frame_digest, str) or len(self.frame_digest) != 64 or any(character not in "0123456789abcdef" for character in self.frame_digest):
                raise ValueError("audio frame digest is invalid")
        elif self.frame_digest is not None:
            raise ValueError("frame digest is only valid for audio frames")


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
            raise TypeError("native mode is invalid")
        self.mode = mode
        self._fresh_epoch_required = False
        self._resume_required = False
        self._final_turns: set[str] = set()
        self._generation_state: dict[tuple[str, str, str, VoiceSemanticActKind], NativeSignalKind] = {}
        self._audio_ids: dict[tuple[str, str, str, VoiceSemanticActKind], str] = {}
        self._audio_bindings: dict[tuple[str, str, str, VoiceSemanticActKind], tuple[str, str]] = {}
        self._playout_records: dict[
            tuple[str, str, str, VoiceSemanticActKind],
            tuple[str, str, str, _NativePlayoutState],
        ] = {}
        self._last_frame_ordinals: dict[tuple[str, str, str, VoiceSemanticActKind], int] = {}
        self._seen_frame_digests: set[str] = set()
        self._internal_audio_ms = 0

    def accept_permit(self, event, *, lifecycle) -> bool:
        with self._authority_lock:
            if self._fresh_epoch_required or self._resume_required:
                return False
            return super().accept_permit(event, lifecycle=lifecycle)

    def resume_permit_admission(
        self,
        *,
        result,
        event,
        lifecycle,
    ) -> bool:
        """Commit a resume only after the canonical lifecycle accepted it."""
        with self._authority_lock:
            if not self._resume_required:
                return False
            if not super().resume_permit_admission(
                result=result,
                event=event,
                lifecycle=lifecycle,
            ):
                return False
            self._resume_required = False
            return True

    @property
    def selectable_offline(self) -> bool:
        return False

    def provider_configuration(self) -> dict[str, object]:
        return {
            "adapter_only": True,
            "automatic_activity_detection": (self.mode is NativeMode.AUTOMATIC_CONTROL),
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
        if signal.kind in {
            NativeSignalKind.SESSION_DISCONNECTED,
            NativeSignalKind.SESSION_GO_AWAY,
            NativeSignalKind.SESSION_REESTABLISHED,
        }:
            # A resumable transport boundary revokes every active permit. Keep
            # the bounded ordinal, opaque-identity, and audio-accounting
            # tombstones so reconnect cannot replay frames or reset the quota.
            # REESTABLISHED requires a new adapter/epoch, so this instance can
            # then discard its tombstones as part of terminal cleanup.
            if signal.kind in {
                NativeSignalKind.SESSION_DISCONNECTED,
                NativeSignalKind.SESSION_GO_AWAY,
            }:
                self.revoke_permits_for_disconnect()
                self._resume_required = True
            self._generation_state.clear()
            self._audio_ids.clear()
            self._audio_bindings.clear()
            self._playout_records.clear()
            self._final_turns.clear()
            if signal.kind is NativeSignalKind.SESSION_REESTABLISHED:
                self._resume_required = False
                self._retire_permit_admission_for_fresh_epoch()
                self._last_frame_ordinals.clear()
                self._seen_frame_digests.clear()
                self._internal_audio_ms = 0
                self._fresh_epoch_required = True
        mapping = {
            NativeSignalKind.INPUT_FINAL: (
                VoiceEventKind.INPUT_TURN_FINAL,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.GENERATION_STARTED: (
                VoiceEventKind.GENERATION_STARTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.AUDIO_FRAME: (
                VoiceEventKind.AUDIO_FRAME_GENERATED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            NativeSignalKind.AUDIO_BOUND: (
                # Shared lifecycle name for the local text/audio identity
                # binding; this does not claim a separate TTS provider.
                VoiceEventKind.TTS_BOUND,
                VoiceSource.LOCAL_AUTHORITATIVE,
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
            NativeSignalKind.PLAYOUT_BOUND: (
                VoiceEventKind.PLAYOUT_BOUND,
                VoiceSource.LOCAL_AUTHORITATIVE,
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
        if signal.kind is NativeSignalKind.INPUT_FINAL:
            with self._authority_lock:
                if self._terminally_closed or self._permit_admission_closed:
                    return self.rejected(AdapterRejectReason.STALE_EPOCH)
                if signal.context.input_turn_id in self._final_turns or payload.text_digest is None or payload.audio_id is not None or payload.playout_id is not None:
                    return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
                if not self.admit_input(signal.context):
                    return self.rejected(
                        AdapterRejectReason.LIMIT_EXCEEDED
                    )
                preflight = self.preflight(
                    context=signal.context,
                    usage=signal.usage,
                    permit_required=False,
                )
                if preflight is not None:
                    return preflight
                self._final_turns.add(signal.context.input_turn_id)
                kind, source = mapping[signal.kind]
                return self._accept_final_transition(
                    signal.context.event(
                        kind,
                        source=source,
                        payload=payload,
                    )
                )
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
        if signal.kind is NativeSignalKind.AUDIO_FRAME and (payload.ordinal is None or payload.duration_ms is None or payload.duration_ms < 1 or payload.audio_id is None or payload.playout_id is not None):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind is NativeSignalKind.AUDIO_BOUND and (payload.text_digest is None or payload.audio_id is None or payload.playout_id is not None):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if (
            signal.kind
            in {
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
            }
            and not empty_payload
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind in {
            NativeSignalKind.PLAYOUT_BOUND,
            NativeSignalKind.TRANSPORT_RESOLVED,
            NativeSignalKind.PLAYOUT_CLEARED,
        } and (payload.text_digest is None or payload.audio_id is None or payload.playout_id is None):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        output_signal = signal.kind in {
            NativeSignalKind.GENERATION_STARTED,
            NativeSignalKind.AUDIO_FRAME,
            NativeSignalKind.AUDIO_BOUND,
            NativeSignalKind.GENERATION_COMPLETED,
            NativeSignalKind.TURN_COMPLETED,
            NativeSignalKind.PROVIDER_INTERRUPTED,
            NativeSignalKind.PLAYOUT_BOUND,
            NativeSignalKind.TRANSPORT_RESOLVED,
            NativeSignalKind.PLAYOUT_CLEARED,
        }
        if output_signal and not self.permitted(signal.context):
            return self.rejected(AdapterRejectReason.PERMIT_REQUIRED)
        if signal.kind is NativeSignalKind.AUDIO_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            if self._audio_ids.get(key) != payload.audio_id or key in self._audio_bindings:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is NativeSignalKind.PLAYOUT_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            if self._audio_bindings.get(key) != (payload.text_digest, payload.audio_id) or key in self._playout_records:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is NativeSignalKind.TRANSPORT_RESOLVED:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            if self._playout_records.get(key) != (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
                _NativePlayoutState.BOUND,
            ):
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is NativeSignalKind.PLAYOUT_CLEARED:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            if self._playout_records.get(key) != (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
                _NativePlayoutState.BOUND,
            ):
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is NativeSignalKind.AUDIO_FRAME and key in self._audio_ids and self._audio_ids[key] != payload.audio_id:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is NativeSignalKind.AUDIO_FRAME:
            assert payload.ordinal is not None
            assert payload.duration_ms is not None
            assert signal.frame_digest is not None
            expected_ordinal = self._last_frame_ordinals.get(key, -1) + 1
            if payload.ordinal != expected_ordinal or signal.frame_digest in self._seen_frame_digests:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is NativeSignalKind.AUDIO_FRAME and self._playout_records.get(key, (None, None, None, None))[3] is _NativePlayoutState.CLEARED:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
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
        if signal.kind is NativeSignalKind.AUDIO_FRAME and self._internal_audio_ms + payload.duration_ms >= self.limits.audio_ms:
            return self.fail(
                signal.context,
                reason=AdapterRejectReason.LIMIT_EXCEEDED,
            )
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
        if signal.kind is NativeSignalKind.AUDIO_FRAME:
            assert payload.audio_id is not None
            assert payload.ordinal is not None
            assert payload.duration_ms is not None
            assert signal.frame_digest is not None
            self._audio_ids[key] = payload.audio_id
            self._last_frame_ordinals[key] = payload.ordinal
            self._seen_frame_digests.add(signal.frame_digest)
            self._internal_audio_ms += payload.duration_ms
        if signal.kind is NativeSignalKind.AUDIO_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            self._audio_bindings[key] = (
                payload.text_digest,
                payload.audio_id,
            )
        if signal.kind is NativeSignalKind.PLAYOUT_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            self._playout_records[key] = (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
                _NativePlayoutState.BOUND,
            )
        if signal.kind is NativeSignalKind.TRANSPORT_RESOLVED:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            self._playout_records[key] = (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
                _NativePlayoutState.RESOLVED,
            )
        if signal.kind is NativeSignalKind.PLAYOUT_CLEARED:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            self._playout_records[key] = (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
                _NativePlayoutState.CLEARED,
            )
            self._audio_ids.pop(key, None)
            self._audio_bindings.pop(key, None)
        kind, source = mapping[signal.kind]
        if (
            signal.kind
            in {
                NativeSignalKind.PROVIDER_INTERRUPTED,
                NativeSignalKind.TURN_COMPLETED,
            }
            and key not in self._playout_records
        ):
            self._audio_ids.pop(key, None)
            self._audio_bindings.pop(key, None)
        event = signal.context.event(
            kind,
            source=source,
            payload=payload,
        )
        if signal.kind is NativeSignalKind.SESSION_RESUMED:
            return self._accept_resume_transition(event)
        return self.accepted(event)


__all__ = [
    "BASELINE_128_LIMITS",
    "RUNAWAY_ONLY_LIMITS",
    "NativeGeminiAdapter",
    "NativeMode",
    "NativeSignal",
    "NativeSignalKind",
]
