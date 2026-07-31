"""Sealed offline materialization for inert call-lifecycle speech intents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_materializer import FixedProposalMaterializer
from app.services.voice_bakeoff_turn_composition import (
    CompositionPolicy,
    VersionedIntakeStore,
)
from app.services.voice_call_lifecycle import (
    CallIntent,
    CallIntentKind,
    SilencePhase,
)
from app.services.voice_candidates import OfflineCandidateAdapter
from app.services.voice_candidates.chained_streaming import (
    ChainedStreamingAdapter,
)
from app.services.voice_candidates.conversation_relay import (
    ConversationRelayAdapter,
)
from app.services.voice_candidates.manual_native import ManualNativeAdapter
from app.services.voice_candidates.native_gemini import NativeGeminiAdapter
from app.services.voice_lifecycle import (
    VOICE_SCHEMA_VERSION,
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSensitivity,
    VoiceSessionBinding,
    VoiceSource,
)
from app.services.voice_speech_control import (
    ReservedSpeech,
)

_ADAPTER_TYPES = {
    NativeGeminiAdapter,
    ChainedStreamingAdapter,
    ConversationRelayAdapter,
    ManualNativeAdapter,
}
_REQUEST_KINDS = {
    CallIntentKind.REQUEST_PRESENCE_CHECK,
    CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT,
    CallIntentKind.REQUEST_CLOSING,
}
_EXPECTED_RESULT_KIND = {
    CallIntentKind.REQUEST_PRESENCE_CHECK:
        CallIntentKind.ARM_TIMER,
    CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT:
        CallIntentKind.ARM_TIMER,
    CallIntentKind.REQUEST_CLOSING:
        CallIntentKind.TERMINAL_ELIGIBLE,
}


class LifecycleActStatus(str, Enum):
    PENDING = "pending"
    OBSERVED = "observed"
    TERMINAL_ELIGIBLE = "terminal_eligible"


@dataclass(frozen=True, slots=True)
class LifecycleActResult:
    status: LifecycleActStatus
    request_kind: CallIntentKind
    act_id: str
    semantic_act_kind: VoiceSemanticActKind
    locale: str
    proposal_digest: str
    text_digest: str
    emitted_intents: tuple[CallIntent, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, LifecycleActStatus)
            or self.request_kind not in _REQUEST_KINDS
            or not _identifier(self.act_id)
            or not isinstance(
                self.semantic_act_kind,
                VoiceSemanticActKind,
            )
            or not _locale(self.locale)
            or not _digest(self.proposal_digest)
            or not _digest(self.text_digest)
            or not isinstance(self.emitted_intents, tuple)
            or any(
                not isinstance(intent, CallIntent)
                for intent in self.emitted_intents
            )
            or (
                self.status is LifecycleActStatus.PENDING
                and self.emitted_intents
            )
            or (
                self.status is not LifecycleActStatus.PENDING
                and len(self.emitted_intents) != 1
            )
        ):
            raise ValueError("lifecycle act result is invalid")


@dataclass(slots=True)
class _PendingLifecycleAct:
    intent: CallIntent
    reserved: tuple[ReservedSpeech, ...]
    authorization: VoiceEvent
    state_version: int
    locale: str
    proposal_digest: str
    confirmed: bool = False
    tts_bound: bool = False
    playout_bound: bool = False
    transport_resolved: bool = False


@dataclass(frozen=True, slots=True)
class _TerminalAuthority:
    intent: CallIntent
    state_version: int
    turn_id: str
    turn_sequence: int


class SilenceLifecycleController:
    """One-at-a-time offline bridge from reducer intent to canonical speech."""

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        adapter: OfflineCandidateAdapter,
        lifecycle: VoiceLifecycle,
        state: VersionedIntakeStore,
        coordinator: VoiceBakeoffCoordinator,
        materializer: FixedProposalMaterializer,
        policy: CompositionPolicy,
    ) -> None:
        if (
            not isinstance(binding, VoiceSessionBinding)
            or type(adapter) not in _ADAPTER_TYPES
            or adapter.binding != binding
            or adapter.canonical_lifecycle is not lifecycle
            or type(lifecycle) is not VoiceLifecycle
            or lifecycle.binding != binding
            or type(state) is not VersionedIntakeStore
            or state.binding != binding
            or type(coordinator) is not VoiceBakeoffCoordinator
            or coordinator.calls.binding != binding
            or coordinator.calls.voice_lifecycle is not lifecycle
            or type(materializer) is not FixedProposalMaterializer
            or type(policy) is not CompositionPolicy
        ):
            raise ValueError("silence lifecycle dependencies are invalid")
        self.binding = binding
        self.adapter = adapter
        self.lifecycle = lifecycle
        self.state = state
        self.coordinator = coordinator
        self.materializer = materializer
        self.policy = policy
        self._pending: _PendingLifecycleAct | None = None
        self._terminal_authority: _TerminalAuthority | None = None
        self._lock = RLock()

    def prepare(
        self,
        intent: CallIntent,
        *,
        at_ms: int,
    ) -> LifecycleActResult | None:
        """Consume one exact request and create one canonical speech permit."""
        with self._lock:
            if (
                not isinstance(intent, CallIntent)
                or intent.kind not in _REQUEST_KINDS
                or intent.binding != self.binding
                or intent.turn_id is None
                or intent.turn_sequence is None
                or type(at_ms) is not int
                or at_ms < 0
                or self._pending is not None
                or self._terminal_authority is not None
            ):
                return None
            reserved: tuple[ReservedSpeech, ...] = ()
            authorization: VoiceEvent | None = None
            with self.state.delivery_guard():
                if not self.state.is_current(
                    turn_id=intent.turn_id,
                    sequence=intent.turn_sequence,
                ):
                    self._fail_closed(
                        reserved=(),
                        authorization=None,
                        at_ms=at_ms,
                    )
                    return None
                snapshot = self.state.snapshot()
                try:
                    proposal = self.materializer.lifecycle_act(
                        intent_kind=intent.kind,
                        state_version=snapshot.version,
                        locale=snapshot.state.language,
                    )
                    speech_authorization = (
                        self.policy.authorize_lifecycle(
                            proposal=proposal,
                            binding=self.binding,
                            turn_id=intent.turn_id,
                            state_version=snapshot.version,
                        )
                    )
                    sequence, reserve_at_ms = (
                        self.lifecycle.next_position(at_ms=at_ms)
                    )
                    reserved = self.coordinator.reserve_batch(
                        plan=proposal.plan,
                        authorization=speech_authorization,
                        event_id=(
                            f"lifecycle_reserve_{intent.revision}"
                        ),
                        sequence=sequence,
                        at_ms=reserve_at_ms,
                    )
                except Exception:  # noqa: BLE001
                    reserved = ()
                if len(reserved) != 1:
                    if reserved:
                        self._fail_closed(
                            reserved=reserved,
                            authorization=None,
                            at_ms=at_ms,
                        )
                    else:
                        self._fail_closed(
                            reserved=(),
                            authorization=None,
                            at_ms=at_ms,
                        )
                    return None
                item = reserved[0]
                try:
                    text_authorized = self.coordinator.speech.authorize_text(
                        item.act_id,
                        item.text,
                    )
                except Exception:  # noqa: BLE001
                    text_authorized = False
                if not text_authorized:
                    self._fail_closed(
                        reserved=reserved,
                        authorization=None,
                        at_ms=at_ms,
                    )
                    return None
                try:
                    intent_consumed = self.coordinator.calls.consume_intent(
                        intent,
                        materialized_act_id=item.act_id,
                    )
                except Exception:  # noqa: BLE001
                    intent_consumed = False
                if not intent_consumed:
                    self._fail_closed(
                        reserved=reserved,
                        authorization=None,
                        at_ms=at_ms,
                    )
                    return None
                sequence, canonical_at_ms = (
                    self.lifecycle.next_position(at_ms=at_ms)
                )
                authorization = VoiceEvent(
                    schema_version=VOICE_SCHEMA_VERSION,
                    kind=VoiceEventKind.RESPONSE_AUTHORIZED,
                    source=VoiceSource.LOCAL_AUTHORITATIVE,
                    sensitivity=VoiceSensitivity.OPERATIONAL,
                    binding=self.binding,
                    sequence=sequence,
                    at_ms=canonical_at_ms,
                    input_turn_id=intent.turn_id,
                    generation_id=(
                        f"lifecycle_{proposal.proposal_digest}"
                    ),
                    semantic_act_id=item.act_id,
                    semantic_act_kind=item.kind,
                    payload=VoicePayload(),
                )
                try:
                    permit_accepted = self.lifecycle.ingest(
                        authorization
                    ) and self.adapter.accept_permit(
                        authorization,
                        lifecycle=self.lifecycle,
                    )
                except Exception:  # noqa: BLE001
                    permit_accepted = False
                if not permit_accepted:
                    self._fail_closed(
                        reserved=reserved,
                        authorization=authorization,
                        at_ms=canonical_at_ms,
                    )
                    return None
                self._pending = _PendingLifecycleAct(
                    intent=intent,
                    reserved=reserved,
                    authorization=authorization,
                    state_version=snapshot.version,
                    locale=proposal.locale,
                    proposal_digest=proposal.proposal_digest,
                )
                return self._result(
                    self._pending,
                    status=LifecycleActStatus.PENDING,
                )

    def authorization_receipt(
        self,
        act_id: str,
    ) -> VoiceEvent | None:
        if not _identifier(act_id):
            return None
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            with self.state.delivery_guard():
                if not self._is_current(pending):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=pending.authorization.at_ms,
                    )
                    return None
                if (
                    pending.authorization.semantic_act_id != act_id
                    or not self.coordinator.speech.is_live(act_id)
                    or not self.adapter.has_permit(
                        pending.authorization
                    )
                ):
                    return None
                return pending.authorization

    def accept_semantic_confirmation(
        self,
        *,
        event: VoiceEvent,
    ) -> bool:
        with self._lock:
            pending = self._pending
            if pending is None:
                return False
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else pending.authorization.at_ms
            )
            with self.state.delivery_guard():
                if not self._is_current(pending):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=failure_at_ms,
                    )
                    return False
                if (
                    not isinstance(event, VoiceEvent)
                    or event.semantic_act_id
                    != pending.authorization.semantic_act_id
                    or pending.confirmed
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(
                        pending.authorization
                    )
                ):
                    return False
                if not self.adapter.can_accept_semantic_confirmation(
                    event,
                    lifecycle=self.lifecycle,
                ):
                    return False
                try:
                    accepted = self.adapter.accept_semantic_confirmation(
                        event,
                        lifecycle=self.lifecycle,
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                if not accepted:
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return False
                pending.confirmed = True
                return True

    def accept_tts_binding(
        self,
        *,
        event: VoiceEvent,
    ) -> bool:
        """Bind canonical TTS identity to the exact reviewed reserved text."""
        with self._lock:
            pending = self._pending
            if pending is None:
                return False
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else pending.authorization.at_ms
            )
            with self.state.delivery_guard():
                if not self._is_current(pending):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=failure_at_ms,
                    )
                    return False
                if (
                    not isinstance(event, VoiceEvent)
                    or event.kind is not VoiceEventKind.TTS_BOUND
                    or event.semantic_act_id
                    != pending.authorization.semantic_act_id
                    or not pending.confirmed
                    or pending.tts_bound
                    or self.lifecycle.act_state(
                        pending.authorization
                    )
                    is not VoiceEventKind.TTS_BOUND
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(
                        pending.authorization
                    )
                ):
                    return False
                try:
                    accepted = (
                        event.payload.audio_id is not None
                        and self.coordinator.speech.bind_tts(
                            event.semantic_act_id,
                            audio_id=event.payload.audio_id,
                        )
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                binding = self.coordinator.speech.audio_binding(event.semantic_act_id)
                if (
                    not accepted
                    or binding is None
                    or binding.binding != self.binding
                    or binding.text_digest != event.payload.text_digest
                    or binding.audio_id != event.payload.audio_id
                ):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return False
                pending.tts_bound = True
                return True

    def accept_playout_binding(
        self,
        *,
        event: VoiceEvent,
    ) -> bool:
        """Bind canonical playout to the exact SpeechControl audio receipt."""
        with self._lock:
            pending = self._pending
            if pending is None:
                return False
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else pending.authorization.at_ms
            )
            with self.state.delivery_guard():
                if not self._is_current(pending):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=failure_at_ms,
                    )
                    return False
                if (
                    not isinstance(event, VoiceEvent)
                    or event.kind is not VoiceEventKind.PLAYOUT_BOUND
                    or event.semantic_act_id
                    != pending.authorization.semantic_act_id
                    or not pending.confirmed
                    or not pending.tts_bound
                    or pending.playout_bound
                    or self.lifecycle.act_state(
                        pending.authorization
                    )
                    is not VoiceEventKind.PLAYOUT_BOUND
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(
                        pending.authorization
                    )
                ):
                    return False
                try:
                    accepted = (
                        event.payload.playout_id is not None
                        and self.coordinator.speech.bind_playout(
                            event.semantic_act_id,
                            playout_id=event.payload.playout_id,
                        )
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                binding = self.coordinator.speech.playout_binding(event.semantic_act_id)
                if (
                    not accepted
                    or binding is None
                    or binding.binding != self.binding
                    or binding.text_digest != event.payload.text_digest
                    or binding.audio_id != event.payload.audio_id
                    or binding.playout_id != event.payload.playout_id
                ):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return False
                pending.playout_bound = True
                return True

    def accept_transport_resolution(
        self,
        *,
        event: VoiceEvent,
        event_id: str,
        sequence: int,
    ) -> bool:
        with self._lock:
            pending = self._pending
            if pending is None:
                return False
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else pending.authorization.at_ms
            )
            with self.state.delivery_guard():
                if not self._is_current(pending):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=failure_at_ms,
                    )
                    return False
                if (
                    not isinstance(event, VoiceEvent)
                    or event.semantic_act_id
                    != pending.authorization.semantic_act_id
                    or not pending.confirmed
                    or not pending.tts_bound
                    or not pending.playout_bound
                    or pending.transport_resolved
                    or not self.lifecycle.accepts_transport_resolution(
                        event
                    )
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(
                        pending.authorization
                    )
                ):
                    return False
                binding = self.coordinator.speech.playout_binding(event.semantic_act_id)
                if (
                    binding is None
                    or binding.binding != self.binding
                    or binding.text_digest != event.payload.text_digest
                    or binding.audio_id != event.payload.audio_id
                    or binding.playout_id != event.payload.playout_id
                ):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return False
                try:
                    accepted = self.coordinator.calls.transport_resolved(
                        event=event,
                        event_id=event_id,
                        sequence=sequence,
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                if not accepted:
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return False
                pending.transport_resolved = True
                return True

    def observe_playback(
        self,
        *,
        event: VoiceEvent,
        event_id: str,
        sequence: int,
    ) -> LifecycleActResult | None:
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else pending.authorization.at_ms
            )
            with self.state.delivery_guard():
                if not self._is_current(pending):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=failure_at_ms,
                    )
                    return None
                if (
                    not isinstance(event, VoiceEvent)
                    or event.semantic_act_id
                    != pending.authorization.semantic_act_id
                    or not pending.confirmed
                    or not pending.tts_bound
                    or not pending.playout_bound
                    or not pending.transport_resolved
                    or not self.lifecycle.accepts_caller_playback(
                        event
                    )
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(
                        pending.authorization
                    )
                ):
                    return None
                binding = self.coordinator.speech.playout_binding(event.semantic_act_id)
                if (
                    binding is None
                    or binding.binding != self.binding
                    or binding.text_digest != event.payload.text_digest
                    or binding.audio_id != event.payload.audio_id
                    or binding.playout_id != event.payload.playout_id
                ):
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return None
                try:
                    intents = self.coordinator.caller_playback(
                        event=event,
                        event_id=event_id,
                        sequence=sequence,
                    )
                except Exception:  # noqa: BLE001
                    intents = ()
                expected = _EXPECTED_RESULT_KIND[pending.intent.kind]
                if len(intents) != 1 or intents[0].kind is not expected:
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return None
                try:
                    completed = self.adapter.retire_permit(
                        pending.authorization
                    ) and self.coordinator.complete_batch(pending.reserved)
                except Exception:  # noqa: BLE001
                    completed = False
                if not completed:
                    self._fail_closed(
                        reserved=pending.reserved,
                        authorization=pending.authorization,
                        at_ms=event.at_ms,
                    )
                    return None
                if expected is CallIntentKind.TERMINAL_ELIGIBLE:
                    self._terminal_authority = _TerminalAuthority(
                        intent=intents[0],
                        state_version=pending.state_version,
                        turn_id=pending.intent.turn_id or "",
                        turn_sequence=(
                            pending.intent.turn_sequence
                            if pending.intent.turn_sequence is not None
                            else -1
                        ),
                    )
                self._pending = None
                return self._result(
                    pending,
                    status=(
                        LifecycleActStatus.TERMINAL_ELIGIBLE
                        if expected is CallIntentKind.TERMINAL_ELIGIBLE
                        else LifecycleActStatus.OBSERVED
                    ),
                    emitted_intents=intents,
                )

    def terminalize(self, intent: CallIntent) -> bool:
        """Consume exact local eligibility; never perform a live hang-up."""
        with self._lock:
            authority = self._terminal_authority
            if self._pending is not None or authority is None:
                return False
            with self.state.delivery_guard():
                if intent is not authority.intent or not self.state.revalidate(
                    expected_version=authority.state_version,
                    turn_id=authority.turn_id,
                    sequence=authority.turn_sequence,
                ):
                    self._fail_closed(
                        reserved=(),
                        authorization=None,
                        at_ms=0,
                    )
                    return False
                try:
                    accepted = self.coordinator.calls.terminalize(intent)
                except Exception:  # noqa: BLE001
                    accepted = False
                self._terminal_authority = None
                if not accepted:
                    self._fail_closed(
                        reserved=(),
                        authorization=None,
                        at_ms=0,
                    )
                    return False
                speech_closed = self._hard_terminalize_speech()
                self.adapter.terminalize_permit_admission()
                return speech_closed and self.adapter.terminally_closed

    @property
    def pending_count(self) -> int:
        with self._lock:
            return 0 if self._pending is None else 1

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return (
                self._pending is None
                and self.coordinator.calls.phase
                is SilencePhase.TERMINATED
            )

    def _fail_closed(
        self,
        *,
        reserved: tuple[ReservedSpeech, ...],
        authorization: VoiceEvent | None,
        at_ms: int,
    ) -> None:
        success = True
        if authorization is not None:
            if self.adapter.has_permit(authorization):
                success = (
                    self.adapter.retire_permit(authorization)
                    and success
                )
            state = self.lifecycle.act_state(authorization)
            if state not in {
                None,
                VoiceEventKind.ACT_FAILED,
                VoiceEventKind.ACT_TIMED_OUT,
                VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
                VoiceEventKind.PLAYOUT_PARTIAL,
                VoiceEventKind.PLAYOUT_CLEARED,
                VoiceEventKind.PLAYOUT_INTERRUPTED,
            }:
                payload = self.lifecycle.terminal_payload(
                    authorization
                )
                if payload is None:
                    success = False
                else:
                    sequence, canonical_at_ms = (
                        self.lifecycle.next_position(at_ms=at_ms)
                    )
                    success = (
                        self.lifecycle.ingest(
                            VoiceEvent(
                                schema_version=VOICE_SCHEMA_VERSION,
                                kind=VoiceEventKind.ACT_FAILED,
                                source=VoiceSource.LOCAL_AUTHORITATIVE,
                                sensitivity=(
                                    VoiceSensitivity.OPERATIONAL
                                ),
                                binding=self.binding,
                                sequence=sequence,
                                at_ms=canonical_at_ms,
                                input_turn_id=(
                                    authorization.input_turn_id
                                ),
                                generation_id=(
                                    authorization.generation_id
                                ),
                                semantic_act_id=(
                                    authorization.semantic_act_id
                                ),
                                semantic_act_kind=(
                                    authorization.semantic_act_kind
                                ),
                                payload=payload,
                            )
                        )
                        and success
                    )
        for item in reserved:
            success = (
                self.coordinator.speech.hard_terminalize(
                    item.act_id
                )
                and success
            )
        if reserved:
            success = (
                self.coordinator.retire_batch(reserved)
                and success
            )
        self.coordinator.calls.hard_terminalize()
        success = self._hard_terminalize_speech() and success
        self._pending = None
        self._terminal_authority = None
        self.adapter.terminalize_permit_admission()

    def _hard_terminalize_speech(self) -> bool:
        """Close every durable act even if binding-wide cleanup misreports."""
        speech = self.coordinator.speech
        act_ids = speech.act_ids_for_binding(self.binding)
        try:
            binding_closed = speech.hard_terminalize_binding(self.binding)
        except Exception:  # noqa: BLE001
            binding_closed = False
        if not binding_closed or any(speech.is_live(act_id) for act_id in act_ids):
            for act_id in act_ids:
                try:
                    speech.hard_terminalize(act_id)
                except Exception:  # noqa: BLE001, S112
                    continue
        return all(not speech.is_live(act_id) for act_id in act_ids)

    def _is_current(self, pending: _PendingLifecycleAct) -> bool:
        turn_id = pending.intent.turn_id
        sequence = pending.intent.turn_sequence
        return (
            turn_id is not None
            and sequence is not None
            and self.state.revalidate(
                expected_version=pending.state_version,
                turn_id=turn_id,
                sequence=sequence,
            )
        )

    @staticmethod
    def _result(
        pending: _PendingLifecycleAct,
        *,
        status: LifecycleActStatus,
        emitted_intents: tuple[CallIntent, ...] = (),
    ) -> LifecycleActResult:
        item = pending.reserved[0]
        return LifecycleActResult(
            status=status,
            request_kind=pending.intent.kind,
            act_id=item.act_id,
            semantic_act_kind=item.kind,
            locale=pending.locale,
            proposal_digest=pending.proposal_digest,
            text_digest=hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
            emitted_intents=emitted_intents,
        )


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and value
        and len(value) <= 128
        and value.replace("_", "").isalnum()
    )


def _locale(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("-")
    return (
        1 <= len(parts) <= 2
        and all(
            2 <= len(part) <= 8
            and part.isalpha()
            and part.islower()
            for part in parts
        )
    )


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "LifecycleActResult",
    "LifecycleActStatus",
    "SilenceLifecycleController",
]
