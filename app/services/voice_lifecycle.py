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
    RESPONSE_AUTHORIZED = "response_authorized"
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
        if self.text_digest is not None and (
            not isinstance(self.text_digest, str)
            or len(self.text_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.text_digest)
        ):
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
        if not all(isinstance(value, enum) for value, enum in ((self.kind, VoiceEventKind), (self.source, VoiceSource), (self.sensitivity, VoiceSensitivity), (self.semantic_act_kind, VoiceSemanticActKind))):
            raise ValueError("invalid voice enum")
        if not isinstance(self.binding, VoiceSessionBinding) or not isinstance(self.payload, VoicePayload):
            raise ValueError("invalid voice event binding or payload")
        _nonnegative(self.sequence, "sequence")
        _nonnegative(self.at_ms, "at_ms")
        for name in ("input_turn_id", "generation_id", "semantic_act_id"):
            _identifier(getattr(self, name), name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VoiceEvent":
        required = {"schema_version", "kind", "source", "sensitivity", "binding", "sequence", "at_ms", "input_turn_id", "generation_id", "semantic_act_id", "semantic_act_kind", "payload"}
        if set(data) - required:
            raise ValueError("unknown voice event field")
        if set(data) != required:
            raise ValueError("missing voice event field")
        binding = data["binding"]
        payload = data["payload"]
        if not isinstance(binding, dict) or set(binding) != {"environment", "contractor_binding", "call_binding", "stream_binding", "epoch"}:
            raise ValueError("invalid voice event binding")
        if not isinstance(payload, dict) or set(payload) - {"ordinal", "duration_ms", "text_digest", "audio_id", "playout_id"}:
            raise ValueError("invalid voice event payload")
        return cls(
            schema_version=data["schema_version"],
            kind=VoiceEventKind(data["kind"]),
            source=VoiceSource(data["source"]),
            sensitivity=VoiceSensitivity(data["sensitivity"]),
            binding=VoiceSessionBinding(**binding),
            sequence=data["sequence"], at_ms=data["at_ms"],
            input_turn_id=data["input_turn_id"], generation_id=data["generation_id"],
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
        if not all(isinstance(value, enum) for value, enum in ((self.kind, VoiceCommandKind), (self.capability, VoiceCapability), (self.sensitivity, VoiceSensitivity), (self.confirmation, ConfirmationPolicy))):
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


class VoiceLifecycle:
    def __init__(self, *, binding: VoiceSessionBinding) -> None:
        self.binding = binding
        self._sequence = -1
        self._at_ms = -1
        self._acts: dict[str, tuple[VoiceEventKind, str, str, VoiceSemanticActKind, str | None, str | None, str | None]] = {}
        self._commands: dict[str, VoiceCommand] = {}
        self.rejected_event_count = 0
        self.idempotent_command_count = 0
        self.pending_question_active = False

    def ingest(self, event: VoiceEvent) -> bool:
        if event.binding != self.binding or event.sequence <= self._sequence or event.at_ms < self._at_ms:
            self.rejected_event_count += 1
            return False
        required_source = {
            VoiceEventKind.RESPONSE_AUTHORIZED: VoiceSource.LOCAL_AUTHORITATIVE,
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
        prior_record = self._acts.get(event.semantic_act_id)
        prior = prior_record[0] if prior_record is not None else None
        expected = {
            VoiceEventKind.RESPONSE_AUTHORIZED: set(),
            VoiceEventKind.SEMANTIC_ACT_CONFIRMED: {VoiceEventKind.RESPONSE_AUTHORIZED},
            VoiceEventKind.TTS_BOUND: {VoiceEventKind.SEMANTIC_ACT_CONFIRMED},
            VoiceEventKind.PLAYOUT_BOUND: {VoiceEventKind.TTS_BOUND, VoiceEventKind.PLAYOUT_RECONNECTED},
            VoiceEventKind.TRANSPORT_RESOLVED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_PARTIAL: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_CLEARED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_INTERRUPTED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.PLAYOUT_RECONNECTED: {VoiceEventKind.PLAYOUT_BOUND},
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED: {VoiceEventKind.TRANSPORT_RESOLVED},
            VoiceEventKind.ACT_FAILED: {VoiceEventKind.RESPONSE_AUTHORIZED, VoiceEventKind.SEMANTIC_ACT_CONFIRMED, VoiceEventKind.TTS_BOUND, VoiceEventKind.PLAYOUT_BOUND, VoiceEventKind.TRANSPORT_RESOLVED},
            VoiceEventKind.ACT_TIMED_OUT: {VoiceEventKind.RESPONSE_AUTHORIZED, VoiceEventKind.SEMANTIC_ACT_CONFIRMED, VoiceEventKind.TTS_BOUND, VoiceEventKind.PLAYOUT_BOUND, VoiceEventKind.TRANSPORT_RESOLVED},
        }[event.kind]
        if (prior is None and expected) or (prior is not None and prior not in expected):
            self.rejected_event_count += 1
            return False
        if prior_record is not None and prior_record[1:4] != (event.input_turn_id, event.generation_id, event.semantic_act_kind):
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
            if prior_record is None or prior_record[4:6] != (event.payload.text_digest, event.payload.audio_id):
                self.rejected_event_count += 1
                return False
        if event.kind in {VoiceEventKind.TRANSPORT_RESOLVED, VoiceEventKind.PLAYOUT_PARTIAL, VoiceEventKind.PLAYOUT_CLEARED, VoiceEventKind.PLAYOUT_INTERRUPTED, VoiceEventKind.PLAYOUT_RECONNECTED, VoiceEventKind.CALLER_PLAYBACK_OBSERVED}:
            if event.payload.text_digest is None or event.payload.audio_id is None or event.payload.playout_id is None or prior_record is None or prior_record[4:7] != (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id):
                self.rejected_event_count += 1
                return False
        if event.kind in {VoiceEventKind.ACT_FAILED, VoiceEventKind.ACT_TIMED_OUT} and prior_record is not None and prior_record[4] is not None:
            if (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id) != prior_record[4:7]:
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
        if event.kind is VoiceEventKind.CALLER_PLAYBACK_OBSERVED and event.semantic_act_kind is VoiceSemanticActKind.QUESTION:
            self.pending_question_active = True
        return True

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
        return (
            isinstance(event, VoiceEvent)
            and event.binding == self.binding
            and event.kind is VoiceEventKind.CALLER_PLAYBACK_OBSERVED
            and event.source is VoiceSource.LOCAL_AUTHORITATIVE
            and record is not None
            and record[0] is VoiceEventKind.CALLER_PLAYBACK_OBSERVED
            and record[1:4] == (event.input_turn_id, event.generation_id, event.semantic_act_kind)
            and record[4:7] == (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id)
        )

    def accepts_transport_resolution(self, event: VoiceEvent) -> bool:
        record = self._acts.get(event.semantic_act_id)
        return (
            isinstance(event, VoiceEvent)
            and event.binding == self.binding
            and event.kind is VoiceEventKind.TRANSPORT_RESOLVED
            and event.source is VoiceSource.TWILIO_AUTHENTICATED
            and record is not None
            and record[0] in {VoiceEventKind.TRANSPORT_RESOLVED, VoiceEventKind.CALLER_PLAYBACK_OBSERVED}
            and record[1:4] == (event.input_turn_id, event.generation_id, event.semantic_act_kind)
            and record[4:7] == (event.payload.text_digest, event.payload.audio_id, event.payload.playout_id)
        )
