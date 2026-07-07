"""Twilio call status persistence tests."""

import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.webhooks import twilio_incoming


class _FakeFormRequest:
    def __init__(self, form: dict[str, str]):
        self._form = form

    async def form(self):
        return self._form


class _FakeRtdbReference:
    def __init__(self, deleted: list[str], path: str):
        self._deleted = deleted
        self._path = path

    def delete(self):
        self._deleted.append(self._path)


@pytest.mark.asyncio
async def test_twilio_completed_status_persists_duration_and_final_status(monkeypatch):
    saved_calls = []
    deleted_paths = []

    async def fake_save_call(call_sid, updates):
        saved_calls.append((call_sid, dict(updates)))

    monkeypatch.setattr("app.db.cache._init_firebase", lambda: None)
    monkeypatch.setattr(
        "firebase_admin.db.reference",
        lambda path: _FakeRtdbReference(deleted_paths, path),
    )
    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr(twilio_incoming.time, "time", lambda: 12345.0)

    response = await twilio_incoming.handle_status(
        _FakeFormRequest({
            "CallSid": "CA123",
            "CallStatus": "completed",
            "CallDuration": "124",
        })
    )

    assert response == {"status": "ok"}
    assert deleted_paths == ["/active_calls/CA123"]
    assert saved_calls == [
        (
            "CA123",
            {
                "call_status": "completed",
                "duration_seconds": 124,
                "ended_at": 12345.0,
            },
        )
    ]
