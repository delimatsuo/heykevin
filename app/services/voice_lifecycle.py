"""Closed, offline-only lifecycle primitives for the voice bakeoff."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

VOICE_SCHEMA_VERSION = 1
_MAX_ID = 128


class VoiceSource(str, Enum):
    TWILIO_AUTHENTICATED = "twilio_authenticated"
    PROVIDER_UNTRUSTED = "provider_untrusted"
    LOCAL_AUTHORITATIVE = "local_authoritative"


class VoiceSensitivity(str, Enum):
    OPERATIONAL = "operational"
    RESTRICTED = "restricted"


class VoiceEventKind(str, Enum):
    INPUT_ACTIVITY_STARTED = "input_activity_started"
    INPUT_ACTIVITY_ENDED = "input_activity_ended"
    INPUT_TURN_PARTIAL = "input_turn_partial"
    INPUT_TURN_FINAL = "input_turn_final"
    RESPONSE_AUTHORIZED = "response_authorized"
    GENERATION_STARTED = "generation_started"
    TEXT_SEGMENT_EMITTED = "text_segment_emitted"
    AUDIO_FRAME_GENERATED = "audio_frame_generated"
    GENERATION_COMPLETED = "generation_completed"
    GENERATION_CANCELLED = "generation_cancelled"
    PROVIDER_TURN_COMPLETED = "provider_turn_completed"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_REESTABLISHED = "session_reestablished"
    SESSION_RESUMED = "session_resumed"
    SESSION_GO_AWAY = "session_go_away"
    SEMANTIC_ACT_CONFIRMED = "semantic_act_confirmed"
    TTS_BOUND = "tts_bound"
    PLAYOUT_BOUND = "playout_bound"
    TRANSPORT_RESOLVED = "transport_resolved"
    PLAYOUT_PARTIAL = "playout_partial"
    PLAYOUT_CLEARED = "playout_cleared"
    PLAYOUT_INTERRUPTED = "playout_interrupted"
    PLAYOUT_RECONNECTED = "playout_reconnected"
    CALLER_PLAYBACK_OBSERVED = "caller_playback_observed"
    ACT_FAILED = "act_failed"
    ACT_TIMED_OUT = "act_timed_out"


class VoiceSemanticActKind(str, Enum):
    ANSWER = "answer"
    QUESTION = "question"
    SAFETY = "safety"
    ACKNOWLEDGEMENT = "acknowledgement"
    PRESENCE_CHECK = "presence_check"
    REPAIR = "repair"
    CLOSING = "closing"
    REPEAT = "repeat"
    SLOWER_SPEECH = "slower_speech"
    LONGER_WAIT = "longer_wait"
    OPT_OUT = "opt_out"
    VOICEMAIL = "voicemail"


class VoiceCommandKind(str, Enum):
    ARM_SILENCE_TIMER = "arm_silence_timer"


class VoiceCapability(str, Enum):
    SILENCE_TIMER = "silence_timer"
    TERMINAL = "terminal"


class ConfirmationPolicy(str, Enum):
    NONE = "none"
    CALLER_PLAYBACK_OR_INFERENCE = "caller_playback_or_inference"


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_ID:
        raise ValueError(f"{name} is invalid")
    return value


def _nonnegative(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class VoiceSessionBinding:
    environment: str
    contractor_binding: str
    call_binding: str
    stream_binding: str
    epoch: int

    def __post_init__(self) -> None:
        for name in ("environment", "contractor_binding", "call_binding", "stream_binding"):
            _identifier(getattr(self, name), name)
        _nonnegative(self.epoch, "epoch")


@dataclass(frozen=True, slots=True)
class VoicePayload:
    """Payload-safe lifecycle facts; correlation IDs are application-minted opaque IDs.

    They must not be derived from or copied into provider/Twilio payloads or telemetry.
    """

    ordinal: int | None = None
    duration_ms: int | None = None
    text_digest: str | None = None
    audio_id: str | None = None
    playout_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("ordinal", self.ordinal), ("duration_ms", self.duration_ms)):
            if value is not None:
                _nonnegative(value, name)
        if self.text_digest is not None and (not isinstance(self.text_digest, str) or len(self.text_digest) != 64 or any(character not in "0123456789abcdef" for character in self.text_digest)):
            raise ValueError("text_digest is invalid")
        for name in ("audio_id", "playout_id"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    schema_version: int
    kind: VoiceEventKind
    source: VoiceSource
    sensitivity: VoiceSensitivity
    binding: VoiceSessionBinding
    sequence: int
    at_ms: int
    input_turn_id: str
    generation_id: str
    semantic_act_id: str
    semantic_act_kind: VoiceSemanticActKind
    payload: VoicePayload

    def __post_init__(self) -> None:
        if self.schema_version != VOICE_SCHEMA_VERSION:
            raise ValueError("unsupported voice schema version")
        if not all(
            isinstance(value, enum)
            for value, enum in (
                (self.kind, VoiceEventKind),
                (self.source, VoiceSource),
                (self.sensitivity, VoiceSensitivity),
                (self.semantic_act_kind, VoiceSemanticActKind),
            )
        ):
            raise ValueError("invalid voice enum")
        if not isinstance(self.binding, VoiceSessionBinding) or not isinstance(self.payload, VoicePayload):
            raise ValueError("invalid voice event binding or payload")
        _nonnegative(self.sequence, "sequence")
        _nonnegative(self.at_ms, "at_ms")
        for name in ("input_turn_id", "generation_id", "semantic_act_id"):
            _identifier(getattr(self, name), name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceEvent":
        required = {
            "schema_version",
            "kind",
            "source",
            "sensitivity",
            "binding",
            "sequence",
            "at_ms",
            "input_turn_id",
            "generation_id",
            "semantic_act_id",
            "semantic_act_kind",
            "payload",
        }
        if set(data) - required:
            raise ValueError("unknown voice event field")
        if set(data) != required:
            raise ValueError("missing voice event field")
        binding = data["binding"]
        payload = data["payload"]
        if not isinstance(binding, dict) or set(binding) != {
            "environment",
            "contractor_binding",
            "call_binding",
            "stream_binding",
            "epoch",
        }:
            raise ValueError("invalid voice event binding")
        if not isinstance(payload, dict) or set(payload) - {
            "ordinal",
            "duration_ms",
            "text_digest",
            "audio_id",
            "playout_id",
        }:
            raise ValueError("invalid voice event payload")
        return cls(
            schema_version=data["schema_version"],
            kind=VoiceEventKind(data["kind"]),
            source=VoiceSource(data["source"]),
            sensitivity=VoiceSensitivity(data["sensitivity"]),
            binding=VoiceSessionBinding(**binding),
            sequence=data["sequence"],
            at_ms=data["at_ms"],
            input_turn_id=data["input_turn_id"],
            generation_id=data["generation_id"],
            semantic_act_id=data["semantic_act_id"],
            semantic_act_kind=VoiceSemanticActKind(data["semantic_act_kind"]),
            payload=VoicePayload(**payload),
        )


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    schema_version: int
    kind: VoiceCommandKind
    binding: VoiceSessionBinding
    action_id: str
    idempotency_key: str
    expires_at_ms: int
    capability: VoiceCapability
    sensitivity: VoiceSensitivity
    confirmation: ConfirmationPolicy
    semantic_act_id: str
    arguments: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if self.schema_version != VOICE_SCHEMA_VERSION:
            raise ValueError("unsupported voice schema version")
        if not all(
            isinstance(value, enum)
            for value, enum in (
                (self.kind, VoiceCommandKind),
                (self.capability, VoiceCapability),
                (self.sensitivity, VoiceSensitivity),
                (self.confirmation, ConfirmationPolicy),
            )
        ):
            raise ValueError("invalid voice command")
        if not isinstance(self.binding, VoiceSessionBinding):
            raise ValueError("invalid command binding")
        for name in ("action_id", "idempotency_key", "semantic_act_id"):
            _identifier(getattr(self, name), name)
        _nonnegative(self.expires_at_ms, "expires_at_ms")
        if self.kind is not VoiceCommandKind.ARM_SILENCE_TIMER or self.capability is not VoiceCapability.SILENCE_TIMER:
            raise ValueError("command capability mismatch")
        if len(self.arguments) != 1 or len(self.arguments[0]) != 2 or self.arguments[0][0] != "timeout_ms" or not isinstance(self.arguments[0][1], int):
            raise ValueError("invalid command arguments")
        _nonnegative(self.arguments[0][1], "timeout_ms")


@dataclass(frozen=True, slots=True)
class VoiceTimeoutIntent:
    """Sequence-free timeout fact for canonical coordinator materialization."""

    binding: VoiceSessionBinding
    input_turn_id: str
    generation_id: str
    semantic_act_id: str
    semantic_act_kind: VoiceSemanticActKind
    payload: VoicePayload = VoicePayload()

    def __post_init__(self) -> None:
        if not isinstance(self.binding, VoiceSessionBinding) or not isinstance(self.semantic_act_kind, VoiceSemanticActKind) or not isinstance(self.payload, VoicePayload):
            raise ValueError("invalid timeout intent binding")
        for name in ("input_turn_id", "generation_id", "semantic_act_id"):
            _identifier(getattr(self, name), name)

    def event(self, *, sequence: int, at_ms: int) -> VoiceEvent:
        return VoiceEvent(
            schema_version=VOICE_SCHEMA_VERSION,
            kind=VoiceEventKind.ACT_TIMED_OUT,
            source=VoiceSource.LOCAL_AUTHORITATIVE,
            sensitivity=VoiceSensitivity.OPERATIONAL,
            binding=self.binding,
            sequence=sequence,
            at_ms=at_ms,
            input_turn_id=self.input_turn_id,
            generation_id=self.generation_id,
            semantic_act_id=self.semantic_act_id,
            semantic_act_kind=self.semantic_act_kind,
            payload=self.payload,
        )


class VoiceLifecycle:
    def __init__(self, *, binding: VoiceSessionBinding) -> None:
        self.binding = binding
        self._sequence = -1
        self._at_ms = -1
        self._acts: dict[
            str,
            tuple[VoiceEventKind, str, str, VoiceSemanticActKind, str | None, str | None, str | None],
        ] = {}
        self._input_states: dict[str, VoiceEventKind] = {}
        self._input_finals: dict[str, VoiceEvent] = {}
        self._generation_states: dict[tuple[str, str, str, VoiceSemanticActKind], VoiceEventKind] = {}
        self._session_event: VoiceEvent | None = None
        self._response_authorizations: dict[str, VoiceEvent] = {}
        self._semantic_confirmations: dict[str, VoiceEvent] = {}
        self._act_terminals: dict[str, VoiceEvent] = {}
        self._commands: dict[str, VoiceCommand] = {}
        self.rejected_event_count = 0
        self.idempotent_command_count = 0
        self.pending_question_active = False

    def ingest(self, event: VoiceEvent) -> bool:
        if event.binding != self.binding or event.sequence <= self._sequence or event.at_ms < self._at_ms:
            self.rejected_event_count += 1
            return False
        required_source = {
            VoiceEventKind.INPUT_ACTIVITY_STARTED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.INPUT_ACTIVITY_ENDED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.INPUT_TURN_PARTIAL: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.INPUT_TURN_FINAL: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.RESPONSE_AUTHORIZED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.GENERATION_STARTED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.TEXT_SEGMENT_EMITTED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.AUDIO_FRAME_GENERATED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.GENERATION_COMPLETED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.GENERATION_CANCELLED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.PROVIDER_TURN_COMPLETED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.SESSION_DISCONNECTED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.SESSION_REESTABLISHED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.SESSION_RESUMED: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.SESSION_GO_AWAY: VoiceSource.PROVIDER_UNTRUSTED,
            VoiceEventKind.SEMANTIC_ACT_CONFIRMED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.TTS_BOUND: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.PLAYOUT_BOUND: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.TRANSPORT_RESOLVED: VoiceSource.TWILIO_AUTHENTICATED,
            VoiceEventKind.PLAYOUT_PARTIAL: VoiceSource.TWILIO_AUTHENTICATED,
            VoiceEventKind.PLAYOUT_CLEARED: VoiceSource.TWILIO_AUTHENTICATED,
            VoiceEventKind.PLAYOUT_INTERRUPTED: VoiceSource.TWILIO_AUTHENTICATED,
            VoiceEventKind.PLAYOUT_RECONNECTED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.ACT_FAILED: VoiceSource.LOCAL_AUTHORITATIVE,
            VoiceEventKind.ACT_TIMED_OUT: VoiceSource.LOCAL_AUTHORITATIVE,
        }[event.kind]
        if event.source is not required_source:
            self.rejected_event_count += 1
            return False
        if event.kind in _OBSERVATIONAL_EVENT_KINDS:
            if not self._ingest_observation(event):
                self.rejected_event_count += 1
                return False
            self._sequence, self._at_ms = event.sequence, event.at_ms
            return True
        prior_record = self._acts.get(event.semantic_act_id)
        prior = prior_record[0] if prior_record is not None else None
        expected = {
            VoiceEventKind.RESPONSE_AUTHORIZED: set(),
            VoiceEventKind.SEMANTIC_ACT_CONFIRMED: {VoiceEventKind.RESPONSE_AUTHORIZED},
            VoiceEventKind.TTS_BOUND: {VoiceEventKind.SEMANTIC_ACT_CONFIRMED},
            VoiceEventKind.PLAYOUT_BOUND: {
                VoiceEventKind.TTS_BOUND,
                VoiceEventKind.PLAYOUT_RECONNECTED,
            },
            VoiceEventKind.TRANSPORT_RESOLVED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_PARTIAL: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_CLEARED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_INTERRUPTED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_RECONNECTED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED: {VoiceEventKind.TRANSPORT_RESOLVED},
            VoiceEventKind.ACT_FAILED: {
                VoiceEventKind.RESPONSE_AUTHORIZED,
                VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
                VoiceEventKind.TTS_BOUND,
                VoiceEventKind.PLAYOUT_BOUND,
                VoiceEventKind.TRANSPORT_RESOLVED,
            },
            VoiceEventKind.ACT_TIMED_OUT: {
                VoiceEventKind.RESPONSE_AUTHORIZED,
                VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
                VoiceEventKind.TTS_BOUND,
                VoiceEventKind.PLAYOUT_BOUND,
                VoiceEventKind.TRANSPORT_RESOLVED,
            },
        }[event.kind]
        if (prior is None and expected) or (prior is not None and prior not in expected):
            self.rejected_event_count += 1
            return False
        if prior_record is not None and prior_record[1:4] != (
            event.input_turn_id,
            event.generation_id,
            event.semantic_act_kind,
        ):
            self.rejected_event_count += 1
            return False
        if event.kind is VoiceEventKind.TTS_BOUND:
            if event.payload.text_digest is None or event.payload.audio_id is None or event.payload.playout_id is not None:
                self.rejected_event_count += 1
                return False
        if event.kind is VoiceEventKind.PLAYOUT_BOUND:
            if event.payload.text_digest is None or event.payload.audio_id is None or event.payload.playout_id is None:
                self.rejected_event_count += 1
                return False
            if prior_record is None or prior_record[4:6] != (
                event.payload.text_digest,
                event.payload.audio_id,
            ):
                self.rejected_event_count += 1
                return False
        if event.kind in {
            VoiceEventKind.TRANSPORT_RESOLVED,
            VoiceEventKind.PLAYOUT_PARTIAL,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.PLAYOUT_INTERRUPTED,
            VoiceEventKind.PLAYOUT_RECONNECTED,
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        }:
            if event.payload.text_digest is None or event.payload.audio_id is None or event.payload.playout_id is None or prior_record is None or prior_record[4:7] != (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id):
                self.rejected_event_count += 1
                return False
        if event.kind in {VoiceEventKind.ACT_FAILED, VoiceEventKind.ACT_TIMED_OUT} and prior_record is not None and prior_record[4] is not None:
            if (
                event.payload.text_digest,
                event.payload.audio_id,
                event.payload.playout_id,
            ) != prior_record[4:7]:
                self.rejected_event_count += 1
                return False
        self._sequence, self._at_ms = event.sequence, event.at_ms
        payload = event.payload
        prior_text = prior_record[4] if prior_record is not None else None
        prior_audio = prior_record[5] if prior_record is not None else None
        prior_playout = prior_record[6] if prior_record is not None else None
        self._acts[event.semantic_act_id] = (
            event.kind,
            event.input_turn_id,
            event.generation_id,
            event.semantic_act_kind,
            payload.text_digest if payload.text_digest is not None else prior_text,
            payload.audio_id if payload.audio_id is not None else prior_audio,
            payload.playout_id if payload.playout_id is not None else prior_playout,
        )
        if event.kind is VoiceEventKind.RESPONSE_AUTHORIZED:
            self._response_authorizations[event.semantic_act_id] = event
        if event.kind is VoiceEventKind.SEMANTIC_ACT_CONFIRMED:
            self._semantic_confirmations[event.semantic_act_id] = event
        if event.kind in {VoiceEventKind.ACT_FAILED, VoiceEventKind.ACT_TIMED_OUT}:
            self._act_terminals[event.semantic_act_id] = event
        if event.kind is VoiceEventKind.CALLER_PLAYBACK_OBSERVED and event.semantic_act_kind is VoiceSemanticActKind.QUESTION:
            self.pending_question_active = True
        return True

    def accepts_input_final(self, event: VoiceEvent) -> bool:
        """Return true only for the exact candidate-final event already ingested."""
        return isinstance(event, VoiceEvent) and event.binding == self.binding and event.kind is VoiceEventKind.INPUT_TURN_FINAL and event.source is VoiceSource.PROVIDER_UNTRUSTED and self._input_finals.get(event.input_turn_id) == event

    def accepts_session_resume(self, event: VoiceEvent) -> bool:
        """Return true only for the exact resume receipt this reducer accepted."""
        return (
            isinstance(event, VoiceEvent)
            and event.binding == self.binding
            and event.kind is VoiceEventKind.SESSION_RESUMED
            and event.source is VoiceSource.PROVIDER_UNTRUSTED
            and self._session_event is event
        )

    def accept_command(self, command: VoiceCommand, *, now_ms: int) -> bool:
        if command.binding != self.binding or command.expires_at_ms < _nonnegative(now_ms, "now_ms"):
            return False
        act = self._acts.get(command.semantic_act_id)
        if act is None or act[0] is not VoiceEventKind.CALLER_PLAYBACK_OBSERVED or act[3] is not VoiceSemanticActKind.QUESTION or command.confirmation is not ConfirmationPolicy.CALLER_PLAYBACK_OR_INFERENCE:
            return False
        existing = self._commands.get(command.idempotency_key)
        if existing is not None:
            if existing != command:
                return False
            self.idempotent_command_count += 1
            return True
        self._commands[command.idempotency_key] = command
        return True

    def accepts_caller_playback(self, event: VoiceEvent) -> bool:
        """Return true only for this lifecycle's already accepted caller receipt."""
        record = self._acts.get(event.semantic_act_id)
        return isinstance(event, VoiceEvent) and event.binding == self.binding and event.kind is VoiceEventKind.CALLER_PLAYBACK_OBSERVED and event.source is VoiceSource.LOCAL_AUTHORITATIVE and record is not None and record[0] is VoiceEventKind.CALLER_PLAYBACK_OBSERVED and record[1:4] == (event.input_turn_id, event.generation_id, event.semantic_act_kind) and record[4:7] == (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id)

    def accepts_transport_resolution(self, event: VoiceEvent) -> bool:
        record = self._acts.get(event.semantic_act_id)
        return isinstance(event, VoiceEvent) and event.binding == self.binding and event.kind is VoiceEventKind.TRANSPORT_RESOLVED and event.source is VoiceSource.TWILIO_AUTHENTICATED and record is not None and record[0] in {VoiceEventKind.TRANSPORT_RESOLVED, VoiceEventKind.CALLER_PLAYBACK_OBSERVED} and record[1:4] == (event.input_turn_id, event.generation_id, event.semantic_act_kind) and record[4:7] == (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id)

    def accepts_semantic_confirmation(self, event: VoiceEvent) -> bool:
        """Return true only for the exact confirmation accepted by this reducer."""
        record = self._acts.get(event.semantic_act_id)
        return isinstance(event, VoiceEvent) and event.binding == self.binding and event.kind is VoiceEventKind.SEMANTIC_ACT_CONFIRMED and event.source is VoiceSource.LOCAL_AUTHORITATIVE and self._semantic_confirmations.get(event.semantic_act_id) == event and record is not None and record[0] is VoiceEventKind.SEMANTIC_ACT_CONFIRMED and record[1:4] == (event.input_turn_id, event.generation_id, event.semantic_act_kind)

    def accepts_response_authorization(self, event: VoiceEvent) -> bool:
        """Return true only for the exact current authorization receipt."""
        record = self._acts.get(event.semantic_act_id)
        return (
            isinstance(event, VoiceEvent)
            and event.binding == self.binding
            and event.kind is VoiceEventKind.RESPONSE_AUTHORIZED
            and event.source is VoiceSource.LOCAL_AUTHORITATIVE
            and self._response_authorizations.get(event.semantic_act_id) is event
            and record is not None
            and record[0] is VoiceEventKind.RESPONSE_AUTHORIZED
            and record[1:4]
            == (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_kind,
            )
        )

    def recognizes_response_authorization(self, event: VoiceEvent) -> bool:
        """Recognize the exact issued authorization after later act progress."""
        return (
            isinstance(event, VoiceEvent)
            and event.binding == self.binding
            and event.kind is VoiceEventKind.RESPONSE_AUTHORIZED
            and event.source is VoiceSource.LOCAL_AUTHORITATIVE
            and self._response_authorizations.get(event.semantic_act_id)
            is event
        )

    def accepts_act_timeout(self, event: VoiceEvent) -> bool:
        """Return true only for the exact timeout accepted by this reducer."""
        record = self._acts.get(event.semantic_act_id)
        return (
            isinstance(event, VoiceEvent)
            and event.binding == self.binding
            and event.kind is VoiceEventKind.ACT_TIMED_OUT
            and event.source is VoiceSource.LOCAL_AUTHORITATIVE
            and self._act_terminals.get(event.semantic_act_id) is event
            and record is not None
            and record[0] is VoiceEventKind.ACT_TIMED_OUT
            and record[1:4]
            == (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_kind,
            )
        )

    def act_state(self, event: VoiceEvent) -> VoiceEventKind | None:
        """Return the current state only for the exact bound semantic act."""
        if not isinstance(event, VoiceEvent) or event.binding != self.binding:
            return None
        record = self._acts.get(event.semantic_act_id)
        if (
            record is None
            or record[1:4]
            != (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_kind,
            )
        ):
            return None
        return record[0]

    def terminal_payload(self, event: VoiceEvent) -> VoicePayload | None:
        """Return the exact accumulated identity needed for an act terminal."""
        if self.act_state(event) is None:
            return None
        record = self._acts[event.semantic_act_id]
        return VoicePayload(
            text_digest=record[4],
            audio_id=record[5],
            playout_id=record[6],
        )

    def next_position(self, *, at_ms: int) -> tuple[int, int]:
        """Allocate the next monotonic position without mutating the reducer."""
        return self._sequence + 1, max(self._at_ms, _nonnegative(at_ms, "at_ms"))

    def _ingest_observation(self, event: VoiceEvent) -> bool:
        if event.kind in _INPUT_EVENT_KINDS:
            prior = self._input_states.get(event.input_turn_id)
            if event.kind is VoiceEventKind.INPUT_ACTIVITY_STARTED:
                allowed = prior in {
                    None,
                    VoiceEventKind.INPUT_ACTIVITY_ENDED,
                    VoiceEventKind.INPUT_TURN_PARTIAL,
                }
            elif event.kind is VoiceEventKind.INPUT_ACTIVITY_ENDED:
                allowed = prior is VoiceEventKind.INPUT_ACTIVITY_STARTED
            elif event.kind is VoiceEventKind.INPUT_TURN_PARTIAL:
                allowed = prior is not VoiceEventKind.INPUT_TURN_FINAL
            else:
                allowed = prior is not VoiceEventKind.INPUT_TURN_FINAL
            if not allowed or event.payload.audio_id is not None or event.payload.playout_id is not None:
                return False
            self._input_states[event.input_turn_id] = event.kind
            if event.kind is VoiceEventKind.INPUT_TURN_FINAL:
                self._input_finals[event.input_turn_id] = event
            return True
        if event.kind in _GENERATION_EVENT_KINDS:
            authorization = self._acts.get(event.semantic_act_id)
            if (
                authorization is None
                or authorization[1:4]
                != (
                    event.input_turn_id,
                    event.generation_id,
                    event.semantic_act_kind,
                )
                or authorization[0]
                not in {
                    VoiceEventKind.RESPONSE_AUTHORIZED,
                    VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
                    VoiceEventKind.TTS_BOUND,
                    VoiceEventKind.PLAYOUT_BOUND,
                    VoiceEventKind.TRANSPORT_RESOLVED,
                }
            ):
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            prior = self._generation_states.get(key)
            allowed_prior = {
                VoiceEventKind.GENERATION_STARTED: {None},
                VoiceEventKind.TEXT_SEGMENT_EMITTED: {
                    VoiceEventKind.GENERATION_STARTED,
                    VoiceEventKind.TEXT_SEGMENT_EMITTED,
                },
                VoiceEventKind.AUDIO_FRAME_GENERATED: {
                    VoiceEventKind.GENERATION_STARTED,
                    VoiceEventKind.AUDIO_FRAME_GENERATED,
                },
                VoiceEventKind.GENERATION_COMPLETED: {
                    VoiceEventKind.GENERATION_STARTED,
                    VoiceEventKind.TEXT_SEGMENT_EMITTED,
                    VoiceEventKind.AUDIO_FRAME_GENERATED,
                },
                VoiceEventKind.GENERATION_CANCELLED: {
                    VoiceEventKind.GENERATION_STARTED,
                    VoiceEventKind.TEXT_SEGMENT_EMITTED,
                    VoiceEventKind.AUDIO_FRAME_GENERATED,
                },
                VoiceEventKind.PROVIDER_TURN_COMPLETED: {
                    VoiceEventKind.GENERATION_COMPLETED,
                },
            }[event.kind]
            if prior not in allowed_prior:
                return False
            if event.kind is VoiceEventKind.TEXT_SEGMENT_EMITTED and event.payload.text_digest is None:
                return False
            if event.kind is VoiceEventKind.AUDIO_FRAME_GENERATED and event.payload.audio_id is None:
                return False
            self._generation_states[key] = event.kind
            return True
        prior = (
            self._session_event.kind
            if self._session_event is not None
            else None
        )
        allowed_prior = {
            VoiceEventKind.SESSION_DISCONNECTED: {
                None,
                VoiceEventKind.SESSION_REESTABLISHED,
                VoiceEventKind.SESSION_RESUMED,
            },
            VoiceEventKind.SESSION_REESTABLISHED: {
                VoiceEventKind.SESSION_DISCONNECTED,
            },
            VoiceEventKind.SESSION_GO_AWAY: {
                None,
                VoiceEventKind.SESSION_REESTABLISHED,
                VoiceEventKind.SESSION_RESUMED,
            },
            VoiceEventKind.SESSION_RESUMED: {
                VoiceEventKind.SESSION_GO_AWAY,
                VoiceEventKind.SESSION_DISCONNECTED,
            },
        }[event.kind]
        if prior not in allowed_prior:
            return False
        self._session_event = event
        return True


_INPUT_EVENT_KINDS = {
    VoiceEventKind.INPUT_ACTIVITY_STARTED,
    VoiceEventKind.INPUT_ACTIVITY_ENDED,
    VoiceEventKind.INPUT_TURN_PARTIAL,
    VoiceEventKind.INPUT_TURN_FINAL,
}
_GENERATION_EVENT_KINDS = {
    VoiceEventKind.GENERATION_STARTED,
    VoiceEventKind.TEXT_SEGMENT_EMITTED,
    VoiceEventKind.AUDIO_FRAME_GENERATED,
    VoiceEventKind.GENERATION_COMPLETED,
    VoiceEventKind.GENERATION_CANCELLED,
    VoiceEventKind.PROVIDER_TURN_COMPLETED,
}
_SESSION_EVENT_KINDS = {
    VoiceEventKind.SESSION_DISCONNECTED,
    VoiceEventKind.SESSION_REESTABLISHED,
    VoiceEventKind.SESSION_RESUMED,
    VoiceEventKind.SESSION_GO_AWAY,
}
_OBSERVATIONAL_EVENT_KINDS = _INPUT_EVENT_KINDS | _GENERATION_EVENT_KINDS | _SESSION_EVENT_KINDS
