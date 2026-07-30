"""Offline Arm C manual-turn native feasibility contract."""

from __future__ import annotations

from collections.abc import Callable
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
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSource,
    VoiceTimeoutIntent,
)


class ManualNativeSignalKind(str, Enum):
    ACTIVITY_STARTED = "activity_started"
    ACTIVITY_ENDED = "activity_ended"
    INPUT_FINAL = "input_final"
    GENERATION_STARTED = "generation_started"
    AUDIO_FRAME = "audio_frame"
    GENERATION_COMPLETED = "generation_completed"
    PROVIDER_INTERRUPTED = "provider_interrupted"
    TTS_BOUND = "tts_bound"
    PLAYOUT_BOUND = "playout_bound"
    PLAYOUT_CLEARED = "playout_cleared"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    UNEXPECTED_TOOL_CALL = "unexpected_tool_call"
    TERMINAL_REQUESTED = "terminal_requested"


@dataclass(frozen=True, slots=True)
class ManualNativeSignal:
    kind: ManualNativeSignalKind
    context: EventContext
    usage: CandidateUsage = field(default_factory=CandidateUsage)
    payload: VoicePayload = field(default_factory=VoicePayload)
    frame_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, ManualNativeSignalKind)
            or not isinstance(self.context, EventContext)
            or not isinstance(self.usage, CandidateUsage)
            or not isinstance(self.payload, VoicePayload)
        ):
            raise TypeError("manual native signal is invalid")
        if self.kind is ManualNativeSignalKind.AUDIO_FRAME:
            # The adapter boundary supplies these as opaque local sequencing and
            # accounting claims. They detect replay and bound mock execution;
            # they do not authenticate audio bytes or prove byte-derived timing.
            if (
                not isinstance(self.frame_digest, str)
                or len(self.frame_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.frame_digest
                )
            ):
                raise ValueError("audio frame digest is invalid")
        elif self.frame_digest is not None:
            raise ValueError("frame digest is only valid for audio frames")


