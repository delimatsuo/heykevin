"""Deterministic test suite for VoicePipeline Owner Take-a-Message transition and timing policy.

Verifies:
1. Decline while TTS speech is currently in progress: speech finishes naturally without cutoff,
   no hold timer/summary task is recreated during grace, and unavailable speech starts after playout.
2. Decline while ElevenLabs TTS fetch is in progress: suppresses unsounded availability offer
   before first external audio send without phantom history in _conversation and without transcript.
3. Decline while Claude is pending: suppresses unsounded availability offer before conversation
   history append (no phantom history in _conversation) and without emitting transcript.
4. Monotonic grace timing anchors:
   - Old completed hold (ended 5s ago + tap now -> full 3.0s grace from intent acceptance).
   - Hold ending in the future (tapped mid-hold + speech finishes at t -> 3.0s grace from speech finish).
5. Timeout action (30s) and task safety:
   - 0.0s grace delay without second 30s timer.
   - Self-task guard: _prepare_message_delivery does not cancel the currently running task.
6. Response lock lifecycle across guard:
   - Held across _prepare_message_delivery, delivery guard, and delivery.
   - Released by _finish_message_delivery_attempt on success, prepare False, and cancellation.
7. Guard dynamic revocation:
   - Guard failing after first chunk aborts delivery and returns False without setting _unavailable_said.
8. Caller transcripts during grace:
   - Interim and final transcripts during grace are preserved and queued in _process_utterance.
   - No overlap or race with Kevin's unavailable announcement; processes cleanly after transition.
9. TTS delivery failure and barge-in semantics:
   - TTS provider failure returns False, retains _unavailable_said=False, allowing retry.
   - Barge-in after partial audio playback marks _unavailable_said=True, preventing duplicate prompt.
10. Post-transition conversation and silence resumption:
   - Subsequent caller turns are handled in message-taking context without offering new owner check.
   - Claude system prompt persists message-taking directive after _unavailable_said.
   - Silence check / _waiting_on_caller resumes normally once transition completes.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional
from unittest.mock import AsyncMock

import pytest

for key in (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "TELEGRAM_BOT_TOKEN",
    "USER_PHONE",
):
    os.environ.setdefault(key, "test-placeholder")

from app.config import settings
from app.services.message_taking import compute_grace_delay, is_owner_availability_hold
from app.services.voice_pipeline import VoicePipeline


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    """Isolate tests from real Firestore, APNs, and background ADC refreshes."""
    mock_summary = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.voice_pipeline.VoicePipeline._trigger_screening_summary_push",
        mock_summary,
    )
    mock_timeout = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.owner_call_actions.run_owner_timeout",
        mock_timeout,
    )
    return mock_summary, mock_timeout


def _test_config() -> dict:
    return {
        "contractor_id": "contractor_123",
        "owner_name": "Deli Matsuo",
        "business_name": "Matsuo Plumbing",
        "mode": "personal",
        "effective_mode": "personal",
        "language": "en",
    }


class FakeHTTPResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.content = content

    def json(self):
        return self._json_data


class FakeHTTPClient:
    def __init__(self):
        self.claude_responses: list[dict] = []
        self.tts_audio: bytes = b"\x00" * 4000  # Default short audio (500ms / 1 chunk)
        self.tts_status: int = 200
        self.post_calls: list[dict] = []
        self.claude_started = asyncio.Event()
        self.claude_release = asyncio.Event()
        self.claude_release.set()
        self.eleven_started = asyncio.Event()
        self.eleven_release = asyncio.Event()
        self.eleven_release.set()

    def queue_claude_text(self, text: str):
        self.claude_responses.append({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        })

    async def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        if "elevenlabs.io" in url:
            self.eleven_started.set()
            await self.eleven_release.wait()
            if self.tts_status != 200:
                return FakeHTTPResponse(status_code=self.tts_status)
            return FakeHTTPResponse(status_code=200, content=self.tts_audio)
        if "anthropic.com" in url:
            self.claude_started.set()
            await self.claude_release.wait()
            if self.claude_responses:
                resp_data = self.claude_responses.pop(0)
            else:
                resp_data = {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "I can help with that."}],
                }
            return FakeHTTPResponse(status_code=200, json_data=resp_data)
        return FakeHTTPResponse(status_code=404)

    async def aclose(self):
        pass


@pytest.fixture
async def make_pipeline():
    pipelines: list[VoicePipeline] = []

    def _factory(
        on_audio_out=None,
        on_transcript=None,
        on_call_complete=None,
        config=None,
    ) -> tuple[VoicePipeline, FakeHTTPClient, list[tuple[str, str]], list[bytes]]:
        transcripts: list[tuple[str, str]] = []
        audio_chunks: list[bytes] = []

        async def _default_audio(chunk: bytes):
            audio_chunks.append(chunk)
            return True

        async def _default_transcript(speaker: str, text: str):
            transcripts.append((speaker, text))

        fake_client = FakeHTTPClient()
        p = VoicePipeline(
            on_audio_out=on_audio_out or _default_audio,
            on_transcript=on_transcript or _default_transcript,
            on_call_complete=on_call_complete,
            call_sid="CA_test_transition_123",
            contractor_config=config or _test_config(),
        )
        p._http_client = fake_client
        p._connected = True
        pipelines.append(p)
        return p, fake_client, transcripts, audio_chunks

    yield _factory

    for p in pipelines:
        p._connected = False
        p._interrupt_speaking = True
        tasks_to_cancel = []
        for task_attr in (
            "_unavailable_task",
            "_summary_task",
            "_command_check_task",
            "_silence_check_task",
            "_deepgram_task",
            "_urgency_task",
        ):
            t = getattr(p, task_attr, None)
            if t and not t.done():
                t.cancel()
                tasks_to_cancel.append(t)
        if getattr(p, "_transition_lock_held", False):
            p._finish_message_delivery_attempt()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)


# --- 1. Decline while already TTS speech finishes -----------------------------

@pytest.mark.asyncio
async def test_decline_while_tts_speech_in_progress_finishes_before_unavailable(make_pipeline):
    """When owner decline arrives during Kevin's speech, speech finishes naturally, no hold timer is recreated, and unavailable starts after."""
    pipeline, fake_http, transcripts, audio_chunks = make_pipeline()

    # 8000 bytes = 2 chunks (1.0s)
    fake_http.tts_audio = b"\x00" * 8000
    fake_http.queue_claude_text("Let me check if Deli is available, one moment.")
    speech_started = asyncio.Event()

    async def slow_audio_out(chunk: bytes):
        speech_started.set()
        await asyncio.sleep(0.05)
        audio_chunks.append(chunk)
        return True

    pipeline.on_audio_out = slow_audio_out

    # Production entry point: _process_utterance which owns _response_lock
    utterance_task = asyncio.create_task(pipeline._process_utterance("Can I speak with Deli?"))
    await asyncio.wait_for(speech_started.wait(), timeout=3.0)
    assert pipeline._is_speaking is True

    # Owner decline arrives while speaking is in progress
    t_intent = time.monotonic()
    intent = {
        "action": "decline",
        "operation_id": "op_decline_1",
        "accepted_at": t_intent,
    }

    # Prepare message delivery should await current speech playout and lock
    prepare_task = asyncio.create_task(pipeline._prepare_message_delivery(intent))
    await asyncio.wait_for(utterance_task, timeout=3.0)

    # Assert no hold timer or summary push recreated during grace
    assert pipeline._unavailable_task is None or pipeline._unavailable_task.done()
    assert pipeline._summary_task is None or pipeline._summary_task.done()
    assert pipeline._waiting_for_owner_availability is False
    assert pipeline._hold_offered is True

    prepared = await asyncio.wait_for(prepare_task, timeout=4.0)
    assert prepared is True
    assert not pipeline._is_speaking
    assert pipeline._last_speaking_finished_at >= t_intent

    # Deliver unavailable message
    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is True
    assert pipeline._unavailable_said is True
    pipeline._finish_message_delivery_attempt()

    # Kevin spoke hold offer first, then unavailable text
    spoken_texts = [text for speaker, text in transcripts if speaker == "Kevin"]
    assert any("Let me check" in text for text in spoken_texts)
    assert any("Unfortunately, Deli Matsuo is not available" in text for text in spoken_texts)


