"""Privacy-preserving event formatting shared by voice engines and tools."""

import logging
import re

from app.utils.logging import get_logger

# Keep the original logger name for existing voice-event filters and alerts.
logger = get_logger("app.services.voice_pipeline")

_SAFE_LOG_METRIC_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")
_KNOWN_TOOL_NAMES = {
    "add_service_to_request",
    "book_appointment",
    "cancel_service_request",
    "check_availability",
    "check_customer",
    "reschedule_service_request",
}


def _call_label(call_sid: str) -> str:
    return call_sid[:8] or "unknown"


def _safe_log_metric(value: object) -> str:
    if isinstance(value, (bool, int, float)):
        return str(value)
    sanitized = _SAFE_LOG_METRIC_PATTERN.sub("_", str(value or "")[:40]).strip("_.:-")
    return sanitized or "unknown"


def _tool_label(tool_name: str) -> str:
    return tool_name if tool_name in _KNOWN_TOOL_NAMES else "unknown"


def _log_voice_event(
    event: str,
    call_sid: str = "",
    *,
    level: int = logging.INFO,
    **metrics: object,
) -> None:
    metric_text = " ".join(
        f"{key}={_safe_log_metric(value)}" for key, value in metrics.items()
    )
    suffix = f" {metric_text}" if metric_text else ""
    logger.log(level, "voice_event event=%s call=%s%s", event, _call_label(call_sid), suffix)


def _log_voice_exception(event: str, error: BaseException, call_sid: str = "") -> None:
    _log_voice_event(
        event,
        call_sid,
        level=logging.WARNING,
        exception_type=type(error).__name__,
    )


def _log_tool_execution_failure(tool_name: str, call_sid: str, exc: Exception) -> None:
    _log_voice_event(
        "tool_execution_error",
        call_sid,
        level=logging.ERROR,
        tool=_tool_label(tool_name),
        exception_type=type(exc).__name__,
    )
