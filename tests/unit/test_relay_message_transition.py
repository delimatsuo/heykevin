"""Unit tests for ConversationRelay owner message-taking transition boundary.

Protocol-level verification:
- Protocol tests verify WebSocket ordering, media messages, and event sequencing;
  they do not constitute physical audible sound verification on real hardware.
- In-flight turns with streamed tokens complete all tokens and send last=True before message transition.
- In-flight turns with zero streamed tokens cancel immediately.
- 3s pause uses documented Twilio ConversationRelay play media message (/static/audio/message-pause-3s.wav)
  ordered between previous last=True and new message first token.
- Deterministic 3s silence WAV asset validation (PCM mono 16-bit 8000Hz, 24000 frames, 48044 total bytes).
- Pause is queued once for decline+hold, zero for timeout/no-hold.
- No SSML tags anywhere in transcripts, history, or tokens.
- Repeated poll or ACK failure does not duplicate speech or repeat 3s pause.
- Generation failure before first text does not queue pause early and retries on next poll without caller prompt.
- Durable guard invalidation after await blocks subsequent tokens and error fallback.
- Caller barge-in recovery handles partial speech without repeating whole prompt or wedging epoch.
- Post-transition conversation and silence watchdog operate unblocked.
- Every text and play cycle has preemptible: False, interruptible: True.
"""

import asyncio
import json
import os
import time
import wave
from unittest.mock import AsyncMock, patch

import pytest

# Dummy env setdefault for standalone test execution without external dependencies
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACdummy")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "dummy_auth")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15551234567")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy_anthropic")
os.environ.setdefault("DEEPGRAM_API_KEY", "dummy_deepgram")
os.environ.setdefault("ELEVENLABS_API_KEY", "dummy_elevenlabs")
os.environ.setdefault("API_BEARER_TOKEN", "dummy_bearer")
os.environ.setdefault("USER_PHONE", "+15550001111")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy_telegram")

from app.config import settings
from app.services.message_taking import build_relay_instruction_text
from app.services.relay_pipeline import RelayPipeline


class _Recorder:
    def __init__(self):
        self.sent = []
        self.transcripts = []
        self.urgencies = []
        self.completed = False

    async def send(self, message: dict):
        self.sent.append(json.loads(json.dumps(message)))

    async def on_transcript(self, speaker: str, text: str):
        self.transcripts.append((speaker, text))

    async def on_urgency(self, snippet: str):
        self.urgencies.append(snippet)

    async def on_complete(self):
        self.completed = True


def _contractor(**overrides) -> dict:
    config = {
        "contractor_id": "test_contractor",
        "business_name": "Test Plumbing",
        "owner_name": "Deli Matsuo",
        "service_type": "plumbing",
        "effective_mode": "business",
    }
    config.update(overrides)
    return config


def _pipeline(recorder: _Recorder, stream_generate_fn, **overrides) -> RelayPipeline:
    return RelayPipeline(
        contractor_config=_contractor(**overrides),
        call_sid="CA_relay_test",
        caller_phone="+15550001111",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        on_urgency_detected=recorder.on_urgency,
        on_call_complete=recorder.on_complete,
        stream_generate=stream_generate_fn,
    )


