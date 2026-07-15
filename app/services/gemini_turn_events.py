"""Bounded, payload-safe Gemini Live caller-turn event decoding."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import json
from typing import Any

from app.services.caller_turns import CallerTurnEvent, CallerTurnEventKind


GEMINI_TURN_EVENT_SCHEMA_VERSION = 1
GEMINI_RAW_MESSAGE_MAX_BYTES = 64 * 1024
_LOCAL_LIFECYCLE_KINDS = {
    CallerTurnEventKind.CONNECTION_CLOSED,
    CallerTurnEventKind.RECONNECT_STARTED,
    CallerTurnEventKind.PIPELINE_STOPPED,
}


class GeminiTurnEventDecodeStatus(str, Enum):
    DECODED = "decoded"
    IGNORED = "ignored"
    REJECTED = "rejected"


class GeminiTurnEventRejectionCode(str, Enum):
    MALFORMED_MESSAGE = "malformed_message"
    MESSAGE_TOO_LARGE = "message_too_large"
    INVALID_TRANSCRIPT_FRAGMENT = "invalid_transcript_fragment"


@dataclass(frozen=True, slots=True)
class GeminiTurnEventBatch:
    events: tuple[CallerTurnEvent, ...]
    status: GeminiTurnEventDecodeStatus
    rejection_code: GeminiTurnEventRejectionCode | None = None

    def redacted_report_dict(self) -> dict[str, Any]:
        event_types = Counter(event.kind.value for event in self.events)
        return {
            "schema_version": GEMINI_TURN_EVENT_SCHEMA_VERSION,
            "status": self.status.value,
            "rejection_code": (
                self.rejection_code.value if self.rejection_code is not None else None
            ),
            "event_count": len(self.events),
            "event_types": dict(sorted(event_types.items())),
        }


class GeminiTurnEventAdapter:
    """Decode supported Gemini raw-message shapes without retaining raw payloads."""

    def adapt_message(
        self,
        message: object,
        *,
        at_ms: int,
        first_sequence: int,
        epoch: int,
    ) -> GeminiTurnEventBatch:
        self._validate_local_metadata(
            at_ms=at_ms,
            first_sequence=first_sequence,
            epoch=epoch,
        )
        if not isinstance(message, dict):
            return self._rejected(GeminiTurnEventRejectionCode.MALFORMED_MESSAGE)
        try:
            encoded = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            return self._rejected(GeminiTurnEventRejectionCode.MALFORMED_MESSAGE)
        if len(encoded) > GEMINI_RAW_MESSAGE_MAX_BYTES:
            return self._rejected(GeminiTurnEventRejectionCode.MESSAGE_TOO_LARGE)

        try:
            decoded = self._decode_message(message)
        except (TypeError, ValueError):
            return self._rejected(GeminiTurnEventRejectionCode.MALFORMED_MESSAGE)

        events: list[CallerTurnEvent] = []
        for offset, (kind, text) in enumerate(decoded):
            try:
                events.append(
                    CallerTurnEvent(
                        kind=kind,
                        at_ms=at_ms,
                        sequence=first_sequence + offset,
                        epoch=epoch,
                        text=text,
                    )
                )
            except (TypeError, ValueError):
                return self._rejected(
                    GeminiTurnEventRejectionCode.INVALID_TRANSCRIPT_FRAGMENT
                )
        if not events:
            return GeminiTurnEventBatch(
                events=(),
                status=GeminiTurnEventDecodeStatus.IGNORED,
            )
        return GeminiTurnEventBatch(
            events=tuple(events),
            status=GeminiTurnEventDecodeStatus.DECODED,
        )

    def adapt_lifecycle(
        self,
        kind: CallerTurnEventKind,
        *,
        at_ms: int,
        sequence: int,
        epoch: int,
    ) -> CallerTurnEvent:
        if kind not in _LOCAL_LIFECYCLE_KINDS:
            raise ValueError("unsupported local lifecycle event kind")
        self._validate_local_metadata(
            at_ms=at_ms,
            first_sequence=sequence,
            epoch=epoch,
        )
        return CallerTurnEvent(
            kind=kind,
            at_ms=at_ms,
            sequence=sequence,
            epoch=epoch,
        )

    @staticmethod
    def _validate_local_metadata(
        *,
        at_ms: int,
        first_sequence: int,
        epoch: int,
    ) -> None:
        for name, value in (
            ("at_ms", at_ms),
            ("first_sequence", first_sequence),
            ("epoch", epoch),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")

    @staticmethod
    def _decode_message(
        message: dict[str, Any],
    ) -> tuple[tuple[CallerTurnEventKind, str], ...]:
        decoded: list[tuple[CallerTurnEventKind, str]] = []
        if "serverContent" in message:
            content = message["serverContent"]
            if not isinstance(content, dict):
                raise TypeError("serverContent must be an object")
            transcript = _decode_input_transcript(content)
            if transcript:
                decoded.append(
                    (CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, transcript)
                )
            if "modelTurn" in content:
                model_turn = content["modelTurn"]
                if not isinstance(model_turn, dict):
                    raise TypeError("modelTurn must be an object")
                parts = model_turn.get("parts", [])
                if not isinstance(parts, list):
                    raise TypeError("modelTurn parts must be an array")
                if parts:
                    decoded.append((CallerTurnEventKind.MODEL_OUTPUT_STARTED, ""))
            for field, kind in (
                ("generationComplete", CallerTurnEventKind.GENERATION_COMPLETE),
                ("turnComplete", CallerTurnEventKind.TURN_COMPLETE),
                ("interrupted", CallerTurnEventKind.INTERRUPTED),
            ):
                if field not in content:
                    continue
                value = content[field]
                if not isinstance(value, bool):
                    raise TypeError(f"{field} must be a boolean")
                if value:
                    decoded.append((kind, ""))

        if "toolCall" in message:
            tool_call = message["toolCall"]
            if not isinstance(tool_call, dict):
                raise TypeError("toolCall must be an object")
            function_calls = tool_call.get("functionCalls", [])
            if not isinstance(function_calls, list):
                raise TypeError("functionCalls must be an array")
            if function_calls:
                decoded.append((CallerTurnEventKind.TOOL_CALL_STARTED, ""))

        if "toolCallCancellation" in message:
            cancellation = message["toolCallCancellation"]
            if not isinstance(cancellation, dict):
                raise TypeError("toolCallCancellation must be an object")
            ids = cancellation.get("ids", [])
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                raise TypeError("tool cancellation ids must be strings")
            if ids:
                decoded.append((CallerTurnEventKind.TOOL_CALL_CANCELLED, ""))
        return tuple(decoded)

    @staticmethod
    def _rejected(code: GeminiTurnEventRejectionCode) -> GeminiTurnEventBatch:
        return GeminiTurnEventBatch(
            events=(),
            status=GeminiTurnEventDecodeStatus.REJECTED,
            rejection_code=code,
        )


def _decode_input_transcript(content: dict[str, Any]) -> str:
    compatibility = content.get("inputTranscript")
    if compatibility is not None and not isinstance(compatibility, str):
        raise TypeError("inputTranscript must be a string")
    if compatibility:
        return compatibility

    transcription = content.get("inputTranscription")
    if transcription is None:
        return ""
    if isinstance(transcription, str):
        return transcription
    if not isinstance(transcription, dict):
        raise TypeError("inputTranscription must be a string or object")
    text = transcription.get("text", "")
    if not isinstance(text, str):
        raise TypeError("inputTranscription text must be a string")
    return text
