import json
from datetime import datetime, timedelta, timezone
import logging
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest
from unittest.mock import AsyncMock

from app.services.gated_actions import ActionKey
from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline

def _upcoming(hour: int) -> str:
    """An upcoming slot, relative to now.

    book_appointment rejects a start_time outside the bookable window — no
    past bookings, and no more than six months out. A frozen date drifts into
    the past and fails the suite on a calendar date rather than a code change.
    Nothing here asserts the literal value.
    """
    when = datetime.now(timezone(timedelta(hours=-4))) + timedelta(days=3)
    return when.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()




async def _noop(*_args, **_kwargs):
    return None


class _DummyAsyncClient:
    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _provider_recovery_ready(monkeypatch):
    from app.services import voice_pipeline

    monkeypatch.setattr(
        voice_pipeline.settings,
        "service_request_recovery_enabled",
        True,
    )
    monkeypatch.setattr(
        voice_pipeline.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _DummyAsyncClient(),
    )


def _pipeline(config):
    return VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid="CA123",
        contractor_config=config,
    )


@pytest.mark.asyncio
async def test_jobber_book_appointment_is_unknown_tool_and_does_not_create_job(monkeypatch):
    created = []

    async def fake_create_job(*args, **kwargs):
        created.append((args, kwargs))
        return "jobber-1"

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "jobber_access_token": "token",
        "jobber_refresh_token": "token-refresh",
        "integration_write_status": "approved",
        "gated_actions": {"jobber_create_job": True, "jobber_create_quote": True},
    })
    result = json.loads(await pipeline._execute_tool("book_appointment", {"title": "Repair"}))

    assert result == {"error": "Unknown tool: book_appointment"}
    assert created == []


@pytest.mark.asyncio
async def test_jobber_check_availability_is_unknown_tool_and_does_not_query_availability(monkeypatch):
    checked = []

    async def fake_get_available_slots(*args, **kwargs):
        checked.append((args, kwargs))
        return []

    monkeypatch.setattr("app.services.jobber.get_available_slots", fake_get_available_slots)

    config = {
        "contractor_id": "c1",
        "jobber_access_token": "token",
        "jobber_refresh_token": "token-refresh",
        "integration_write_status": "approved",
        "gated_actions": {"jobber_create_job": True, "jobber_create_quote": True},
        "automation_approvals": {"jobber_create_job": True, "jobber_create_quote": True},
    }
    pipeline = _pipeline(config)
    tool_input = {"days_ahead": 7}

    result = json.loads(await pipeline._execute_tool("check_availability", tool_input))

    assert result == {"error": "Unknown tool: check_availability"}
    assert checked == []


