"""ConversationRelay engine tests: protocol behavior with a stubbed LLM.

The RelayPipeline owns conversation logic only — Twilio owns audio. These
tests pin the WebSocket contract (token streaming with last flags), history
handling under interruption, urgency escalation, goodbye teardown, tool
round-trips, and the TwiML engine switch.
"""

import asyncio
import json

import pytest

from app.services.relay_pipeline import RelayPipeline, build_greeting_text


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


def _pipeline(recorder: _Recorder, parts_script, **overrides) -> RelayPipeline:
    """parts_script: list of lists — one list of parts per generate call."""
    calls = {"n": 0}

    async def fake_stream(contents):
        index = min(calls["n"], len(parts_script) - 1)
        calls["n"] += 1
        for part in parts_script[index]:
            yield part

    return RelayPipeline(
        contractor_config=_contractor(**overrides),
        call_sid="CA_relay_test",
        caller_phone="+15550001111",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        on_urgency_detected=recorder.on_urgency,
        on_call_complete=recorder.on_complete,
        stream_generate=fake_stream,
    )


async def _drive(pipeline: RelayPipeline, message: dict) -> None:
    """Deliver a message and wait for the (task-based) generation to finish."""
    await pipeline.handle_message(message)
    await pipeline.wait_idle()


@pytest.mark.asyncio
async def test_prompt_streams_tokens_and_closes_turn():
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder, [[{"text": "We can help "}, {"text": "with that."}]]
    )

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "My toilet is broken", "lang": "en-US", "last": True}
    )

    text_messages = [m for m in recorder.sent if m["type"] == "text"]
    assert [m["token"] for m in text_messages] == ["We can help ", "with that.", ""]
    assert [m["last"] for m in text_messages] == [False, False, True]
    assert ("Caller", "My toilet is broken") in recorder.transcripts
    assert ("Kevin", "We can help with that.") in recorder.transcripts


@pytest.mark.asyncio
async def test_interim_prompts_are_ignored():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "hi"}]])

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "My toi", "lang": "en-US", "last": False}
    )

    assert recorder.sent == []
    assert recorder.transcripts == []


@pytest.mark.asyncio
async def test_language_tracked_from_prompt():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Claro!"}]])

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Hola necesito ayuda", "lang": "es-US", "last": True}
    )

    assert pipeline.language == "es"


@pytest.mark.asyncio
async def test_urgency_keyword_fires_escalation_once():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Stay calm, help is coming."}]])

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "There is a gas leak in my kitchen", "last": True}
    )
    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "I said gas leak!", "last": True}
    )

    assert len(recorder.urgencies) == 1


@pytest.mark.asyncio
async def test_interrupt_truncates_last_kevin_turn():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Our hours are nine to five on weekdays."}]])

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "What are your hours?", "last": True}
    )
    await _drive(pipeline,
        {
            "type": "interrupt",
            "utteranceUntilInterrupt": "Our hours are nine",
            "durationUntilInterruptMs": 1200,
        }
    )

    model_turns = [h for h in pipeline._history if h["role"] == "model"]
    assert model_turns[-1]["parts"][0]["text"] == "Our hours are nine"


@pytest.mark.asyncio
async def test_goodbye_reply_ends_call(monkeypatch):
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "You're welcome. Have a great day!"}]])

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Thanks, that's all", "last": True}
    )

    assert recorder.completed
    assert {"type": "end"} in recorder.sent


@pytest.mark.asyncio
async def test_tool_round_executes_and_continues(monkeypatch):
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [
            [{"functionCall": {"name": "check_calendar", "args": {"day": "tomorrow"}}}],
            [{"text": "Tomorrow at 2pm works."}],
        ],
    )

    executed = {}

    async def fake_execute(self, tool_name, tool_args):
        executed["name"] = tool_name
        executed["args"] = tool_args
        return json.dumps({"available": ["14:00"]})

    from app.services.voice_pipeline import VoicePipeline

    monkeypatch.setattr(VoicePipeline, "_execute_tool", fake_execute)

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Can I book tomorrow?", "last": True}
    )

    assert executed == {"name": "check_calendar", "args": {"day": "tomorrow"}}
    text_messages = [m for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert text_messages[-1]["token"] == "Tomorrow at 2pm works."
    # History carries the functionCall/functionResponse round for context.
    roles = [h["role"] for h in pipeline._history]
    assert roles.count("model") >= 2


@pytest.mark.asyncio
async def test_generate_failure_degrades_gracefully():
    recorder = _Recorder()

    async def broken_stream(contents):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_relay_test",
        send_to_twilio=recorder.send,
        stream_generate=broken_stream,
    )

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Hello?", "last": True}
    )

    text_messages = [m for m in recorder.sent if m["type"] == "text"]
    assert text_messages, "caller must never be left in silence"
    assert text_messages[-1]["last"] is True


