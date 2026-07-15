"""Provider-neutral retrospective caller-turn assembly primitives."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any
import unicodedata


CALLER_TURN_SCHEMA_VERSION = 1
DEFAULT_QUIESCENCE_MS = 250
DEFAULT_MAX_EVENTS_PER_TURN = 128
DEFAULT_MAX_TRANSCRIPT_CODEPOINTS = 4_000
DEFAULT_MAX_TRANSCRIPT_UTF8_BYTES = 16_000
DEFAULT_MAX_RETAINED_SEQUENCES = 1_024


class CallerTurnEventKind(str, Enum):
    INPUT_TRANSCRIPT_FRAGMENT = "input_transcript_fragment"
    MODEL_OUTPUT_STARTED = "model_output_started"
    GENERATION_COMPLETE = "generation_complete"
    TURN_COMPLETE = "turn_complete"
    INTERRUPTED = "interrupted"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_CANCELLED = "tool_call_cancelled"
    CONNECTION_CLOSED = "connection_closed"
    RECONNECT_STARTED = "reconnect_started"
    PIPELINE_STOPPED = "pipeline_stopped"


class CallerTurnCompletionStatus(str, Enum):
    RETROSPECTIVE_COMPLETE = "retrospective_complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    DROPPED = "dropped"


class CallerTurnCloseReason(str, Enum):
    MODEL_OUTPUT_STARTED = "model_output_started"
    GENERATION_COMPLETE = "generation_complete"
    TURN_COMPLETE = "turn_complete"
    INTERRUPTED = "interrupted"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_CANCELLED = "tool_call_cancelled"
    CONNECTION_CLOSED = "connection_closed"
    RECONNECT_STARTED = "reconnect_started"
    PIPELINE_STOPPED = "pipeline_stopped"
    RESOURCE_LIMIT = "resource_limit"


_TERMINAL_REASONS = {
    CallerTurnEventKind.MODEL_OUTPUT_STARTED: CallerTurnCloseReason.MODEL_OUTPUT_STARTED,
    CallerTurnEventKind.GENERATION_COMPLETE: CallerTurnCloseReason.GENERATION_COMPLETE,
    CallerTurnEventKind.TURN_COMPLETE: CallerTurnCloseReason.TURN_COMPLETE,
    CallerTurnEventKind.INTERRUPTED: CallerTurnCloseReason.INTERRUPTED,
    CallerTurnEventKind.TOOL_CALL_STARTED: CallerTurnCloseReason.TOOL_CALL_STARTED,
}
_IMMEDIATE_REASONS = {
    CallerTurnEventKind.TOOL_CALL_CANCELLED: CallerTurnCloseReason.TOOL_CALL_CANCELLED,
    CallerTurnEventKind.CONNECTION_CLOSED: CallerTurnCloseReason.CONNECTION_CLOSED,
    CallerTurnEventKind.RECONNECT_STARTED: CallerTurnCloseReason.RECONNECT_STARTED,
    CallerTurnEventKind.PIPELINE_STOPPED: CallerTurnCloseReason.PIPELINE_STOPPED,
}


def _bounded_nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def _normalize_fragment(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("text contains a control character")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("text must be valid Unicode") from exc
    if len(normalized) > DEFAULT_MAX_TRANSCRIPT_CODEPOINTS or len(encoded) > (
        DEFAULT_MAX_TRANSCRIPT_UTF8_BYTES
    ):
        raise ValueError("text fragment exceeds the event bound")
    return normalized


@dataclass(frozen=True, slots=True)
class CallerTurnEvent:
    kind: CallerTurnEventKind
    at_ms: int
    sequence: int
    epoch: int
    text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CallerTurnEventKind):
            raise TypeError("kind must be a caller turn event kind")
        object.__setattr__(self, "at_ms", _bounded_nonnegative_int(self.at_ms, name="at_ms"))
        object.__setattr__(
            self,
            "sequence",
            _bounded_nonnegative_int(self.sequence, name="sequence"),
        )
        object.__setattr__(self, "epoch", _bounded_nonnegative_int(self.epoch, name="epoch"))
        normalized = _normalize_fragment(self.text)
        if self.kind is not CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT and normalized:
            raise ValueError("only transcript fragment events may contain text")
        object.__setattr__(self, "text", normalized)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallerTurnEvent":
        if not isinstance(data, dict):
            raise TypeError("caller turn event must be an object")
        allowed = {"kind", "at_ms", "sequence", "epoch", "text"}
        if set(data) - allowed:
            raise ValueError("unknown caller turn event field")
        text = data.get("text", "")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return cls(
            kind=CallerTurnEventKind(data["kind"]),
            at_ms=data["at_ms"],
            sequence=data["sequence"],
            epoch=data["epoch"],
            text=text,
        )


@dataclass(frozen=True, slots=True)
class RetrospectiveCallerTurn:
    schema_version: int
    epoch: int
    turn_id: int
    transcript: str
    status: CallerTurnCompletionStatus
    close_reason: CallerTurnCloseReason
    first_event_at_ms: int
    last_transcript_at_ms: int
    terminal_at_ms: int
    finalized_at_ms: int
    event_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "epoch": self.epoch,
            "turn_id": self.turn_id,
            "transcript": self.transcript,
            "status": self.status.value,
            "close_reason": self.close_reason.value,
            "first_event_at_ms": self.first_event_at_ms,
            "last_transcript_at_ms": self.last_transcript_at_ms,
            "terminal_at_ms": self.terminal_at_ms,
            "finalized_at_ms": self.finalized_at_ms,
            "event_count": self.event_count,
        }

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "epoch": self.epoch,
            "turn_id": self.turn_id,
            "status": self.status.value,
            "close_reason": self.close_reason.value,
            "first_event_at_ms": self.first_event_at_ms,
            "last_transcript_at_ms": self.last_transcript_at_ms,
            "terminal_at_ms": self.terminal_at_ms,
            "finalized_at_ms": self.finalized_at_ms,
            "event_count": self.event_count,
            "transcript_codepoints": len(self.transcript),
            "transcript_utf8_bytes": len(self.transcript.encode("utf-8")),
        }


class CallerTurnAssembler:
    """Assemble bounded retrospective turns from receipt-ordered typed events."""

    def __init__(
        self,
        *,
        active_epoch: int,
        quiescence_ms: int = DEFAULT_QUIESCENCE_MS,
        max_events_per_turn: int = DEFAULT_MAX_EVENTS_PER_TURN,
        max_transcript_codepoints: int = DEFAULT_MAX_TRANSCRIPT_CODEPOINTS,
        max_transcript_utf8_bytes: int = DEFAULT_MAX_TRANSCRIPT_UTF8_BYTES,
        max_retained_sequences: int = DEFAULT_MAX_RETAINED_SEQUENCES,
    ) -> None:
        self._active_epoch = _bounded_nonnegative_int(active_epoch, name="active_epoch")
        self._quiescence_ms = _bounded_nonnegative_int(quiescence_ms, name="quiescence_ms")
        if self._quiescence_ms == 0:
            raise ValueError("quiescence_ms must be positive")
        self._max_events = _bounded_nonnegative_int(
            max_events_per_turn,
            name="max_events_per_turn",
        )
        self._max_codepoints = _bounded_nonnegative_int(
            max_transcript_codepoints,
            name="max_transcript_codepoints",
        )
        self._max_utf8_bytes = _bounded_nonnegative_int(
            max_transcript_utf8_bytes,
            name="max_transcript_utf8_bytes",
        )
        if min(self._max_events, self._max_codepoints, self._max_utf8_bytes) == 0:
            raise ValueError("caller turn resource limits must be positive")
        self._max_retained_sequences = _bounded_nonnegative_int(
            max_retained_sequences,
            name="max_retained_sequences",
        )
        if self._max_retained_sequences == 0:
            raise ValueError("retained sequence limit must be positive")

        self._next_turn_id = 1
        self._seen_sequences: set[int] = set()
        self._seen_sequence_order: deque[int] = deque()
        self._last_at_ms = 0
        self.duplicate_event_count = 0
        self.stale_event_count = 0
        self._reset_pending()

    @property
    def active_epoch(self) -> int:
        return self._active_epoch

    @property
    def next_deadline_ms(self) -> int | None:
        return self._deadline_ms

    @property
    def retained_sequence_count(self) -> int:
        return len(self._seen_sequences)

    def ingest(self, event: CallerTurnEvent) -> tuple[RetrospectiveCallerTurn, ...]:
        if not isinstance(event, CallerTurnEvent):
            raise TypeError("event must be a CallerTurnEvent")
        if event.epoch < self._active_epoch:
            self.stale_event_count += 1
            return ()
        if (
            event.epoch > self._active_epoch
            and event.kind is not CallerTurnEventKind.RECONNECT_STARTED
        ):
            self.stale_event_count += 1
            return ()
        if event.at_ms < self._last_at_ms:
            raise ValueError("event time must be monotonic within an epoch")

        emitted = list(self._finalize_expired(event.at_ms))
        if event.epoch > self._active_epoch:
            emitted.extend(
                self._finalize(
                    at_ms=event.at_ms,
                    status=CallerTurnCompletionStatus.PARTIAL,
                    reason=CallerTurnCloseReason.RECONNECT_STARTED,
                )
            )
            self._active_epoch = event.epoch
            self._seen_sequences.clear()
            self._seen_sequence_order.clear()
            self._last_at_ms = event.at_ms

        self._last_at_ms = event.at_ms

        if event.sequence in self._seen_sequences:
            self.duplicate_event_count += 1
            return tuple(emitted)
        self._seen_sequences.add(event.sequence)
        self._seen_sequence_order.append(event.sequence)
        if len(self._seen_sequence_order) > self._max_retained_sequences:
            expired = self._seen_sequence_order.popleft()
            self._seen_sequences.discard(expired)

        if event.kind is CallerTurnEventKind.RECONNECT_STARTED:
            if self._has_pending:
                emitted.extend(
                    self._finalize(
                        at_ms=event.at_ms,
                        status=CallerTurnCompletionStatus.PARTIAL,
                        reason=CallerTurnCloseReason.RECONNECT_STARTED,
                    )
                )
            return tuple(emitted)

        self._record_event(event)
        if self._event_count > self._max_events:
            emitted.extend(self._drop_for_resource_limit(event.at_ms))
            return tuple(emitted)

        if event.kind is CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT:
            self._fragments.append(event.text)
            self._last_transcript_at_ms = event.at_ms
            if self._transcript_exceeds_limit():
                emitted.extend(self._drop_for_resource_limit(event.at_ms))
            elif self._terminal_at_ms is not None:
                self._deadline_ms = event.at_ms + self._quiescence_ms
            return tuple(emitted)

        reason = _TERMINAL_REASONS.get(event.kind)
        if reason is not None:
            self._terminal_at_ms = event.at_ms
            self._close_reason = reason
            self._deadline_ms = event.at_ms + self._quiescence_ms
            return tuple(emitted)

        immediate_reason = _IMMEDIATE_REASONS.get(event.kind)
        if immediate_reason is not None:
            status = (
                CallerTurnCompletionStatus.CANCELLED
                if event.kind is CallerTurnEventKind.TOOL_CALL_CANCELLED
                else CallerTurnCompletionStatus.PARTIAL
            )
            emitted.extend(
                self._finalize(
                    at_ms=event.at_ms,
                    status=status,
                    reason=immediate_reason,
                )
            )
        return tuple(emitted)

    def advance_time(self, at_ms: int) -> tuple[RetrospectiveCallerTurn, ...]:
        at_ms = _bounded_nonnegative_int(at_ms, name="at_ms")
        if at_ms < self._last_at_ms:
            raise ValueError("time must be monotonic")
        self._last_at_ms = at_ms
        return self._finalize_expired(at_ms)

    def finish(
        self,
        *,
        at_ms: int,
        reason: CallerTurnCloseReason,
    ) -> tuple[RetrospectiveCallerTurn, ...]:
        at_ms = _bounded_nonnegative_int(at_ms, name="at_ms")
        if at_ms < self._last_at_ms:
            raise ValueError("time must be monotonic")
        if not isinstance(reason, CallerTurnCloseReason):
            raise TypeError("reason must be a caller turn close reason")
        self._last_at_ms = at_ms
        status = (
            CallerTurnCompletionStatus.CANCELLED
            if reason is CallerTurnCloseReason.TOOL_CALL_CANCELLED
            else CallerTurnCompletionStatus.PARTIAL
        )
        return self._finalize(at_ms=at_ms, status=status, reason=reason)

    def _record_event(self, event: CallerTurnEvent) -> None:
        if self._first_event_at_ms is None:
            self._first_event_at_ms = event.at_ms
        self._event_count += 1

    @property
    def _has_pending(self) -> bool:
        return self._event_count > 0 or bool(self._fragments)

    def _transcript_exceeds_limit(self) -> bool:
        transcript = "".join(self._fragments)
        return len(transcript) > self._max_codepoints or len(transcript.encode("utf-8")) > (
            self._max_utf8_bytes
        )

    def _drop_for_resource_limit(self, at_ms: int) -> tuple[RetrospectiveCallerTurn, ...]:
        return self._finalize(
            at_ms=at_ms,
            status=CallerTurnCompletionStatus.DROPPED,
            reason=CallerTurnCloseReason.RESOURCE_LIMIT,
            retain_transcript=False,
        )

    def _finalize_expired(
        self,
        observed_at_ms: int,
    ) -> tuple[RetrospectiveCallerTurn, ...]:
        if self._deadline_ms is None or observed_at_ms < self._deadline_ms:
            return ()
        deadline_ms = self._deadline_ms
        reason = self._close_reason or CallerTurnCloseReason.TURN_COMPLETE
        return self._finalize(
            at_ms=deadline_ms,
            status=CallerTurnCompletionStatus.RETROSPECTIVE_COMPLETE,
            reason=reason,
        )

    def _finalize(
        self,
        *,
        at_ms: int,
        status: CallerTurnCompletionStatus,
        reason: CallerTurnCloseReason,
        retain_transcript: bool = True,
    ) -> tuple[RetrospectiveCallerTurn, ...]:
        transcript = unicodedata.normalize("NFC", "".join(self._fragments)).strip()
        if not transcript and status is not CallerTurnCompletionStatus.DROPPED:
            self._reset_pending()
            return ()
        if not self._has_pending:
            return ()

        first_event_at_ms = self._first_event_at_ms if self._first_event_at_ms is not None else at_ms
        terminal_at_ms = self._terminal_at_ms if self._terminal_at_ms is not None else at_ms
        last_transcript_at_ms = (
            self._last_transcript_at_ms
            if self._last_transcript_at_ms is not None
            else first_event_at_ms
        )
        turn = RetrospectiveCallerTurn(
            schema_version=CALLER_TURN_SCHEMA_VERSION,
            epoch=self._active_epoch,
            turn_id=self._next_turn_id,
            transcript=transcript if retain_transcript else "",
            status=status,
            close_reason=reason,
            first_event_at_ms=first_event_at_ms,
            last_transcript_at_ms=last_transcript_at_ms,
            terminal_at_ms=terminal_at_ms,
            finalized_at_ms=at_ms,
            event_count=self._event_count,
        )
        self._next_turn_id += 1
        self._reset_pending()
        return (turn,)

    def _reset_pending(self) -> None:
        self._fragments: list[str] = []
        self._event_count = 0
        self._first_event_at_ms: int | None = None
        self._last_transcript_at_ms: int | None = None
        self._terminal_at_ms: int | None = None
        self._close_reason: CallerTurnCloseReason | None = None
        self._deadline_ms: int | None = None
