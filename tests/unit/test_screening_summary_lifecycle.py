"""Unit tests for screening summary notification lifecycle.

Tests that screening summary push tasks are retained on the pipeline, properly
cancelled and settled upon pipeline stop or hold completion, guarded by liveness
predicates at the send boundary, and not scheduled or dispatched after teardown.
"""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.relay_pipeline import RelayPipeline
from app.services.voice_pipeline import VoicePipeline
from app.services import push_notification
from app.services import screening_summary


@pytest.fixture(autouse=True)
def _isolated_owner_decisions(monkeypatch):
    """Keep lifecycle fixtures on an in-memory version of the durable adapter."""
    from copy import deepcopy
    import time
    from unittest.mock import AsyncMock
    from app.services import owner_call_actions as actions
    from app.services import legacy_call_commands as legacy_cmds
    owners = {
        'CA_relay_test_100': 'c_relay_test', 'CA_gemini_test_200': 'c_gemini_test',
        'CA_voice_test_300': 'c_voice_test', 'CA_spanish_test_400': 'c_spanish_test',
        'CA_system_quote_test_500': 'c_system_quote_test',
    }
    records = {sid: {'contractor_id': owner, 'state': 'screening',
                    'state_updated_at': time.time() - 1, 'ws_token': 'ws1'} for sid, owner in owners.items()}
    async def read(sid):
        return deepcopy(records.get(sid))
    async def transaction(sid, callback):
        records[sid] = callback(deepcopy(records.get(sid)))
        return deepcopy(records[sid])
    monkeypatch.setattr(actions, '_run_rtdb_transaction', transaction)
    monkeypatch.setattr(actions, 'read_record', read)
    monkeypatch.setattr(legacy_cmds, 'read_legacy_command', AsyncMock(return_value=None))


# --- Helpers and Fixtures ---


class _Recorder:
    def __init__(self):
        self.sent = []
        self.transcripts = []
        self.urgencies = []
        self.completed = False

    async def send(self, message: dict):
        self.sent.append(message)

    async def on_transcript(self, speaker: str, text: str):
        self.transcripts.append((speaker, text))

    async def on_urgency(self, snippet: str):
        self.urgencies.append(snippet)

    async def on_complete(self):
        self.completed = True


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload: str):
        self.sent.append(payload)

    async def close(self):
        return None

    async def recv(self):
        await asyncio.sleep(3600)


async def _noop_audio(_chunk: bytes):
    return None


async def _noop_transcript(_speaker: str, _text: str):
    return None


def _make_relay_pipeline(
    contractor_id: str = "c_relay_test",
    call_sid: str = "CA_relay_test_100",
) -> tuple[RelayPipeline, _Recorder]:
    recorder = _Recorder()

    async def fake_generate(_contents):
        yield {"text": "Deli is not available right now. Can I take a message?"}

    pipeline = RelayPipeline(
        contractor_config={
            "contractor_id": contractor_id,
            "business_name": "Test Plumbing",
            "owner_name": "Deli Matsuo",
            "effective_mode": "business",
        },
        call_sid=call_sid,
        caller_phone="+15550001111",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        on_urgency_detected=recorder.on_urgency,
        on_call_complete=recorder.on_complete,
        stream_generate=fake_generate,
    )
    pipeline._command_ws_token = "ws1"
    pipeline._history.append({"role": "user", "parts": [{"text": "Hello, my pipe burst."}]})
    return pipeline, recorder


def _make_gemini_pipeline(
    contractor_id: str = "c_gemini_test",
    call_sid: str = "CA_gemini_test_200",
) -> GeminiPipeline:
    pipeline = GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid=call_sid,
        caller_phone="+15550002222",
        contractor_config={
            "contractor_id": contractor_id,
            "business_name": "Test Plumbing",
            "owner_name": "Deli Matsuo",
            "effective_mode": "business",
        },
    )
    pipeline._command_ws_token = "ws1"
    pipeline._ws = FakeWebSocket()
    pipeline._connected = True
    pipeline._transcript_lines.append("Caller: Need help with a broken water heater.")
    return pipeline


def _make_voice_pipeline(
    contractor_id: str = "c_voice_test",
    call_sid: str = "CA_voice_test_300",
) -> VoicePipeline:
    pipeline = VoicePipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid=call_sid,
        caller_phone="+15550003333",
        contractor_config={
            "contractor_id": contractor_id,
            "business_name": "Test Plumbing",
            "owner_name": "Deli Matsuo",
            "effective_mode": "business",
        },
    )
    pipeline._command_ws_token = "ws1"
    pipeline._connected = True
    pipeline._conversation.append({"role": "user", "content": "<caller_speech>I have an emergency roof leak.</caller_speech>"})
    return pipeline


# --- RelayPipeline Tests ---