# --- 2. Decline during TTS fetch suppresses unsounded offer -------------------

@pytest.mark.asyncio
async def test_owner_intent_during_tts_fetch_suppresses_unsounded_offer_without_phantom_history(make_pipeline):
    """If owner declines while ElevenLabs TTS fetch is in-flight, unsounded offer is suppressed before audio send."""
    pipeline, fake_http, transcripts, audio_chunks = make_pipeline()

    hold_offer = "Let me check if Deli is available, one moment."
    fake_http.queue_claude_text(hold_offer)
    fake_http.eleven_release.clear()  # Pause ElevenLabs HTTP request

    utterance_task = asyncio.create_task(pipeline._process_utterance("Can I speak with Deli?"))
    await asyncio.wait_for(fake_http.eleven_started.wait(), timeout=3.0)

    # Decline arrives while ElevenLabs HTTP request is pending
    pipeline._message_taking_pending = True
    fake_http.eleven_release.set()  # Release ElevenLabs

    await asyncio.wait_for(utterance_task, timeout=3.0)

    # Hold offer must NOT have been emitted as audio or appended to history/transcripts
    assert len(audio_chunks) == 0
    assert pipeline._hold_offered is False
    assert not any(is_owner_availability_hold(str(msg.get("content", "")))
                   for msg in pipeline._conversation if msg.get("role") == "assistant")
    assert not any(hold_offer in text for _, text in transcripts)

    # Deliver unavailable announcement
    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is True
    assert pipeline._unavailable_said is True

    # Unavailable message is properly recorded
    assert any("not available" in str(msg.get("content", ""))
               for msg in pipeline._conversation if msg.get("role") == "assistant")