@pytest.mark.asyncio
async def test_google_book_appointment_requires_automation_approval(monkeypatch):
    async def fake_save_call(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "token",
        "google_calendar_refresh_token": "token-refresh",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })
    monkeypatch.setattr(
        pipeline,
        "_create_managed_google_booking",
        AsyncMock(side_effect=AssertionError("provider saga must not run")),
    )
    # A valid start_time is now a precondition: book_appointment rejects an
    # implausible or missing one before the gate runs. This test is about the
    # gate, so it supplies a real slot instead of relying on the old behaviour
    # of recording a request with no time at all.
    _slot = datetime.now(timezone(timedelta(hours=-4))) + timedelta(days=3)
    result = json.loads(await pipeline._execute_tool("book_appointment", {
        "title": "Repair",
        "start_time": _slot.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
        "end_time": _slot.replace(hour=11, minute=0, second=0, microsecond=0).isoformat(),
    }))

    # Unapproved automation becomes an owner-confirmed request, never a write.
    # See tests/unit/test_appointment_requests.py for the request contract.
    assert result["booked"] is False
    assert result["status"] == "request_recorded"
    pipeline._create_managed_google_booking.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_book_appointment_calls_managed_saga_when_gate_allows(monkeypatch):
    created = []

    async def fake_managed_create(*, tool_input, operation_id):
        created.append((tool_input, operation_id))
        return {"success": True, "revision": 1}

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "google_calendar_refresh_token": "gcal-refresh",
        "integration_write_status": "approved",
        "service_request_mutations_enabled": True,
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })
    monkeypatch.setattr(
        pipeline,
        "_create_managed_google_booking",
        fake_managed_create,
    )
    result = json.loads(await pipeline._execute_tool(
        "book_appointment",
        {
            "title": "Sink repair",
            "start_time": _upcoming(13),
            "end_time": _upcoming(14),
            "description": "Caller asked for the upstairs sink.",
            "ignored": "not passed through",
        },
    ))

    assert result == {"success": True, "revision": 1}
    assert created == [
        ({
            "title": "Sink repair",
            "start_time": _upcoming(13),
            "end_time": _upcoming(14),
            "description": "Caller asked for the upstairs sink.",
            "ignored": "not passed through",
        }, "")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_value", [None, False, 1, "true"])
async def test_google_booking_requires_literal_tenant_mutation_flag(
    monkeypatch,
    flag_value,
):
    config = {
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "google_calendar_refresh_token": "gcal-refresh",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    }
    if flag_value is not None:
        config["service_request_mutations_enabled"] = flag_value
    pipeline = _pipeline(config)
    managed_create = AsyncMock(side_effect=AssertionError("provider saga must stay closed"))
    monkeypatch.setattr(pipeline, "_create_managed_google_booking", managed_create)
    monkeypatch.setattr(
        pipeline,
        "_record_appointment_request",
        AsyncMock(return_value='{"status":"request_recorded","booked":false}'),
    )

    result = json.loads(await pipeline._execute_tool(
        "book_appointment",
        {
            "title": "Sink repair",
            "start_time": _upcoming(13),
            "end_time": _upcoming(14),
        },
    ))

    assert result == {"status": "request_recorded", "booked": False}
    managed_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_booking_stays_request_only_until_recovery_is_ready(monkeypatch):
    from app.services import voice_pipeline

    monkeypatch.setattr(
        voice_pipeline.settings,
        "service_request_recovery_enabled",
        False,
    )
    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "google_calendar_refresh_token": "gcal-refresh",
        "integration_write_status": "approved",
        "service_request_mutations_enabled": True,
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })
    managed_create = AsyncMock(side_effect=AssertionError("provider saga must stay closed"))
    monkeypatch.setattr(pipeline, "_create_managed_google_booking", managed_create)
    monkeypatch.setattr(
        pipeline,
        "_record_appointment_request",
        AsyncMock(return_value='{"status":"request_recorded","booked":false}'),
    )

    result = json.loads(await pipeline._execute_tool(
        "book_appointment",
        {
            "title": "Sink repair",
            "start_time": _upcoming(13),
            "end_time": _upcoming(14),
        },
    ))

    assert result == {"status": "request_recorded", "booked": False}
    managed_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_calendar_create_error_logging_omits_response_text(monkeypatch, caplog):
    from app.services import calendar

    class FakeResponse:
        status_code = 400
        text = (
            '{"error":{"message":"Cannot create appointment for Jane Private at '
            '123 Secret Lane, call +15551234567, gate code 2468."}}'
        )

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
    )
    monkeypatch.setattr(calendar.httpx, "AsyncClient", FakeAsyncClient)

    import base64
    import time
    import datetime
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")

    class _FakeSnap:
        def __init__(self, d):
            self._d = d
            self.exists = d is not None
            self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc)

        def to_dict(self):
            return dict(self._d) if self.exists else {}

    class _FakeTransaction:
        def __init__(self, db):
            self._db = db
            self._staged_updates = []
            self._staged_sets = []
            self._staged_deletes = []
            self.committed = False
            self._read_only = False
            self._id = b"fake-tx-id"
            self._max_attempts = 5
            self.in_progress = True

        def get(self, doc_ref):
            if self._staged_updates or self._staged_sets or self._staged_deletes:
                raise RuntimeError("Firestore transaction read-after-write violation")
            return doc_ref.get()

        def update(self, doc_ref, updates):
            self._staged_updates.append((doc_ref, dict(updates)))

        def delete(self, doc_ref):
            self._staged_deletes.append(doc_ref)

        def set(self, doc_ref, data):
            self._staged_sets.append((doc_ref, dict(data)))

        def commit(self):
            for doc_ref, data in self._staged_sets:
                doc_ref.set(data)
            for doc_ref, updates in self._staged_updates:
                doc_ref.update(updates)
            for doc_ref in self._staged_deletes:
                doc_ref.delete()
            self.committed = True

        def _begin(self, *args, **kwargs):
            pass

        def _clean_up(self):
            pass

        def _rollback(self):
            self._staged_sets.clear()
            self._staged_updates.clear()
            self._staged_deletes.clear()

        def _commit(self):
            self.commit()
            return []

    class _FakeDocRef:
        def __init__(self, d):
            self._d = d
            self.data = d
            self.deleted = False

        def get(self, *args, **kwargs):
            return _FakeSnap(self._d)

        def update(self, updates, *args, **kwargs):
            if self._d is None:
                self._d = {}
            for k, v in updates.items():
                if str(type(v).__name__) == "Sentinel" or "DELETE" in str(v):
                    self._d.pop(k, None)
                else:
                    self._d[k] = v

        def set(self, data, *args, **kwargs):
            self._d = dict(data)
            self.deleted = False

        def delete(self, *args, **kwargs):
            self.deleted = True
            self._d = None

    class _FakeFirestore:
        def __init__(self):
            self._docs = {}

        def collection(self, name):
            class _Coll:
                def __init__(self, docs):
                    self.docs = docs

                def document(self, doc_id):
                    return self.docs.setdefault(doc_id, _FakeDocRef({
                        "contractor_id": doc_id,
                        "active": True,
                        "google_calendar_connected": True,
                        "google_calendar_generation": 0,
                        "google_calendar_lifecycle_epoch": 0,
                        "google_calendar_access_token": "gcal-token",
                        "google_calendar_refresh_token": "gcal-refresh",
                    }))
            return _Coll(self._docs.setdefault(name, {}))

        def transaction(self):
            return _FakeTransaction(self)

    db = _FakeFirestore()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    with caplog.at_level(logging.ERROR):
        result = await calendar.book_appointment(
            {
                "contractor_id": "c1",
                "google_calendar_access_token": "gcal-token",
                "google_calendar_refresh_token": "gcal-refresh",
            },
            title="Jane Private repair",
            start_time=_upcoming(13),
            end_time=_upcoming(14),
            description="123 Secret Lane callback +15551234567",
        )

    assert result is None
    assert "Google Calendar create event error" in caplog.text
    assert "operation=create_event" in caplog.text
    assert "status_code=400" in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_jobber_book_appointment_unknown_tool_does_not_call_create_job_or_log_payload(monkeypatch, caplog):
    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
    )
    created = []

    async def fake_create_job(*args, **kwargs):
        created.append((args, kwargs))
        return "jobber-1"

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    pipeline = _pipeline({
        "contractor_id": "c1",
        "jobber_access_token": "token",
        "jobber_refresh_token": "token-refresh",
        "integration_write_status": "approved",
        "gated_actions": {"jobber_create_job": True, "jobber_create_quote": True},
        "automation_approvals": {"jobber_create_job": True, "jobber_create_quote": True},
    })

    with caplog.at_level(logging.ERROR):
        result = json.loads(await pipeline._execute_tool(
            "book_appointment",
            {"title": "Jane Private repair", "description": "123 Secret Lane"},
        ))

    assert result == {"error": "Unknown tool: book_appointment"}
    assert created == []
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

    async def fake_managed_create(*_args, **_kwargs):
        raise ValueError(
            "Calendar rejected appointment for Jane Private at 123 Secret Lane, "
            "callback +15551234567, gate code 2468."
        )

    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "google_calendar_refresh_token": "gcal-refresh",
        "integration_write_status": "approved",
        "service_request_mutations_enabled": True,
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })
    monkeypatch.setattr(
        pipeline,
        "_create_managed_google_booking",
        fake_managed_create,
    )

    with caplog.at_level(logging.ERROR):
        result = json.loads(await pipeline._execute_tool(
            "book_appointment",
            {
                "title": "Jane Private repair",
                "start_time": _upcoming(13),
                "end_time": _upcoming(14),
                "description": "123 Secret Lane callback +15551234567",
            },
        ))

    assert result["success"] is False
    assert result["error"] == "unavailable"
    # The model needs a recovery instruction, not just a failure flag: without
    # one it has nothing to say and the caller hears dead air (call CA54f11e).
    assert "instruction" in result
    assert "call back" in result["instruction"].lower()
    # The failure payload reaches the model, so it must not carry caller data.
    for sensitive_value in sensitive_values:
        assert sensitive_value not in json.dumps(result)

    assert "book_appointment" in caplog.text
    assert "CA123" in caplog.text
    assert "ValueError" in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text
        assert sensitive_value not in json.dumps(result)


