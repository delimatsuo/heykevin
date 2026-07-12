"""Privacy contracts for the legacy Deepgram/Claude/ElevenLabs pipeline logs."""

import ast
import asyncio
import inspect
import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import voice_pipeline as voice_pipeline_module
from app.services.voice_pipeline import VoicePipeline


@pytest.mark.asyncio
async def test_legacy_urgency_log_excludes_transcript(caplog):
    callback_finished = asyncio.Event()

    async def on_urgency(_text: str):
        callback_finished.set()

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = "CA_test"
    pipeline._urgency_detected = False
    pipeline.on_urgency_detected = on_urgency
    pipeline._unavailable_task = None
    pipeline._is_speaking = False
    pipeline.on_clear_audio = None
    private_transcript = "There is a fire at a private caller location."
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    pipeline._check_urgency(private_transcript)
    await asyncio.wait_for(callback_finished.wait(), timeout=1)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_event event=urgency_detected" in messages
    assert "keyword=fire" not in messages
    assert "chars=" in messages
    assert private_transcript not in messages


@pytest.mark.asyncio
async def test_legacy_utterance_log_uses_counts_only(caplog):
    processed = []
    processed_event = asyncio.Event()

    async def process_utterance(text: str):
        processed.append(text)
        processed_event.set()

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = "CA_test"
    pipeline._utterance_buffer = ["private caller", "request details"]
    pipeline._urgency_detected = False
    pipeline.on_urgency_detected = None
    pipeline._process_utterance = process_utterance
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    await pipeline._flush_utterance()
    await asyncio.wait_for(processed_event.wait(), timeout=1)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert processed == ["private caller request details"]
    assert "voice_event event=utterance_complete" in messages
    assert "chars=30" in messages
    assert "private caller" not in messages


@pytest.mark.asyncio
async def test_legacy_deepgram_error_log_excludes_exception_message(caplog):
    private_error = "private Deepgram payload from caller"

    class FailingWebSocket:
        async def recv(self):
            raise RuntimeError(private_error)

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = "CA_test"
    pipeline._deepgram_ws = FailingWebSocket()
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    await pipeline._deepgram_receive_loop()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_event event=deepgram_receive_error" in messages
    assert "exception_type=RuntimeError" in messages
    assert private_error not in messages


@pytest.mark.asyncio
async def test_legacy_tts_error_log_excludes_provider_body(caplog):
    private_provider_body = "private provider response with speech text"

    class ErrorResponse:
        status_code = 500
        text = private_provider_body
        content = b""

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            return ErrorResponse()

    async def noop_audio(_chunk: bytes):
        return None

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = "CA_test"
    pipeline._http_client = FakeClient()
    pipeline._tts_voice_id = "voice"
    pipeline._tts_model_id = "model"
    pipeline._connected = True
    pipeline.on_audio_out = noop_audio
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    await pipeline._speak("private speech input")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_event event=tts_provider_error" in messages
    assert "status_code=500" in messages
    assert private_provider_body not in messages
    assert "private speech input" not in messages


def test_legacy_voice_logger_fstrings_do_not_embed_sensitive_values():
    forbidden_names = {
        "api_err",
        "caller_phone",
        "combined",
        "e",
        "kevin_text",
        "msg",
        "text",
        "transcript",
    }
    tree = ast.parse(inspect.getsource(voice_pipeline_module))

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        function = call.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "logger"
        ):
            continue

        assert not any(keyword.arg == "exc_info" for keyword in call.keywords)
        formatted_names = {
            node.id
            for argument in call.args
            for node in ast.walk(argument)
            if isinstance(node, ast.FormattedValue)
            for node in ast.walk(node.value)
            if isinstance(node, ast.Name)
        }
        assert formatted_names.isdisjoint(forbidden_names), ast.unparse(call)
        assert "response.text" not in ast.unparse(call)
