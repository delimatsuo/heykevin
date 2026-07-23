"""Provider-neutral, final-turn-only observation extraction for the bakeoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from threading import Lock
from typing import Protocol

from app.services.receptionist_state import CallerObservation, CallbackConfirmation, CallbackIntent
from app.services.voice_lifecycle import VoiceSessionBinding


_MAX_CONTENT_CHARS = 4_000
_OBSERVATION_FIELDS = frozenset(
    {
        "language",
        "identity_confirmed",
        "business_scope",
        "business_scope_reason",
        "intent",
        "service_object",
        "service_action",
        "urgency",
        "callback_intent",
        "callback_confirmation",
        "callback_phone_last_four",
        "address_need",
    }
)


class Finality(str, Enum):
    FINAL = "final"
    PARTIAL = "partial"


class BackendOutcome(str, Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"


class ExtractionOutcome(str, Enum):
    ACCEPTED = "accepted"
    NOT_FINAL = "not_final"
    LOW_CONFIDENCE = "low_confidence"
    MALFORMED = "malformed"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    CANCELLED = "cancelled"
    LATE = "late"


@dataclass(frozen=True, slots=True)
class CandidateFinalTurn:
    binding: VoiceSessionBinding
    input_turn_id: str
    sequence: int
    at_ms: int
    finality: Finality
    content: str = field(repr=False)
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.binding, VoiceSessionBinding):
            raise ValueError("candidate binding is invalid")
        if not isinstance(self.input_turn_id, str) or not self.input_turn_id or len(self.input_turn_id) > 128:
            raise ValueError("candidate turn id is invalid")
        if type(self.sequence) is not int or self.sequence < 0 or type(self.at_ms) is not int or self.at_ms < 0:
            raise ValueError("candidate sequence or time is invalid")
        if not isinstance(self.finality, Finality) or type(self.cancelled) is not bool:
            raise ValueError("candidate finality is invalid")
        if not isinstance(self.content, str) or not self.content or len(self.content) > _MAX_CONTENT_CHARS:
            raise ValueError("candidate content is invalid")


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    request_id: str
    binding: VoiceSessionBinding
    input_turn_id: str
    sequence: int
    at_ms: int
    configuration_digest: str
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BackendResponse:
    request_id: str
    configuration_digest: str
    outcome: BackendOutcome
    fields: dict[str, object]
    confidences: dict[str, float]


class ObservationBackend(Protocol):
    def __call__(self, request: ExtractionRequest) -> BackendResponse: ...


class CurrentTurnGuard(Protocol):
    def __call__(self, turn: CandidateFinalTurn) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    outcome: ExtractionOutcome
    observation: CallerObservation | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is ExtractionOutcome.ACCEPTED:
            if not isinstance(self.observation, CallerObservation) or self.reason is not None:
                raise ValueError("accepted extraction result is invalid")
        elif self.observation is not None or self.reason not in {"admission", "backend", "schema", "confidence", "late"}:
            raise ValueError("rejected extraction result is invalid")


class ObservationExtractor:
    """Admits final turns and parses one closed untrusted observation response."""

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        configuration_digest: str,
        min_field_confidence: float,
        min_aggregate_confidence: float,
    ) -> None:
        if not isinstance(binding, VoiceSessionBinding) or not _digest(configuration_digest):
            raise ValueError("extractor binding or configuration is invalid")
        for value in (min_field_confidence, min_aggregate_confidence):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("confidence threshold is invalid")
        self.binding = binding
        self.configuration_digest = configuration_digest
        self.min_field_confidence = float(min_field_confidence)
        self.min_aggregate_confidence = float(min_aggregate_confidence)
        self._last_admitted_sequence = -1
        self._last_admitted_at_ms = -1
        self._admission_lock = Lock()

    def extract(self, turn: CandidateFinalTurn, *, backend: ObservationBackend, current_turn: CurrentTurnGuard | None = None) -> ExtractionResult:
        guard = current_turn or _default_current_turn
        admission = self._admit(turn, guard=guard)
        if admission is not None:
            return admission
        request = self._request(turn)
        try:
            response = backend(request)
        except Exception:
            return _rejected(ExtractionOutcome.PROVIDER_ERROR, "backend")
        if not guard(turn):
            return _rejected(ExtractionOutcome.LATE, "late")
        if not isinstance(response, BackendResponse) or response.request_id != request.request_id or response.configuration_digest != self.configuration_digest:
            return _rejected(ExtractionOutcome.LATE, "late")
        if not isinstance(response.outcome, BackendOutcome):
            return _rejected(ExtractionOutcome.MALFORMED, "schema")
        if response.outcome is BackendOutcome.TIMEOUT:
            return _rejected(ExtractionOutcome.TIMEOUT, "backend")
        if response.outcome is BackendOutcome.ERROR:
            return _rejected(ExtractionOutcome.PROVIDER_ERROR, "backend")
        if response.outcome is BackendOutcome.CANCELLED:
            return _rejected(ExtractionOutcome.CANCELLED, "backend")
        if response.outcome is not BackendOutcome.OK:
            return _rejected(ExtractionOutcome.MALFORMED, "schema")
        parsed = self._parse(response)
        if isinstance(parsed, ExtractionResult):
            return parsed
        if turn.binding != self.binding or not guard(turn):
            return _rejected(ExtractionOutcome.LATE, "late")
        return ExtractionResult(ExtractionOutcome.ACCEPTED, observation=parsed)

    def _admit(self, turn: CandidateFinalTurn, *, guard: CurrentTurnGuard) -> ExtractionResult | None:
        if not isinstance(turn, CandidateFinalTurn) or turn.binding != self.binding:
            return _rejected(ExtractionOutcome.LATE, "late")
        if turn.finality is not Finality.FINAL:
            return _rejected(ExtractionOutcome.NOT_FINAL, "admission")
        if turn.cancelled:
            return _rejected(ExtractionOutcome.CANCELLED, "admission")
        if not guard(turn):
            return _rejected(ExtractionOutcome.LATE, "late")
        with self._admission_lock:
            if turn.sequence <= self._last_admitted_sequence or turn.at_ms < self._last_admitted_at_ms:
                return _rejected(ExtractionOutcome.LATE, "late")
            self._last_admitted_sequence = turn.sequence
            self._last_admitted_at_ms = turn.at_ms
        return None

    def _request(self, turn: CandidateFinalTurn) -> ExtractionRequest:
        material = {
            "binding": (
                turn.binding.environment,
                turn.binding.contractor_binding,
                turn.binding.call_binding,
                turn.binding.stream_binding,
                turn.binding.epoch,
            ),
            "turn": turn.input_turn_id,
            "sequence": turn.sequence,
            "configuration": self.configuration_digest,
            "content_digest": hashlib.sha256(turn.content.encode("utf-8")).hexdigest(),
        }
        request_id = "request_" + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ExtractionRequest(
            request_id,
            turn.binding,
            turn.input_turn_id,
            turn.sequence,
            turn.at_ms,
            self.configuration_digest,
            turn.content,
        )

    def _parse(self, response: BackendResponse) -> CallerObservation | ExtractionResult:
        if not isinstance(response.fields, dict) or not response.fields or set(response.fields) - _OBSERVATION_FIELDS or not isinstance(response.confidences, dict) or set(response.confidences) != set(response.fields):
            return _rejected(ExtractionOutcome.MALFORMED, "schema")
        values = tuple(response.confidences.values())
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1 for value in values):
            return _rejected(ExtractionOutcome.MALFORMED, "schema")
        if any(value < self.min_field_confidence for value in values) or sum(values) / len(values) < self.min_aggregate_confidence:
            return _rejected(ExtractionOutcome.LOW_CONFIDENCE, "confidence")
        try:
            observation = CallerObservation.from_dict(response.fields)
        except (TypeError, ValueError):
            return _rejected(ExtractionOutcome.MALFORMED, "schema")
        if observation.callback_intent is CallbackIntent.DECLINED and (observation.callback_confirmation is CallbackConfirmation.CONFIRMED or observation.callback_phone_last_four is not None):
            return _rejected(ExtractionOutcome.MALFORMED, "schema")
        return observation


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _rejected(outcome: ExtractionOutcome, reason: str) -> ExtractionResult:
    return ExtractionResult(outcome, reason=reason)


def _default_current_turn(turn: CandidateFinalTurn) -> bool:
    return not turn.cancelled
