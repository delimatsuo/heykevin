"""Jobber v1 exposes caller lookup only, not scheduling or booking."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline


def test_voice_pipeline_jobber_tools_do_not_expose_booking():
    names = {tool["name"] for tool in VoicePipeline.JOBBER_TOOLS}

    assert names == {"check_customer"}
    assert "check_availability" not in names
    assert "book_appointment" not in names


def test_gemini_pipeline_jobber_tools_do_not_expose_booking():
    pipeline = GeminiPipeline.__new__(GeminiPipeline)
    pipeline._contractor_config = {
        "contractor_id": "c1",
        "jobber_access_token": "jobber-token",
        "jobber_refresh_token": "jobber-refresh",
    }

    tools = pipeline._build_gemini_tools()
    declarations = tools[0]["function_declarations"]
    names = {declaration["name"] for declaration in declarations}

    assert names == {"check_customer"}
    assert "check_availability" not in names
    assert "book_appointment" not in names