@pytest.mark.asyncio
async def test_gemini_jobber_book_appointment_returns_unknown_tool_and_does_not_create_job(monkeypatch, caplog):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    created = []

    async def fake_create_job(*args, **kwargs):
        created.append((args, kwargs))
        return "jobber-1"

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-GEMINI-PRIVACY",
        contractor_config={
            "contractor_id": "c1",
            "jobber_access_token": "token",
            "jobber_refresh_token": "token-refresh",
            "integration_write_status": "approved",
            "gated_actions": {"jobber_create_job": True, "jobber_create_quote": True},
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
                            "error": "Unknown tool: book_appointment",
                        },
                    }
                ]
            }
        }
    ]
    assert created == []
    assert "voice_event event=tool_call call=CA-GEMIN tool=book_appointment" in caplog.text
    assert "CA-GEMINI-PRIVACY" not in caplog.text
    for sensitive_value in (
        "Jane Private",
        "123 Secret Lane",
        "2468",
        "+15551234567",
        "client-sensitive",
    ):
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_gemini_tool_log_allowlists_provider_tool_name(monkeypatch, caplog):
    from app.services.voice_pipeline import VoicePipeline

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    async def fake_execute_tool(_self, _tool_name, _tool_input):
        return json.dumps({"error": "Unavailable"})

    monkeypatch.setattr(VoicePipeline, "_execute_tool", fake_execute_tool)
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-GEMINI-PRIVATE-LONG",
        contractor_config={"contractor_id": "c1"},
    )
    pipeline._ws = FakeWebSocket()
    provider_tool_name = "private_tool\nforged_event"

    with caplog.at_level(logging.INFO):
        await pipeline._handle_tool_calls(
            [{"id": "tool-1", "name": provider_tool_name, "args": {}}]
        )

    assert "voice_event event=tool_call call=CA-GEMIN tool=unknown" in caplog.text
    assert provider_tool_name not in caplog.text
    assert "CA-GEMINI-PRIVATE-LONG" not in caplog.text


