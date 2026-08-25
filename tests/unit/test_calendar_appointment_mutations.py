"""Focused Google Calendar appointment update and cancellation tests."""

import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services import calendar


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, calls: list, responses: list[_FakeResponse]):
        self.calls = calls
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def _request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    async def post(self, url: str, **kwargs):
        return await self._request("POST", url, **kwargs)

    async def get(self, url: str, **kwargs):
        return await self._request("GET", url, **kwargs)

    async def patch(self, url: str, **kwargs):
        return await self._request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs):
        return await self._request("DELETE", url, **kwargs)


def _patch_client(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(
        calendar.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(calls, responses),
    )
    return calls


class _FakeDocRef:
    def __init__(self, data=None):
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []

    def get(self, *args, transaction=None, **kwargs):
        import datetime
        import time

        class _Snap:
            def __init__(self, d, deleted):
                self._d = dict(d) if d is not None else None
                self.exists = (d is not None) and (not deleted)
                self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc)

            def to_dict(self):
                return dict(self._d) if self.exists else {}

        return _Snap(self.data, self.deleted)

    def update(self, updates, *args, **kwargs):
        if self.data is None:
            self.data = {}
        self.updates.append(dict(updates))
        for k, v in updates.items():
            if str(type(v).__name__) == "Sentinel" or "DELETE" in str(v):
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def set(self, data, *args, **kwargs):
        self.data = dict(data)
        self.deleted = False

    def delete(self, *args, **kwargs):
        self.deleted = True
        self.data = None


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
            raise RuntimeError("Firestore transaction read-after-write violation: all reads must occur before writes/deletes/creates")
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


class _FakeFirestore:
    def __init__(self, collections=None):
        self.collections = collections or {}
        self.last_transaction = None

    def collection(self, name):
        class _Coll:
            def __init__(self, docs):
                self.docs = docs

            def document(self, doc_id):
                return self.docs.setdefault(doc_id, _FakeDocRef({"contractor_id": doc_id, "active": True}))

        return _Coll(self.collections.setdefault(name, {}))

    def transaction(self):
        tx = _FakeTransaction(self)
        self.last_transaction = tx
        return tx


def _contractor(**overrides):
    contractor = {
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
    }
    contractor.update(overrides)
    return contractor


@pytest.fixture(autouse=True)
def _calendar_state(monkeypatch):
    import base64
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app import config
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")

    calendar._REFRESH_LOCKS.clear()
    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")
    doc_ref = _FakeDocRef({
        "contractor_id": "contractor-1",
        "active": True,
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_connected": True,
    })
    db = _FakeFirestore({"contractors": {"contractor-1": doc_ref}})
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    yield
    calendar._REFRESH_LOCKS.clear()


@pytest.mark.asyncio
async def test_update_appointment_encodes_event_id_and_sends_partial_patch(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "provider-id"})])

    updated = await calendar.update_appointment(
        _contractor(),
        "event/with spaces?private=true",
        "2026-08-13T09:00:00-04:00",
        "2026-08-13T10:00:00-04:00",
        title="Drain cleaning",
    )

    assert updated is True
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "PATCH"
    assert url == (f"{calendar.EVENTS_URL}/event%2Fwith%20spaces%3Fprivate%3Dtrue")
    assert kwargs["headers"]["Authorization"] == "Bearer access-token"
    assert kwargs["json"] == {
        "start": {"dateTime": "2026-08-13T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-13T10:00:00-04:00"},
        "summary": "Drain cleaning",
    }


@pytest.mark.asyncio
async def test_managed_create_uses_explicit_id_and_operation_marker(monkeypatch):
    operation_id = "a" * 64
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "event-1"})])

    created = await calendar.create_managed_appointment(
        _contractor(),
        event_id="event-1",
        title="Furnace tune-up",
        start_time="2026-08-13T09:00:00-04:00",
        end_time="2026-08-13T10:00:00-04:00",
        description="Caller notes",
        logical_operation_id=operation_id,
    )

    assert created is True
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", calendar.EVENTS_URL)
    assert kwargs["json"] == {
        "id": "event-1",
        "summary": "Furnace tune-up",
        "description": "Caller notes",
        "start": {"dateTime": "2026-08-13T09:00:00-04:00"},
        "end": {"dateTime": "2026-08-13T10:00:00-04:00"},
        "extendedProperties": {"private": {calendar.HEY_KEVIN_OPERATION_PRIVATE_KEY: operation_id}},
    }


