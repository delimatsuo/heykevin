"""Unit tests for shared Twilio stream transport extraction and compatibility shims."""

import ast
import asyncio
import logging
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

# Fictional test configuration, after tests/conftest.py captures the pristine environment.
for _env_key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TELEGRAM_BOT_TOKEN", "USER_PHONE"):
    os.environ.setdefault(_env_key, "test-placeholder")

from app.webhooks import media_stream, stream_transport


_REEXPORTED_SYMBOLS = (
    "WS_MAX_MESSAGE_SIZE",
    "MAX_MEDIA_INGRESS_AUDIO_BYTES",
    "MAX_MEDIA_INGRESS_AUDIO_CHUNKS",
    "MAX_PENDING_TWILIO_PLAYBACK_MARKS",
    "TRANSCRIPT_THROTTLE",
    "_call_label",
    "_log_safe_exception",
    "_safe_urgent_push_body",
    "_send_twilio_audio",
    "_send_twilio_clear",
    "_send_twilio_playback_mark",
    "_finish_max_call_duration",
    "_complete_twilio_call",
    "_log_task_exception",
    "_TwilioIngressEvent",
    "_PendingPlaybackMark",
    "_TwilioPlaybackMarks",
    "_TwilioMediaIngress",
    "_cancel_task",
    "_consume_twilio_ingress",
    "_serve_pipeline_ingress",
    "_active_call_fallback",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_relay_and_public_demo_import_stream_transport_without_media_stream():
    assert Path("app/webhooks/stream_transport.py").is_file()
    assert not Path("app/services/twilio_media_transport.py").exists()

    relay_imports = _imported_modules(Path("app/webhooks/relay_stream.py"))
    demo_imports = _imported_modules(Path("app/webhooks/public_demo.py"))

    assert "app.webhooks.stream_transport" in relay_imports
    assert "app.webhooks.media_stream" not in relay_imports
    assert "app.services.voice_pipeline" not in relay_imports
    assert "app.services.voice_events" in relay_imports

    assert "app.webhooks.stream_transport" in demo_imports
    assert "app.webhooks.media_stream" not in demo_imports


def test_stream_transport_preserves_media_stream_logger_category(caplog):
    assert stream_transport.logger.name == "app.webhooks.media_stream"

    class _DummySocket:
        async def send_json(self, _frame):
            return None

    with caplog.at_level(logging.INFO, logger="app.webhooks.media_stream"):
        stream_transport._log_safe_exception("unit_probe", ValueError("secret"), "CA_unit_123456")
        asyncio.run(
            stream_transport._send_twilio_clear(
                _DummySocket(),
                stream_sid="MZ_unit",
                call_sid="CA_unit_123456",
            )
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("event=unit_probe" in msg and "ValueError" in msg for msg in messages)
    assert any("event=twilio_audio_cleared" in msg for msg in messages)
    assert all("secret" not in msg for msg in messages)


def test_media_stream_reexports_all_extracted_transport_symbols():
    for name in _REEXPORTED_SYMBOLS:
        assert hasattr(media_stream, name), f"missing re-export: {name}"
        assert getattr(media_stream, name) is getattr(stream_transport, name)

    assert issubclass(media_stream.LiveTranscriptPusher, stream_transport.LiveTranscriptPusher)
    assert media_stream.LiveTranscriptPusher._compat_module is media_stream
    assert stream_transport.LiveTranscriptPusher._compat_module is None
    assert callable(media_stream._resolve_active_call)


@pytest.mark.asyncio
async def test_media_stream_live_transcript_pusher_delegates_monkeypatched_globals(monkeypatch):
    recorded_writes: list[tuple[str, str, str, str]] = []
    monotonic_calls = 0

    async def fake_update(
        call_sid: str, text: str, *, contractor_id: str, ws_token: str, **_kwargs
    ) -> bool:
        recorded_writes.append((call_sid, text, contractor_id, ws_token))
        return True

    def fake_monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 42.0 + monotonic_calls

    fake_time = SimpleNamespace(monotonic=fake_monotonic, time=time.time)
    monkeypatch.setattr(media_stream, "update_live_transcript", fake_update)
    monkeypatch.setattr(media_stream, "time", fake_time)

    class RelayInjectedPusher(media_stream.LiveTranscriptPusher):
        pass

    lines = ["Caller: Hello"]
    pusher = RelayInjectedPusher(
        "CA_test",
        lines,
        throttle=0.0,
        contractor_id="ctr_1",
        ws_token="ws_tok_1",
    )
    pusher.push()
    await pusher.close()

    assert recorded_writes == [("CA_test", "Caller: Hello", "ctr_1", "ws_tok_1")]
    assert monotonic_calls >= 1


@pytest.mark.asyncio
async def test_stream_transport_live_transcript_pusher_uses_own_globals_directly(monkeypatch):
    direct_writes: list[tuple[str, str]] = []

    async def fake_transport_update(
        call_sid: str, text: str, *, contractor_id: str, ws_token: str, **_kwargs
    ) -> bool:
        direct_writes.append((call_sid, text))
        return True

    monkeypatch.setattr(stream_transport, "update_live_transcript", fake_transport_update)

    lines = ["Kevin: Hi there"]
    pusher = stream_transport.LiveTranscriptPusher(
        "CA_direct",
        lines,
        throttle=0.0,
        contractor_id="ctr_2",
        ws_token="ws_tok_2",
    )
    pusher.push()
    await pusher.close()

    assert direct_writes == [("CA_direct", "Kevin: Hi there")]


@pytest.mark.asyncio
async def test_resolve_active_call_supports_media_stream_monkeypatch_and_explicit_fn(monkeypatch):
    sentinel = SimpleNamespace(call_sid="CA_patched", contractor_id="ctr_patched")

    async def patched_lookup(call_sid: str):
        assert call_sid == "CA_patched"
        return sentinel

    monkeypatch.setattr(media_stream, "get_active_call", patched_lookup)
    resolved = await media_stream._resolve_active_call("CA_patched", None)
    assert resolved is sentinel

    async def miss_lookup(_call_sid: str):
        return None

    fallback = await stream_transport._resolve_active_call(
        "CA_fallback",
        {"contractor_id": "ctr_fb", "caller_phone": "+15550100", "accepted": False},
        get_active_call_fn=miss_lookup,
    )
    assert fallback is not None
    assert fallback.contractor_id == "ctr_fb"
    assert fallback.caller_phone == "+15550100"
    assert fallback.accepted is False