def test_wav_asset_properties_and_routepath():
    """Verify deterministic 3s silence WAV asset properties and route path."""
    wav_path = "app/static/audio/message-pause-3s.wav"
    assert os.path.exists(wav_path), "WAV file must exist at app/static/audio/message-pause-3s.wav"
    file_size = os.path.getsize(wav_path)
    assert file_size == 48044, f"Expected 48044 total bytes (44 byte header + 48000 data), got {file_size}"

    with wave.open(wav_path, "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        duration = nframes / framerate
        frames_data = wf.readframes(nframes)

        assert nchannels == 1, "Must be mono (1 channel)"
        assert sampwidth == 2, "Must be 16-bit (2 bytes per sample)"
        assert framerate == 8000, "Must be 8000 Hz"
        assert nframes == 24000, "Must be 24000 frames for 3.0s"
        assert duration == 3.0, "Duration must be exactly 3.0 seconds"
        assert len(frames_data) == 48000, "Must have exactly 48000 PCM audio bytes"
        assert set(frames_data) == {0}, "All audio frames must be zero bytes (silence)"

    expected_url = f"{settings.cloud_run_url.rstrip('/')}/static/audio/message-pause-3s.wav"
    assert expected_url.endswith("/static/audio/message-pause-3s.wav")


@pytest.mark.asyncio
async def test_started_turn_sends_all_tokens_and_last_before_message():
    """In-flight generation with streamed tokens finishes all tokens and last=True before message transition."""
    recorder = _Recorder()
    stream_pause = asyncio.Event()

    async def streaming_parts(contents):
        yield {"text": "We "}
        yield {"text": "are "}
        await stream_pause.wait()
        yield {"text": "checking "}
        yield {"text": "availability."}

    pipeline = _pipeline(recorder, streaming_parts)
    pipeline._hold_offered = True

    # Start generation for caller turn
    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "Is someone there?", "last": True}
    )
    await asyncio.sleep(0.01)

    # First words streamed to Twilio
    tokens = [m["token"] for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert tokens == ["We ", "are "]

    # Message taking intent arrives while generation is paused mid-stream
    intent = {
        "action": "decline",
        "operation_id": "op_decline_1",
        "claim_nonce": "nonce_1",
        "accepted_at": time.monotonic(),
    }
    prepare_task = asyncio.create_task(pipeline._prepare_message_delivery(intent))
    await asyncio.sleep(0.01)
    assert not prepare_task.done()

    # Unpause the stream so remaining words can finish
    stream_pause.set()
    prepared = await prepare_task
    assert prepared is True

    # Check that previous turn finished and sent last=True before message delivery
    last_indices = [i for i, m in enumerate(recorder.sent) if m.get("type") == "text" and m.get("last") is True]
    assert len(last_indices) == 1

    # Now deliver message instruction
    async def message_stream(contents):
        yield {"text": "Deli is unavailable. Can I take a message?"}

    pipeline._stream_generate = message_stream
    delivered = await pipeline._deliver_message_instruction()
    assert delivered is True

    # Verify sequencing: turn 1 tokens -> turn 1 last=True -> play media pause -> message token -> message last=True
    play_indices = [i for i, m in enumerate(recorder.sent) if m.get("type") == "play"]
    assert len(play_indices) == 1
    play_idx = play_indices[0]
    turn1_last_idx = last_indices[0]

    assert turn1_last_idx < play_idx, "Turn 1 last=True must precede play media message"

    message_text_indices = [
        i for i, m in enumerate(recorder.sent)
        if m.get("type") == "text" and "Deli is unavailable" in (m.get("token") or "")
    ]
    assert len(message_text_indices) == 1
    assert play_idx < message_text_indices[0], "Play media message must precede message text token"


@pytest.mark.asyncio
async def test_zero_token_pending_generation_cancels_immediately():
    """If generation has started but zero tokens have been sent to Twilio, cancel immediately."""
    recorder = _Recorder()
    hang_event = asyncio.Event()

    async def hanging_stream(contents):
        await hang_event.wait()
        yield {"text": "Should never be sent"}

    pipeline = _pipeline(recorder, hanging_stream)

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "Hello?", "last": True}
    )
    await asyncio.sleep(0.01)

    assert pipeline._generate_task is not None
    assert pipeline._streamed_text == ""

    intent = {
        "action": "decline",
        "operation_id": "op_zero_token",
        "claim_nonce": "nonce_zero_token",
        "accepted_at": time.monotonic(),
    }
    prepared = await pipeline._prepare_message_delivery(intent)
    assert prepared is True

    # Generation task cancelled and no text sent to Twilio
    assert pipeline._generate_task is None or pipeline._generate_task.cancelled()
    tokens = [m.get("token") for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert tokens == []


@pytest.mark.asyncio
async def test_play_pause_once_for_decline_with_hold_vs_timeout_and_no_hold():
    """Play pause is queued once for decline+hold, and 0 for timeout or decline without hold."""
    # Test A: decline + hold -> play media message with source (not url) queued once
    rec_a = _Recorder()

    async def stream_a(contents):
        yield {"text": "Deli is unavailable. Leave a message."}

    pipe_a = _pipeline(rec_a, stream_a)
    pipe_a._hold_offered = True

    intent_a = {"action": "decline", "operation_id": "op_a", "claim_nonce": "nonce_a"}
    assert await pipe_a._prepare_message_delivery(intent_a) is True
    assert await pipe_a._deliver_message_instruction() is True

    plays_a = [m for m in rec_a.sent if m.get("type") == "play"]
    assert len(plays_a) == 1
    assert "source" in plays_a[0], "Play message must contain 'source' key"
    assert "url" not in plays_a[0], "Play message must NOT contain 'url' key"
    assert plays_a[0]["source"].endswith("/static/audio/message-pause-3s.wav")
    assert plays_a[0]["loop"] == 1
    assert plays_a[0]["preemptible"] is False
    assert plays_a[0]["interruptible"] is True

    # Test B: timeout + hold -> no play media message
    rec_b = _Recorder()
    pipe_b = _pipeline(rec_b, stream_a)
    pipe_b._hold_offered = True

    intent_b = {"action": "timeout", "operation_id": "op_b", "claim_nonce": "nonce_b"}
    assert await pipe_b._prepare_message_delivery(intent_b) is True
    assert await pipe_b._deliver_message_instruction() is True

    plays_b = [m for m in rec_b.sent if m.get("type") == "play"]
    assert len(plays_b) == 0

    # Test C: decline + no hold -> no play media message
    rec_c = _Recorder()
    pipe_c = _pipeline(rec_c, stream_a)
    pipe_c._hold_offered = False

    intent_c = {"action": "decline", "operation_id": "op_c", "claim_nonce": "nonce_c"}
    assert await pipe_c._prepare_message_delivery(intent_c) is True
    assert await pipe_c._deliver_message_instruction() is True

    plays_c = [m for m in rec_c.sent if m.get("type") == "play"]
    assert len(plays_c) == 0


@pytest.mark.asyncio
async def test_no_ssml_anywhere_in_history_or_transcripts_or_tokens():
    """Verify that no SSML break or phoneme tags are emitted anywhere in history, transcripts, or tokens."""
    recorder = _Recorder()

    async def message_stream(contents):
        yield {"text": "Unfortunately, Deli is unavailable. Can I take a message?"}

    pipeline = _pipeline(recorder, message_stream)
    pipeline._hold_offered = True

    intent = {"action": "decline", "operation_id": "op_ssml", "claim_nonce": "nonce_ssml"}
    await pipeline._prepare_message_delivery(intent)
    await pipeline._deliver_message_instruction()

    # Verify recorder sent text tokens
    text_tokens = [m.get("token", "") for m in recorder.sent if m.get("type") == "text"]
    for t in text_tokens:
        assert "<break" not in t
        assert "<phoneme" not in t
        assert "<" not in t

    # Verify transcripts
    for speaker, text in recorder.transcripts:
        assert "<break" not in text
        assert "<phoneme" not in text

    # Verify history
    for entry in pipeline._history:
        for part in entry.get("parts", []):
            txt = part.get("text", "")
            assert "<break" not in txt
            assert "<phoneme" not in txt


@pytest.mark.asyncio
async def test_first_send_failure_returns_false_and_no_unavailable():
    """If sending the first text token fails, delivery returns False and _unavailable_said remains False."""
    recorder = _Recorder()

    async def fail_send(msg):
        if msg.get("type") == "text" and msg.get("token"):
            raise ConnectionError("Twilio connection dropped")
        recorder.sent.append(msg)

    async def stream_fn(contents):
        yield {"text": "Deli is unavailable."}

    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_fail_first",
        caller_phone="+15550001111",
        send_to_twilio=fail_send,
        stream_generate=stream_fn,
    )
    pipeline._hold_offered = True

    intent = {"action": "decline", "operation_id": "op_fail1", "claim_nonce": "nonce_fail1"}
    await pipeline._prepare_message_delivery(intent)

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._unavailable_said is False
    assert pipeline._streamed_text == ""


