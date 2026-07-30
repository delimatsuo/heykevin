"""Offline Arm B1 streamed-chain adapter contract.

This module contains no Deepgram, model, ElevenLabs, Twilio, socket, or legacy
VoicePipeline construction. Provider identities remain unselected.
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
    VoiceEvent,
    VoiceEventKind,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSource,
)


class ChainedSignalKind(str, Enum):
    INPUT_PARTIAL = "input_partial"
    INPUT_FINAL = "input_final"
    GENERATION_STARTED = "generation_started"
    TEXT_SEGMENT = "text_segment"
    GENERATION_COMPLETED = "generation_completed"
    TTS_BOUND = "tts_bound"
    PLAYOUT_BOUND = "playout_bound"
    TRANSPORT_RESOLVED = "transport_resolved"
    PLAYOUT_PARTIAL = "playout_partial"
    PLAYOUT_CLEARED = "playout_cleared"
    PLAYOUT_INTERRUPTED = "playout_interrupted"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    UNEXPECTED_TOOL_CALL = "unexpected_tool_call"
    TERMINAL_REQUESTED = "terminal_requested"


@dataclass(frozen=True, slots=True)
class ChainedSignal:
    kind: ChainedSignalKind
    context: EventContext
    usage: CandidateUsage = field(default_factory=CandidateUsage)
    payload: VoicePayload = field(default_factory=VoicePayload)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, ChainedSignalKind)
            or not isinstance(self.context, EventContext)
            or not isinstance(self.usage, CandidateUsage)
            or not isinstance(self.payload, VoicePayload)
        ):
            raise TypeError("chained signal is invalid")


class ChainedStreamingAdapter(OfflineCandidateAdapter):
    """Pure B1 finality, permit, and lifecycle mapper."""

    arm = CandidateArm.B1

    def __init__(self, *, binding, limits: CandidateLimits) -> None:
        super().__init__(binding=binding, limits=limits)
        self._final_turns: set[str] = set()
        self._fresh_epoch_required = False
        self._reconnect_required = False
        self._generation_state: dict[
            tuple[str, str, str, VoiceSemanticActKind], ChainedSignalKind
        ] = {}
        self._tts_bindings: dict[
            tuple[str, str, str, VoiceSemanticActKind], tuple[str, str]
        ] = {}
        self._playout_bindings: dict[
            tuple[str, str, str, VoiceSemanticActKind], tuple[str, str, str]
        ] = {}
        self._transport_resolved: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()

    @property
    def selectable_offline(self) -> bool:
        return False

    def provider_configuration(self) -> dict[str, object]:
        return {
            "adapter_only": True,
            "candidate_final_required": True,
            "streaming_text": True,
            "streaming_tts": True,
        }

    def accept_permit(self, event: VoiceEvent, *, lifecycle) -> bool:
        if (
            self._fresh_epoch_required
            or self._reconnect_required
            or not isinstance(event, VoiceEvent)
            or event.input_turn_id not in self._final_turns
        ):
            return False
        return super().accept_permit(event, lifecycle=lifecycle)

    def handle(self, signal: ChainedSignal) -> AdapterResult:
        if not isinstance(signal, ChainedSignal):
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
        session_signal = signal.kind in {
            ChainedSignalKind.SESSION_DISCONNECTED,
            ChainedSignalKind.SESSION_REESTABLISHED,
        }
        if self._reconnect_required and not session_signal:
            return self.rejected(AdapterRejectReason.STALE_EPOCH)
        if session_signal:
            if signal.kind is ChainedSignalKind.SESSION_DISCONNECTED:
                self.revoke_permits_for_disconnect()
                self._reconnect_required = True
            else:
                self._retire_permit_admission_for_fresh_epoch()
                self._reconnect_required = False
                self._fresh_epoch_required = True
            self._final_turns.clear()
            self._generation_state.clear()
            self._tts_bindings.clear()
            self._playout_bindings.clear()
            self._transport_resolved.clear()
        payload = signal.payload
        empty_payload = payload == VoicePayload()
        input_signal = signal.kind in {
            ChainedSignalKind.INPUT_PARTIAL,
            ChainedSignalKind.INPUT_FINAL,
        }
        if input_signal and (
            payload.text_digest is None
            or payload.audio_id is not None
            or payload.playout_id is not None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if session_signal and not empty_payload:
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind in {
            ChainedSignalKind.INPUT_PARTIAL,
            ChainedSignalKind.INPUT_FINAL,
        }:
            with self._authority_lock:
                if self._terminally_closed or self._permit_admission_closed:
                    return self.rejected(AdapterRejectReason.STALE_EPOCH)
                if (
                    signal.kind is ChainedSignalKind.INPUT_FINAL
                    and signal.context.input_turn_id in self._final_turns
                ):
                    return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
                if not self.admit_input(signal.context):
                    return self.rejected(AdapterRejectReason.LIMIT_EXCEEDED)
                preflight = self.preflight(
                    context=signal.context,
                    usage=signal.usage,
                    permit_required=False,
                )
                if preflight is not None:
                    return preflight
                if signal.kind is ChainedSignalKind.INPUT_FINAL:
                    self._final_turns.add(signal.context.input_turn_id)
                kind = (
                    VoiceEventKind.INPUT_TURN_FINAL
                    if signal.kind is ChainedSignalKind.INPUT_FINAL
                    else VoiceEventKind.INPUT_TURN_PARTIAL
                )
                event = signal.context.event(
                    kind,
                    source=VoiceSource.PROVIDER_UNTRUSTED,
                    payload=signal.payload,
                )
                if signal.kind is ChainedSignalKind.INPUT_FINAL:
                    return self._accept_final_transition(event)
                return self.accepted(event)
        if signal.kind in {
            ChainedSignalKind.SESSION_DISCONNECTED,
            ChainedSignalKind.SESSION_REESTABLISHED,
        }:
            kind = (
                VoiceEventKind.SESSION_DISCONNECTED
                if signal.kind is ChainedSignalKind.SESSION_DISCONNECTED
                else VoiceEventKind.SESSION_REESTABLISHED
            )
            preflight = self.preflight(
                context=signal.context,
                usage=signal.usage,
                permit_required=False,
            )
            if preflight is not None:
                return preflight
            result = self.accepted(
                signal.context.event(
                    kind,
                    source=VoiceSource.PROVIDER_UNTRUSTED,
                )
            )
            return result
        if not self.permitted(signal.context):
            return self.rejected(AdapterRejectReason.PERMIT_REQUIRED)
        if signal.context.input_turn_id not in self._final_turns:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        key = self.permit_key(signal.context)
        prior = self._generation_state.get(key)
        allowed_prior = {
            ChainedSignalKind.GENERATION_STARTED: {None},
            ChainedSignalKind.TEXT_SEGMENT: {
                ChainedSignalKind.GENERATION_STARTED,
                ChainedSignalKind.TEXT_SEGMENT,
            },
            ChainedSignalKind.GENERATION_COMPLETED: {
                ChainedSignalKind.GENERATION_STARTED,
                ChainedSignalKind.TEXT_SEGMENT,
            },
        }
        if signal.kind in allowed_prior and prior not in allowed_prior[signal.kind]:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        mapping = {
            ChainedSignalKind.GENERATION_STARTED: (
                VoiceEventKind.GENERATION_STARTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ChainedSignalKind.TEXT_SEGMENT: (
                VoiceEventKind.TEXT_SEGMENT_EMITTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ChainedSignalKind.GENERATION_COMPLETED: (
                VoiceEventKind.GENERATION_COMPLETED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ChainedSignalKind.TTS_BOUND: (
                VoiceEventKind.TTS_BOUND,
                VoiceSource.LOCAL_AUTHORITATIVE,
            ),
            ChainedSignalKind.PLAYOUT_BOUND: (
                VoiceEventKind.PLAYOUT_BOUND,
                VoiceSource.LOCAL_AUTHORITATIVE,
            ),
            ChainedSignalKind.TRANSPORT_RESOLVED: (
                VoiceEventKind.TRANSPORT_RESOLVED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            ChainedSignalKind.PLAYOUT_PARTIAL: (
                VoiceEventKind.PLAYOUT_PARTIAL,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            ChainedSignalKind.PLAYOUT_CLEARED: (
                VoiceEventKind.PLAYOUT_CLEARED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            ChainedSignalKind.PLAYOUT_INTERRUPTED: (
                VoiceEventKind.PLAYOUT_INTERRUPTED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
        }
        if signal.kind in {
            ChainedSignalKind.UNEXPECTED_TOOL_CALL,
            ChainedSignalKind.TERMINAL_REQUESTED,
        }:
            if not empty_payload:
                return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
            preflight = self.preflight(
                context=signal.context,
                usage=signal.usage,
                permit_required=True,
            )
            if preflight is not None:
                return preflight
            return self.fail(
                signal.context,
                reason=(
                    AdapterRejectReason.TOOL_DENIED
                    if signal.kind is ChainedSignalKind.UNEXPECTED_TOOL_CALL
                    else AdapterRejectReason.TERMINAL_DENIED
                ),
            )
        kind, source = mapping[signal.kind]
        if kind in {
            VoiceEventKind.GENERATION_STARTED,
            VoiceEventKind.GENERATION_COMPLETED,
        } and not empty_payload:
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if (
            kind is VoiceEventKind.TEXT_SEGMENT_EMITTED
            and (
                payload.text_digest is None
                or payload.audio_id is not None
                or payload.playout_id is not None
            )
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if kind is VoiceEventKind.TTS_BOUND and (
            payload.text_digest is None
            or payload.audio_id is None
            or payload.playout_id is not None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if (
            kind is VoiceEventKind.TTS_BOUND
            and (
                key not in self._confirmed
                or self._generation_state.get(key)
                is not ChainedSignalKind.GENERATION_COMPLETED
                or key in self._tts_bindings
            )
        ):
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if kind in {
            VoiceEventKind.PLAYOUT_BOUND,
            VoiceEventKind.TRANSPORT_RESOLVED,
            VoiceEventKind.PLAYOUT_PARTIAL,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.PLAYOUT_INTERRUPTED,
        } and (
            payload.text_digest is None
            or payload.audio_id is None
            or payload.playout_id is None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if kind is VoiceEventKind.PLAYOUT_BOUND:
            expected = self._tts_bindings.get(key)
            if (
                expected
                != (
                    payload.text_digest,
                    payload.audio_id,
                )
                or key in self._playout_bindings
            ):
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if kind in {
            VoiceEventKind.TRANSPORT_RESOLVED,
            VoiceEventKind.PLAYOUT_PARTIAL,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.PLAYOUT_INTERRUPTED,
        } and self._playout_bindings.get(key) != (
            payload.text_digest,
            payload.audio_id,
            payload.playout_id,
        ):
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if key in self._transport_resolved and kind in {
            VoiceEventKind.TRANSPORT_RESOLVED,
            VoiceEventKind.PLAYOUT_PARTIAL,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.PLAYOUT_INTERRUPTED,
        }:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        preflight = self.preflight(
            context=signal.context,
            usage=signal.usage,
            permit_required=True,
            count_request=signal.kind is ChainedSignalKind.GENERATION_STARTED,
        )
        if preflight is not None:
            return preflight
        if signal.kind in allowed_prior:
            self._generation_state[key] = signal.kind
        if kind is VoiceEventKind.TTS_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            self._tts_bindings[key] = (
                payload.text_digest,
                payload.audio_id,
            )
        if kind is VoiceEventKind.PLAYOUT_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            self._playout_bindings[key] = (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
            )
        if kind is VoiceEventKind.TRANSPORT_RESOLVED:
            self._transport_resolved.add(key)
        result = self.accepted(
            signal.context.event(
                kind,
                source=source,
                payload=payload,
            )
        )
        if kind in {
            VoiceEventKind.PLAYOUT_PARTIAL,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.PLAYOUT_INTERRUPTED,
        }:
            self._failed.add(self.permit_key(signal.context))
        return result


__all__ = [
    "ChainedSignal",
    "ChainedSignalKind",
    "ChainedStreamingAdapter",
]
