"""Jobber v1 exposes caller lookup only, not scheduling or booking."""

from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline


def test_voice_pipeline_jobber_tools_do_not_expose_booking():
    names = {tool["name"] for tool in VoicePipeline.JOBBER_TOOLS}

    assert names == {"check_customer"}
    assert "check_availability" not in names
    assert "book_appointment" not in names


def test_gemini_pipeline_jobber_tools_do_not_expose_booking():
    pipeline = GeminiPipeline.__new__(GeminiPipeline)
    pipeline._contractor_config = {"jobber_access_token": "jobber-token"}

    tools = pipeline._build_gemini_tools()
    declarations = tools[0]["function_declarations"]
    names = {declaration["name"] for declaration in declarations}

    assert names == {"check_customer"}
    assert "check_availability" not in names
    assert "book_appointment" not in names