def test_greeting_matches_gemini_engine_wording():
    business = build_greeting_text(_contractor(), after_hours=False)
    assert business == (
        "Hi, thank you for calling Test Plumbing. My name is Kevin. "
        "How can I help you?"
    )
    after = build_greeting_text(_contractor(), after_hours=True)
    assert after == "Test Plumbing is currently closed. My name is Kevin. How can I help?"
    personal = build_greeting_text(
        _contractor(effective_mode="personal"), after_hours=False
    )
    assert personal == "Hi, this is Kevin, Deli's assistant. How can I help?"


def test_relay_twiml_engine_switch():
    from app.webhooks.twilio_incoming import _relay_screening_twiml

    twiml = _relay_screening_twiml(
        "CA_test123", "tok_abc", _contractor(voice_engine="relay")
    )

    assert "<ConversationRelay" in twiml
    assert 'url="wss://' in twiml
    assert "/relay-stream/CA_test123" in twiml
    assert '<Language code="multi"' in twiml
    assert '<Parameter name="ws_token" value="tok_abc"' in twiml
    assert '<Parameter name="spoken_greeting"' in twiml
    assert "Kevin" in twiml  # welcomeGreeting present


def test_relay_twiml_greets_a_known_caller_by_name():
    from app.webhooks.twilio_incoming import _relay_screening_twiml

    twiml = _relay_screening_twiml(
        "CA_returning",
        "tok_returning",
        _contractor(
            voice_engine="relay",
            known_caller_name="Jonathan Smith",
            known_caller_name_trusted=True,
        ),
    )

    assert "Hello, Jonathan. How can I help you today?" in twiml
    assert "demo" not in twiml.lower()


def test_relay_history_uses_the_exact_greeting_twilio_already_spoke():
    recorder = _Recorder()
    heard = "Hello, Jonathan. How can I help you today?"

    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_returning",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        spoken_greeting=heard,
    )

    assert pipeline.greeting_text == heard
    assert pipeline._history[0] == {
        "role": "model",
        "parts": [{"text": heard}],
    }


@pytest.mark.asyncio
async def test_returning_customer_tool_uses_transport_call_id(monkeypatch):
    from app.services.receptionist_tools import CANCEL_SERVICE_REQUEST
    from app.services.voice_pipeline import VoicePipeline

    captured = {}

    async def fake_execute(
        self,
        tool_name,
        tool_args,
        *,
        operation_id="",
    ):
        captured.update(
            tool_name=tool_name,
            tool_args=tool_args,
            operation_id=operation_id,
        )
        return json.dumps({"status": "applied"})

    monkeypatch.setattr(VoicePipeline, "_execute_tool", fake_execute)
    recorder = _Recorder()
    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_returning",
        caller_phone="+16175550123",
        send_to_twilio=recorder.send,
    )

    result = await pipeline._execute_tool(
        {
            "id": "provider-call-7",
            "name": CANCEL_SERVICE_REQUEST,
            "args": {"request_id": "request-1", "expected_revision": 1},
        }
    )

    assert result == {"status": "applied"}
    assert captured == {
        "tool_name": CANCEL_SERVICE_REQUEST,
        "tool_args": {"request_id": "request-1", "expected_revision": 1},
        "operation_id": "provider-call-7",
    }