@pytest.mark.asyncio
async def test_gemini_delegated_tool_exception_returns_generic_error_and_sanitizes_logs(monkeypatch, caplog):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
        "client-sensitive",
    )

    async def fake_execute_tool(self, tool_name, tool_input):
        assert self._call_sid == "CA-GEMINI-EXCEPTION"
        assert tool_name == "book_appointment"
        assert tool_input["client_id"] == "client-sensitive"
        raise RuntimeError(
            "Jobber rejected Jane Private at 123 Secret Lane, "
            "callback +15551234567, gate code 2468, client-sensitive."
        )

    monkeypatch.setattr(VoicePipeline, "_execute_tool", fake_execute_tool)

    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-GEMINI-EXCEPTION",
        contractor_config={"contractor_id": "c1"},
    )
    pipeline._ws = FakeWebSocket()

    sensitive_args = {
        "title": "Jane Private kitchen sink repair",
        "description": "Address 123 Secret Lane, gate code 2468, call +15551234567.",
        "client_id": "client-sensitive",
    }

    with caplog.at_level(logging.ERROR):
        await pipeline._handle_tool_calls([
            {"id": "tool-1", "name": "book_appointment", "args": sensitive_args}
        ])

    assert len(pipeline._ws.sent) == 1
    sent_responses = pipeline._ws.sent[0]["tool_response"]["function_responses"]
    assert len(sent_responses) == 1
    assert sent_responses[0]["id"] == "tool-1"
    assert sent_responses[0]["name"] == "book_appointment"
    sent_payload = sent_responses[0]["response"]
    assert sent_payload["success"] is False
    assert sent_payload["error"] == "unavailable"
    # See the sibling assertion above: a failure with no recovery instruction is
    # what left a real caller in 54 seconds of silence.
    assert "instruction" in sent_payload
    assert "book_appointment" in caplog.text
    assert "call=CA-GEMIN" in caplog.text
    assert "CA-GEMINI-EXCEPTION" not in caplog.text
    assert "RuntimeError" in caplog.text
    response_payload = json.dumps(pipeline._ws.sent)
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text
        assert sensitive_value not in response_payload


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
        "google_calendar_refresh_token": "gcal-refresh",
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
                                "start_time": _upcoming(13),
                                "end_time": _upcoming(14),
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
        "google_calendar_refresh_token": "gcal-refresh",
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

    assert "voice_event event=tool_call call=CA123 tool=book_appointment" in caplog.text
    for sensitive_value in (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        _upcoming(13),
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


@pytest.mark.asyncio
async def test_gemini_staging_disables_model_tools_and_denies_calls_without_payload_logs(
    monkeypatch,
    caplog,
):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    async def fail_if_executed(*_args, **_kwargs):
        raise AssertionError("A staging model tool must not execute")

    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: True,
    )
    monkeypatch.setattr(VoicePipeline, "_execute_tool", fail_if_executed)
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-GEMINI-STAGING-PRIVATE",
        contractor_config={
            "contractor_id": "c1",
            "jobber_access_token": "private-jobber-token",
            "jobber_refresh_token": "private-jobber-refresh",
            "google_calendar_access_token": "private-calendar-token",
            "google_calendar_refresh_token": "private-calendar-refresh",
        },
    )
    pipeline._ws = FakeWebSocket()
    private_tool_args = {
        "phone": "+15551234567",
        "note": "private address and customer information",
    }
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    assert pipeline._build_gemini_tools() == []
    await pipeline._handle_tool_calls([
        {
            "id": "private-tool-id",
            "name": "check_customer",
            "args": private_tool_args,
        }
    ])

    assert pipeline._ws.sent == [{
        "tool_response": {
            "function_responses": [{
                "id": "private-tool-id",
                "name": "check_customer",
                "response": {"error": "Tools are unavailable for this call."},
            }],
        }
    }]
    assert "voice_timing event=live_tools_disabled" in caplog.text
    assert "voice_timing event=tool_call_denied" in caplog.text
    for private_value in (
        "CA-GEMINI-STAGING-PRIVATE",
        "private-jobber-token",
        "private-calendar-token",
        "+15551234567",
        "private address",
    ):
        assert private_value not in caplog.text


