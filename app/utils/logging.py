"""Structured JSON logging with call_sid correlation."""

import logging
import json
import sys
from contextvars import ContextVar

# Context variable for per-request call_sid correlation
call_sid_var: ContextVar[str] = ContextVar("call_sid", default="")


TRACE_FIELD_NAMES = {
    "event",
    "call_sid",
    "caller_phone",
    "trust_score",
    "route",
    "action",
    "duration_ms",
    "contractor_id",
    "source",
    "resource_id",
    "allowed",
    "reason",
    "turn_id",
    "stage",
    "status",
    "voice_engine",
    "provider",
    "attempt",
    "http_status",
    "bytes_total",
    "bytes_sent",
    "audio_seconds",
    "utterance_chars",
    "word_count",
    "transcript_chars",
    "first_audio_ms",
    "stream_sid",
}

_DISALLOWED_TRACE_FIELDS = {"raw_text", "transcript", "prompt", "messages", "request_body", "response_body"}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        call_sid = call_sid_var.get("")
        if call_sid:
            log_entry["call_sid"] = call_sid
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Include any extra fields
        for key in TRACE_FIELD_NAMES:
            if hasattr(record, key):
                value = getattr(record, key)
                if key == "caller_phone" and isinstance(value, str):
                    value = redact_phone(value)
                log_entry[key] = value
        return json.dumps(log_entry)


def redact_phone(phone: str) -> str:
    """Redact phone number for logging, keeping last 4 digits."""
    if not phone or len(phone) < 4:
        return "[REDACTED]"
    return f"***{phone[-4:]}"


def setup_logging(level: str = "INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def trace_event(logger: logging.Logger, event: str, **fields):
    """Emit a structured, privacy-safe call trace event.

    Trace events intentionally allow only bounded metadata fields. Do not pass
    transcript text, prompts, request bodies, or response bodies here.
    """
    safe_fields = {"event": str(event)}
    for key, value in fields.items():
        if key in _DISALLOWED_TRACE_FIELDS or key not in TRACE_FIELD_NAMES:
            continue
        safe_fields[key] = value
    logger.info("call_trace", extra=safe_fields)
