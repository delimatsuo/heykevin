"""Tests for payload-safe voice bakeoff telemetry."""

import pytest

from app.services.voice_telemetry import (
    VoiceBakeoffArm,
    VoiceTelemetryError,
    VoiceTelemetryEvent,
    VoiceTelemetryProjector,
)


def test_projection_uses_hmac_pseudonym_and_allowlisted_values_only():
    projector = VoiceTelemetryProjector(hmac_key=b"test-key", environment="bakeoff")

    projected = projector.project(
        session_binding="opaque-session",
        candidate_arm=VoiceBakeoffArm.B1,
        event=VoiceTelemetryEvent.GENERATION_COMPLETE,
        ordinal=2,
        duration_ms=123,
        success=True,
        error_class=VoiceTelemetryError.NONE,
    )

    assert projected["session"] != "opaque-session"
    assert projected == {
        "schema_version": 1,
        "environment": "bakeoff",
        "session": projected["session"],
        "candidate_arm": "B1",
        "event": "generation_complete",
        "ordinal": 2,
        "duration_ms": 123,
        "success": True,
        "error_class": "none",
    }


def test_projection_rejects_raw_or_unknown_fields():
    projector = VoiceTelemetryProjector(hmac_key=b"test-key", environment="bakeoff")

    with pytest.raises(ValueError, match="unknown telemetry field"):
        projector.project(
            session_binding="opaque-session",
            candidate_arm=VoiceBakeoffArm.B1,
            event=VoiceTelemetryEvent.GENERATION_COMPLETE,
            transcript="caller wording",
        )


def test_projection_rejects_phone_and_identifier_shaped_values():
    projector = VoiceTelemetryProjector(hmac_key=b"test-key", environment="bakeoff")

    with pytest.raises(ValueError, match="mapped error class"):
        projector.project(
            session_binding="opaque-session",
            candidate_arm=VoiceBakeoffArm.B1,
            event=VoiceTelemetryEvent.GENERATION_COMPLETE,
            error_class="CA12345678901234567890123456789012",
        )