def test_gemini_nonstaging_retains_configured_model_tools(monkeypatch):
    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        contractor_config={
            "contractor_id": "c1",
            "jobber_access_token": "configured",
            "jobber_refresh_token": "configured-refresh",
        },
    )

    declarations = pipeline._build_gemini_tools()

    assert declarations == [{
        "function_declarations": [{
            "name": "check_customer",
            "description": (
                "Look up the caller in the business's customer database by phone number."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "phone": {
                        "type": "STRING",
                        "description": "Phone number in E.164 format",
                    },
                },
                "required": ["phone"],
            },
        }],
    }]


def test_gemini_personal_mode_suppresses_tools_even_when_tokens_configured(monkeypatch):
    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        contractor_config={
            "contractor_id": "c1",
            "effective_mode": "personal",
            "jobber_access_token": "configured",
            "jobber_refresh_token": "configured-refresh",
            "google_calendar_access_token": "configured-gcal",
        },
    )

    declarations = pipeline._build_gemini_tools()
    assert declarations == []


@pytest.mark.asyncio
async def test_claude_personal_mode_suppresses_tools_even_when_calendar_configured():
    recorded_requests = []

    class FakeClaudeClient:
        async def post(self, _url, headers=None, json=None, timeout=None):
            recorded_requests.append(json)

            class FakeResponse:
                status_code = 200

                def json(self):
                    return {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "Got it."}],
                    }

            return FakeResponse()

    pipeline = _pipeline({
        "contractor_id": "c1",
        "effective_mode": "personal",
        "google_calendar_access_token": "gcal-token",
        "google_calendar_refresh_token": "gcal-refresh",
    })
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeClaudeClient()

    async def fake_speak(_text):
        return None

    pipeline._speak = fake_speak

    await pipeline._handle_caller_speech("Can I talk to Deli?")

    assert len(recorded_requests) == 1
    assert "tools" not in recorded_requests[0]
