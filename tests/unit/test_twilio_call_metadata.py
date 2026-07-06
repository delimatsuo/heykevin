"""Call metadata persistence tests for Twilio webhooks."""

import os
from types import SimpleNamespace

import firebase_admin
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.db import cache, calls as call_db
from app.webhooks import twilio_incoming


class _FakeFormRequest:
    def __init__(self, form: dict[str, str]):
        self._form = form

    async def form(self):
        return self._form


@pytest.mark.asyncio
async def test_status_webhook_persists_terminal_duration_and_outcome(monkeypatch):
    saved_calls = []
    deleted_paths = []

    async def fake_save_call(call_sid, updates):
        saved_calls.append((call_sid, dict(updates)))

    class _FakeRef:
        def __init__(self, path):
            self.path = path

        def delete(self):
            deleted_paths.append(self.path)

    monkeypatch.setattr(call_db, "save_call", fake_save_call)
    monkeypatch.setattr(call_db, "get_call", lambda call_sid: _async_return({}))
    monkeypatch.setattr(cache, "_init_firebase", lambda: None)
    monkeypatch.setattr(
        firebase_admin,
        "db",
        SimpleNamespace(reference=lambda path: _FakeRef(path)),
        raising=False,
    )

    response = await twilio_incoming.handle_status(_FakeFormRequest({
        "CallSid": "CA123",
        "CallStatus": "completed",
        "CallDuration": "37",
    }))

    assert response == {"status": "ok"}
    assert deleted_paths == ["/active_calls/CA123"]
    assert saved_calls == [("CA123", {
        "twilio_status": "completed",
        "duration_seconds": 37,
        "outcome": "completed",
    })]


async def _async_return(value):
    return value
