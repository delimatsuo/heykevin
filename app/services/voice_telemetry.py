"""Immutable allowlist projection for offline voice bakeoff telemetry."""

import hashlib
import hmac
from enum import Enum
from typing import Any


_SCHEMA_VERSION = 1
class VoiceBakeoffArm(str, Enum):
    A = "A"
    B1 = "B1"
    B2 = "B2"
    C = "C"


class VoiceTelemetryEvent(str, Enum):
    RESPONSE_AUTHORIZED = "response_authorized"
    GENERATION_COMPLETE = "generation_complete"
    TRANSPORT_RESOLVED = "transport_resolved"
    CALLER_PLAYBACK_OBSERVED = "caller_playback_observed"


class VoiceTelemetryError(str, Enum):
    NONE = "none"
    PROVIDER_FAILURE = "provider_failure"
    TRANSPORT_FAILURE = "transport_failure"
    AUTH_REJECTED = "auth_rejected"


class VoiceTelemetryProjector:
    """Project bounded operational facts without retaining raw voice payloads."""

    def __init__(self, *, hmac_key: bytes, environment: str) -> None:
        if not isinstance(hmac_key, bytes) or not hmac_key:
            raise ValueError("hmac_key is required")
        self._hmac_key = hmac_key
        self._environment = self._safe_token(environment)

    def project(
        self,
        *,
        session_binding: str,
        candidate_arm: VoiceBakeoffArm,
        event: VoiceTelemetryEvent,
        **metrics: Any,
    ) -> dict[str, object]:
        allowed = {"ordinal", "duration_ms", "success", "error_class"}
        unknown = set(metrics) - allowed
        if unknown:
            raise ValueError("unknown telemetry field")
        if not isinstance(session_binding, str) or not session_binding:
            raise ValueError("session binding is required")
        if not isinstance(candidate_arm, VoiceBakeoffArm):
            raise ValueError("candidate arm is invalid")
        projected: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "environment": self._environment,
            "session": hmac.new(
                self._hmac_key, session_binding.encode("utf-8"), hashlib.sha256
            ).hexdigest()[:24],
            "candidate_arm": candidate_arm.value,
            "event": event.value if isinstance(event, VoiceTelemetryEvent) else self._reject_event(),
        }
        if "ordinal" in metrics:
            projected["ordinal"] = self._nonnegative(metrics["ordinal"], "ordinal")
        if "duration_ms" in metrics:
            projected["duration_ms"] = self._nonnegative(metrics["duration_ms"], "duration_ms")
        if "success" in metrics:
            if not isinstance(metrics["success"], bool):
                raise ValueError("success must be a boolean")
            projected["success"] = metrics["success"]
        if "error_class" in metrics:
            if not isinstance(metrics["error_class"], VoiceTelemetryError):
                raise ValueError("mapped error class is required")
            projected["error_class"] = metrics["error_class"].value
        return projected

    @staticmethod
    def _nonnegative(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be nonnegative")
        return value

    @staticmethod
    def _safe_token(value: object) -> str:
        if not isinstance(value, str) or value not in {"bakeoff"}:
            raise ValueError("safe token is required")
        return value

    @staticmethod
    def _reject_event() -> str:
        raise ValueError("telemetry event enum is required")