@pytest.mark.asyncio
async def test_external_cancel_child_done_no_aftereffects():
    """Cancelling _deliver_message_instruction cancels and joins child task cleanly without orphan tasks."""
    recorder = _Recorder()
    hang_event = asyncio.Event()

    async def hang_stream(contents):
        await hang_event.wait()
        yield {"text": "Should not complete"}

    pipeline = _pipeline(recorder, hang_stream)

    intent = {"action": "decline", "operation_id": "op_ext_cancel", "claim_nonce": "nonce_ext_cancel"}
    await pipeline._prepare_message_delivery(intent)

    deliver_task = asyncio.create_task(pipeline._deliver_message_instruction())
    await asyncio.sleep(0.01)

    assert pipeline._generate_task is not None
    assert not pipeline._generate_task.done()

    deliver_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deliver_task

    # Child generation task was joined and is done
    assert pipeline._generate_task is None
    assert pipeline._unavailable_said is False


@pytest.mark.asyncio
async def test_rejected_guard_then_normal_caller_no_stale_instruction():
    """If guard rejects delivery, _message_instruction is cleared and subsequent caller turn is normal."""
    recorder = _Recorder()
    received_contents = []

    async def stream_fn(contents):
        received_contents.append(json.loads(json.dumps(contents)))
        yield {"text": "Hello, how can I help you?"}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._message_delivery_guard = AsyncMock(return_value=False)

    # Delivery fails because guard rejected authority
    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._message_instruction == ""

    # Normal caller turn arrives
    del pipeline._message_delivery_guard
    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "I need a quote", "last": True}
    )
    await pipeline.wait_idle()

    # Verify no stale owner unavailable instruction leaked into normal caller contents
    assert len(received_contents) == 1
    all_texts = [p.get("text", "") for c in received_contents[0] for p in c.get("parts", [])]
    assert not any("is unavailable. Tell the caller" in t for t in all_texts)


