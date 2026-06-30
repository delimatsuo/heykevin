import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.api import calls as calls_api


class _Doc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _DocRef:
    def __init__(self, store, sid):
        self.store = store
        self.sid = sid

    def get(self):
        return _Doc(self.store.get(self.sid))

    def set(self, data, merge=False):
        existing = self.store.setdefault(self.sid, {})
        existing.update(data)


class _Collection:
    def __init__(self, store):
        self.store = store

    def document(self, sid):
        return _DocRef(self.store, sid)


class _DB:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        assert name == "calls"
        return _Collection(self.store)


class _State:
    contractor_id = "owner-1"
    is_admin = False


class _Request:
    state = _State()


@pytest.mark.asyncio
async def test_mark_read_rejects_mixed_owner_sids(monkeypatch):
    store = {
        "CA-owner": {"contractor_id": "owner-1", "read": False},
        "CA-other": {"contractor_id": "owner-2", "read": False},
    }
    monkeypatch.setattr(calls_api, "get_firestore_client", lambda: _DB(store))

    with pytest.raises(HTTPException) as exc:
        await calls_api.api_mark_calls_read(
            calls_api.MarkReadRequest(call_sids=["CA-owner", "CA-other"]),
            _Request(),
        )

    assert exc.value.status_code == 403
    assert store["CA-owner"]["read"] is False
    assert store["CA-other"]["read"] is False


@pytest.mark.asyncio
async def test_mark_read_updates_only_owned_sids(monkeypatch):
    store = {
        "CA-owner-1": {"contractor_id": "owner-1", "read": False},
        "CA-owner-2": {"contractor_id": "owner-1", "read": False},
    }
    monkeypatch.setattr(calls_api, "get_firestore_client", lambda: _DB(store))

    result = await calls_api.api_mark_calls_read(
        calls_api.MarkReadRequest(call_sids=["CA-owner-1", "CA-owner-2"]),
        _Request(),
    )

    assert result == {"status": "ok", "updated": 2}
    assert store["CA-owner-1"]["read"] is True
    assert store["CA-owner-2"]["read"] is True