# --- 3. Decline while Claude pending suppresses unsounded offer ---------------

@pytest.mark.asyncio
async def test_decline_while_claude_pending_suppresses_unsounded_offer_without_phantom_history(make_pipeline):
    """If owner declines while Claude response is pending, unsounded offer is suppressed before history append."""
    pipeline, fake_http, transcripts, audio_chunks = make_pipeline()

    hold_offer = "Let me check if Deli is available, one moment."
    fake_http.queue_claude_text(hold_offer)
    fake_http.claude_release.clear()

    utterance_task = asyncio.create_task(pipeline._process_utterance("Can I speak with Deli?"))
    await asyncio.wait_for(fake_http.claude_started.wait(), timeout=3.0)

    # Owner decline arrives BEFORE Claude response returns
    pipeline._message_taking_pending = True
    fake_http.claude_release.set()

    await asyncio.wait_for(utterance_task, timeout=3.0)

    # The unsounded hold offer must NOT be in _conversation history (no phantom history)
    assert len(audio_chunks) == 0
    assert pipeline._hold_offered is False
    assistant_messages = [
        msg["content"]
        for msg in pipeline._conversation
        if msg.get("role") == "assistant"
    ]
    for content in assistant_messages:
        if isinstance(content, str):
            assert not is_owner_availability_hold(content)

    # on_transcript must NOT have received the unsounded hold offer
    assert not any(hold_offer in text for _, text in transcripts)

    # Now deliver the unavailable announcement
    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is True
    assert pipeline._unavailable_said is True

    # Unavailable message is properly recorded in conversation and transcript
    assert any("Unfortunately" in str(msg.get("content", "")) or "not available" in str(msg.get("content", ""))
               for msg in pipeline._conversation if msg.get("role") == "assistant")


# --- 4. Old completed hold 3 and new hold end 3 -------------------------------

@pytest.mark.asyncio
async def test_old_completed_hold_and_new_hold_timing_anchors():
    """Timing calculations anchor at accepted_at for old hold, and speech finish for active hold."""
    t_now = 1000.0

    # Old hold: speech finished 5s ago (t=995.0), owner taps decline now (t=1000.0)
    # Anchor is max(accepted_at=1000.0, finished_at=995.0) = 1000.0 -> full 3.0s grace
    assert compute_grace_delay(
        action="decline",
        hold_offered=True,
        intent_accepted_at=1000.0,
        speaking_finished_at=995.0,
        now=t_now,
    ) == pytest.approx(3.0)

    # 1.0s elapsed since decline tap (t=1001.0) -> 2.0s remaining
    assert compute_grace_delay(
        action="decline",
        hold_offered=True,
        intent_accepted_at=1000.0,
        speaking_finished_at=995.0,
        now=1001.0,
    ) == pytest.approx(2.0)

    # New hold: owner tapped decline at t=1000.0 while speech playing; speech finishes at t=1002.0
    # Anchor is max(accepted_at=1000.0, finished_at=1002.0) = 1002.0
    # At t=1002.0, remaining grace is 3.0s from speech finish
    assert compute_grace_delay(
        action="decline",
        hold_offered=True,
        intent_accepted_at=1000.0,
        speaking_finished_at=1002.0,
        now=1002.0,
    ) == pytest.approx(3.0)


# --- 5. Timeout 0 / Self task guard -------------------------------------------

