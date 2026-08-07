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
    assert "Kevin" in twiml  # welcomeGreeting present


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