def test_generate_body_disables_thinking_and_uses_configured_model():
    """CA80d3fd regression: model must come from settings (2.5-flash is gated
    for this API project) and thinking must be pinned off — current flash
    models otherwise burn the whole token budget on thoughts and return
    empty text, which the caller hears as Kevin not understanding anything.
    """
    from app.config import settings

    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "ok"}]])

    body = pipeline._build_generate_body([{"role": "user", "parts": [{"text": "hi"}]}])

    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert settings.relay_text_model != "gemini-2.5-flash"
    assert body["generationConfig"]["maxOutputTokens"] > 0
    assert body["system_instruction"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_tool_round_preserves_thought_signature_and_call_id(monkeypatch):
    """CAcae04f regression: Gemini 3.x requires functionCall parts to be
    replayed VERBATIM — including thoughtSignature — or every tool round is
    rejected with 400 'Function call is missing a thought_signature'. The
    functionResponse must also echo the call id when one was assigned.
    """
    recorder = _Recorder()
    captured_contents = []
    calls = {"n": 0}

    scripts = [
        [
            {
                "functionCall": {
                    "name": "check_availability",
                    "args": {"day": "tomorrow"},
                    "id": "call_abc123",
                },
                "thoughtSignature": "SIG_XYZ",
            }
        ],
        [{"text": "Tomorrow at 2pm works."}],
    ]

    async def fake_stream(contents):
        captured_contents.append([json.loads(json.dumps(c)) for c in contents])
        index = min(calls["n"], len(scripts) - 1)
        calls["n"] += 1
        for part in scripts[index]:
            yield part

    async def fake_execute(self, tool_name, tool_args):
        return json.dumps({"available": ["14:00"]})

    from app.services.voice_pipeline import VoicePipeline

    monkeypatch.setattr(VoicePipeline, "_execute_tool", fake_execute)

    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_relay_test",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        stream_generate=fake_stream,
    )

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Can I book tomorrow?", "last": True}
    )

    assert len(captured_contents) == 2
    second_request = captured_contents[1]
    model_turn = second_request[-2]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0]["thoughtSignature"] == "SIG_XYZ"
    assert model_turn["parts"][0]["functionCall"]["id"] == "call_abc123"
    response_turn = second_request[-1]
    assert response_turn["role"] == "user"
    assert response_turn["parts"][0]["functionResponse"]["id"] == "call_abc123"
    assert response_turn["parts"][0]["functionResponse"]["response"] == {
        "available": ["14:00"]
    }


@pytest.mark.asyncio
async def test_new_prompt_supersedes_inflight_generation():
    """CA73dbd1 regression: caller speech during generation must cancel the
    in-flight reply (its spoken partial preserved in history) and answer the
    fuller history — never silently drop the utterance or keep streaming
    stale tokens over the caller.
    """
    recorder = _Recorder()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fake_stream(contents):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"text": "Let me check the sched"}
            await release.wait()  # hangs until cancelled
            yield {"text": "ule for you."}
        else:
            yield {"text": "Yes, 2pm tomorrow works."}

    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_relay_test",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        stream_generate=fake_stream,
    )

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "Do you do toilets?", "last": True}
    )
    await asyncio.sleep(0)  # let the first generation start streaming
    await _drive(
        pipeline,
        {"type": "prompt", "voicePrompt": "Actually just book me tomorrow 2pm", "last": True},
    )

    # The stale turn's partial is in history; the new reply answered.
    model_texts = [
        h["parts"][0].get("text", "")
        for h in pipeline._history
        if h["role"] == "model"
    ]
    assert "Let me check the sched" in model_texts
    assert "Yes, 2pm tomorrow works." in model_texts
    # No tokens from the cancelled turn arrive after the new turn's tokens.
    tokens = [m["token"] for m in recorder.sent if m["type"] == "text"]
    assert "ule for you." not in tokens


@pytest.mark.asyncio
async def test_interrupt_cancels_inflight_and_preserves_spoken_portion():
    recorder = _Recorder()
    release = asyncio.Event()

    async def fake_stream(contents):
        yield {"text": "We are open nine to"}
        await release.wait()  # hangs until cancelled
        yield {"text": " five weekdays."}

    pipeline = RelayPipeline(
        contractor_config=_contractor(),
        call_sid="CA_relay_test",
        send_to_twilio=recorder.send,
        on_transcript=recorder.on_transcript,
        stream_generate=fake_stream,
    )

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "What are your hours?", "last": True}
    )
    await asyncio.sleep(0)
    await _drive(
        pipeline,
        {"type": "interrupt", "utteranceUntilInterrupt": "We are open", "durationUntilInterruptMs": 900},
    )

    model_turns = [h for h in pipeline._history if h["role"] == "model"]
    assert model_turns[-1]["parts"][0]["text"] == "We are open"
    tokens = [m["token"] for m in recorder.sent if m["type"] == "text"]
    assert " five weekdays." not in tokens


# --- goodbye teardown waits for playback ---------------------------------
#
# Twilio documents the events="tokens-played" subscription but not the shape
# of the receipt it sends, so these tests pin the one thing we rely on: a
# receipt ARRIVING means audio is still playing. Nothing reads its contents.