@pytest.mark.asyncio
async def test_managed_create_verifies_our_duplicate_before_success(monkeypatch):
    operation_id = "b" * 64
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                409,
                {"error": {"errors": [{"reason": "duplicate"}]}},
            ),
            _FakeResponse(
                200,
                {
                    "start": {"dateTime": "2026-08-13T09:00:00-04:00"},
                    "end": {"dateTime": "2026-08-13T10:00:00-04:00"},
                    "extendedProperties": {
                        "private": {calendar.HEY_KEVIN_OPERATION_PRIVATE_KEY: operation_id}
                    },
                },
            ),
        ],
    )

    created = await calendar.create_managed_appointment(
        _contractor(),
        event_id="event-1",
        title="Furnace tune-up",
        start_time="2026-08-13T09:00:00-04:00",
        end_time="2026-08-13T10:00:00-04:00",
        description="Caller notes",
        logical_operation_id=operation_id,
    )

    assert created is True
    assert [method for method, _url, _kwargs in calls] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_managed_create_rejects_unverified_duplicate(monkeypatch):
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                409,
                {"error": {"errors": [{"reason": "duplicate"}]}},
            ),
            _FakeResponse(
                200,
                {
                    "start": {"dateTime": "2026-08-13T09:00:00-04:00"},
                    "end": {"dateTime": "2026-08-13T10:00:00-04:00"},
                    "extendedProperties": {
                        "private": {calendar.HEY_KEVIN_OPERATION_PRIVATE_KEY: "c" * 64}
                    },
                },
            ),
        ],
    )

    created = await calendar.create_managed_appointment(
        _contractor(),
        event_id="event-1",
        title="Furnace tune-up",
        start_time="2026-08-13T09:00:00-04:00",
        end_time="2026-08-13T10:00:00-04:00",
        description="Caller notes",
        logical_operation_id="d" * 64,
    )

    assert created is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_update_appointment_can_deliberately_clear_optional_metadata(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(200)])

    updated = await calendar.update_appointment(
        _contractor(),
        "event-1",
        "2026-08-13T09:00:00-04:00",
        "2026-08-13T10:00:00-04:00",
        title="",
        description="",
    )

    assert updated is True
    assert calls[0][2]["json"]["summary"] == ""
    assert calls[0][2]["json"]["description"] == ""


@pytest.mark.asyncio
async def test_service_metadata_preserves_event_and_other_private_properties(monkeypatch):
    private_value = '{"v":1,"services":["Furnace tune-up","Drain cleaning"]}'
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-1"',
                    "summary": "Original title",
                    "description": "Customer supplied notes",
                    "start": {"dateTime": "2026-08-13T09:00:00-04:00"},
                    "end": {"dateTime": "2026-08-13T10:00:00-04:00"},
                    "extendedProperties": {
                        "private": {"another_integration": "keep-me"},
                        "shared": {"visible_to_attendees": "keep-remotely"},
                    },
                },
            ),
            _FakeResponse(200),
        ],
    )

    updated = await calendar.set_appointment_service_metadata(
        _contractor(),
        "event/with spaces",
        private_value,
    )

    assert updated is True
    event_url = f"{calendar.EVENTS_URL}/event%2Fwith%20spaces"
    assert calls[0] == (
        "GET",
        event_url,
        {
            "headers": {"Authorization": "Bearer access-token"},
            "timeout": 8.0,
        },
    )
    method, url, kwargs = calls[1]
    assert (method, url) == ("PATCH", event_url)
    assert kwargs["headers"] == {
        "Authorization": "Bearer access-token",
        "Content-Type": "application/json",
        "If-Match": '"etag-1"',
    }
    assert kwargs["json"] == {
        "extendedProperties": {
            "private": {
                "another_integration": "keep-me",
                calendar.HEY_KEVIN_SERVICES_PRIVATE_KEY: private_value,
            }
        }
    }
    assert "summary" not in kwargs["json"]
    assert "description" not in kwargs["json"]
    assert "start" not in kwargs["json"]
    assert "end" not in kwargs["json"]


@pytest.mark.asyncio
async def test_service_metadata_exact_replay_is_get_only(monkeypatch):
    private_value = '{"v":1,"services":["Drain cleaning"]}'
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-1"',
                    "extendedProperties": {
                        "private": {
                            calendar.HEY_KEVIN_SERVICES_PRIVATE_KEY: private_value,
                        }
                    },
                },
            )
        ],
    )

    updated = await calendar.set_appointment_service_metadata(
        _contractor(),
        "event-1",
        private_value,
    )

    assert updated is True
    assert [method for method, _url, _kwargs in calls] == ["GET"]


@pytest.mark.asyncio
async def test_service_metadata_refetches_and_remerges_once_after_412(monkeypatch):
    private_value = '{"v":1,"services":["Drain cleaning"]}'
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-1"',
                    "extendedProperties": {"private": {"first": "preserved"}},
                },
            ),
            _FakeResponse(412, {"private": "must not be logged"}),
            _FakeResponse(
                200,
                {
                    "etag": '"etag-2"',
                    "extendedProperties": {
                        "private": {
                            "first": "preserved",
                            "concurrent": "also-preserved",
                        }
                    },
                },
            ),
            _FakeResponse(200),
        ],
    )

    updated = await calendar.set_appointment_service_metadata(
        _contractor(),
        "event-1",
        private_value,
    )

    assert updated is True
    assert [method for method, _url, _kwargs in calls] == [
        "GET",
        "PATCH",
        "GET",
        "PATCH",
    ]
    assert calls[1][2]["headers"]["If-Match"] == '"etag-1"'
    assert calls[3][2]["headers"]["If-Match"] == '"etag-2"'
    assert calls[3][2]["json"]["extendedProperties"]["private"] == {
        "first": "preserved",
        "concurrent": "also-preserved",
        calendar.HEY_KEVIN_SERVICES_PRIVATE_KEY: private_value,
    }


