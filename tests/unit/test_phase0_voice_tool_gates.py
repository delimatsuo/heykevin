import json
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services.gated_actions import ActionKey
from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


def _pipeline(config):
    return VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid="CA123",
        contractor_config=config,
    )


@pytest.mark.asyncio
async def test_jobber_book_appointment_requires_automation_approval(monkeypatch):
    created = []

    async def fake_create_job(*args, **kwargs):
        created.append((args, kwargs))
        return "jobber-1"

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "jobber_access_token": "token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.JOBBER_CREATE_JOB.value: True},
    })
    result = json.loads(await pipeline._execute_tool("book_appointment", {"title": "Repair"}))

    assert result == {"success": False, "error": "Owner confirmation is required for this action."}
    assert created == []


@pytest.mark.asyncio
async def test_jobber_book_appointment_calls_create_job_when_gate_allows(monkeypatch):
    created = []

    async def fake_create_job(*args, **kwargs):
        created.append((args, kwargs))
        return "jobber-1"

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    config = {
        "contractor_id": "c1",
        "jobber_access_token": "token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.JOBBER_CREATE_JOB.value: True},
        "automation_approvals": {ActionKey.JOBBER_CREATE_JOB.value: True},
    }
    pipeline = _pipeline(config)
    tool_input = {"title": "Repair"}

    result = json.loads(await pipeline._execute_tool("book_appointment", tool_input))

    assert result == {"success": True, "job_id": "jobber-1"}
    assert created == [((config, tool_input), {})]


@pytest.mark.asyncio
async def test_google_book_appointment_requires_automation_approval(monkeypatch):
    created = []

    async def fake_book_appointment(*args, **kwargs):
        created.append((args, kwargs))
        return "event-1"

    monkeypatch.setattr("app.services.calendar.book_appointment", fake_book_appointment)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })
    result = json.loads(await pipeline._execute_tool("book_appointment", {"title": "Repair"}))

    assert result == {"success": False, "error": "Owner confirmation is required for this action."}
    assert created == []


@pytest.mark.asyncio
async def test_gemini_tool_calls_delegate_to_voice_pipeline_with_call_sid(monkeypatch):
    seen = []

    async def fake_execute_tool(self, tool_name, tool_input):
        seen.append((self._call_sid, self._contractor_config, tool_name, tool_input))
        return json.dumps({"success": False, "error": "blocked"})

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    monkeypatch.setattr(VoicePipeline, "_execute_tool", fake_execute_tool)

    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-GEMINI",
        contractor_config={"contractor_id": "c1"},
    )
    pipeline._ws = FakeWebSocket()

    await pipeline._handle_tool_calls([{"id": "tool-1", "name": "book_appointment", "args": {"title": "Repair"}}])

    assert seen == [("CA-GEMINI", {"contractor_id": "c1"}, "book_appointment", {"title": "Repair"})]
    assert pipeline._ws.sent == [
        {
            "tool_response": {
                "function_responses": [
                    {
                        "id": "tool-1",
                        "name": "book_appointment",
                        "response": {"success": False, "error": "blocked"},
                    }
                ]
            }
        }
    ]