def _fast_teardown(pipeline: RelayPipeline) -> None:
    """Shrink the poll budget so tests don't wait on real playout timing."""
    pipeline.PLAYBACK_QUIET_POLLS = 2
    pipeline.PLAYBACK_FALLBACK_POLLS = 4
    pipeline.PLAYBACK_MAX_POLLS = 20


@pytest.mark.asyncio
async def test_goodbye_waits_while_playback_receipts_keep_arriving(monkeypatch):
    """The abrupt hangup on CA40a9f4: `end` went out while TTS was still playing."""
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Thanks for calling. Goodbye!"}]])
    _fast_teardown(pipeline)

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        # Keep "playing" for the first few polls, then fall silent.
        if polls["n"] <= 6:
            await pipeline.handle_message(
                {"type": "info", "name": "tokensPlayed", "value": "..."}
            )

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "That's all, thanks", "last": True}
    )

    assert {"type": "end"} in recorder.sent
    # Must outlast the receipts rather than firing on a fixed grace.
    assert polls["n"] > 6


@pytest.mark.asyncio
async def test_goodbye_falls_back_to_a_grace_when_no_receipts_arrive(monkeypatch):
    """Contractors whose TwiML lacks the events attribute must still hang up."""
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Have a great day!"}]])
    _fast_teardown(pipeline)

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Bye", "last": True}
    )

    assert {"type": "end"} in recorder.sent
    assert polls["n"] <= pipeline.PLAYBACK_MAX_POLLS


@pytest.mark.asyncio
async def test_goodbye_never_hangs_up_on_endless_receipts(monkeypatch):
    """A receipt stream that never stops must not hold the call open forever."""
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Take care!"}]])
    _fast_teardown(pipeline)

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        await pipeline.handle_message({"type": "info", "name": "tokensPlayed"})

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Bye", "last": True}
    )

    assert {"type": "end"} in recorder.sent
    assert polls["n"] <= pipeline.PLAYBACK_MAX_POLLS + 2


@pytest.mark.asyncio
async def test_caller_speaking_during_playout_aborts_the_hangup(monkeypatch):
    """"Wait, one more thing" during the goodbye must keep the call alive."""
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Have a great day!"}]])
    _fast_teardown(pipeline)

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        if polls["n"] == 1:
            pipeline._turn_epoch += 1  # caller barged in

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline,
        {"type": "prompt", "voicePrompt": "Bye", "last": True}
    )

    assert {"type": "end"} not in recorder.sent
    assert not recorder.completed


# --- playout completion is measured, not guessed --------------------------
#
# Quiescence alone is not a completion signal: on CA0438f3 receipts during a
# single utterance were 1.5-6.6s apart, so a 1.5s "quiet" window expired
# between two receipts and hung up mid-goodbye. The receipt body is
# undocumented, but when it carries the played text we can compare its length
# against what we streamed and know exactly when speech has finished.


def _measured_teardown(pipeline: RelayPipeline) -> None:
    """Put quiescence far out of reach so only measured completion can end it."""
    pipeline.PLAYBACK_QUIET_POLLS = 30
    pipeline.PLAYBACK_FALLBACK_POLLS = 30
    pipeline.PLAYBACK_MAX_POLLS = 60


@pytest.mark.asyncio
async def test_playout_ends_as_soon_as_the_spoken_text_is_accounted_for(monkeypatch):
    recorder = _Recorder()
    reply = "Thanks for calling. Goodbye!"
    pipeline = _pipeline(recorder, [[{"text": reply}]])
    _measured_teardown(pipeline)

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        if polls["n"] == 1:
            # One receipt reporting the whole utterance as played.
            await pipeline.handle_message(
                {"type": "info", "name": "tokensPlayed", "value": reply}
            )

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Bye", "last": True})

    assert {"type": "end"} in recorder.sent
    # Must not sit through the quiet window once playback is demonstrably done.
    assert polls["n"] <= 3


@pytest.mark.asyncio
async def test_playout_handles_incremental_receipts(monkeypatch):
    """Receipts that each carry only the newly played fragment."""
    recorder = _Recorder()
    reply = "Thanks for calling. Goodbye!"
    pipeline = _pipeline(recorder, [[{"text": reply}]])
    _measured_teardown(pipeline)

    fragments = ["Thanks for ", "calling. ", "Goodbye!"]
    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        if polls["n"] <= len(fragments):
            await pipeline.handle_message(
                {"type": "info", "name": "tokensPlayed",
                 "value": fragments[polls["n"] - 1]}
            )

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Bye", "last": True})

    assert {"type": "end"} in recorder.sent
    assert polls["n"] <= len(fragments) + 2