@pytest.mark.asyncio
async def test_service_metadata_failure_does_not_log_event_or_response(monkeypatch, caplog):
    event_id = "private-event-id"
    private_body = "Jonathan at 123 Secret Lane"
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-1"',
                    "description": private_body,
                },
            ),
            _FakeResponse(403, {"error": private_body}),
        ],
    )

    with caplog.at_level(logging.ERROR, logger="app.services.calendar"):
        updated = await calendar.set_appointment_service_metadata(
            _contractor(),
            event_id,
            '{"v":1,"services":["Drain cleaning"]}',
        )

    assert updated is False
    assert len(calls) == 2
    assert "status_code=403" in caplog.text
    assert event_id not in caplog.text
    assert private_body not in caplog.text


@pytest.mark.asyncio
async def test_service_metadata_rejects_oversized_value_without_http(monkeypatch):
    calls = _patch_client(monkeypatch, [])

    updated = await calendar.set_appointment_service_metadata(
        _contractor(),
        "event-1",
        "x" * (calendar.MAX_PRIVATE_PROPERTY_VALUE_BYTES + 1),
    )

    assert updated is False
    assert calls == []


@pytest.mark.asyncio
async def test_cancel_appointment_encodes_event_id_and_accepts_no_content(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(204)])

    cancelled = await calendar.cancel_appointment(
        _contractor(),
        "event/with spaces",
    )

    assert cancelled is True
    assert calls == [
        (
            "DELETE",
            f"{calendar.EVENTS_URL}/event%2Fwith%20spaces",
            {
                "headers": {"Authorization": "Bearer access-token"},
                "timeout": 8.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_cancel_appointment_accepts_already_deleted_retry(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(410)])

    cancelled = await calendar.cancel_appointment(_contractor(), "event-1")

    assert cancelled is True
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "cancel"])
async def test_appointment_mutation_refreshes_once_after_401(monkeypatch, operation):
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(401, {"error": "expired"}),
            _FakeResponse(200, {"access_token": "fresh-token", "expires_in": 3600}),
            _FakeResponse(204 if operation == "cancel" else 200),
        ],
    )
    contractor = _contractor()

    if operation == "cancel":
        succeeded = await calendar.cancel_appointment(contractor, "event-1")
    else:
        succeeded = await calendar.update_appointment(
            contractor,
            "event-1",
            "2026-08-13T09:00:00-04:00",
            "2026-08-13T10:00:00-04:00",
        )

    assert succeeded is True
    mutation_calls = [call for call in calls if call[0] != "POST"]
    assert len(mutation_calls) == 2
    assert mutation_calls[0][2]["headers"]["Authorization"] == "Bearer access-token"
    assert calls[1][0:2] == ("POST", calendar.TOKEN_URL)
    assert mutation_calls[1][2]["headers"]["Authorization"] == "Bearer fresh-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "cancel"])
async def test_appointment_mutation_failure_is_false_and_does_not_log_provider_body(
    monkeypatch,
    caplog,
    operation,
):
    private_body = "Private appointment for Jonathan at 123 Secret Lane"
    calls = _patch_client(
        monkeypatch,
        [_FakeResponse(403, {"error": private_body})],
    )

    with caplog.at_level(logging.ERROR, logger="app.services.calendar"):
        if operation == "cancel":
            succeeded = await calendar.cancel_appointment(_contractor(), "event-1")
        else:
            succeeded = await calendar.update_appointment(
                _contractor(),
                "event-1",
                "2026-08-13T09:00:00-04:00",
                "2026-08-13T10:00:00-04:00",
            )

    assert succeeded is False
    assert len(calls) == 1
    assert "status_code=403" in caplog.text
    assert private_body not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "cancel"])
async def test_appointment_mutation_without_access_token_is_false_without_http(
    monkeypatch,
    operation,
):
    calls = _patch_client(monkeypatch, [])
    contractor = {
        "contractor_id": "contractor-unauthenticated",
        "google_calendar_access_token": "",
        "google_calendar_refresh_token": "",
    }

    if operation == "cancel":
        succeeded = await calendar.cancel_appointment(contractor, "event-1")
    else:
        succeeded = await calendar.update_appointment(
            contractor,
            "event-1",
            "2026-08-13T09:00:00-04:00",
            "2026-08-13T10:00:00-04:00",
        )

    assert succeeded is False
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "cancel"])
async def test_appointment_mutation_rejects_empty_event_id_without_http(
    monkeypatch,
    operation,
):
    calls = _patch_client(monkeypatch, [])

    if operation == "cancel":
        succeeded = await calendar.cancel_appointment(_contractor(), "")
    else:
        succeeded = await calendar.update_appointment(
            _contractor(),
            "",
            "2026-08-13T09:00:00-04:00",
            "2026-08-13T10:00:00-04:00",
        )

    assert succeeded is False
    assert calls == []
