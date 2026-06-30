import json
import logging
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
async def test_google_book_appointment_calls_gcal_book_when_gate_allows(monkeypatch):
    created = []

    async def fake_book_appointment(token, *, title, start_time, end_time, description):
        created.append({
            "token": token,
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "description": description,
        })
        return "event-1"

    monkeypatch.setattr("app.services.calendar.book_appointment", fake_book_appointment)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })
    result = json.loads(await pipeline._execute_tool(
        "book_appointment",
        {
            "title": "Sink repair",
            "start_time": "2026-07-01T13:00:00-04:00",
            "end_time": "2026-07-01T14:00:00-04:00",
            "description": "Caller asked for the upstairs sink.",
            "ignored": "not passed through",
        },
    ))

    assert result == {"success": True, "event_id": "event-1"}
    assert created == [
        {
            "token": "gcal-token",
            "title": "Sink repair",
            "start_time": "2026-07-01T13:00:00-04:00",
            "end_time": "2026-07-01T14:00:00-04:00",
            "description": "Caller asked for the upstairs sink.",
        }
    ]


@pytest.mark.asyncio
async def test_jobber_tool_exception_returns_generic_error_and_sanitizes_logs(monkeypatch, caplog):
    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
    )

    async def fake_create_job(*_args, **_kwargs):
        raise RuntimeError(
            "Jobber rejected appointment for Jane Private at 123 Secret Lane, "
            "callback +15551234567, gate code 2468."
        )

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "jobber_access_token": "token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.JOBBER_CREATE_JOB.value: True},
        "automation_approvals": {ActionKey.JOBBER_CREATE_JOB.value: True},
    })

    with caplog.at_level(logging.ERROR):
        result = json.loads(await pipeline._execute_tool(
            "book_appointment",
            {"title": "Jane Private repair", "description": "123 Secret Lane"},
        ))

    assert result == {"success": False, "error": "Tool execution failed."}
    assert "book_appointment" in caplog.text
    assert "CA123" in caplog.text
    assert "RuntimeError" in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text
        assert sensitive_value not in json.dumps(result)


@pytest.mark.asyncio
async def test_google_tool_exception_returns_generic_error_and_sanitizes_logs(monkeypatch, caplog):
    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
    )

    async def fake_book_appointment(*_args, **_kwargs):
        raise ValueError(
            "Calendar rejected appointment for Jane Private at 123 Secret Lane, "
            "callback +15551234567, gate code 2468."
        )

    monkeypatch.setattr("app.services.calendar.book_appointment", fake_book_appointment)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })

    with caplog.at_level(logging.ERROR):
        result = json.loads(await pipeline._execute_tool(
            "book_appointment",
            {
                "title": "Jane Private repair",
                "start_time": "2026-07-01T13:00:00-04:00",
                "end_time": "2026-07-01T14:00:00-04:00",
                "description": "123 Secret Lane callback +15551234567",
            },
        ))

    assert result == {"success": False, "error": "Tool execution failed."}
    assert "book_appointment" in caplog.text
    assert "CA123" in caplog.text
    assert "ValueError" in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text
        assert sensitive_value not in json.dumps(result)


@pytest.mark.asyncio
async def test_gemini_denied_book_appointment_uses_real_voice_gate_and_sanitized_logging(monkeypatch, caplog):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    async def fail_create_job(*_args, **_kwargs):
        raise AssertionError("create_job must not be called when Gemini gate denies")

    monkeypatch.setattr("app.services.jobber.create_job", fail_create_job)

    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-GEMINI-PRIVACY",
        contractor_config={
            "contractor_id": "c1",
            "jobber_access_token": "token",
            "integration_write_status": "approved",
            "gated_actions": {ActionKey.JOBBER_CREATE_JOB.value: True},
        },
    )
    pipeline._ws = FakeWebSocket()

    sensitive_args = {
        "title": "Jane Private kitchen sink repair",
        "instructions": "Address 123 Secret Lane, gate code 2468, call +15551234567.",
        "client_id": "client-sensitive",
    }

    with caplog.at_level(logging.INFO):
        await pipeline._handle_tool_calls([
            {"id": "tool-1", "name": "book_appointment", "args": sensitive_args}
        ])

    assert pipeline._ws.sent == [
        {
            "tool_response": {
                "function_responses": [
                    {
                        "id": "tool-1",
                        "name": "book_appointment",
                        "response": {
                            "success": False,
                            "error": "Owner confirmation is required for this action.",
                        },
                    }
                ]
            }
        }
    ]
    assert "Gemini tool call: book_appointment call_sid=CA-GEMINI-PRIVACY" in caplog.text
    for sensitive_value in (
        "Jane Private",
        "123 Secret Lane",
        "2468",
        "+15551234567",
        "client-sensitive",
    ):
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_voice_tool_error_result_logging_does_not_include_sensitive_payload(monkeypatch, caplog):
    class FakeClaudeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class FakeClaudeClient:
        async def post(self, *_args, **_kwargs):
            return FakeClaudeResponse({
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "book_appointment",
                        "input": {
                            "title": "Jane Private faucet repair",
                            "description": "Address 123 Secret Lane, callback +15551234567.",
                        },
                    }
                ],
            })

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
    })
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeClaudeClient()

    async def fake_execute_tool(tool_name, tool_input):
        assert tool_name == "book_appointment"
        assert tool_input["title"] == "Jane Private faucet repair"
        return json.dumps({
            "error": "Calendar rejected Jane Private at 123 Secret Lane, callback +15551234567."
        })

    async def fake_speak(_text):
        return None

    monkeypatch.setattr(pipeline, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(pipeline, "_speak", fake_speak)

    with caplog.at_level(logging.WARNING):
        await pipeline._handle_caller_speech("I need help with a leak.")

    assert "book_appointment" in caplog.text
    assert "CA123" in caplog.text
    for sensitive_value in (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
    ):
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_voice_tool_call_logging_does_not_include_sensitive_tool_input(monkeypatch, caplog):
    class FakeClaudeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class FakeClaudeClient:
        def __init__(self):
            self.responses = [
                FakeClaudeResponse({
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "book_appointment",
                            "input": {
                                "title": "Jane Private faucet repair",
                                "start_time": "2026-07-01T13:00:00-04:00",
                                "end_time": "2026-07-01T14:00:00-04:00",
                                "description": "Address 123 Secret Lane, callback +15551234567.",
                            },
                        }
                    ],
                }),
                FakeClaudeResponse({
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "I can take a message."}],
                }),
            ]

        async def post(self, *_args, **_kwargs):
            return self.responses.pop(0)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
    })
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeClaudeClient()

    async def fake_execute_tool(tool_name, tool_input):
        assert tool_name == "book_appointment"
        assert tool_input["title"] == "Jane Private faucet repair"
        return json.dumps({"success": False, "message": "not created"})

    async def fake_speak(_text):
        return None

    monkeypatch.setattr(pipeline, "_execute_tool", fake_execute_tool)
    monkeypatch.setattr(pipeline, "_speak", fake_speak)

    with caplog.at_level(logging.INFO):
        await pipeline._handle_caller_speech("I need help with a leak.")

    assert "Tool call: book_appointment call_sid=CA123" in caplog.text
    for sensitive_value in (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "2026-07-01T13:00:00-04:00",
    ):
        assert sensitive_value not in caplog.text


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
