"""Full model-facing schemas and transport-specific declaration behavior."""

import json
import logging
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.config import settings, staging_native_live_safety_controls_enabled
from app.services import relay_pipeline
from app.services.gemini_pipeline import GeminiPipeline
from app.services.relay_pipeline import RelayPipeline
from app.services.voice_pipeline import VoicePipeline

CALENDAR = {
    "google_calendar_access_token": "synthetic-calendar",
    "google_calendar_refresh_token": "synthetic-refresh",
}
JOBBER = {"jobber_access_token": "synthetic-jobber", "jobber_refresh_token": "synthetic-refresh"}
REQUESTS = {
    "service_request_context": {"customer_key": "test", "open_service_requests": [{"request_id": "r1"}]},
    "service_request_mutations_enabled": True,
    "integration_write_status": "approved",
}
CALENDAR_NAMES = ("check_availability", "book_appointment")
REQUEST_NAMES = ("cancel_service_request", "reschedule_service_request", "add_service_to_request")


@pytest.fixture(scope="module")
def schemas():
    # Snapshot of the declarations before extraction, including transport-specific
    # descriptions, omitted optional keys, and the case used for schema types.
    return json.loads((Path(__file__).parents[1] / "fixtures/voice_tool_declarations.json").read_text())


@pytest.fixture(autouse=True)
def tool_settings(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "service_request_recovery_enabled", True)


async def _noop(*_args):
    pass


def _gemini(config):
    return GeminiPipeline(on_audio_out=_noop, on_transcript=_noop, contractor_config=config)


def _relay(config):
    return RelayPipeline(send_to_twilio=_noop, contractor_config=config, call_sid="CA123456-private")


def _expected(schemas, names):
    return [{"function_declarations": [schemas["gemini"][name] for name in names]}] if names else []


@pytest.mark.parametrize(
    "overrides,names",
    [
        ({}, ()),
        (CALENDAR, CALENDAR_NAMES),
        (CALENDAR | REQUESTS, CALENDAR_NAMES + REQUEST_NAMES),
        (JOBBER | CALENDAR | REQUESTS, ("check_customer",) + REQUEST_NAMES),
        (CALENDAR | {"mode": "business", "subscription_tier": "personal"}, ()),
        (CALENDAR | {"mode": "personal", "effective_mode": "business"}, CALENDAR_NAMES),
        (CALENDAR | REQUESTS | {"service_request_mutations_enabled": 1}, CALENDAR_NAMES),
        (CALENDAR | REQUESTS | {"integration_write_status": "revoked"}, CALENDAR_NAMES),
        (CALENDAR | REQUESTS | {"google_calendar_connected": False}, ()),
    ],
)
def test_transports_advertise_exact_declarations(overrides, names, schemas):
    config = {"contractor_id": "synthetic"} | overrides
    expected = _expected(schemas, names)

    assert _gemini(config)._build_gemini_tools() == expected
    assert _relay(config)._tools == expected


def test_legacy_class_preserves_anthropic_declarations(schemas):
    assert VoicePipeline.JOBBER_TOOLS == schemas["anthropic_jobber"]
    assert VoicePipeline.CALENDAR_TOOLS == schemas["anthropic_calendar"]


def test_nested_parameters_are_fresh_for_each_build(schemas):
    config = {"contractor_id": "synthetic"} | CALENDAR | REQUESTS
    relay = _relay(config)
    declarations = relay._tools[0]["function_declarations"]
    declarations[0]["parameters"]["properties"].clear()
    declarations[-1]["parameters"]["required"].clear()

    expected = _expected(schemas, CALENDAR_NAMES + REQUEST_NAMES)
    assert relay._build_tools() == expected
    assert _gemini(config)._build_gemini_tools() == expected


@pytest.mark.parametrize("mode", ["personal", "business"])
def test_staging_denial_keeps_transport_logging_and_gate_order(mode, monkeypatch, caplog):
    monkeypatch.setattr(settings, "environment", " Staging ")
    relay_stage = Mock(wraps=staging_native_live_safety_controls_enabled)
    monkeypatch.setattr(relay_pipeline, "staging_native_live_safety_controls_enabled", relay_stage)
    caplog.set_level(logging.INFO)
    config = {"contractor_id": "synthetic", "effective_mode": mode} | CALENDAR

    assert _relay(config)._tools == []
    assert relay_stage.call_count == (0 if mode == "personal" else 1)
    assert "live_tools_disabled" not in caplog.text
    assert _gemini(config)._build_gemini_tools() == []
    assert "voice_timing event=live_tools_disabled" in caplog.text
    assert "reason=staging_native_safety" in caplog.text


def test_declaration_error_propagates_from_gemini_and_is_sanitized_by_relay(monkeypatch, caplog):
    config = {"contractor_id": "synthetic"} | CALENDAR
    gemini, relay = _gemini(config), _relay(config)

    def failed_entitlement(*_args):
        raise ValueError("synthetic private detail")

    monkeypatch.setattr("app.services.entitlements.has_entitled_token", failed_entitlement)
    with pytest.raises(ValueError, match="synthetic private detail"):
        gemini._build_gemini_tools()
    assert relay._build_tools() == []
    assert "relay_event event=tool_build_error call=CA123456 type=ValueError" in caplog.text
    assert "synthetic private detail" not in caplog.text
    assert "CA123456-private" not in caplog.text