class ManualNativeAdapter(OfflineCandidateAdapter):
    """Pure external-turn and manual-permit mapper; no native client exists."""

    arm = CandidateArm.C

    def __init__(
        self,
        *,
        binding,
        limits: CandidateLimits,
        generation_timeout_ms: int,
    ) -> None:
        super().__init__(binding=binding, limits=limits)
        if (
            isinstance(generation_timeout_ms, bool)
            or not isinstance(generation_timeout_ms, int)
            or generation_timeout_ms < 1
        ):
            raise ValueError("generation timeout must be positive")
        self.generation_timeout_ms = generation_timeout_ms
        self._activity_open: set[str] = set()
        self._completed_activity_turns: set[str] = set()
        self._final_turns: set[str] = set()
        self._begin_deadlines: dict[
            tuple[str, str, str, VoiceSemanticActKind], tuple[int, EventContext]
        ] = {}
        self._completion_deadlines: dict[
            tuple[str, str, str, VoiceSemanticActKind], tuple[int, EventContext]
        ] = {}
        self._audio_seen: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._audio_ids: dict[
            tuple[str, str, str, VoiceSemanticActKind], str
        ] = {}
        self._last_frame_ordinals: dict[
            tuple[str, str, str, VoiceSemanticActKind], int
        ] = {}
        self._seen_frame_digests: set[str] = set()
        self._internal_audio_ms = 0
        self._tts_bindings: dict[
            tuple[str, str, str, VoiceSemanticActKind], tuple[str, str]
        ] = {}
        self._playout_bindings: dict[
            tuple[str, str, str, VoiceSemanticActKind], tuple[str, str, str]
        ] = {}
        self._pending_timeouts: dict[
            tuple[str, str, str, VoiceSemanticActKind],
            tuple[int, VoiceTimeoutIntent],
        ] = {}
        self._pending_timeout_receipts: dict[
            tuple[str, str, str, VoiceSemanticActKind],
            VoiceEvent,
        ] = {}
        self._timeout_materializer_owner: object | None = None
        self._timeout_request_callback: (
            Callable[[VoiceTimeoutIntent, VoiceLifecycle, int], VoiceEvent | None]
            | None
        ) = None
        self._timeout_terminal_callback: (
            Callable[[str, int], bool] | None
        ) = None
        self._timeout_materialization_active = False
        self._terminalization_pending = False
        self._fresh_epoch_required = False
        self._reconnect_required = False

    @property
    def selectable_offline(self) -> bool:
        return False

    def provider_configuration(self) -> dict[str, object]:
        return {
            "adapter_only": True,
            "automatic_activity_detection": False,
            "manual_activity_start_end": True,
            "generation_timeout_ms": self.generation_timeout_ms,
        }

    def accept_permit(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._authority_lock:
            if (
                self._terminally_closed
                or self._fresh_epoch_required
                or self._reconnect_required
                or not isinstance(event, VoiceEvent)
                or event.input_turn_id not in self._final_turns
            ):
                return False
            if not super().accept_permit(event, lifecycle=lifecycle):
                return False
            context = EventContext(
                binding=event.binding,
                sequence=event.sequence,
                at_ms=event.at_ms,
                input_turn_id=event.input_turn_id,
                generation_id=event.generation_id,
                semantic_act_id=event.semantic_act_id,
                semantic_act_kind=event.semantic_act_kind,
            )
            self._begin_deadlines[self.permit_key(context)] = (
                event.at_ms + self.generation_timeout_ms,
                context,
            )
            return True

    def terminalize_permit_admission(self) -> None:
        """Permanently clear native permit and timeout authority atomically."""
        terminal_callback: Callable[[str, int], bool] | None = None
        with self._authority_lock:
            if self._terminally_closed:
                return
            if self._timeout_materialization_active:
                self._terminalization_pending = True
                return
            self._terminalize_native_authority_locked()
            terminal_callback = self._timeout_terminal_callback
        if terminal_callback is not None:
            terminal_callback("timeout_authority_terminalized", 0)

    def _terminalize_native_authority_locked(self) -> None:
        super().terminalize_permit_admission()
        self._begin_deadlines.clear()
        self._completion_deadlines.clear()
        self._pending_timeouts.clear()
        self._pending_timeout_receipts.clear()

    def bind_timeout_materializer(self, owner: object) -> bool:
        """Bind timeout commit once to one exact offline assembly owner."""
        from app.services.voice_bakeoff_turn_composition import (
            TurnCompositionTransaction,
        )

        with self._authority_lock:
            if (
                type(owner) is not TurnCompositionTransaction
                or owner.adapter is not self
                or owner.lifecycle is not self._canonical_lifecycle
                or owner.binding != self.binding
                or self._terminally_closed
            ):
                return False
            request_callback = (
                TurnCompositionTransaction._materialize_timeout_from_adapter.__get__(
                    owner,
                    TurnCompositionTransaction,
                )
            )
            terminal_callback = (
                TurnCompositionTransaction._terminalize_owned_timeout_authority.__get__(
                    owner,
                    TurnCompositionTransaction,
                )
            )
            if self._timeout_materializer_owner is None:
                self._timeout_materializer_owner = owner
                self._timeout_request_callback = request_callback
                self._timeout_terminal_callback = terminal_callback
            return self._timeout_materializer_owner is owner

    def handle(self, signal: ManualNativeSignal) -> AdapterResult:
        with self._authority_lock:
            return self._handle_locked(signal)

    def _handle_locked(self, signal: ManualNativeSignal) -> AdapterResult:
        if not isinstance(signal, ManualNativeSignal):
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
            ManualNativeSignalKind.SESSION_DISCONNECTED,
            ManualNativeSignalKind.SESSION_REESTABLISHED,
        }
        if self._reconnect_required and not session_signal:
            return self.rejected(AdapterRejectReason.STALE_EPOCH)
        if session_signal:
            if signal.kind is ManualNativeSignalKind.SESSION_DISCONNECTED:
                self.revoke_permits_for_disconnect()
                self._reconnect_required = True
            else:
                self._retire_permit_admission_for_fresh_epoch()
                self._reconnect_required = False
                self._fresh_epoch_required = True
            self._activity_open.clear()
            self._completed_activity_turns.clear()
            self._final_turns.clear()
            self._begin_deadlines.clear()
            self._completion_deadlines.clear()
            self._pending_timeouts.clear()
            self._pending_timeout_receipts.clear()
            self._audio_seen.clear()
            self._audio_ids.clear()
            self._tts_bindings.clear()
            self._playout_bindings.clear()
            if signal.kind is ManualNativeSignalKind.SESSION_REESTABLISHED:
                self._last_frame_ordinals.clear()
                self._seen_frame_digests.clear()
                self._internal_audio_ms = 0
        turn_id = signal.context.input_turn_id
        payload = signal.payload
        empty_payload = payload == VoicePayload()
        if signal.kind is ManualNativeSignalKind.ACTIVITY_STARTED:
            if turn_id in self._final_turns or turn_id in self._activity_open:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            if not empty_payload:
                return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
            if not self.admit_input(signal.context):
                return self.rejected(AdapterRejectReason.LIMIT_EXCEEDED)
            preflight = self.preflight(
                context=signal.context,
                usage=signal.usage,
                permit_required=False,
            )
            if preflight is not None:
                return preflight
            self._activity_open.add(turn_id)
            return self.accepted(
                signal.context.event(
                    VoiceEventKind.INPUT_ACTIVITY_STARTED,
                    source=VoiceSource.LOCAL_AUTHORITATIVE,
                    payload=payload,
                )
            )
        if signal.kind is ManualNativeSignalKind.ACTIVITY_ENDED:
            if turn_id not in self._activity_open:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            if not empty_payload:
                return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
            preflight = self.preflight(
                context=signal.context,
                usage=signal.usage,
                permit_required=False,
            )
            if preflight is not None:
                return preflight
            self._activity_open.remove(turn_id)
            self._completed_activity_turns.add(turn_id)
            return self.accepted(
                signal.context.event(
                    VoiceEventKind.INPUT_ACTIVITY_ENDED,
                    source=VoiceSource.LOCAL_AUTHORITATIVE,
                    payload=payload,
                )
            )
        if signal.kind is ManualNativeSignalKind.INPUT_FINAL:
            with self._authority_lock:
                if self._terminally_closed or self._permit_admission_closed:
                    return self.rejected(AdapterRejectReason.STALE_EPOCH)
                if (
                    turn_id in self._activity_open
                    or turn_id in self._final_turns
                    or turn_id not in self._completed_activity_turns
                ):
                    return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
                if (
                    payload.text_digest is None
                    or payload.audio_id is not None
                    or payload.playout_id is not None
                ):
                    return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
                preflight = self.preflight(
                    context=signal.context,
                    usage=signal.usage,
                    permit_required=False,
                )
                if preflight is not None:
                    return preflight
                self._final_turns.add(turn_id)
                return self._accept_final_transition(
                    signal.context.event(
                        VoiceEventKind.INPUT_TURN_FINAL,
                        source=VoiceSource.PROVIDER_UNTRUSTED,
                        payload=payload,
                    )
                )
        if signal.kind in {
            ManualNativeSignalKind.SESSION_DISCONNECTED,
            ManualNativeSignalKind.SESSION_REESTABLISHED,
        }:
            kind = (
                VoiceEventKind.SESSION_DISCONNECTED
                if signal.kind is ManualNativeSignalKind.SESSION_DISCONNECTED
                else VoiceEventKind.SESSION_REESTABLISHED
            )
            if not empty_payload:
                return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
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
        if turn_id not in self._final_turns:
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        key = self.permit_key(signal.context)
        if key in self._pending_timeouts:
            return self.rejected(AdapterRejectReason.TIMEOUT)
        if signal.kind in {
            ManualNativeSignalKind.GENERATION_STARTED,
            ManualNativeSignalKind.GENERATION_COMPLETED,
            ManualNativeSignalKind.PROVIDER_INTERRUPTED,
        } and not empty_payload:
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if (
            signal.kind is ManualNativeSignalKind.AUDIO_FRAME
            and (
                payload.ordinal is None
                or payload.duration_ms is None
                or payload.duration_ms < 1
                or payload.audio_id is None
                or payload.playout_id is not None
            )
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind is ManualNativeSignalKind.TTS_BOUND and (
            payload.text_digest is None
            or payload.audio_id is None
            or payload.playout_id is not None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind in {
            ManualNativeSignalKind.PLAYOUT_BOUND,
            ManualNativeSignalKind.PLAYOUT_CLEARED,
        } and (
            payload.text_digest is None
            or payload.audio_id is None
            or payload.playout_id is None
        ):
            return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
        if signal.kind is ManualNativeSignalKind.GENERATION_STARTED:
            pending = self._begin_deadlines.get(key)
            if pending is None or key in self._completion_deadlines:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            if signal.context.at_ms >= pending[0]:
                return self._time_out(key, signal.context, deadline_ms=pending[0])
        elif signal.kind in {
            ManualNativeSignalKind.AUDIO_FRAME,
            ManualNativeSignalKind.GENERATION_COMPLETED,
            ManualNativeSignalKind.PROVIDER_INTERRUPTED,
        }:
            pending = self._completion_deadlines.get(key)
            if pending is None:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            if signal.context.at_ms >= pending[0]:
                return self._time_out(key, signal.context, deadline_ms=pending[0])
        if (
            signal.kind is ManualNativeSignalKind.AUDIO_FRAME
            and key in self._audio_ids
            and self._audio_ids[key] != payload.audio_id
        ):
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if signal.kind is ManualNativeSignalKind.AUDIO_FRAME:
            assert payload.ordinal is not None
            assert payload.duration_ms is not None
            assert signal.frame_digest is not None
            expected_ordinal = self._last_frame_ordinals.get(key, -1) + 1
            if (
                payload.ordinal != expected_ordinal
                or signal.frame_digest in self._seen_frame_digests
            ):
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if (
            signal.kind is ManualNativeSignalKind.TTS_BOUND
            and (
                key not in self._confirmed
                or key not in self._audio_seen
                or self._audio_ids.get(key) != payload.audio_id
                or key in self._tts_bindings
            )
        ):
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if (
            signal.kind is ManualNativeSignalKind.PLAYOUT_BOUND
            and (
                self._tts_bindings.get(key)
                != (payload.text_digest, payload.audio_id)
                or key in self._playout_bindings
            )
        ):
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        if (
            signal.kind is ManualNativeSignalKind.PLAYOUT_CLEARED
            and self._playout_bindings.get(key)
            != (payload.text_digest, payload.audio_id, payload.playout_id)
        ):
            return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
        mapping = {
            ManualNativeSignalKind.GENERATION_STARTED: (
                VoiceEventKind.GENERATION_STARTED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ManualNativeSignalKind.AUDIO_FRAME: (
                VoiceEventKind.AUDIO_FRAME_GENERATED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ManualNativeSignalKind.GENERATION_COMPLETED: (
                VoiceEventKind.GENERATION_COMPLETED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ManualNativeSignalKind.PROVIDER_INTERRUPTED: (
                VoiceEventKind.GENERATION_CANCELLED,
                VoiceSource.PROVIDER_UNTRUSTED,
            ),
            ManualNativeSignalKind.TTS_BOUND: (
                VoiceEventKind.TTS_BOUND,
                VoiceSource.LOCAL_AUTHORITATIVE,
            ),
            ManualNativeSignalKind.PLAYOUT_BOUND: (
                VoiceEventKind.PLAYOUT_BOUND,
                VoiceSource.LOCAL_AUTHORITATIVE,
            ),
            ManualNativeSignalKind.PLAYOUT_CLEARED: (
                VoiceEventKind.PLAYOUT_CLEARED,
                VoiceSource.TWILIO_AUTHENTICATED,
            ),
        }
        if signal.kind in {
            ManualNativeSignalKind.UNEXPECTED_TOOL_CALL,
            ManualNativeSignalKind.TERMINAL_REQUESTED,
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
                    if signal.kind is ManualNativeSignalKind.UNEXPECTED_TOOL_CALL
                    else AdapterRejectReason.TERMINAL_DENIED
                ),
            )
        kind, source = mapping[signal.kind]
        preflight = self.preflight(
            context=signal.context,
            usage=signal.usage,
            permit_required=True,
            count_request=(
                signal.kind is ManualNativeSignalKind.GENERATION_STARTED
            ),
        )
        if preflight is not None:
            return preflight
        if (
            signal.kind is ManualNativeSignalKind.AUDIO_FRAME
            and self._internal_audio_ms + payload.duration_ms
            >= self.limits.audio_ms
        ):
            return self.fail(
                signal.context,
                reason=AdapterRejectReason.LIMIT_EXCEEDED,
            )
        if signal.kind is ManualNativeSignalKind.GENERATION_STARTED:
            self._begin_deadlines.pop(key, None)
            self._completion_deadlines[key] = (
                signal.context.at_ms + self.generation_timeout_ms,
                signal.context,
            )
        if signal.kind is ManualNativeSignalKind.AUDIO_FRAME:
            assert payload.audio_id is not None
            assert payload.ordinal is not None
            assert payload.duration_ms is not None
            assert signal.frame_digest is not None
            self._audio_seen.add(key)
            self._audio_ids[key] = payload.audio_id
            self._last_frame_ordinals[key] = payload.ordinal
            self._seen_frame_digests.add(signal.frame_digest)
            self._internal_audio_ms += payload.duration_ms
        if signal.kind is ManualNativeSignalKind.TTS_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            self._tts_bindings[key] = (payload.text_digest, payload.audio_id)
        if signal.kind is ManualNativeSignalKind.PLAYOUT_BOUND:
            assert payload.text_digest is not None
            assert payload.audio_id is not None
            assert payload.playout_id is not None
            self._playout_bindings[key] = (
                payload.text_digest,
                payload.audio_id,
                payload.playout_id,
            )
        if kind in {
            VoiceEventKind.GENERATION_COMPLETED,
            VoiceEventKind.GENERATION_CANCELLED,
        }:
            self._completion_deadlines.pop(key, None)
        if kind is VoiceEventKind.PLAYOUT_CLEARED:
            self._failed.add(key)
            self._audio_ids.pop(key, None)
        return self.accepted(
            signal.context.event(
                kind,
                source=source,
                payload=payload,
            )
        )

    def _time_out(
        self,
        key: tuple[str, str, str, VoiceSemanticActKind],
        context: EventContext,
        *,
        deadline_ms: int,
    ) -> AdapterResult:
        with self._authority_lock:
            if self._terminally_closed:
                return self.rejected(AdapterRejectReason.STALE_EPOCH)
            self._begin_deadlines.pop(key, None)
            self._completion_deadlines.pop(key, None)
            pending = self._pending_timeouts.get(key)
            if pending is None:
                intent = VoiceTimeoutIntent(
                    binding=context.binding,
                    input_turn_id=context.input_turn_id,
                    generation_id=context.generation_id,
                    semantic_act_id=context.semantic_act_id,
                    semantic_act_kind=context.semantic_act_kind,
                    payload=self._terminal_payload(key),
                )
                self._pending_timeouts[key] = (deadline_ms, intent)
            else:
                _, intent = pending
            return AdapterResult(
                False,
                timeout_intents=(intent,),
                reason=AdapterRejectReason.TIMEOUT,
            )

    def _terminal_payload(
        self,
        key: tuple[str, str, str, VoiceSemanticActKind],
    ) -> VoicePayload:
        playout = self._playout_bindings.get(key)
        if playout is not None:
            return VoicePayload(
                text_digest=playout[0],
                audio_id=playout[1],
                playout_id=playout[2],
            )
        tts = self._tts_bindings.get(key)
        if tts is not None:
            return VoicePayload(text_digest=tts[0], audio_id=tts[1])
        return VoicePayload()

    def accept_timeout(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._authority_lock:
            return self._accept_timeout_locked(event, lifecycle=lifecycle)

    def _accept_timeout_locked(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        if (
            self._terminally_closed
            or not isinstance(event, VoiceEvent)
            or not isinstance(lifecycle, VoiceLifecycle)
            or lifecycle is not self._canonical_lifecycle
            or not lifecycle.accepts_act_timeout(event)
        ):
            return False
        key = (
            event.input_turn_id,
            event.generation_id,
            event.semantic_act_id,
            event.semantic_act_kind,
        )
        pending = self._pending_timeouts.get(key)
        if (
            pending is None
            or pending[1].payload != event.payload
            or pending[1].binding != event.binding
            or self._pending_timeout_receipts.get(key) is not event
        ):
            return False
        self._pending_timeout_receipts.pop(key, None)
        self._pending_timeouts.pop(key, None)
        self._failed.add(key)
        self._audio_seen.discard(key)
        self._audio_ids.pop(key, None)
        self._last_frame_ordinals.pop(key, None)
        self._tts_bindings.pop(key, None)
        self._playout_bindings.pop(key, None)
        return True

    def materialize_timeout(
        self,
        intent: VoiceTimeoutIntent,
        *,
        lifecycle: VoiceLifecycle,
        at_ms: int,
        owner: object,
    ) -> VoiceEvent | None:
        """Route every public timeout request through the exact bound owner."""
        with self._authority_lock:
            if (
                owner is not self._timeout_materializer_owner
                or lifecycle is not self._canonical_lifecycle
                or self._timeout_request_callback is None
            ):
                return None
            request_callback = self._timeout_request_callback
        return request_callback(intent, lifecycle, at_ms)

    def _commit_timeout_from_owner(
        self,
        intent: VoiceTimeoutIntent,
        *,
        lifecycle: VoiceLifecycle,
        at_ms: int,
        owner: object,
    ) -> VoiceEvent | None:
        """Mint and consume only for the already-bound exact owner."""
        terminal_callback: Callable[[str, int], bool] | None = None
        terminal_reason: str | None = None
        terminal_at_ms = at_ms
        result: VoiceEvent | None = None
        error: Exception | None = None
        with self._authority_lock:
            if self._timeout_materialization_active:
                return None
            self._timeout_materialization_active = True
            try:
                if (
                    owner is not self._timeout_materializer_owner
                    or not self.authorizes_timeout(intent, now_ms=at_ms)
                    or lifecycle is not self._canonical_lifecycle
                ):
                    result = None
                else:
                    key = (
                        intent.input_turn_id,
                        intent.generation_id,
                        intent.semantic_act_id,
                        intent.semantic_act_kind,
                    )
                    sequence, canonical_at_ms = lifecycle.next_position(
                        at_ms=at_ms
                    )
                    event = intent.event(
                        sequence=sequence,
                        at_ms=canonical_at_ms,
                    )
                    if lifecycle.ingest(event):
                        self._pending_timeout_receipts[key] = event
                        if not self._accept_timeout_locked(
                            event,
                            lifecycle=lifecycle,
                        ):
                            raise RuntimeError(
                                "canonical timeout receipt was not consumed"
                            )
                        result = event
                        terminal_callback = self._timeout_terminal_callback
                        terminal_reason = "act_timed_out"
                        terminal_at_ms = event.at_ms
            except Exception as caught:
                error = caught
            finally:
                self._timeout_materialization_active = False
                if self._terminalization_pending:
                    self._terminalization_pending = False
                    self._terminalize_native_authority_locked()
                    terminal_callback = self._timeout_terminal_callback
                    terminal_reason = "timeout_authority_terminalized"
        if terminal_callback is not None and terminal_reason is not None:
            terminal_callback(terminal_reason, terminal_at_ms)
        if error is not None:
            raise error
        return result

    def authorizes_timeout(
        self,
        intent: VoiceTimeoutIntent,
        *,
        now_ms: int,
    ) -> bool:
        with self._authority_lock:
            if (
                self._terminally_closed
                or not isinstance(intent, VoiceTimeoutIntent)
                or isinstance(now_ms, bool)
                or not isinstance(now_ms, int)
                or now_ms < 0
            ):
                return False
            key = (
                intent.input_turn_id,
                intent.generation_id,
                intent.semantic_act_id,
                intent.semantic_act_kind,
            )
            pending = self._pending_timeouts.get(key)
            return (
                intent.binding == self.binding
                and pending is not None
                and pending[1] is intent
                and now_ms >= pending[0]
                and key not in self._failed
            )

    def timer_fired(self, *, now_ms: int) -> AdapterResult:
        with self._authority_lock:
            if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
                return self.rejected(AdapterRejectReason.INVALID_SIGNAL)
            if self._terminally_closed:
                return self.rejected(AdapterRejectReason.STALE_EPOCH)
            expired_by_key = {
                key: context
                for deadlines in (self._begin_deadlines, self._completion_deadlines)
                for key, (deadline, context) in deadlines.items()
                if now_ms >= deadline
            }
            expired = list(expired_by_key.items())
            if not expired:
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            intents = []
            for key, context in expired:
                deadline = (
                    self._begin_deadlines.get(key)
                    or self._completion_deadlines.get(key)
                )
                assert deadline is not None
                result = self._time_out(
                    key,
                    context,
                    deadline_ms=deadline[0],
                )
                intents.extend(result.timeout_intents)
            return AdapterResult(
                False,
                timeout_intents=tuple(intents),
                reason=AdapterRejectReason.TIMEOUT,
            )


__all__ = [
    "ManualNativeAdapter",
    "ManualNativeSignal",
    "ManualNativeSignalKind",
]
