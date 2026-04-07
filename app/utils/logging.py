"""Structured JSON logging with call_sid correlation."""

import logging
import json
import sys
from contextvars import ContextVar

# Context variable for per-request call_sid correlation
call_sid_var: ContextVar[str] = ContextVar("call_sid", default="")


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
        for key in ("caller_phone", "trust_score", "route", "action", "duration_ms"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        return json.dumps(log_entry)


def setup_logging(level: str = "INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
