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


@pytest.mark.asyncio
async def test_prompt_streams_tokens_and_closes_turn():
    recorder = _Recorder()
    pipeline = _pipeline(
        recorder, [[{"text": "We can help "}, {"text": "with that."}]]
    )

    await pipeline.handle_message(
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

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "My toi", "lang": "en-US", "last": False}
    )

    assert recorder.sent == []
    assert recorder.transcripts == []


@pytest.mark.asyncio
async def test_language_tracked_from_prompt():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Claro!"}]])

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "Hola necesito ayuda", "lang": "es-US", "last": True}
    )

    assert pipeline.language == "es"


@pytest.mark.asyncio
async def test_urgency_keyword_fires_escalation_once():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Stay calm, help is coming."}]])

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "There is a gas leak in my kitchen", "last": True}
    )
    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "I said gas leak!", "last": True}
    )

    assert len(recorder.urgencies) == 1


@pytest.mark.asyncio
async def test_interrupt_truncates_last_kevin_turn():
    recorder = _Recorder()
    pipeline = _pipeline(recorder, [[{"text": "Our hours are nine to five on weekdays."}]])

    await pipeline.handle_message(
        {"type": "prompt", "voicePrompt": "What are your hours?", "last": True}
    )
    await pipeline.handle_message(
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

    await pipeline.handle_message(
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

    await pipeline.handle_message(
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

    await pipeline.handle_message(
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