@pytest.mark.asyncio
async def test_playout_does_not_end_early_on_a_partial_receipt(monkeypatch):
    """Half the goodbye played is not the whole goodbye played."""
    recorder = _Recorder()
    reply = "Thanks for calling Test Plumbing. Have a great day!"
    pipeline = _pipeline(recorder, [[{"text": reply}]])
    _measured_teardown(pipeline)
    pipeline.PLAYBACK_QUIET_POLLS = 6

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        if polls["n"] == 1:
            await pipeline.handle_message(
                {"type": "info", "name": "tokensPlayed", "value": reply[:10]}
            )

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Bye", "last": True})

    # Ends only via the quiet fallback, well after the partial receipt.
    assert {"type": "end"} in recorder.sent
    assert polls["n"] > pipeline.PLAYBACK_QUIET_POLLS


@pytest.mark.asyncio
async def test_playout_falls_back_when_the_receipt_carries_no_text(monkeypatch):
    """A non-string value tells us nothing; quiescence must still bound it."""
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Take care!"}]])
    _measured_teardown(pipeline)
    pipeline.PLAYBACK_QUIET_POLLS = 6

    polls = {"n": 0}

    async def counting_sleep(_seconds):
        polls["n"] += 1
        if polls["n"] <= 2:
            await pipeline.handle_message(
                {"type": "info", "name": "tokensPlayed", "value": 42}
            )

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Bye", "last": True})

    assert {"type": "end"} in recorder.sent
    assert polls["n"] <= pipeline.PLAYBACK_MAX_POLLS


# --- owner-availability hold (personal mode's "let me see if Deli is free") --
#
# CAa5e0de: Kevin said "Let me see if Deli is available, one moment" and then
# nothing, ever — the 30s unavailability return exists in both old engines but
# was never ported to relay. The caller sat in dead air until Twilio killed
# the session 4m40s later.


@pytest.mark.asyncio
async def test_hold_reply_arms_the_unavailability_timer_and_returns():
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [
            [{"text": "Got it. Let me see if Deli is available, one moment."}],
            [{"text": "Deli isn't available right now. Can I take a message?"}],
        ],
    )
    pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS = 0

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Is Deli there?", "last": True})
    assert pipeline._hold_task is not None
    await pipeline._hold_task
    await pipeline.wait_idle()

    tokens = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert "Deli isn't available right now. Can I take a message?" in tokens
    assert pipeline._unavailable_said is True


@pytest.mark.asyncio
async def test_ordinary_replies_do_not_arm_the_hold_timer():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "We open at nine tomorrow."}]])

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "When do you open?", "last": True})

    assert pipeline._hold_task is None


@pytest.mark.asyncio
async def test_hold_timer_defers_to_an_earlier_take_message():
    """If the owner already declined via the app, the timer must not speak again."""
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [
            [{"text": "Let me see if Deli is available, one moment."}],
            [{"text": "should never stream"}],
        ],
    )
    pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS = 0.05

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Is Deli there?", "last": True})
    pipeline._unavailable_said = True  # take_message command got there first
    await pipeline._hold_task
    await pipeline.wait_idle()

    tokens = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert "should never stream" not in tokens


# --- caller-silence watchdog ----------------------------------------------
#
# Relay had no idle guard at all: after the hold bug ate the conversation,
# the session sat for 4m40s until Twilio's own timeout closed it.


@pytest.mark.asyncio
async def test_idle_session_gets_a_nudge_then_a_goodbye():
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [
            [{"text": "Hi Jonathan, what do you need?"}],
            [{"text": "Are you still there?"}],
            [{"text": "I'll let Deli know you called. Goodbye!"}],
        ],
    )
    pipeline.SILENCE_CHECK_INTERVAL_SECONDS = 0.01
    pipeline.CALLER_SILENCE_PROMPT_SECONDS = 0.05
    pipeline.CALLER_SILENCE_HANGUP_SECONDS = 0.05
    pipeline.PLAYBACK_POLL_SECONDS = 0  # instant teardown polls
    pipeline.PLAYBACK_FALLBACK_POLLS = 1

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "hi", "last": True})
    pipeline.start_background_tasks()
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if recorder.completed:
                break
    finally:
        await pipeline.stop()

    tokens = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert "Are you still there?" in tokens
    assert {"type": "end"} in recorder.sent
    assert recorder.completed