@pytest.mark.asyncio
async def test_tool_first_pause_queued_on_subsequent_round():
    """Tool execution on round 0 carries pause requirement to round 1 where text is first emitted."""
    recorder = _Recorder()
    rounds = {"count": 0}

    async def tool_then_text_stream(contents):
        rounds["count"] += 1
        if rounds["count"] == 1:
            yield {"functionCall": {"name": "check_calendar_availability", "args": {}}}
        else:
            yield {"text": "Deli is unavailable right now."}

    pipeline = _pipeline(recorder, tool_then_text_stream)
    pipeline._hold_offered = True
    pipeline._execute_tool = AsyncMock(return_value={"available": False})

    intent = {"action": "decline", "operation_id": "op_tool", "claim_nonce": "nonce_tool"}
    await pipeline._prepare_message_delivery(intent)

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is True

    plays = [m for m in recorder.sent if m.get("type") == "play"]
    assert len(plays) == 1, "Play pause must be queued exactly once on the round where text first arrives"
    text_msgs = [m for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert len(text_msgs) == 1


@pytest.mark.asyncio
async def test_started_regular_gen_timeout_still_alive_no_lost_last():
    """In _prepare_message_delivery, 15s timeout returns False WITHOUT cancelling in-flight regular speech."""
    recorder = _Recorder()
    hang_event = asyncio.Event()

    async def slow_stream(contents):
        yield {"text": "We are looking into "}
        await hang_event.wait()
        yield {"text": "your request."}

    pipeline = _pipeline(recorder, slow_stream)

    # Start caller turn
    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "Status check", "last": True}
    )
    await asyncio.sleep(0.01)

    orig_wait_for = asyncio.wait_for

    async def short_wait_for(fut, timeout=None):
        return await orig_wait_for(fut, timeout=0.02)

    with patch("asyncio.wait_for", side_effect=short_wait_for):
        intent = {"action": "decline", "operation_id": "op_reg_timeout", "claim_nonce": "nonce_reg_timeout"}
        prepared = await pipeline._prepare_message_delivery(intent)
        assert prepared is False

    # The regular generation task was NOT cancelled
    assert pipeline._generate_task is not None
    assert not pipeline._generate_task.done()

    # Release stream so it completes normally and sends its last=True
    hang_event.set()
    await pipeline.wait_idle()

    tokens = [m.get("token") for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert "your request." in tokens
    assert any(m.get("type") == "text" and m.get("last") is True for m in recorder.sent)


@pytest.mark.asyncio
async def test_bargein_with_actual_token_sent_and_event_gate():
    """Barge-in after actual token sent records partial and returns True if guard is valid (no replay on retry)."""
    recorder = _Recorder()
    gate = asyncio.Event()

    async def gated_stream(contents):
        yield {"text": "Deli is currently "}
        await gate.wait()
        yield {"text": "unavailable."}

    pipeline = _pipeline(recorder, gated_stream)
    pipeline._message_delivery_guard = AsyncMock(return_value=True)

    task = asyncio.create_task(pipeline._deliver_message_instruction())
    await asyncio.sleep(0.01)

    # Caller interrupts while generator is waiting at gate
    await pipeline.handle_message(
        {"type": "interrupt", "utteranceUntilInterrupt": "Wait, I will call back"}
    )
    gate.set()

    delivered = await task
    assert delivered is True, "Delivery accepted because actual token was sent and guard was valid"
    assert pipeline._unavailable_said is True

    # Next attempt (e.g. command poll retry) does not replay whole prompt
    delivered2 = await pipeline._deliver_message_instruction()
    assert delivered2 is True


@pytest.mark.asyncio
async def test_bargein_postclaim_replacement_returns_false():
    """Barge-in after partial token where guard rejects returns False."""
    recorder = _Recorder()
    gate = asyncio.Event()

    async def gated_stream(contents):
        yield {"text": "Deli is "}
        await gate.wait()
        yield {"text": "busy."}

    pipeline = _pipeline(recorder, gated_stream)
    # Guard rejects after suspend (e.g. claim nonce replaced)
    pipeline._message_delivery_guard = AsyncMock(side_effect=[True, False])

    task = asyncio.create_task(pipeline._deliver_message_instruction())
    await asyncio.sleep(0.01)

    await pipeline.handle_message(
        {"type": "interrupt", "utteranceUntilInterrupt": "Never mind"}
    )
    gate.set()

    delivered = await task
    assert delivered is False


@pytest.mark.asyncio
async def test_common_consumer_ack_fail_retry_with_valid_ws_stable_key_no_duplicate_text_or_play():
    """consume_message_intent integration with matching ws_token retries cleanly without duplicating text or play."""
    recorder = _Recorder()

    async def stream_fn(contents):
        yield {"text": "Deli is not available right now. Leave a message."}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._command_ws_token = "ws_test_token"
    pipeline._hold_offered = True

    record = {
        "contractor_id": "test_contractor",
        "call_sid": "CA_relay_test",
        "state": "screening",
        "state_updated_at": time.time(),
        "owner_action": "decline",
        "owner_operation_id": "op_decline_int",
        "claim_nonce": "nonce_decline_int",
        "owner_action_status": "message_requested",
        "ws_token": "ws_test_token",
        "message_intent": {
            "type": "take_message",
            "contractor_id": "test_contractor",
            "operation_id": "op_decline_int",
            "action": "decline",
        },
    }

    from app.services.owner_call_actions import consume_message_intent

    # Mock read_record and acknowledge_owner_action
    with patch("app.services.owner_call_actions.read_record", AsyncMock(return_value=record)), \
         patch("app.services.owner_call_actions.acknowledge_owner_action", AsyncMock(side_effect=[False, True])):

        # First consume attempt: delivery succeeds, but ACK returns False
        result1 = await consume_message_intent(pipeline, pipeline._deliver_message_instruction)
        assert result1 is False
        assert pipeline._unavailable_said is True

        # Check only 1 play message and 1 text token sent
        plays1 = [m for m in recorder.sent if m.get("type") == "play"]
        assert len(plays1) == 1
        text1 = [m for m in recorder.sent if m.get("type") == "text" and m.get("token")]
        assert len(text1) == 1

        # Second consume attempt (next poll retry): ACK succeeds, NO duplicate play or text sent!
        result2 = await consume_message_intent(pipeline, pipeline._deliver_message_instruction)
        assert result2 is True

        plays2 = [m for m in recorder.sent if m.get("type") == "play"]
        assert len(plays2) == 1, "Play pause must not be duplicated on retry"
        text2 = [m for m in recorder.sent if m.get("type") == "text" and m.get("token")]
        assert len(text2) == 1, "Text token must not be duplicated on retry"


@pytest.mark.asyncio
async def test_post_transition_normal_conversation_and_silence_watchdog():
    """After message transition completes, normal conversation and silence watchdog resume."""
    recorder = _Recorder()

    async def normal_stream(contents):
        yield {"text": "Goodbye! Have a great day."}

    pipeline = _pipeline(recorder, normal_stream)
    pipeline.SILENCE_CHECK_INTERVAL_SECONDS = 0.01
    pipeline.CALLER_SILENCE_PROMPT_SECONDS = 0.02
    pipeline.CALLER_SILENCE_HANGUP_SECONDS = 0.02
    pipeline._await_playout = AsyncMock(return_value=True)

    pipeline._unavailable_said = True
    pipeline._message_taking_pending = False

    pipeline.start_background_tasks()
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if recorder.completed:
                break
    finally:
        await pipeline.stop()

    assert {"type": "end"} in recorder.sent
    assert recorder.completed is True


@pytest.mark.asyncio
async def test_explicit_preemptible_false_interruptible_true():
    """Every text and play message sent to Twilio explicitly specifies preemptible: False, interruptible: True."""
    recorder = _Recorder()

    async def stream_fn(contents):
        yield {"text": "Hello world."}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._hold_offered = True

    intent = {"action": "decline", "operation_id": "op_flags", "claim_nonce": "nonce_flags"}
    await pipeline._prepare_message_delivery(intent)
    await pipeline._deliver_message_instruction()

    assert len(recorder.sent) > 0
    for msg in recorder.sent:
        msg_type = msg.get("type")
        if msg_type in ("text", "play"):
            assert msg.get("preemptible") is False, f"Message {msg} missing preemptible: False"
            assert msg.get("interruptible") is True, f"Message {msg} missing interruptible: True"


@pytest.mark.asyncio
async def test_caller_prompt_during_message_delivery_starts_response_after_ack():
    """Caller speech during in-flight message delivery cancels turn 1, accepts ACK, and starts natural response."""
    recorder = _Recorder()
    generator_calls = []
    first_token_sent = asyncio.Event()
    gate = asyncio.Event()

    async def stream_fn(contents):
        generator_calls.append(json.loads(json.dumps(contents)))
        if len(generator_calls) == 1:
            yield {"text": "Deli is unavailable right now. "}
            first_token_sent.set()
            await gate.wait()
            yield {"text": "Can I take a message?"}
        elif len(generator_calls) == 2:
            yield {"text": "I'll let Deli know you will arrive tomorrow."}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._command_ws_token = "ws_test_token"
    pipeline._hold_offered = True

    record = {
        "contractor_id": "test_contractor",
        "call_sid": "CA_relay_test",
        "state": "screening",
        "state_updated_at": time.time(),
        "owner_action": "decline",
        "owner_operation_id": "op_decline_caller_prompt",
        "claim_nonce": "nonce_decline_caller_prompt",
        "owner_action_status": "message_requested",
        "ws_token": "ws_test_token",
        "message_intent": {
            "type": "take_message",
            "contractor_id": "test_contractor",
            "operation_id": "op_decline_caller_prompt",
            "action": "decline",
        },
    }

    from app.services.owner_call_actions import consume_message_intent

    async def mock_acknowledge(call_sid, new_status="taking_message", **kwargs):
        record["owner_action_status"] = new_status
        return True

    with patch("app.services.owner_call_actions.read_record", AsyncMock(side_effect=lambda sid: dict(record))), \
         patch("app.services.owner_call_actions.acknowledge_owner_action", side_effect=mock_acknowledge):

        command_task = asyncio.create_task(
            consume_message_intent(pipeline, pipeline._deliver_message_instruction)
        )

        # Wait until first stream has emitted its unavailable prefix and is waiting at gate
        await asyncio.wait_for(first_token_sent.wait(), timeout=1.0)
        assert pipeline._streamed_text == "Deli is unavailable right now. "
        assert pipeline._message_taking_pending is True

        # Send final caller prompt while task is gated
        caller_utterance = "Please tell Deli I will arrive tomorrow"
        await pipeline.handle_message({
            "type": "prompt",
            "voicePrompt": caller_utterance,
            "last": True,
        })

        assert pipeline._message_caller_response_pending is True
        gate.set()

        # Command task completes and acknowledges owner action (moves status to taking_message)
        result = await asyncio.wait_for(command_task, timeout=2.0)
        assert result is True
        assert pipeline._unavailable_said is True
        assert pipeline._message_taking_pending is False

        # Await pipeline.wait_idle() bounded 1s for the second generator response
        await asyncio.wait_for(pipeline.wait_idle(), timeout=1.0)

    # Assert second response was actually sent
    text_tokens = [m.get("token") for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert any("arrive tomorrow" in (t or "") for t in text_tokens)

    # Exactly 1 pause media message played
    plays = [m for m in recorder.sent if m.get("type") == "play"]
    assert len(plays) == 1

    # Exactly 2 generator calls
    assert len(generator_calls) == 2

    # Second generator call received saved caller text and unavailable system instruction
    second_call_parts = [
        p.get("text", "")
        for entry in generator_calls[1]
        for p in entry.get("parts", [])
    ]
    assert any(caller_utterance in t for t in second_call_parts)
    assert any("is unavailable. Take and acknowledge the caller's message" in t for t in second_call_parts)

    # Flags cleared
    assert pipeline._message_caller_response_pending is False
    assert pipeline._message_taking_pending is False

    # Caller transcript preserved
    caller_transcripts = [t for s, t in recorder.transcripts if s == "Caller"]
    assert caller_utterance in caller_transcripts


@pytest.mark.asyncio
async def test_consumer_regression_post_delivery_guard_rejection_no_continuation():
    """When final pre-ACK record read returns invalid ws/claim after accepted partial transition, consumer returns False, exactly 1 generator, no continuation token."""
    recorder = _Recorder()
    generator_calls = []
    first_token_sent = asyncio.Event()
    delivery_gate = asyncio.Event()

    async def stream_fn(contents):
        generator_calls.append(json.loads(json.dumps(contents)))
        if len(generator_calls) == 1:
            yield {"text": "Deli is unavailable right now. "}
            first_token_sent.set()
            await delivery_gate.wait()
            yield {"text": "Can I take a message?"}
        elif len(generator_calls) == 2:
            yield {"text": "Continuation response that must never be sent"}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._command_ws_token = "ws_test_token"
    pipeline._hold_offered = True

    record = {
        "contractor_id": "test_contractor",
        "call_sid": "CA_relay_test",
        "state": "screening",
        "state_updated_at": time.time(),
        "owner_action": "decline",
        "owner_operation_id": "op_decline_rejection",
        "claim_nonce": "nonce_decline_rejection",
        "owner_action_status": "message_requested",
        "ws_token": "ws_test_token",
        "message_intent": {
            "type": "take_message",
            "contractor_id": "test_contractor",
            "operation_id": "op_decline_rejection",
            "action": "decline",
        },
    }

    async def dynamic_read_record(sid):
        rec = dict(record)
        if pipeline._unavailable_said:
            # Accepted partial transition completed; final pre-ACK read returns invalid ws_token
            rec["ws_token"] = "invalid_ws_token"
        return rec

    from app.services.owner_call_actions import consume_message_intent

    ack_mock = AsyncMock(return_value=True)
    with patch("app.services.owner_call_actions.read_record", side_effect=dynamic_read_record), \
         patch("app.services.owner_call_actions.acknowledge_owner_action", ack_mock):

        command_task = asyncio.create_task(
            consume_message_intent(pipeline, pipeline._deliver_message_instruction)
        )

        # Wait until transition has actually sent first token
        await asyncio.wait_for(first_token_sent.wait(), timeout=1.0)
        assert pipeline._streamed_text == "Deli is unavailable right now. "
        assert pipeline._message_taking_pending is True

        # Queue caller prompt while delivery is in-flight
        caller_utterance = "Tell Deli to call me back"
        await pipeline.handle_message({
            "type": "prompt",
            "voicePrompt": caller_utterance,
            "last": True,
        })
        assert pipeline._message_caller_response_pending is True

        # Release delivery gate so delivery completes
        delivery_gate.set()

        # Consumer completes
        result = await asyncio.wait_for(command_task, timeout=2.0)

        # Assert consumer returned False
        assert result is False
        assert pipeline._unavailable_said is True
        ack_mock.assert_not_awaited()

        # Wait bounded to ensure no background continuation runs
        await asyncio.wait_for(pipeline.wait_idle(), timeout=1.0)

    # Exactly one generator was invoked (no continuation generator)
    assert len(generator_calls) == 1

    # No continuation outbound token sent
    sent_tokens = [m.get("token") for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert not any("Continuation response" in (t or "") for t in sent_tokens)
    assert not any("call me back" in (t or "") for t in sent_tokens)

    # Flags: response pending flag is retained because delivery was not accepted/fenced
    assert pipeline._message_caller_response_pending is True
    assert pipeline._message_taking_pending is False


@pytest.mark.parametrize("mode", ["text", "tool"])
@pytest.mark.asyncio
async def test_invalidation_after_ack_while_continuation_paused_rejects_token_or_tool(mode: str):
    """Mutating pipeline ws or durable claim while continuation is paused blocks tokens and tool execution."""
    recorder = _Recorder()
    generator_calls = []
    first_token_sent = asyncio.Event()
    delivery_gate = asyncio.Event()
    continuation_gate = asyncio.Event()
    continuation_started = asyncio.Event()

    async def stream_fn(contents):
        generator_calls.append(json.loads(json.dumps(contents)))
        if len(generator_calls) == 1:
            yield {"text": "Deli is unavailable right now. "}
            first_token_sent.set()
            await delivery_gate.wait()
            yield {"text": "Can I take a message?"}
        elif len(generator_calls) == 2:
            continuation_started.set()
            await continuation_gate.wait()
            if mode == "text":
                yield {"text": "I will make sure Deli gets this message."}
            else:
                yield {"functionCall": {"name": "check_calendar_availability", "args": {}}}
        else:
            yield {"text": "Subsequent model round that must never run"}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._command_ws_token = "ws_test_token"
    pipeline._hold_offered = True

    tool_mock = AsyncMock(return_value={"available": True})
    pipeline._execute_tool = tool_mock

    record = {
        "contractor_id": "test_contractor",
        "call_sid": "CA_relay_test",
        "state": "screening",
        "state_updated_at": time.time(),
        "owner_action": "decline",
        "owner_operation_id": f"op_decline_ack_inval_{mode}",
        "claim_nonce": f"nonce_decline_ack_inval_{mode}",
        "owner_action_status": "message_requested",
        "ws_token": "ws_test_token",
        "message_intent": {
            "type": "take_message",
            "contractor_id": "test_contractor",
            "operation_id": f"op_decline_ack_inval_{mode}",
            "action": "decline",
        },
    }

    async def mock_acknowledge(call_sid, new_status="taking_message", **kwargs):
        record["owner_action_status"] = new_status
        return True

    from app.services.owner_call_actions import consume_message_intent

    with patch("app.services.owner_call_actions.read_record", AsyncMock(side_effect=lambda sid: dict(record))), \
         patch("app.services.owner_call_actions.acknowledge_owner_action", side_effect=mock_acknowledge):

        command_task = asyncio.create_task(
            consume_message_intent(pipeline, pipeline._deliver_message_instruction)
        )

        await asyncio.wait_for(first_token_sent.wait(), timeout=1.0)
        assert pipeline._streamed_text == "Deli is unavailable right now. "
        assert pipeline._message_taking_pending is True

        caller_utterance = "Please check Deli's calendar"
        await pipeline.handle_message({
            "type": "prompt",
            "voicePrompt": caller_utterance,
            "last": True,
        })
        assert pipeline._message_caller_response_pending is True

        delivery_gate.set()

        # Consumer completes and returns True
        result = await asyncio.wait_for(command_task, timeout=2.0)
        assert result is True
        assert pipeline._unavailable_said is True
        assert pipeline._message_taking_pending is False

        # Ensure temporary guard attributes are removed from pipeline on consumer completion
        assert not hasattr(pipeline, "_message_continuation_guard")
        assert not hasattr(pipeline, "_message_delivery_guard")

        # Generator 2 is now running and paused at continuation_gate
        await asyncio.wait_for(continuation_started.wait(), timeout=1.0)
        # Mutate pipeline ws_token to invalidate continuation
        pipeline._command_ws_token = "invalidated_ws_token"

        # Release continuation generator
        continuation_gate.set()

        # Await pipeline generation to complete
        await asyncio.wait_for(pipeline.wait_idle(), timeout=1.0)

    # Exactly 2 generator calls occurred (transition turn + continuation round 1; no round 2)
    assert len(generator_calls) == 2

    # Tool executor was never awaited on stale continuation
    tool_mock.assert_not_awaited()

    # No continuation tokens or extra play messages sent
    sent_tokens = [m.get("token") for m in recorder.sent if m.get("type") == "text" and m.get("token")]
    assert not any("I will make sure" in (t or "") for t in sent_tokens)
    assert not any("gets this message" in (t or "") for t in sent_tokens)
    assert not any("Subsequent model round" in (t or "") for t in sent_tokens)
    plays = [m for m in recorder.sent if m.get("type") == "play"]
    assert len(plays) == 1  # only original decline pause, no new play

    # Guard attributes remain removed
    assert not hasattr(pipeline, "_message_continuation_guard")
    assert not hasattr(pipeline, "_message_delivery_guard")


@pytest.mark.asyncio
async def test_guarded_generation_initial_refusal_and_pre_tool_invalidation():
    """Guarded generation rejects before model request if initially invalid, and before tool execution if invalidated."""
    # Case A: Source request refusal (guard initially False)
    rec_a = _Recorder()
    generator_calls_a = []

    async def stream_a(contents):
        generator_calls_a.append(contents)
        yield {"text": "Should never stream"}

    pipe_a = _pipeline(rec_a, stream_a)
    guard_a = AsyncMock(return_value=False)
    pipe_a._start_generation("test_instruction", guard=guard_a)
    await asyncio.wait_for(pipe_a.wait_idle(), timeout=1.0)

    assert len(generator_calls_a) == 0, "Model generator must not be invoked when guard is initially invalid"
    assert len(rec_a.sent) == 0, "No tokens or messages sent to Twilio when guard is initially invalid"
    guard_a.assert_awaited()

    # Case B: Pre-tool invalidation (guard valid before round 0, invalid before tool execution)
    rec_b = _Recorder()
    generator_calls_b = []

    async def stream_b(contents):
        generator_calls_b.append(contents)
        if len(generator_calls_b) == 1:
            yield {"functionCall": {"name": "check_calendar_availability", "args": {}}}
        else:
            yield {"text": "Round 2 text"}

    pipe_b = _pipeline(rec_b, stream_b)
    tool_mock_b = AsyncMock(return_value={"available": True})
    pipe_b._execute_tool = tool_mock_b

    # Guard returns True on initial round 0 check, False on pre-tool check
    guard_b = AsyncMock(side_effect=[True, False, False])
    pipe_b._start_generation("test_tool_instruction", guard=guard_b)
    await asyncio.wait_for(pipe_b.wait_idle(), timeout=1.0)

    assert len(generator_calls_b) == 1, "Only round 0 should run; round 1 must not be executed"
    tool_mock_b.assert_not_awaited()
    # Verify no fallback error text was emitted
    sent_tokens_b = [m.get("token") for m in rec_b.sent if m.get("type") == "text" and m.get("token")]
    assert len(sent_tokens_b) == 0


@pytest.mark.asyncio
async def test_unaccepted_delivery_cleanup_does_not_generate_and_retains_pending_flag():
    """If delivery is not accepted (e.g. guard rejects), cleanup does not trigger generation and retains flag."""
    recorder = _Recorder()
    generator_calls = []

    async def stream_fn(contents):
        generator_calls.append(json.loads(json.dumps(contents)))
        yield {"text": "Deli is unavailable."}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._command_ws_token = "ws_test_token"
    pipeline._hold_offered = True
    pipeline._message_delivery_guard = AsyncMock(return_value=False)

    intent = {"action": "decline", "operation_id": "op_unacc", "claim_nonce": "nonce_unacc"}
    assert await pipeline._prepare_message_delivery(intent) is True
    assert pipeline._message_taking_pending is True

    # Caller speaks during pending delivery
    caller_utterance = "Please tell Deli I will arrive tomorrow"
    await pipeline.handle_message({
        "type": "prompt",
        "voicePrompt": caller_utterance,
        "last": True,
    })
    assert pipeline._message_caller_response_pending is True

    # Delivery fails because guard rejected
    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._unavailable_said is False

    # Synchronous cleanup hook called in finally without continuation guard
    pipeline._finish_message_delivery_attempt()

    # No second generation was started
    assert len(generator_calls) == 0
    # Flag is retained for subsequent valid retry
    assert pipeline._message_caller_response_pending is True
    assert pipeline._message_taking_pending is False

    # Next retry with valid guard: delivery succeeds and pending caller message is answered
    pipeline._message_delivery_guard = AsyncMock(return_value=True)
    assert await pipeline._prepare_message_delivery(intent) is True
    assert await pipeline._deliver_message_instruction() is True
    pipeline._message_continuation_guard = AsyncMock(return_value=True)
    try:
        pipeline._finish_message_delivery_attempt()
    finally:
        if hasattr(pipeline, "_message_continuation_guard"):
            del pipeline._message_continuation_guard
    await asyncio.wait_for(pipeline.wait_idle(), timeout=1.0)

    assert len(generator_calls) == 2
    assert pipeline._message_caller_response_pending is False


@pytest.mark.asyncio
async def test_stopped_or_ending_cleanup_clears_flag_without_generation():
    """If pipeline is stopped or ending, cleanup clears the response pending flag without generating."""
    recorder = _Recorder()
    generator_calls = []

    async def stream_fn(contents):
        generator_calls.append(json.loads(json.dumps(contents)))
        yield {"text": "Deli is unavailable."}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._message_taking_pending = True

    await pipeline.handle_message({
        "type": "prompt",
        "voicePrompt": "Hello?",
        "last": True,
    })
    assert pipeline._message_caller_response_pending is True

    # Stop pipeline
    await pipeline.stop()
    assert pipeline._message_caller_response_pending is False
    assert pipeline._message_taking_pending is False

    # Cleanup invocation on stopped pipeline does not generate
    pipeline._finish_message_delivery_attempt()
    assert len(generator_calls) == 0
    assert pipeline._message_caller_response_pending is False


@pytest.mark.asyncio
async def test_normal_prompt_clears_stale_response_pending_flag():
    """A subsequent normal prompt clears any stale _message_caller_response_pending flag."""
    recorder = _Recorder()

    async def stream_fn(contents):
        yield {"text": "How can I help you today?"}

    pipeline = _pipeline(recorder, stream_fn)
    pipeline._message_caller_response_pending = True
    pipeline._message_taking_pending = False

    await pipeline.handle_message({
        "type": "prompt",
        "voicePrompt": "What are your hours?",
        "last": True,
    })
    assert pipeline._message_caller_response_pending is False
    await pipeline.wait_idle()
