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
        for key in (
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
        ):
            if hasattr(record, key):
                value = getattr(record, key)
                if key == "caller_phone" and isinstance(value, str):
                    value = redact_phone(value)
                log_entry[key] = value
        return json.dumps(log_entry)


_OAUTH_CALLBACK_PATHS = (
    "/api/integrations/jobber/callback",
    "/api/integrations/google-calendar/callback",
)
_UVICORN_ACCESS_MSG_FORMAT = '%s - "%s %s HTTP/%s" %d'


class OAuthCallbackAccessLogFilter(logging.Filter):
    """Filter for uvicorn.access logger to redact query parameters from OAuth callbacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            getattr(record, "exc_info", None)
            or getattr(record, "exc_text", None)
            or getattr(record, "stack_info", None)
        ):
            return False

        if getattr(record, "msg", None) != _UVICORN_ACCESS_MSG_FORMAT:
            return False

        args = getattr(record, "args", None)
        if not isinstance(args, tuple) or len(args) != 5:
            return False

        client, method, target, http_version, status = args
        if (
            not isinstance(client, str)
            or not isinstance(method, str)
            or not isinstance(target, str)
            or not isinstance(http_version, str)
            or not isinstance(status, int)
            or isinstance(status, bool)
        ):
            return False

        path = target.split("?", 1)[0]
        if path.rstrip("/") in _OAUTH_CALLBACK_PATHS:
            if target != path:
                record.args = (client, method, path, http_version, status)

        return True


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
    # httpx logs every request at INFO as 'HTTP Request: GET <full URL>' —
    # outbound URLs can carry PII and API keys in query params (e.g. the
    # Geocoding call). Same for httpcore's connection logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # twilio-python (twilio/http/__init__.py, log_request/log_response) logs
    # every REST call at INFO: the request line with its query string, the
    # query params, and all non-authorization headers. Lookup and list calls
    # put caller phone numbers in that URL, so keep the SDK at WARNING.
    for name in ("twilio", "twilio.http_client", "twilio.async_http_client"):
        logging.getLogger(name).setLevel(logging.WARNING)

    uvicorn_access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, OAuthCallbackAccessLogFilter) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(OAuthCallbackAccessLogFilter())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
