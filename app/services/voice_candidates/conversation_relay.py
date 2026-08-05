"""Offline Arm B2 ConversationRelay adapter contract.

The managed STT/TTS provider identity is unselected. This module maps only
closed, payload-safe protocol facts and exposes no WebSocket route.
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


class RelaySignalKind(str, Enum):
    PROMPT_PARTIAL = "prompt_partial"
    PROMPT_FINAL = "prompt_final"
    GENERATION_STARTED = "generation_started"
    TEXT_TOKEN = "text_token"
    LAST_TEXT_TOKEN = "last_text_token"
    INTERRUPTION = "interruption"
    PLAYOUT_PREEMPTED = "playout_preempted"
    NORMAL_PLAYBACK_RECEIPT = "normal_playback_receipt"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    PROVIDER_ERROR = "provider_error"
    UNEXPECTED_TOOL_CALL = "unexpected_tool_call"
    TERMINAL_REQUESTED = "terminal_requested"


@dataclass(frozen=True, slots=True)
class RelaySignal:
    kind: RelaySignalKind
    context: EventContext
    usage: CandidateUsage = field(default_factory=CandidateUsage)
    payload: VoicePayload = field(default_factory=VoicePayload)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, RelaySignalKind)
            or not isinstance(self.context, EventContext)
            or not isinstance(self.usage, CandidateUsage)
            or not isinstance(self.payload, VoicePayload)
        ):
            raise TypeError("relay signal is invalid")


class ConversationRelayAdapter(OfflineCandidateAdapter):
    """Pure B2 mapper; normal playback remains explicitly unavailable."""

    arm = CandidateArm.B2

    def __init__(self, *, binding, limits: CandidateLimits) -> None:
        super().__init__(binding=binding, limits=limits)
        self._final_turns: set[str] = set()
        self._fresh_epoch_required = False
        self._reconnect_required = False
        self._generation_state: dict[
            tuple[str, str, str, VoiceSemanticActKind], RelaySignalKind
        ] = {}

    @property
    def selectable_offline(self) -> bool:
        return False

    @property
    def supports_normal_playback_receipt(self) -> bool:
        return False

    def provider_configuration(self) -> dict[str, object]:
        return {
            "adapter_only": True,
            "control_only": True,
            "managed_stt_tts": True,
            "normal_playback_receipt": "unavailable",
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

    def handle(self, signal: RelaySignal) -> AdapterResult:
        if not isinstance(signal, RelaySignal):
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
            RelaySignalKind.SESSION_DISCONNECTED,
            RelaySignalKind.SESSION_REESTABLISHED,
        }
        if self._reconnect_required and not session_signal:
            return self.rejected(AdapterRejectReason.STALE_EPOCH)
        if session_signal:
            if signal.kind is RelaySignalKind.SESSION_DISCONNECTED:
                self.revoke_permits_for_disconnect()
                self._reconnect_required = True
            else:
                self._retire_permit_admission_for_fresh_epoch()
                self._reconnect_required = False
                self._fresh_epoch_required = True
            self._final_turns.clear()
            self._generation_state.clear()
        payload = signal.payload
        empty_payload = payload == VoicePayload()
        prompt_signal = signal.kind in {
            RelaySignalKind.PROMPT_PARTIAL,
            RelaySignalKind.PROMPT_FINAL,
        }
        if prompt_signal and (
            payload.text_digest is None
            or payload.audio_id is not None
            or payload.playout_id is not None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if session_signal and not empty_payload:
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind in {
            RelaySignalKind.PROMPT_PARTIAL,
            RelaySignalKind.PROMPT_FINAL,
        }:
            with self._authority_lock:
                if self._terminally_closed or self._permit_admission_closed:
                    return self.rejected(AdapterRejectReason.STALE_EPOCH)
                if (
                    signal.kind is RelaySignalKind.PROMPT_FINAL
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
                if signal.kind is RelaySignalKind.PROMPT_FINAL:
                    self._final_turns.add(signal.context.input_turn_id)
                kind = (
                    VoiceEventKind.INPUT_TURN_FINAL
                    if signal.kind is RelaySignalKind.PROMPT_FINAL
                    else VoiceEventKind.INPUT_TURN_PARTIAL
                )
                event = signal.context.event(
                    kind,
                    source=VoiceSource.PROVIDER_UNTRUSTED,
                    payload=signal.payload,
                )
                if signal.kind is RelaySignalKind.PROMPT_FINAL:
                    return self._accept_final_transition(event)
                return self.accepted(event)
        if signal.kind in {
            RelaySignalKind.SESSION_DISCONNECTED,
            RelaySignalKind.SESSION_REESTABLISHED,
        }:
            kind = (
                VoiceEventKind.SESSION_DISCONNECTED
                if signal.kind is RelaySignalKind.SESSION_DISCONNECTED
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
            RelaySignalKind.GENERATION_STARTED: {None},
            RelaySignalKind.TEXT_TOKEN: {
                RelaySignalKind.GENERATION_STARTED,
                RelaySignalKind.TEXT_TOKEN,
            },
            RelaySignalKind.LAST_TEXT_TOKEN: {
                RelaySignalKind.GENERATION_STARTED,
                RelaySignalKind.TEXT_TOKEN,
            },
        }
        if signal.kind in allowed_prior:
            if prior not in allowed_prior[signal.kind]:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            if (
                signal.kind is RelaySignalKind.TEXT_TOKEN
                and signal.context.semantic_act_kind
                in {
                    VoiceSemanticActKind.QUESTION,
                    VoiceSemanticActKind.SAFETY,
                    VoiceSemanticActKind.CLOSING,
                    VoiceSemanticActKind.OPT_OUT,
                    VoiceSemanticActKind.VOICEMAIL,
                }
                and key not in self._confirmed
            ):
                return self.rejected(AdapterRejectReason.PERMIT_REQUIRED)
        mapping = {
            RelaySignalKind.GENERATION_STARTED: (
                VoiceEventKind.GENERATION_STARTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            RelaySignalKind.TEXT_TOKEN: (
                VoiceEventKind.TEXT_SEGMENT_EMITTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            RelaySignalKind.LAST_TEXT_TOKEN: (
                VoiceEventKind.GENERATION_COMPLETED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            RelaySignalKind.INTERRUPTION: (
                VoiceEventKind.PLAYOUT_INTERRUPTED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            RelaySignalKind.PLAYOUT_PREEMPTED: (
                VoiceEventKind.PLAYOUT_CLEARED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
            RelaySignalKind.PROVIDER_ERROR: (
                VoiceEventKind.ACT_FAILED,
                VoiceSource.LOCAL_AUTHORITATIVE,
            ),
        }
        if signal.kind in {
            RelaySignalKind.UNEXPECTED_TOOL_CALL,
            RelaySignalKind.TERMINAL_REQUESTED,
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
                    if signal.kind is RelaySignalKind.UNEXPECTED_TOOL_CALL
                    else AdapterRejectReason.TERMINAL_DENIED
                ),
            )
        if signal.kind is RelaySignalKind.NORMAL_PLAYBACK_RECEIPT:
            if (
                payload.text_digest is None
                or payload.audio_id is None
                or payload.playout_id is None
            ):
                return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
            return self.rejected(AdapterRejectReason.UNSUPPORTED_CAPABILITY)
        kind, source = mapping[signal.kind]
        if kind in {
            VoiceEventKind.GENERATION_STARTED,
            VoiceEventKind.GENERATION_COMPLETED,
            VoiceEventKind.ACT_FAILED,
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
        if kind in {
            VoiceEventKind.PLAYOUT_INTERRUPTED,
            VoiceEventKind.PLAYOUT_CLEARED,
        } and (
            payload.text_digest is None
            or payload.audio_id is None
            or payload.playout_id is None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        preflight = self.preflight(
            context=signal.context,
            usage=signal.usage,
            permit_required=True,
            count_request=signal.kind is RelaySignalKind.GENERATION_STARTED,
        )
        if preflight is not None:
            return preflight
        if signal.kind in allowed_prior:
            self._generation_state[key] = signal.kind
        result = self.accepted(
            signal.context.event(
                kind,
                source=source,
                payload=payload,
            )
        )
        if kind in {
            VoiceEventKind.PLAYOUT_INTERRUPTED,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.ACT_FAILED,
        }:
            self._failed.add(self.permit_key(signal.context))
        return result


__all__ = [
    "ConversationRelayAdapter",
    "RelaySignal",
    "RelaySignalKind",
]
