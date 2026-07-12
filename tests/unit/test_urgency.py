"""Deterministic, negation-safe live urgency classification."""

import asyncio
import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.urgency import URGENCY_KEYWORDS, find_urgent_signal
from app.services.voice_pipeline import VoicePipeline


@pytest.mark.parametrize(
    "text",
    [
        "There is smoke in the basement.",
        "No fire, but there is smoke in the basement.",
        "This is not just a leak; water is everywhere.",
        "The breaker tripped and the panel is sparking.",
        "We have no water.",
        "This is not an emergency; there is carbon monoxide.",
        "The flooding has not stopped.",
        "I am not sure whether this is a gas leak.",
        "The fire is not under control.",
        "There is a fire over here.",
        "The flooding is not over.",
        "The fire is over here.",
        "The fire is out of control.",
        "The flooding had stopped but has started again.",
        "The gas leak was resolved but returned.",
        "I have not ruled out a gas leak.",
    ],
)
def test_urgent_signal_detects_unnegated_emergency_condition(text):
    assert find_urgent_signal(text) in URGENCY_KEYWORDS


@pytest.mark.parametrize(
    "text",
    [
        "This is not an emergency, just routine service.",
        "There is no fire.",
        "The flooding has stopped.",
        "The gas leak is resolved.",
        "It is no longer flooding.",
        "The fireplace needs an inspection.",
        "There is a smoky smell but no smoke.",
        "The panel is not sparking.",
        "We do not smell smoke.",
        "There is no sign of a gas leak.",
        "There is no electrical fire.",
        "The electrical fire is under control.",
        "The fire is over.",
        "We haven't had a fire.",
        "I have never smelled smoke.",
        "There is no active fire.",
        "There is no known gas leak.",
        "There is no fire or smoke.",
        "The flooding, which has stopped.",
        "This isn\u2019t an emergency.",
    ],
)
def test_urgent_signal_rejects_negated_resolved_or_substring_mentions(text):
    assert find_urgent_signal(text) is None


def test_live_pipelines_expose_the_shared_keyword_set():
    assert GeminiPipeline.URGENCY_KEYWORDS is URGENCY_KEYWORDS
    assert VoicePipeline.URGENCY_KEYWORDS is URGENCY_KEYWORDS


async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_gemini_live_urgency_ignores_negation_then_fires_once():
    escalations = []

    async def record_urgency(text):
        escalations.append(text)

    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_urgency_detected=record_urgency,
        call_sid="CA_test",
        contractor_config={"effective_mode": "personal"},
    )
    pipeline._caller_transcript_buf = [
        "This is not an emergency, just routine service."
    ]
    await pipeline._flush_caller_transcript()
    await asyncio.sleep(0)

    assert escalations == []
    assert pipeline._urgency_detected is False

    pipeline._caller_transcript_buf = ["There is smoke in the basement."]
    await pipeline._flush_caller_transcript()
    await asyncio.sleep(0)

    assert escalations == ["There is smoke in the basement."]
    assert pipeline._urgency_detected is True


@pytest.mark.asyncio
async def test_legacy_voice_urgency_uses_same_classifier():
    escalations = []

    async def record_urgency(text):
        escalations.append(text)

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._urgency_detected = False
    pipeline.on_urgency_detected = record_urgency
    pipeline._unavailable_task = None
    pipeline._is_speaking = False
    pipeline._interrupt_speaking = False
    pipeline.on_clear_audio = None
    pipeline._call_sid = "CA_test"

    pipeline._check_urgency("The flooding has stopped.")
    await asyncio.sleep(0)
    assert escalations == []

    pipeline._check_urgency("Water is everywhere.")
    await asyncio.sleep(0)
    assert escalations == ["Water is everywhere."]

    pipeline._check_urgency("There is smoke now.")
    await asyncio.sleep(0)
    assert escalations == ["Water is everywhere."]


@pytest.mark.asyncio
async def test_legacy_voice_classifies_the_completed_utterance_across_segments():
    escalations = []

    async def record_urgency(text):
        escalations.append(text)

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._urgency_detected = False
    pipeline.on_urgency_detected = record_urgency
    pipeline._unavailable_task = None
    pipeline._is_speaking = False
    pipeline._interrupt_speaking = False
    pipeline.on_clear_audio = None
    pipeline._call_sid = "CA_test"
    pipeline._process_utterance = _noop
    pipeline._utterance_buffer = ["There is no", "fire."]

    await pipeline._flush_utterance()
    await asyncio.sleep(0)
    assert escalations == []

    pipeline._utterance_buffer = ["There is no", "fire, but there is smoke."]
    await pipeline._flush_utterance()
    await asyncio.sleep(0)
    assert escalations == ["There is no fire, but there is smoke."]


@pytest.mark.asyncio
async def test_legacy_voice_urgency_log_excludes_transcript_and_signal(caplog):
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._urgency_detected = False
    pipeline.on_urgency_detected = _noop
    pipeline._unavailable_task = None
    pipeline._is_speaking = False
    pipeline._interrupt_speaking = False
    pipeline.on_clear_audio = None
    pipeline._call_sid = "CA_test_private_identifier"

    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")
    pipeline._check_urgency(
        "There is smoke at my private address, 100 Market Street."
    )
    await asyncio.sleep(0)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_event event=urgency_detected" in messages
    assert "keyword=smoke" not in messages
    assert "100 Market Street" not in messages
    assert "CA_test_private_identifier" not in messages
