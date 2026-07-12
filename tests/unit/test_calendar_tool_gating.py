"""Calendar voice tools are read-only until writes have durable deduplication."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline


def test_legacy_calendar_tools_only_expose_availability():
    names = {tool["name"] for tool in VoicePipeline.CALENDAR_TOOLS}

    assert names == {"check_availability"}
    assert "book_appointment" not in names


def test_gemini_calendar_tools_only_expose_availability():
    pipeline = GeminiPipeline.__new__(GeminiPipeline)
    pipeline._contractor_config = {"google_calendar_access_token": "calendar-token"}

    tools = pipeline._build_gemini_tools()
    declarations = tools[0]["function_declarations"]
    names = {declaration["name"] for declaration in declarations}

    assert names == {"check_availability"}
    assert "book_appointment" not in names