@pytest.mark.asyncio
async def test_watchdog_stays_quiet_while_the_hold_window_is_open():
    """The caller was told to wait — 'are you still there' would be nonsense."""
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [[{"text": "Let me see if Deli is available, one moment."}]],
    )
    pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS = 10  # far away; window stays open
    pipeline.SILENCE_CHECK_INTERVAL_SECONDS = 0.01
    pipeline.CALLER_SILENCE_PROMPT_SECONDS = 0.05
    pipeline.CALLER_SILENCE_HANGUP_SECONDS = 0.05

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Is Deli in?", "last": True})
    pipeline.start_background_tasks()
    try:
        await asyncio.sleep(0.3)
    finally:
        await pipeline.stop()

    tokens = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert len(tokens) == 1  # only the hold reply; no nudge, no goodbye
    assert {"type": "end"} not in recorder.sent


@pytest.mark.asyncio
async def test_playback_receipts_count_as_activity_for_the_watchdog():
    """Kevin speaking a long reply must not read as caller silence."""
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "A long explanation of pricing."}]])
    pipeline.SILENCE_CHECK_INTERVAL_SECONDS = 0.01
    pipeline.CALLER_SILENCE_PROMPT_SECONDS = 0.06
    pipeline.CALLER_SILENCE_HANGUP_SECONDS = 5

    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Pricing?", "last": True})
    pipeline.start_background_tasks()
    try:
        # Receipts keep arriving (Kevin still talking) past the prompt window.
        for _ in range(6):
            await asyncio.sleep(0.03)
            await pipeline.handle_message(
                {"type": "info", "name": "tokensPlayed", "value": "..."}
            )
        nudged_early = any(
            m for m in recorder.sent[2:] if m["type"] == "text" and m["token"]
        )
    finally:
        await pipeline.stop()

    assert not nudged_early


@pytest.mark.asyncio
async def test_caller_speech_during_hold_updates_transcript_and_preserves_silence():
    """Speech while on hold must update live transcript without breaking silence."""
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [
            [{"text": "Got it. Let me see if Deli is available, one moment."}],
            [{"text": "Deli is not available right now. Can I take a message?"}],
        ],
    )
    pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS = 0.1

    # Initial prompt that puts the caller on hold
    await _drive(pipeline, {"type": "prompt", "voicePrompt": "Can I talk to Deli?", "last": True})
    assert pipeline._hold_task is not None
    assert not pipeline._hold_task.done()

    tokens_before = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert "Got it. Let me see if Deli is available, one moment." in tokens_before

    # Caller adds information while the hold is active
    await pipeline.handle_message({
        "type": "prompt",
        "voicePrompt": "This is about a car he wants to buy.",
        "last": True,
    })

    # The transcript must be forwarded immediately so the owner sees it in the app
    assert ("Caller", "This is about a car he wants to buy.") in recorder.transcripts

    # But Kevin must NOT have generated any new reply yet (still silent on hold)
    tokens_during_hold = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert tokens_during_hold == tokens_before

    # The speech must be recorded in history
    assert any(
        entry.get("role") == "user"
        and any("car he wants to buy" in p.get("text", "") for p in entry.get("parts", []))
        for entry in pipeline._history
    )

    # When the hold timer expires, unavailability reply is generated
    await pipeline._hold_task
    await pipeline.wait_idle()

    tokens_after = [m["token"] for m in recorder.sent if m["type"] == "text" and m["token"]]
    assert "Deli is not available right now. Can I take a message?" in tokens_after
    assert pipeline._unavailable_said is True


def test_personal_mode_has_no_tools_even_with_calendar_or_jobber():
    """Personal assistant mode must never expose scheduling or CRM tools."""
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder,
        [],
        effective_mode="personal",
        integration_tokens={
            "google_calendar": {
                "access_token": "ya29.test",
                "expires_at": 9999999999,
            },
            "jobber": {
                "access_token": "jobber.test",
                "expires_at": 9999999999,
            },
        },
    )
    assert pipeline._tools == []

    from app.services.gemini_pipeline import GeminiPipeline

    gemini = GeminiPipeline.__new__(GeminiPipeline)
    gemini._contractor_config = pipeline._contractor_config
    gemini._log_voice_timing = lambda *args, **kwargs: None
    assert gemini._build_gemini_tools() == []