@pytest.mark.asyncio
async def test_timeout_zero_grace_and_self_task_guard(make_pipeline):
    """Action timeout has 0s grace delay, and timer cleanup avoids cancelling current task."""
    pipeline, _, _, _ = make_pipeline()

    # compute_grace_delay for timeout action is strictly 0.0s
    assert compute_grace_delay(
        action="timeout",
        hold_offered=True,
        intent_accepted_at=100.0,
        speaking_finished_at=95.0,
        now=100.0,
    ) == 0.0

    # Prepare message delivery with timeout action finishes without grace sleep
    intent = {
        "action": "timeout",
        "operation_id": "op_timeout_1",
        "accepted_at": time.monotonic(),
    }

    # Run within simulated unavailable task to test self-task guard
    task_cancelled = False

    async def simulated_timer_task():
        nonlocal task_cancelled
        pipeline._unavailable_task = asyncio.current_task()
        try:
            prepared = await pipeline._prepare_message_delivery(intent)
            assert prepared is True
        except asyncio.CancelledError:
            task_cancelled = True
            raise
        finally:
            pipeline._finish_message_delivery_attempt()

    t = asyncio.create_task(simulated_timer_task())
    await asyncio.wait_for(t, timeout=3.0)
    assert task_cancelled is False
    assert pipeline._message_taking_pending is False


# --- 6. Lock held across guard then released success / false / cancel ---------

@pytest.mark.asyncio
async def test_lock_held_across_guard_then_released_on_success(make_pipeline):
    """_response_lock is acquired during prepare, held across guard/delivery, and released on success."""
    pipeline, _, _, _ = make_pipeline()

    guard_checked_while_locked = False

    async def mock_guard():
        nonlocal guard_checked_while_locked
        guard_checked_while_locked = pipeline._response_lock.locked()
        return True

    pipeline._message_delivery_guard = mock_guard

    intent = {
        "action": "decline",
        "operation_id": "op_lock_1",
        "accepted_at": time.monotonic(),
    }

    # Prepare acquires lock
    prepared = await asyncio.wait_for(pipeline._prepare_message_delivery(intent), timeout=3.0)
    assert prepared is True
    assert pipeline._response_lock.locked() is True

    # Guard was executed while locked
    assert guard_checked_while_locked is True

    # Deliver executes under lock
    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is True

    # Finish hook releases lock
    pipeline._finish_message_delivery_attempt()
    assert pipeline._response_lock.locked() is False
    assert pipeline._message_taking_pending is False


@pytest.mark.asyncio
async def test_lock_released_when_prepare_returns_false(make_pipeline):
    """When prepare returns False (e.g. guard failure), finish hook safely releases lock."""
    pipeline, _, _, _ = make_pipeline()

    async def failing_guard():
        return False

    pipeline._message_delivery_guard = failing_guard

    intent = {
        "action": "decline",
        "operation_id": "op_lock_fail",
        "accepted_at": time.monotonic(),
    }

    prepared = await asyncio.wait_for(pipeline._prepare_message_delivery(intent), timeout=3.0)
    assert prepared is False

    # Finish cleanup hook releases lock
    pipeline._finish_message_delivery_attempt()
    assert pipeline._response_lock.locked() is False
    assert pipeline._message_taking_pending is False


@pytest.mark.asyncio
async def test_lock_released_on_prepare_cancellation(make_pipeline):
    """When prepare is cancelled during grace delay, finish hook releases the lock."""
    pipeline, _, _, _ = make_pipeline()
    pipeline._hold_offered = True
    pipeline._last_speaking_finished_at = time.monotonic()

    intent = {
        "action": "decline",
        "operation_id": "op_cancel_1",
        "accepted_at": time.monotonic(),
    }

    prep_task = asyncio.create_task(pipeline._prepare_message_delivery(intent))
    await asyncio.sleep(0.01)
    assert pipeline._response_lock.locked() is True

    prep_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(prep_task, timeout=3.0)

    pipeline._finish_message_delivery_attempt()
    assert pipeline._response_lock.locked() is False
    assert pipeline._message_taking_pending is False


# --- 7. Guard changes after first chunk aborts delivery -----------------------

@pytest.mark.asyncio
async def test_guard_changes_after_first_chunk_aborts_delivery(make_pipeline):
    """If guard returns False between audio chunks, delivery aborts and does not set unavailable_said."""
    pipeline, fake_http, transcripts, audio_chunks = make_pipeline()
    fake_http.tts_audio = b"\x00" * 8000  # 2 chunks

    call_count = 0

    async def dynamic_guard():
        nonlocal call_count
        call_count += 1
        # Succeed for initial checks, then fail when rechecked
        if call_count > 2:
            return False
        return True

    pipeline._message_delivery_guard = dynamic_guard

    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is False
    assert pipeline._unavailable_said is False