@pytest.mark.asyncio
async def test_relay_active_hold_sends_single_summary_and_repeat_is_noop(monkeypatch):
    pipeline, _ = _make_relay_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    async def fake_extract(*args, **kwargs):
        return {"caller_name": "Alice", "reason": "Pipe burst"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", fake_extract)

    # Initial hold trigger
    pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 1
    call_kwargs = push_mock.call_args.kwargs
    assert call_kwargs["contractor_id"] == "c_relay_test"
    assert call_kwargs["call_sid"] == "CA_relay_test_100"
    assert call_kwargs["caller_name"] == "Alice"
    assert call_kwargs["reason"] == "Pipe burst"

    # Repeat hold trigger must not send again
    pipeline._maybe_start_owner_hold("Let me check if Deli is available")
    await asyncio.sleep(0)
    assert push_mock.call_count == 1

    await pipeline.stop()


@pytest.mark.asyncio
async def test_relay_pause_extraction_stop_cancels_and_sends_zero(monkeypatch):
    pipeline, _ = _make_relay_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def paused_extract(*args, **kwargs):
        started_event.set()
        await resume_event.wait()
        return {"caller_name": "Alice", "reason": "Pipe burst"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", paused_extract)

    pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    await asyncio.wait_for(started_event.wait(), timeout=1)

    # Stop pipeline while extraction is paused
    await pipeline.stop()
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    assert pipeline._summary_task.done()
    assert pipeline._summary_task.cancelled()
    assert push_mock.call_count == 0


@pytest.mark.asyncio
async def test_relay_stale_summary_after_teardown_sends_zero(monkeypatch):
    pipeline, _ = _make_relay_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    await pipeline.stop()
    pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    if pipeline._summary_task:
        try:
            await pipeline._summary_task
        except asyncio.CancelledError:
            pass

    assert push_mock.call_count == 0

    # Also test explicit stale _trigger_screening_summary_push
    await pipeline._trigger_screening_summary_push()
    assert push_mock.call_count == 0


@pytest.mark.asyncio
async def test_relay_hold_completion_cancels_pending_summary(monkeypatch):
    pipeline, _ = _make_relay_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def paused_extract(*args, **kwargs):
        started_event.set()
        await resume_event.wait()
        return {"caller_name": "Alice", "reason": "Pipe burst"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", paused_extract)

    pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    await asyncio.wait_for(started_event.wait(), timeout=1)

    # Exercise the production timeout transition rather than canceling the
    # summary directly in the test.
    pipeline._hold_task.cancel()
    try:
        await pipeline._hold_task
    except asyncio.CancelledError:
        pass
    monkeypatch.setattr(pipeline, "OWNER_AVAILABILITY_TIMEOUT_SECONDS", 0)
    await pipeline._owner_hold_timer()
    assert pipeline._unavailable_said is True
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    assert pipeline._summary_task.cancelled()
    assert push_mock.call_count == 0
    await pipeline.stop()


# --- GeminiPipeline Tests ---


@pytest.mark.asyncio
async def test_gemini_active_hold_sends_single_summary_and_repeat_is_noop(monkeypatch):
    pipeline = _make_gemini_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    async def fake_extract(*args, **kwargs):
        return {"caller_name": "Bob", "reason": "Broken water heater"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", fake_extract)

    pipeline._start_owner_availability_wait()
    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 1
    call_kwargs = push_mock.call_args.kwargs
    assert call_kwargs["contractor_id"] == "c_gemini_test"
    assert call_kwargs["call_sid"] == "CA_gemini_test_200"
    assert call_kwargs["caller_name"] == "Bob"
    assert call_kwargs["reason"] == "Broken water heater"

    # Repeat hold trigger is no-op
    pipeline._start_owner_availability_wait()
    await asyncio.sleep(0)
    assert push_mock.call_count == 1

    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_pause_extraction_stop_cancels_and_sends_zero(monkeypatch):
    pipeline = _make_gemini_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def paused_extract(*args, **kwargs):
        started_event.set()
        await resume_event.wait()
        return {"caller_name": "Bob", "reason": "Broken water heater"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", paused_extract)

    pipeline._start_owner_availability_wait()
    await asyncio.wait_for(started_event.wait(), timeout=1)

    await pipeline.stop()
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    assert pipeline._summary_task.done()
    assert pipeline._summary_task.cancelled()
    assert push_mock.call_count == 0


@pytest.mark.asyncio
async def test_gemini_stale_summary_after_teardown_sends_zero(monkeypatch):
    pipeline = _make_gemini_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    await pipeline.stop()
    pipeline._start_owner_availability_wait()
    if pipeline._summary_task:
        try:
            await pipeline._summary_task
        except asyncio.CancelledError:
            pass

    assert push_mock.call_count == 0

    await pipeline._trigger_screening_summary_push()
    assert push_mock.call_count == 0


@pytest.mark.asyncio
async def test_gemini_finish_owner_availability_wait_cancels_pending_summary(monkeypatch):
    pipeline = _make_gemini_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def paused_extract(*args, **kwargs):
        started_event.set()
        await resume_event.wait()
        return {"caller_name": "Bob", "reason": "Broken water heater"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", paused_extract)

    pipeline._start_owner_availability_wait()
    await asyncio.wait_for(started_event.wait(), timeout=1)

    pipeline._finish_owner_availability_wait()
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    assert pipeline._summary_task.cancelled()
    assert push_mock.call_count == 0
    await pipeline.stop()


# --- VoicePipeline Tests ---


@pytest.mark.asyncio
async def test_voice_active_hold_sends_single_summary_and_repeat_is_noop(monkeypatch):
    pipeline = _make_voice_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    async def fake_extract(*args, **kwargs):
        return {"caller_name": "Charlie", "reason": "Roof leak"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", fake_extract)

    pipeline._start_owner_availability_wait()
    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 1
    call_kwargs = push_mock.call_args.kwargs
    assert call_kwargs["contractor_id"] == "c_voice_test"
    assert call_kwargs["call_sid"] == "CA_voice_test_300"
    assert call_kwargs["caller_name"] == "Charlie"
    assert call_kwargs["reason"] == "Roof leak"

    pipeline._start_owner_availability_wait()
    await asyncio.sleep(0)
    assert push_mock.call_count == 1

    await pipeline.stop()


@pytest.mark.asyncio
async def test_voice_pause_extraction_stop_cancels_and_sends_zero(monkeypatch):
    pipeline = _make_voice_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def paused_extract(*args, **kwargs):
        started_event.set()
        await resume_event.wait()
        return {"caller_name": "Charlie", "reason": "Roof leak"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", paused_extract)

    pipeline._start_owner_availability_wait()
    await asyncio.wait_for(started_event.wait(), timeout=1)

    await pipeline.stop()
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    assert pipeline._summary_task.done()
    assert pipeline._summary_task.cancelled()
    assert push_mock.call_count == 0


@pytest.mark.asyncio
async def test_voice_stale_summary_after_teardown_sends_zero(monkeypatch):
    pipeline = _make_voice_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    await pipeline.stop()
    pipeline._start_owner_availability_wait()
    if pipeline._summary_task:
        try:
            await pipeline._summary_task
        except asyncio.CancelledError:
            pass

    assert push_mock.call_count == 0

    await pipeline._trigger_screening_summary_push()
    assert push_mock.call_count == 0


@pytest.mark.asyncio
async def test_voice_finish_owner_availability_wait_cancels_pending_summary(monkeypatch):
    pipeline = _make_voice_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def paused_extract(*args, **kwargs):
        started_event.set()
        await resume_event.wait()
        return {"caller_name": "Charlie", "reason": "Roof leak"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", paused_extract)

    pipeline._start_owner_availability_wait()
    await asyncio.wait_for(started_event.wait(), timeout=1)

    pipeline._finish_owner_availability_wait()
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    assert pipeline._summary_task.cancelled()
    assert push_mock.call_count == 0
    await pipeline.stop()


# --- Cancellation-Resistant Mock Test ---


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["relay", "gemini", "voice"])
async def test_cancellation_resistant_mock_aborts_at_liveness_boundary(monkeypatch, engine):
    """A late result must be rejected even when extraction survives cancellation."""
    if engine == "relay":
        pipeline, _ = _make_relay_pipeline()
    elif engine == "gemini":
        pipeline = _make_gemini_pipeline()
    else:
        pipeline = _make_voice_pipeline()
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    started_event = asyncio.Event()
    resume_event = asyncio.Event()

    async def resistant_extract(*args, **kwargs):
        started_event.set()
        try:
            await resume_event.wait()
        except asyncio.CancelledError:
            pass
        return {"caller_name": "David", "reason": "Immune to cancellation"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", resistant_extract)

    if engine == "relay":
        pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    else:
        pipeline._start_owner_availability_wait()
    await asyncio.wait_for(started_event.wait(), timeout=1)

    # Stop the pipeline (pipeline becomes inactive / torn down)
    await pipeline.stop()
    resume_event.set()

    try:
        await pipeline._summary_task
    except asyncio.CancelledError:
        pass

    await asyncio.sleep(0.01)

    # Even though resistant_extract returned a dict without raising CancelledError,
    # the is_active predicate failed at the send boundary, so zero pushes were sent.
    assert push_mock.call_count == 0


# --- Language Switch and Transcript Selection Regressions ---


@pytest.mark.asyncio
async def test_voice_switch_language_fallback_summary_excludes_system_instruction(monkeypatch):
    pipeline = _make_voice_pipeline(
        contractor_id="c_spanish_test",
        call_sid="CA_spanish_test_400",
    )
    pipeline._conversation.clear()
    pipeline._conversation.append({"role": "assistant", "content": "Hola, gracias por llamar a Test Plumbing."})

    # Real switch_language mid-call
    await pipeline._switch_language("es")

    # Production-wrapped caller utterance
    caller_speech = "Hola, se rompió la tubería principal y tengo una fuga grande."
    pipeline._conversation.append({"role": "user", "content": f"<caller_speech>{caller_speech}</caller_speech>"})

    # Disable Anthropic API key so real deterministic fallback extraction executes
    monkeypatch.setattr(screening_summary.settings, "anthropic_api_key", "")

    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    pipeline._start_owner_availability_wait()
    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 1
    call_kwargs = push_mock.call_args.kwargs
    assert call_kwargs["contractor_id"] == "c_spanish_test"
    assert call_kwargs["call_sid"] == "CA_spanish_test_400"
    assert call_kwargs["caller_phone"] == "+15550003333"
    assert call_kwargs["reason"] == caller_speech
    assert "[System:" not in call_kwargs["reason"]
    assert "<caller_speech>" not in call_kwargs["reason"]
    assert "</caller_speech>" not in call_kwargs["reason"]
    assert "[System:" not in call_kwargs.get("caller_name", "")
    assert "<caller_speech>" not in call_kwargs.get("caller_name", "")

    await pipeline.stop()


@pytest.mark.asyncio
async def test_voice_caller_speech_quoting_system_is_preserved_not_filtered(monkeypatch):
    pipeline = _make_voice_pipeline(
        contractor_id="c_system_quote_test",
        call_sid="CA_system_quote_test_500",
    )
    pipeline._conversation.clear()
    pipeline._conversation.append({"role": "assistant", "content": "Hello, thanks for calling."})
    caller_speech = "The panel says [System: low battery]. Please check the alarm."
    pipeline._conversation.append({"role": "user", "content": f"<caller_speech>{caller_speech}</caller_speech>"})

    monkeypatch.setattr(screening_summary.settings, "anthropic_api_key", "")

    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    pipeline._start_owner_availability_wait()
    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 1
    call_kwargs = push_mock.call_args.kwargs
    assert call_kwargs["contractor_id"] == "c_system_quote_test"
    assert call_kwargs["call_sid"] == "CA_system_quote_test_500"
    assert call_kwargs["reason"] == caller_speech
    assert "<caller_speech>" not in call_kwargs["reason"]

    await pipeline.stop()


@pytest.mark.parametrize("engine", ["relay", "gemini", "voice"])
@pytest.mark.asyncio
async def test_all_engines_pass_captured_command_ws_token_and_publish_reason(monkeypatch, engine):
    from app.services import owner_call_actions

    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    async def fake_extract(*args, **kwargs):
        return {"caller_name": "Test Caller", "reason": "Water leak repair"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", fake_extract)

    if engine == "relay":
        pipeline, _ = _make_relay_pipeline()
    elif engine == "gemini":
        pipeline = _make_gemini_pipeline()
    else:
        pipeline = _make_voice_pipeline()

    assert pipeline._command_ws_token == "ws1"

    if engine == "relay":
        pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    else:
        pipeline._start_owner_availability_wait()

    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 1
    assert push_mock.call_args.kwargs["reason"] == "Water leak repair"

    record = await owner_call_actions.read_record(pipeline._call_sid)
    assert record is not None
    assert record["screening_reason"] == "Water leak repair"

    await pipeline.stop()


@pytest.mark.parametrize("engine", ["relay", "gemini", "voice"])
@pytest.mark.asyncio
async def test_engine_mismatched_ws_token_rejects_publish_and_suppresses_push(monkeypatch, engine):
    push_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(push_notification, "send_screening_summary_push", push_mock)

    async def fake_extract(*args, **kwargs):
        return {"caller_name": "Test Caller", "reason": "Water leak repair"}

    monkeypatch.setattr(screening_summary, "extract_screening_summary", fake_extract)

    if engine == "relay":
        pipeline, _ = _make_relay_pipeline()
    elif engine == "gemini":
        pipeline = _make_gemini_pipeline()
    else:
        pipeline = _make_voice_pipeline()

    pipeline._command_ws_token = "invalid_rotated_token"

    if engine == "relay":
        pipeline._maybe_start_owner_hold("Let me see if Deli is available")
    else:
        pipeline._start_owner_availability_wait()

    assert pipeline._summary_task is not None
    await pipeline._summary_task

    assert push_mock.call_count == 0

    await pipeline.stop()
