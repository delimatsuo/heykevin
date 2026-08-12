"""Public demo ingress must remain isolated from ordinary tenant side effects."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app import config as app_config
from app.db.contractors import PROTECTED_FIELDS
from app.services.gemini_pipeline import GeminiPipeline
from app.services.public_demo import build_public_demo_profile, build_public_demo_system_prompt
from app.services.public_demo_pipeline import (
    PUBLIC_DEMO_FRIENDLY_MALE_VOICE,
    PUBLIC_DEMO_GEMINI_MODEL,
    PublicDemoGeminiPipeline,
)
from app.webhooks import public_demo


class _Request:
    def __init__(self, values: dict):
        self._values = values

    async def form(self):
        return dict(self._values)


class _WebSocket:
    def __init__(self, start_message: dict):
        self._start_message = start_message
        self.accepted = False
        self.closed = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return json.dumps(self._start_message)

    async def iter_text(self):
        if False:
            yield ""

    async def close(self, code=None):
        self.closed.append(code)


def _configure(monkeypatch):
    monkeypatch.setattr(public_demo.settings, "public_demo_enabled", True)
    monkeypatch.setattr(public_demo.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(
        public_demo.settings,
        "public_demo_hmac_secret",
        "public-demo-test-secret-that-is-long-enough",
    )
    monkeypatch.setattr(public_demo.settings, "public_demo_per_caller_limit", 3)
    monkeypatch.setattr(public_demo.settings, "public_demo_per_caller_window_seconds", 3600)
    monkeypatch.setattr(public_demo.settings, "public_demo_daily_call_limit", 100)
    monkeypatch.setattr(public_demo.settings, "public_demo_concurrency_limit", 2)
    monkeypatch.setattr(public_demo.settings, "public_demo_max_call_duration_seconds", 180)
    monkeypatch.setattr(public_demo.settings, "public_demo_lease_ttl_seconds", 300)
    monkeypatch.setattr(public_demo.settings, "twilio_account_sid", "AC" + "1" * 32)
    monkeypatch.setattr(
        public_demo.settings,
        "public_demo_twilio_usage_trigger_sid",
        "UT" + "2" * 32,
    )
    monkeypatch.setattr(
        public_demo.settings,
        "public_demo_twilio_daily_spend_limit_usd",
        5.0,
    )
    monkeypatch.setattr(public_demo.settings, "cloud_run_url", "https://demo.example.test")


def _incoming(**overrides):
    values = {
        "CallSid": "CA11111111111111111111111111111111",
        "From": "+12025550147",
        "To": "+12025550199",
    }
    values.update(overrides)
    return _Request(values)


def _fresh_usage_trigger_date() -> str:
    return datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _stale_usage_trigger_date() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (-60, True),
        (-60.001, False),
        (899.999, True),
        (900, False),
    ],
)
def test_usage_callback_freshness_boundaries(age, expected):
    assert public_demo._public_demo_usage_callback_age_is_fresh(age) is expected


@pytest.mark.asyncio
async def test_disabled_demo_fails_closed_before_rate_limits(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(public_demo.settings, "public_demo_enabled", False)

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled demo must not touch admission storage")

    monkeypatch.setattr(public_demo, "check_and_increment", unexpected)
    monkeypatch.setattr(public_demo, "acquire_public_demo_lease", unexpected)

    response = await public_demo.handle_public_demo_incoming(_incoming(), None)
    body = response.body.decode()

    assert "demo is unavailable" in body
    assert "<Hangup" in body
    assert "<Dial" not in body


@pytest.mark.asyncio
async def test_wrong_number_fails_closed_without_forwarding(monkeypatch):
    _configure(monkeypatch)

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("misbound To number must not enter admission")

    monkeypatch.setattr(public_demo, "check_and_increment", unexpected)

    response = await public_demo.handle_public_demo_incoming(
        _incoming(To="+12025550000"),
        None,
    )
    body = response.body.decode()

    assert "demo is unavailable" in body
    assert "<Dial" not in body


@pytest.mark.asyncio
async def test_admitted_call_connects_directly_to_native_demo_voice_and_never_embeds_caller_phone(monkeypatch):
    _configure(monkeypatch)
    rate_keys = []
    rate_ttls = []

    async def allow_rate(**kwargs):
        rate_keys.append(kwargs["key"])
        rate_ttls.append(kwargs["document_ttl_seconds"])
        return SimpleNamespace(allowed=True)

    async def allow_lease(*_args, **_kwargs):
        return True

    monkeypatch.setattr(public_demo, "check_and_increment", allow_rate)
    monkeypatch.setattr(public_demo, "acquire_public_demo_lease", allow_lease)
    monkeypatch.setattr(
        public_demo,
        "sign_public_demo_stream_token",
        lambda *_args, **_kwargs: "signed-demo-token",
    )

    response = await public_demo.handle_public_demo_incoming(_incoming(), None)
    body = response.body.decode()

    assert "<Say" not in body
    assert "Polly.Matthew" not in body
    assert 'url="wss://demo.example.test/public-demo-stream"' in body
    assert "signed-demo-token" in body
    assert "CA11111111111111111111111111111111" not in body
    assert "+12025550147" not in body
    assert rate_keys[1] == "all"
    assert rate_keys[0] != "+12025550147"
    assert "12025550147" not in rate_keys[0]
    assert rate_ttls == [3600, 86_400]


def test_demo_pipeline_uses_friendly_male_voice_and_conversational_greeting(monkeypatch):
    def fake_base_init(self, **_kwargs):
        self._voice = "Puck"

    monkeypatch.setattr(PublicDemoGeminiPipeline.__mro__[1], "__init__", fake_base_init)
    pipeline = PublicDemoGeminiPipeline()

    greeting = pipeline._build_greeting_text()

    assert PUBLIC_DEMO_FRIENDLY_MALE_VOICE == "Achird"
    assert PUBLIC_DEMO_GEMINI_MODEL == "gemini-3.1-flash-live-preview"
    assert pipeline._model == PUBLIC_DEMO_GEMINI_MODEL
    assert pipeline._voice == "Achird"
    assert "AI receptionist" in greeting
    assert "fictional" not in greeting.lower()
    assert "services" in greeting
    assert "booking a visit" in greeting


def test_demo_pipeline_uses_high_speech_detection_with_noise_padding():
    pipeline = PublicDemoGeminiPipeline.__new__(PublicDemoGeminiPipeline)

    config = pipeline._build_realtime_input_config()
    activity = config["automatic_activity_detection"]

    assert activity["start_of_speech_sensitivity"] == "START_SENSITIVITY_HIGH"
    assert activity["prefix_padding_ms"] >= 300
    assert activity["end_of_speech_sensitivity"] == "END_SENSITIVITY_HIGH"
    assert config["activity_handling"] == "START_OF_ACTIVITY_INTERRUPTS"
    assert config["turn_coverage"] == "TURN_INCLUDES_ONLY_ACTIVITY"
    assert pipeline.CALLER_SILENCE_PROMPT_SECONDS == 20
    assert pipeline.PACE_AUDIO_OUTPUT is False
    assert pipeline.AUDIO_START_BUFFER_SECONDS == 0.8


def test_demo_pipeline_uses_realtime_text_instructions_for_gemini_3():
    pipeline = PublicDemoGeminiPipeline.__new__(PublicDemoGeminiPipeline)
    pipeline._model = PUBLIC_DEMO_GEMINI_MODEL

    assert pipeline._build_text_instruction_payload("Answer briefly.") == {
        "realtime_input": {"text": "Answer briefly."}
    }


@pytest.mark.asyncio
async def test_demo_pipeline_reframes_tiny_gemini_chunks_before_twilio(monkeypatch):
    converted_frames = []

    async def capture_frame(_self, frame: bytes):
        converted_frames.append(frame)

    monkeypatch.setattr(GeminiPipeline, "_enqueue_model_audio", capture_frame)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = PublicDemoGeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
    )
    pipeline._response_turn_number = 1
    source = bytes(range(256)) * 8
    offsets = (1, 3, 8, 21, 55, 144, 377, 987, len(source))
    start = 0
    for end in offsets:
        await pipeline._enqueue_model_audio(source[start:end])
        start = end

    assert [len(frame) for frame in converted_frames] == [960, 960]
    assert b"".join(converted_frames) == source[:1920]
    assert bytes(pipeline._output_pcm_buffer) == source[1920:]

    # A new response cannot inherit an incomplete PCM sample/frame tail.
    pipeline._response_turn_number = 2
    next_turn = b"\x5a" * pipeline.OUTPUT_PCM_FRAME_BYTES
    await pipeline._enqueue_model_audio(next_turn)

    assert converted_frames[-1] == next_turn
    assert pipeline._output_pcm_buffer == bytearray()


@pytest.mark.asyncio
async def test_demo_pipeline_emits_canonical_twilio_audio_frames(monkeypatch):
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = PublicDemoGeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
    )
    monkeypatch.setattr(pipeline, "_ensure_audio_playout_task", lambda: None)
    pipeline._response_turn_number = 1

    # The provider may split PCM in the middle of a 16-bit sample. Nothing is
    # converted until one complete 20 ms frame has been reconstructed.
    await pipeline._enqueue_model_audio(b"\x00")
    assert pipeline._audio_queue.empty()
    await pipeline._enqueue_model_audio(
        b"\x00" * (pipeline.OUTPUT_PCM_FRAME_BYTES - 1)
    )

    queued = pipeline._audio_queue.get_nowait()
    mulaw_chunk = queued[0]
    pipeline._audio_queue.task_done()

    assert len(mulaw_chunk) == 160
    assert pipeline._queued_audio_bytes == 160


@pytest.mark.asyncio
async def test_rate_limited_call_never_acquires_provider_lease(monkeypatch):
    _configure(monkeypatch)

    async def deny_rate(**_kwargs):
        return SimpleNamespace(allowed=False)

    async def unexpected_lease(*_args, **_kwargs):
        raise AssertionError("rate-limited caller must stop before concurrency admission")

    monkeypatch.setattr(public_demo, "check_and_increment", deny_rate)
    monkeypatch.setattr(public_demo, "acquire_public_demo_lease", unexpected_lease)

    response = await public_demo.handle_public_demo_incoming(_incoming(), None)
    body = response.body.decode()

    assert "demo is busy" in body
    assert "public-demo-stream" not in body
    assert "<Dial" not in body


@pytest.mark.asyncio
async def test_admission_storage_error_fails_closed_before_provider(monkeypatch):
    _configure(monkeypatch)

    async def broken_rate_limit(**_kwargs):
        raise RuntimeError("private backend detail")

    async def unexpected_lease(*_args, **_kwargs):
        raise AssertionError("storage failure must stop before provider admission")

    monkeypatch.setattr(public_demo, "check_and_increment", broken_rate_limit)
    monkeypatch.setattr(public_demo, "acquire_public_demo_lease", unexpected_lease)

    response = await public_demo.handle_public_demo_incoming(_incoming(), None)
    body = response.body.decode()

    assert "demo is unavailable" in body
    assert "<Hangup" in body
    assert "public-demo-stream" not in body
    assert "<Dial" not in body


@pytest.mark.asyncio
async def test_daily_storage_error_releases_acquired_lease(monkeypatch):
    _configure(monkeypatch)
    calls = 0
    released = []

    async def rate_limit(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(allowed=True)
        raise RuntimeError("daily budget unavailable")

    async def allow_lease(*_args, **_kwargs):
        return True

    async def release(call_sid, _secret):
        released.append(call_sid)
        return True

    monkeypatch.setattr(public_demo, "check_and_increment", rate_limit)
    monkeypatch.setattr(public_demo, "acquire_public_demo_lease", allow_lease)
    monkeypatch.setattr(public_demo, "release_public_demo_lease", release)

    response = await public_demo.handle_public_demo_incoming(_incoming(), None)
    body = response.body.decode()

    assert released == ["CA11111111111111111111111111111111"]
    assert "demo is unavailable" in body
    assert "public-demo-stream" not in body


@pytest.mark.asyncio
async def test_wall_clock_cutoff_fires_without_media(monkeypatch):
    sleeps = []
    events = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def on_max_duration():
        events.append("complete")

    class DeadlineSocket:
        async def close(self, code=None):
            events.append(("closed", code))

    monkeypatch.setattr(public_demo.asyncio, "sleep", fake_sleep)
    await public_demo._enforce_public_demo_max_duration(
        started_at=time.monotonic() - 2,
        max_duration_seconds=30,
        websocket=DeadlineSocket(),
        safe_call_label="safe-label",
        on_max_duration=on_max_duration,
    )

    assert 27 <= sleeps[0] <= 28
    assert ("closed", 1000) in events
    assert "complete" in events


@pytest.mark.asyncio
async def test_wall_clock_cutoff_does_not_wait_for_cancellation_hostile_close(monkeypatch):
    release_hanging_close = asyncio.Event()
    completion_fired = asyncio.Event()
    original_sleep = asyncio.sleep

    class CancellationHostileSocket:
        async def close(self, code=None):
            assert code == 1000
            try:
                await release_hanging_close.wait()
            except asyncio.CancelledError:
                await release_hanging_close.wait()

    async def immediate_sleep(_seconds):
        return None

    async def on_max_duration():
        completion_fired.set()

    monkeypatch.setattr(public_demo.asyncio, "sleep", immediate_sleep)
    monkeypatch.setattr(public_demo, "PUBLIC_DEMO_WEBSOCKET_CLOSE_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    await public_demo._enforce_public_demo_max_duration(
        started_at=time.monotonic(),
        max_duration_seconds=30,
        websocket=CancellationHostileSocket(),
        safe_call_label="safe-label",
        on_max_duration=on_max_duration,
    )
    elapsed = time.monotonic() - started

    assert completion_fired.is_set()
    assert elapsed < 0.06
    release_hanging_close.set()
    await original_sleep(0)


@pytest.mark.asyncio
async def test_hanging_twilio_rest_is_bounded_and_stream_always_closes(monkeypatch):
    raw_sid = "CA_PRIVATE_MUST_NOT_REACH_LOGS"
    finished = threading.Event()

    class FakeHttpClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == public_demo.PUBLIC_DEMO_TWILIO_HTTP_TIMEOUT_SECONDS
            assert kwargs["max_retries"] == 0

    class Calls:
        def __call__(self, call_sid):
            assert call_sid == raw_sid
            return self

        def update(self, **_kwargs):
            time.sleep(0.08)
            finished.set()

    class FakeClient:
        def __init__(self, *_args, **kwargs):
            assert isinstance(kwargs["http_client"], FakeHttpClient)
            self.calls = Calls()

    websocket = _WebSocket({})
    monkeypatch.setattr("twilio.http.http_client.TwilioHttpClient", FakeHttpClient)
    monkeypatch.setattr("twilio.rest.Client", FakeClient)
    monkeypatch.setattr(public_demo, "PUBLIC_DEMO_COMPLETION_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    completed = await public_demo._complete_public_demo_call(
        call_sid=raw_sid,
        safe_call_label="safe-hmac-label",
        websocket=websocket,
    )
    elapsed = time.monotonic() - started

    assert completed is False
    assert elapsed < 0.06
    assert websocket.closed == [1000]
    await asyncio.to_thread(finished.wait, 0.2)


def test_sensitive_provider_transport_loggers_are_suppressed(caplog):
    caplog.set_level(logging.DEBUG)
    public_demo.suppress_public_demo_sensitive_transport_logs()

    logging.getLogger("twilio.http_client").info(
        "POST /Accounts/AC_PRIVATE/Calls/CA_PRIVATE.json"
    )
    logging.getLogger("websockets.client").debug(
        "GET /?key=GEMINI_PRIVATE and private audio frame"
    )

    assert "AC_PRIVATE" not in caplog.text
    assert "CA_PRIVATE" not in caplog.text
    assert "GEMINI_PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_stream_extracts_raw_sid_only_in_memory_and_uses_hmac_transport_label(
    monkeypatch,
):
    _configure(monkeypatch)
    monkeypatch.setattr(public_demo.settings, "gemini_api_key", "demo-provider-key")
    raw_sid = "CA22222222222222222222222222222222"
    captured = {}
    released = []

    class _Pipeline:
        def __init__(self, **kwargs):
            captured["pipeline_kwargs"] = kwargs

        async def stop(self):
            return None

    async def serve(_pipeline, _ingress, **kwargs):
        captured["transport_call_sid"] = kwargs["call_sid"]
        return True

    async def release(call_sid, _secret):
        released.append(call_sid)
        return True

    def verify(token, _secret, call_sid, to_number):
        captured["verified"] = (token, call_sid, to_number)
        return True

    async def claim(call_sid, _secret, ttl_seconds):
        captured["claimed"] = (call_sid, ttl_seconds)
        return True

    monkeypatch.setattr(public_demo, "PublicDemoGeminiPipeline", _Pipeline)
    monkeypatch.setattr(public_demo, "_serve_pipeline_ingress", serve)
    monkeypatch.setattr(public_demo, "release_public_demo_lease", release)
    monkeypatch.setattr(public_demo, "verify_public_demo_stream_token", verify)
    monkeypatch.setattr(public_demo, "claim_public_demo_stream", claim)

    websocket = _WebSocket(
        {
            "event": "start",
            "streamSid": "MZ111",
            "start": {
                "callSid": raw_sid,
                "customParameters": {"demo_token": "signed-token"},
            },
        }
    )
    await public_demo.public_demo_stream_ws(websocket)

    assert websocket.accepted is True
    assert captured["verified"] == ("signed-token", raw_sid, "+12025550199")
    assert captured["claimed"] == (raw_sid, 240)
    assert raw_sid not in captured["transport_call_sid"]
    assert len(captured["transport_call_sid"]) == 64
    assert "call_sid" not in captured["pipeline_kwargs"]
    assert "caller_phone" not in captured["pipeline_kwargs"]
    assert "contractor_config" not in captured["pipeline_kwargs"]
    assert released == [raw_sid]


@pytest.mark.asyncio
async def test_invalid_stream_cannot_release_an_authenticated_calls_lease(monkeypatch):
    _configure(monkeypatch)

    monkeypatch.setattr(
        public_demo,
        "verify_public_demo_stream_token",
        lambda *_args, **_kwargs: False,
    )

    async def unexpected_release(*_args, **_kwargs):
        raise AssertionError("unauthenticated CallSid must not mutate lease state")

    monkeypatch.setattr(public_demo, "release_public_demo_lease", unexpected_release)
    websocket = _WebSocket(
        {
            "event": "start",
            "streamSid": "MZ-invalid",
            "start": {
                "callSid": "CA-active-call-sid-known-to-attacker",
                "customParameters": {"demo_token": "invalid"},
            },
        }
    )

    await public_demo.public_demo_stream_ws(websocket)

    assert websocket.closed == [1008]


@pytest.mark.asyncio
async def test_replayed_stream_is_rejected_before_gemini_and_keeps_active_lease(monkeypatch):
    _configure(monkeypatch)
    raw_sid = "CA33333333333333333333333333333333"

    monkeypatch.setattr(
        public_demo,
        "verify_public_demo_stream_token",
        lambda *_args, **_kwargs: True,
    )

    async def replayed(*_args, **_kwargs):
        return False

    async def unexpected_release(*_args, **_kwargs):
        raise AssertionError("a replay must not release the active session's lease")

    class UnexpectedPipeline:
        def __init__(self, **_kwargs):
            raise AssertionError("replayed stream must not start Gemini")

    monkeypatch.setattr(public_demo, "claim_public_demo_stream", replayed)
    monkeypatch.setattr(public_demo, "release_public_demo_lease", unexpected_release)
    monkeypatch.setattr(public_demo, "PublicDemoGeminiPipeline", UnexpectedPipeline)

    websocket = _WebSocket(
        {
            "event": "start",
            "streamSid": "MZ222",
            "start": {
                "callSid": raw_sid,
                "customParameters": {"demo_token": "replayed-token"},
            },
        }
    )
    await public_demo.public_demo_stream_ws(websocket)

    assert websocket.closed == [1008]


@pytest.mark.asyncio
async def test_demo_fallback_releases_lease_and_never_dials_real_user(monkeypatch):
    _configure(monkeypatch)
    private_owner = "+12025550999"
    released = []

    async def release(call_sid, _secret):
        released.append(call_sid)
        return True

    monkeypatch.setattr(public_demo.settings, "user_phone", private_owner)
    monkeypatch.setattr(public_demo, "release_public_demo_lease", release)

    response = await public_demo.handle_public_demo_fallback(_incoming(), None)
    body = response.body.decode()

    assert released == ["CA11111111111111111111111111111111"]
    assert private_owner not in body
    assert "<Dial" not in body
    assert "<Hangup" in body


@pytest.mark.asyncio
async def test_status_callback_only_releases_ephemeral_lease(monkeypatch):
    _configure(monkeypatch)
    released = []

    async def release(call_sid, _secret):
        released.append(call_sid)
        return True

    monkeypatch.setattr(public_demo, "release_public_demo_lease", release)

    result = await public_demo.handle_public_demo_status(
        _Request({
            "CallSid": "CA11111111111111111111111111111111",
            "To": "+12025550199",
            "CallStatus": "completed",
            "From": "+12025550147",
        }),
        None,
    )

    assert result == {"status": "ok"}
    assert released == ["CA11111111111111111111111111111111"]


@pytest.mark.asyncio
async def test_exact_usage_trigger_trips_isolated_twilio_breaker(monkeypatch):
    _configure(monkeypatch)
    tripped = []

    async def trip(**kwargs):
        tripped.append(kwargs)
        return True

    async def claim(*_args, **_kwargs):
        return "new"

    async def complete(*_args, **_kwargs):
        return True

    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", trip)
    monkeypatch.setattr(public_demo, "claim_public_demo_usage_trigger", claim)
    monkeypatch.setattr(public_demo, "complete_public_demo_usage_trigger", complete)
    result = await public_demo.handle_public_demo_usage_limit(
        _Request({
            "AccountSid": "AC" + "1" * 32,
            "UsageTriggerSid": "UT" + "2" * 32,
            "UsageCategory": "totalprice",
            "TriggerBy": "price",
            "Recurring": "daily",
            "TriggerValue": "5.00",
            "CurrentValue": "5.01",
            "IdempotencyToken": "synthetic-test-token",
            "DateFired": _fresh_usage_trigger_date(),
        }),
        None,
    )

    assert result == {"status": "suspended"}
    assert tripped[0]["child_account_sid"] == "AC" + "1" * 32
    assert tripped[0]["usage_trigger_sid"] == "UT" + "2" * 32
    assert tripped[0]["trigger_value"] == 5
    assert tripped[0]["current_value"] == Decimal("5.01")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AccountSid", "AC" + "9" * 32),
        ("UsageTriggerSid", "UT" + "9" * 32),
        ("UsageCategory", "calls"),
        ("TriggerBy", "usage"),
        ("Recurring", "monthly"),
        ("TriggerValue", "6"),
        ("CurrentValue", "4.99"),
        ("DateFired", _stale_usage_trigger_date()),
    ],
)
async def test_misbound_usage_trigger_never_mutates_twilio(monkeypatch, field, value):
    _configure(monkeypatch)

    async def unexpected(**_kwargs):
        raise AssertionError("misbound usage trigger must not mutate Twilio")

    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", unexpected)
    values = {
        "AccountSid": "AC" + "1" * 32,
        "UsageTriggerSid": "UT" + "2" * 32,
        "UsageCategory": "totalprice",
        "TriggerBy": "price",
        "Recurring": "daily",
        "TriggerValue": "5",
        "CurrentValue": "5",
        "IdempotencyToken": "synthetic-bound-token",
        "DateFired": _fresh_usage_trigger_date(),
    }
    values[field] = value
    response = await public_demo.handle_public_demo_usage_limit(_Request(values), None)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_completed_usage_trigger_replay_is_idempotent_after_suspension(monkeypatch):
    _configure(monkeypatch)

    async def claim(*_args, **_kwargs):
        return "completed"

    tripped = []

    async def trip(**_kwargs):
        tripped.append(True)
        return True

    monkeypatch.setattr(public_demo, "claim_public_demo_usage_trigger", claim)
    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", trip)
    result = await public_demo.handle_public_demo_usage_limit(
        _Request({
            "AccountSid": "AC" + "1" * 32,
            "UsageTriggerSid": "UT" + "2" * 32,
            "UsageCategory": "totalprice",
            "TriggerBy": "price",
            "Recurring": "daily",
            "TriggerValue": "5.000000",
            "CurrentValue": "5.010000",
            "IdempotencyToken": "synthetic-replay-token",
            "DateFired": _fresh_usage_trigger_date(),
        }),
        None,
    )

    assert result == {"status": "already_suspended"}
    assert tripped == [True]


@pytest.mark.asyncio
async def test_usage_breaker_failure_returns_retryable_503(monkeypatch):
    _configure(monkeypatch)

    async def claim(*_args, **_kwargs):
        return "pending"

    async def fail_trip(**_kwargs):
        return False

    monkeypatch.setattr(public_demo, "claim_public_demo_usage_trigger", claim)
    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", fail_trip)
    response = await public_demo.handle_public_demo_usage_limit(
        _Request({
            "AccountSid": "AC" + "1" * 32,
            "UsageTriggerSid": "UT" + "2" * 32,
            "UsageCategory": "totalprice",
            "TriggerBy": "price",
            "Recurring": "daily",
            "TriggerValue": "5",
            "CurrentValue": "6",
            "IdempotencyToken": "synthetic-retry-token",
            "DateFired": _fresh_usage_trigger_date(),
        }),
        None,
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_hanging_replay_claim_cannot_delay_provider_suspension(monkeypatch):
    _configure(monkeypatch)
    events = []

    async def trip(**_kwargs):
        events.append("suspended")
        return True

    async def hanging_claim(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("claim-cancelled")
            raise

    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", trip)
    monkeypatch.setattr(public_demo, "claim_public_demo_usage_trigger", hanging_claim)
    monkeypatch.setattr(
        public_demo,
        "PUBLIC_DEMO_USAGE_TRIGGER_CLAIM_TIMEOUT_SECONDS",
        0.05,
    )
    started = time.monotonic()
    response = await public_demo.handle_public_demo_usage_limit(
        _Request({
            "AccountSid": "AC" + "1" * 32,
            "UsageTriggerSid": "UT" + "2" * 32,
            "UsageCategory": "totalprice",
            "TriggerBy": "price",
            "Recurring": "daily",
            "TriggerValue": "5",
            "CurrentValue": "6",
            "IdempotencyToken": "synthetic-hanging-claim-token",
            "DateFired": _fresh_usage_trigger_date(),
        }),
        None,
    )

    assert response.status_code == 503
    assert events == ["suspended", "claim-cancelled"]
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_hanging_replay_completion_returns_retryable_503_after_suspension(monkeypatch):
    _configure(monkeypatch)
    events = []

    async def trip(**_kwargs):
        events.append("suspended")
        return True

    async def claim(*_args, **_kwargs):
        return "new"

    async def hanging_completion(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("completion-cancelled")
            raise

    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", trip)
    monkeypatch.setattr(public_demo, "claim_public_demo_usage_trigger", claim)
    monkeypatch.setattr(
        public_demo,
        "complete_public_demo_usage_trigger",
        hanging_completion,
    )
    monkeypatch.setattr(
        public_demo,
        "PUBLIC_DEMO_USAGE_TRIGGER_CLAIM_TIMEOUT_SECONDS",
        0.05,
    )
    started = time.monotonic()
    response = await public_demo.handle_public_demo_usage_limit(
        _Request({
            "AccountSid": "AC" + "1" * 32,
            "UsageTriggerSid": "UT" + "2" * 32,
            "UsageCategory": "totalprice",
            "TriggerBy": "price",
            "Recurring": "daily",
            "TriggerValue": "5",
            "CurrentValue": "6",
            "IdempotencyToken": "synthetic-hanging-completion-token",
            "DateFired": _fresh_usage_trigger_date(),
        }),
        None,
    )

    assert response.status_code == 503
    assert events == ["suspended", "completion-cancelled"]
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_usage_breaker_signature_is_required_at_asgi_boundary(monkeypatch):
    from app.public_demo_main import app

    async def unexpected(**_kwargs):
        raise AssertionError("unsigned callback must not mutate Twilio")

    monkeypatch.setattr(public_demo, "trip_public_demo_breaker", unexpected)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.post(
            "/webhooks/twilio/public-demo/usage-limit",
            data={"AccountSid": "AC" + "1" * 32},
        )
        invalid = await client.post(
            "/webhooks/twilio/public-demo/usage-limit",
            data={"AccountSid": "AC" + "1" * 32},
            headers={"X-Twilio-Signature": "invalid"},
        )

    assert missing.status_code == 403
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_usage_breaker_global_exception_preserves_retryable_503():
    from app.public_demo_main import app

    class ExplodingRequest:
        url = SimpleNamespace(path="/webhooks/twilio/public-demo/usage-limit")

    response = await app.exception_handlers[Exception](
        ExplodingRequest(),
        RuntimeError("synthetic failure"),
    )

    assert response.status_code == 503


def test_demo_prompt_has_no_real_world_commitment_or_pii_intake():
    prompt = build_public_demo_system_prompt(build_public_demo_profile())

    assert "opening greeting has already told the caller" in prompt
    assert "not a real company" in prompt
    assert "Do not ask for or confirm the caller's real name" in prompt
    assert "no appointment was created" in prompt
    assert "Never try to reach an owner" in prompt
    assert "contact local emergency services" in prompt
    assert "SERVICE PRICE RANGES" in prompt
    assert "handle the conversation exactly like a capable receptionist" in prompt
    assert 'Never preface an ordinary answer with "as part of our demo,"' in prompt


def test_dedicated_demo_pipeline_exposes_only_synthetic_tools():
    pipeline = PublicDemoGeminiPipeline.__new__(PublicDemoGeminiPipeline)
    tools = pipeline._build_gemini_tools()
    names = {
        item["name"]
        for item in tools[0]["function_declarations"]
    }
    assert names == {"check_availability", "book_appointment"}


def test_demo_pipeline_constructor_rejects_injected_identity():
    with pytest.raises(TypeError, match="code-owned"):
        PublicDemoGeminiPipeline(contractor_config={"public_demo": True})


@pytest.mark.asyncio
async def test_demo_pipeline_deadline_synchronously_disables_and_aborts_provider():
    events = []

    class Transport:
        def abort(self):
            events.append("aborted")

    class ProviderSocket:
        transport = Transport()

        async def close(self):
            events.append("closed")

    pipeline = PublicDemoGeminiPipeline.__new__(PublicDemoGeminiPipeline)
    pipeline._connected = True
    pipeline._audio_input_ready = asyncio.Event()
    pipeline._interrupt_speaking = False
    pipeline._tool_epoch = 0
    pipeline._tool_task = None
    pipeline._receive_task = None
    pipeline._recovery_task = None
    pipeline._audio_playout_task = None
    pipeline._silence_check_task = None
    pipeline._unavailable_task = None
    pipeline._command_check_task = None
    pipeline._ws = ProviderSocket()

    pipeline.enforce_deadline()

    assert pipeline._connected is False
    assert pipeline._audio_input_ready.is_set()
    assert pipeline._interrupt_speaking is True
    assert pipeline._ws is None
    assert events == ["aborted"]
    await asyncio.sleep(0)
    assert events == ["aborted", "closed"]


def test_dedicated_demo_app_exposes_no_tenant_or_admin_routes():
    from app.public_demo_main import app

    paths = {route.path for route in app.routes}
    assert "/health" in paths
    assert "/webhooks/twilio/public-demo/incoming" in paths
    assert "/webhooks/twilio/public-demo/fallback" in paths
    assert "/webhooks/twilio/public-demo/status" in paths
    assert "/webhooks/twilio/public-demo/message" in paths
    assert "/webhooks/twilio/public-demo/usage-limit" in paths
    assert "/public-demo-stream" in paths
    assert "/admin" not in paths
    assert "/webhooks/twilio/incoming" not in paths
    assert not any(path.startswith("/api/") for path in paths)


def test_demo_app_requires_inert_legacy_destination_settings(monkeypatch):
    from app import public_demo_main

    monkeypatch.setattr(public_demo_main.settings, "environment", "demo")
    monkeypatch.setattr(public_demo_main.settings, "twilio_account_sid", "AC_DEMO")
    monkeypatch.setattr(public_demo_main.settings, "twilio_auth_token", "demo-token")
    monkeypatch.setattr(public_demo_main.settings, "public_demo_enabled", False)
    monkeypatch.setattr(public_demo_main, "validate_runtime_safety", lambda **_kwargs: None)
    monkeypatch.setattr(public_demo_main.settings, "user_phone", "+12025550999")
    monkeypatch.setattr(
        public_demo_main.settings,
        "telegram_bot_token",
        public_demo_main.PUBLIC_DEMO_INERT_TELEGRAM_BOT_TOKEN,
    )

    with pytest.raises(RuntimeError, match="reserved public-demo placeholder"):
        public_demo_main._validate_public_demo_runtime()

    monkeypatch.setattr(
        public_demo_main.settings,
        "user_phone",
        public_demo_main.PUBLIC_DEMO_INERT_USER_PHONE,
    )
    monkeypatch.setattr(public_demo_main.settings, "telegram_bot_token", "real-token")
    with pytest.raises(RuntimeError, match="inert public-demo placeholder"):
        public_demo_main._validate_public_demo_runtime()


def test_demo_webhook_has_no_ordinary_tenant_side_effect_imports():
    source = inspect.getsource(public_demo)
    forbidden = (
        "settings.user_phone",
        "get_contractor",
        "get_call_history",
        "get_contact",
        "save_call",
        "send_sms",
        "send_regular_push",
        "enqueue_and_run_post_call",
        "process_post_call",
        "twilio.rest.Client",
    )

    for marker in forbidden:
        assert marker not in source


def test_public_demo_runtime_forbids_parent_breaker_authority(monkeypatch):
    from app import public_demo_main

    monkeypatch.setattr(public_demo_main.settings, "environment", "demo")
    monkeypatch.setattr(
        public_demo_main.settings,
        "public_demo_breaker_twilio_parent_main_api_key_secret",
        "parent-secret-must-never-be-public",
    )

    with pytest.raises(RuntimeError, match="forbidden on the public demo service"):
        public_demo_main._validate_public_demo_runtime()


def test_public_demo_classification_is_server_protected():
    assert {"public_demo", "demo_enabled_until", "demo_profile_version"} <= PROTECTED_FIELDS


def test_enabled_demo_requires_dedicated_secret(monkeypatch):
    monkeypatch.setattr(app_config.settings, "environment", "test")
    monkeypatch.setattr(app_config.settings, "public_demo_enabled", True)
    monkeypatch.setattr(app_config.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(app_config.settings, "public_demo_hmac_secret", "too-short")
    monkeypatch.setattr(app_config.settings, "public_demo_per_caller_limit", 3)
    monkeypatch.setattr(app_config.settings, "public_demo_per_caller_window_seconds", 3600)
    monkeypatch.setattr(app_config.settings, "public_demo_daily_call_limit", 100)
    monkeypatch.setattr(app_config.settings, "public_demo_concurrency_limit", 2)
    monkeypatch.setattr(app_config.settings, "public_demo_max_call_duration_seconds", 180)
    monkeypatch.setattr(app_config.settings, "public_demo_lease_ttl_seconds", 300)

    with pytest.raises(RuntimeError, match="PUBLIC_DEMO_HMAC_SECRET"):
        app_config.validate_runtime_safety()