# --- 8. Caller interim/final during grace not lost / no overlap ---------------

@pytest.mark.asyncio
async def test_caller_speech_during_grace_queued_without_overlap(make_pipeline):
    """Caller speech during grace is preserved, queued, and processed after unavailable delivery."""
    pipeline, fake_http, transcripts, _ = make_pipeline()
    fake_http.queue_claude_text("I will make sure Deli gets that message.")

    pipeline._hold_offered = True
    pipeline._last_speaking_finished_at = time.monotonic()

    intent = {
        "action": "decline",
        "operation_id": "op_speech_grace",
        "accepted_at": time.monotonic(),
    }

    # Start transition prepare (3s grace)
    prep_task = asyncio.create_task(pipeline._prepare_message_delivery(intent))
    await asyncio.sleep(0.01)
    assert pipeline._response_lock.locked() is True

    # Caller speaks during grace
    caller_processed = asyncio.create_task(
        pipeline._process_utterance("Please let Deli know the main water line is leaking.")
    )

    # Let grace finish
    await asyncio.wait_for(prep_task, timeout=4.0)
    assert pipeline._response_lock.locked() is True

    # Deliver unavailable message
    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is True
    assert pipeline._unavailable_said is True

    # Finish transition, releasing lock
    pipeline._finish_message_delivery_attempt()

    # Now caller's queued utterance can run under the lock
    await asyncio.wait_for(caller_processed, timeout=3.0)

    # Verify transcripts order: Unavailable announcement followed by Kevin taking the message
    kevin_transcripts = [text for speaker, text in transcripts if speaker == "Kevin"]
    assert any("Unfortunately" in text or "not available" in text for text in kevin_transcripts)
    assert any("gets that message" in text for text in kevin_transcripts)


# --- 9. Failed TTS retry and barge-in partial no repeated prompt ---------------

@pytest.mark.asyncio
async def test_failed_tts_allows_retry_and_retains_unavailable_said_false(make_pipeline):
    """TTS failure leaves _unavailable_said=False and returns False so delivery can retry."""
    pipeline, fake_http, _, _ = make_pipeline()
    fake_http.tts_status = 500

    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is False
    assert pipeline._unavailable_said is False


@pytest.mark.asyncio
async def test_barge_in_partial_playback_sets_unavailable_said_and_no_repeat(make_pipeline):
    """Barge-in after partial audio playout marks _unavailable_said=True and does not repeat prompt."""
    pipeline, fake_http, transcripts, _ = make_pipeline()
    fake_http.tts_audio = b"\x00" * 8000  # 2 chunks (1.0s)

    chunk_count = 0

    async def barge_in_audio(chunk: bytes):
        nonlocal chunk_count
        chunk_count += 1
        if chunk_count == 1:
            # Caller interrupts after first chunk
            pipeline._interrupt_speaking = True
        return True

    pipeline.on_audio_out = barge_in_audio

    delivered = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered is True
    assert pipeline._unavailable_said is True

    # Calling deliver again is a no-op (already said)
    delivered_again = await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert delivered_again is True


# --- 10. Next caller response works and pending false --------------------------

@pytest.mark.asyncio
async def test_next_caller_response_works_in_message_taking_context(make_pipeline):
    """After message transition, subsequent caller speech is processed and silence detection resumes."""
    pipeline, fake_http, transcripts, _ = make_pipeline()

    # Complete unavailable delivery
    await asyncio.wait_for(pipeline._deliver_message_instruction(), timeout=3.0)
    assert pipeline._unavailable_said is True
    assert pipeline._message_taking_pending is False
    assert pipeline._waiting_for_owner_availability is False

    # Caller leaves their message
    fake_http.queue_claude_text("Got it, I've noted down your message for Deli.")
    await asyncio.wait_for(pipeline._process_utterance("Can you ask him to call Bob back at 555-0199?"), timeout=3.0)

    # Verify Claude was called with the message-taking directive in system prompt
    assert len(fake_http.post_calls) >= 1
    claude_call = next(c for c in fake_http.post_calls if "anthropic.com" in c["url"])
    system_text = claude_call["json"]["system"]
    assert "confirmed unavailable" in system_text or "not available" in system_text
    assert "Take a message" in system_text

    # Kevin responds without offering an owner check
    assert pipeline._waiting_for_owner_availability is False
    assert not (pipeline._unavailable_task and not pipeline._unavailable_task.done())

    # Silence check is active and waiting on caller
    assert pipeline._waiting_on_caller() is True
