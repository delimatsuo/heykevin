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


@pytest.mark.asyncio
async def test_reschedule_appointment_remote_desired_is_get_only_success(monkeypatch):
    event_id = "event-1"
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-desired-1"',
                    "start": {"dateTime": desired_start},
                    "end": {"dateTime": desired_end},
                },
            )
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        event_id,
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is True
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == f"{calendar.EVENTS_URL}/{event_id}"


@pytest.mark.asyncio
async def test_reschedule_appointment_third_schedule_is_get_only_conflict(monkeypatch):
    event_id = "event-1"
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"
    third_start = "2026-08-13T14:00:00-04:00"
    third_end = "2026-08-13T15:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-third-1"',
                    "start": {"dateTime": third_start},
                    "end": {"dateTime": third_end},
                },
            )
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        event_id,
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    assert len(calls) == 1
    method, url, _kwargs = calls[0]
    assert method == "GET"
    assert url == f"{calendar.EVENTS_URL}/{event_id}"


@pytest.mark.asyncio
async def test_reschedule_appointment_remote_base_executes_conditional_patch_with_if_match(
    monkeypatch,
):
    event_id = "event/with spaces"
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-base-1"',
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            ),
            _FakeResponse(
                200,
                {
                    "etag": '"etag-desired-2"',
                    "start": {"dateTime": desired_start},
                    "end": {"dateTime": desired_end},
                },
            ),
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        event_id,
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is True
    assert len(calls) == 2
    get_call, patch_call = calls

    assert get_call[0] == "GET"
    assert get_call[1] == f"{calendar.EVENTS_URL}/event%2Fwith%20spaces"

    method, url, kwargs = patch_call
    assert method == "PATCH"
    assert url == f"{calendar.EVENTS_URL}/event%2Fwith%20spaces"
    assert kwargs["headers"]["If-Match"] == '"etag-base-1"'
    assert kwargs["headers"]["Authorization"] == "Bearer access-token"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["json"] == {
        "start": {"dateTime": desired_start},
        "end": {"dateTime": desired_end},
    }
    assert "summary" not in kwargs["json"]
    assert "description" not in kwargs["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_etag",
    [
        None,
        "",
        "   ",
        " \"leading-space\"",
        "\"trailing-space\" ",
        "  \"both-spaces\"  ",
        "\t\"tab-padded\"",
        "\"newline-padded\"\n",
        "etag\nwith\nnewlines",
        "\x1fbad",
        "a" * 1025,
        # A3: strong-ETag structural rejections
        "*",                          # wildcard
        "unquoted-opaque-value",      # no surrounding quotes
        'W/"weak-tag"',               # weak entity-tag
        '"missing-close-quote',       # open quote, no close
        'missing-open-quote"',        # close quote, no open
        '"interior"quote"',           # DQUOTE inside the opaque part
        # P2: interior etagc grammar — only %x21 / %x23-7E accepted
        '"quoted space interior"',    # SP (0x20) inside opaque part
        '"quoted-\xe9tag"',           # Latin-1 é (0xE9) — obs-text rejected
        '"quoted-\u2019tag"',         # Unicode U+2019 — non-ASCII rejected
    ],
)
async def test_reschedule_appointment_rejects_missing_or_malformed_etag_without_patch(
    monkeypatch, bad_etag
):
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    event_payload = {
        "start": {"dateTime": base_start},
        "end": {"dateTime": base_end},
    }
    if bad_etag is not None:
        event_payload["etag"] = bad_etag

    calls = _patch_client(monkeypatch, [_FakeResponse(200, event_payload)])

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    assert len(calls) == 1
    assert calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_reschedule_appointment_forwards_opaque_etag_without_stripping(monkeypatch):
    event_id = "event-opaque-etag"
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"
    opaque_etag = '"etag-complex:opaque_value/123=abc"'

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": opaque_etag,
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            ),
            _FakeResponse(
                200,
                {
                    "etag": '"etag-new-456"',
                    "start": {"dateTime": desired_start},
                    "end": {"dateTime": desired_end},
                },
            ),
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        event_id,
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is True
    assert len(calls) == 2
    patch_call = calls[1]
    assert patch_call[0] == "PATCH"
    assert patch_call[2]["headers"]["If-Match"] == opaque_etag


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_event_shape",
    [
        {"status": "cancelled", "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"start": {"date": "2026-08-13"}, "end": {"date": "2026-08-14"}, "etag": '"e1"'},
        {"recurrence": [], "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"recurrence": ["RRULE:FREQ=DAILY"], "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"recurringEventId": "", "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"recurringEventId": "master-event-id", "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"originalStartTime": {}, "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"originalStartTime": {"dateTime": "2026-08-13T09:00:00-04:00"}, "start": {"dateTime": "2026-08-13T09:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"start": {"dateTime": "not-a-date"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
        {"start": {"dateTime": "2026-08-13T09:00:00"}, "end": {"dateTime": "2026-08-13T10:00:00"}, "etag": '"e1"'},
        {"start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T10:00:00-04:00"}, "etag": '"e1"'},
    ],
)
async def test_reschedule_appointment_rejects_unsafe_shapes_without_patch(
    monkeypatch, bad_event_shape
):
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(monkeypatch, [_FakeResponse(200, bad_event_shape)])

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    assert len(calls) == 1
    assert calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_reschedule_appointment_patch_412_returns_false_with_no_second_patch(monkeypatch):
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-base-1"',
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            ),
            _FakeResponse(412, {"error": "Precondition Failed"}),
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_patch_payload",
    [
        {},
        {"etag": '"etag-new"'},
        {"etag": '"etag-new"', "start": {"dateTime": "not-a-date"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}},
        {"etag": '"etag-new"', "start": {"date": "2026-08-13"}, "end": {"date": "2026-08-14"}},
        {"etag": '"etag-new"', "start": {"dateTime": "2026-08-13T15:00:00-04:00"}, "end": {"dateTime": "2026-08-13T16:00:00-04:00"}},
        {"etag": '"etag-new"', "start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}, "status": "cancelled"},
        {"etag": '"etag-new"', "start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}, "recurrence": []},
        {"etag": '"etag-new"', "start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}, "recurringEventId": ""},
        {"etag": '"etag-new"', "start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}, "originalStartTime": {}},
        {"start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}},
        {"etag": "  \"etag-padded\"  ", "start": {"dateTime": "2026-08-13T11:00:00-04:00"}, "end": {"dateTime": "2026-08-13T12:00:00-04:00"}},
    ],
)
async def test_reschedule_appointment_patch_2xx_invalid_variants_return_false(
    monkeypatch, invalid_patch_payload
):
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-base-1"',
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            ),
            _FakeResponse(200, invalid_patch_payload),
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_reschedule_appointment_get_401_refreshes_and_uses_retried_get_etag(monkeypatch):
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(401, {"error": "expired"}),
            _FakeResponse(200, {"access_token": "fresh-token-1", "expires_in": 3600}),
            _FakeResponse(
                200,
                {
                    "etag": '"fresh-etag-from-retried-get"',
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            ),
            _FakeResponse(
                200,
                {
                    "etag": '"patch-etag-final"',
                    "start": {"dateTime": desired_start},
                    "end": {"dateTime": desired_end},
                },
            ),
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is True
    get_calls = [c for c in calls if c[0] == "GET"]
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert len(get_calls) == 2
    assert len(patch_calls) == 1
    assert get_calls[0][2]["headers"]["Authorization"] == "Bearer access-token"
    assert get_calls[1][2]["headers"]["Authorization"] == "Bearer fresh-token-1"
    assert patch_calls[0][2]["headers"]["If-Match"] == '"fresh-etag-from-retried-get"'
    assert patch_calls[0][2]["headers"]["Authorization"] == "Bearer fresh-token-1"


@pytest.mark.asyncio
async def test_reschedule_appointment_patch_401_refreshes_and_retries_failing_if_412(
    monkeypatch,
):
    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"initial-etag-1"',
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            ),
            _FakeResponse(401, {"error": "expired"}),
            _FakeResponse(200, {"access_token": "fresh-token-2", "expires_in": 3600}),
            _FakeResponse(412, {"error": "Precondition Failed on retry"}),
        ],
    )

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert len(patch_calls) == 2

    # Assert identical schedule-only bodies and no other event fields
    expected_body = {
        "start": {"dateTime": desired_start},
        "end": {"dateTime": desired_end},
    }
    assert patch_calls[0][2]["json"] == expected_body
    assert patch_calls[1][2]["json"] == expected_body
    assert "summary" not in patch_calls[0][2]["json"]
    assert "description" not in patch_calls[0][2]["json"]
    assert "summary" not in patch_calls[1][2]["json"]
    assert "description" not in patch_calls[1][2]["json"]

    # Assert identical If-Match with only Authorization changing
    assert patch_calls[0][2]["headers"]["If-Match"] == '"initial-etag-1"'
    assert patch_calls[1][2]["headers"]["If-Match"] == '"initial-etag-1"'
    assert patch_calls[0][2]["headers"]["Authorization"] == "Bearer access-token"
    assert patch_calls[1][2]["headers"]["Authorization"] == "Bearer fresh-token-2"


@pytest.mark.asyncio
async def test_reschedule_appointment_logging_privacy(monkeypatch, caplog):
    # --- Unique sentinels -------------------------------------------------------
    sentinel_access = "SENTINEL_ACCESS_TOKEN_SECRET_999"
    sentinel_refresh = "SENTINEL_REFRESH_TOKEN_SECRET_888"
    sentinel_cid = "contractor-sentinel-cid-777"
    sentinel_customer_id = "SENTINEL_CUSTOMER_ID_B2_444"
    sentinel_customer_key = "SENTINEL_CUSTOMER_KEY_B2_443"
    sentinel_request_id = "SENTINEL_REQUEST_ID_B2_442"
    sentinel_logical_op = "SENTINEL_LOGICAL_OPERATION_B2_441"
    sentinel_eid = "event-sentinel-eid-666"
    sentinel_etag = '"etag-sentinel-opaque-555"'
    sentinel_base_start = "2026-08-13T09:17:33-04:00"
    sentinel_base_end = "2026-08-13T10:17:33-04:00"
    sentinel_desired_start = "2026-08-13T11:44:55-04:00"
    sentinel_desired_end = "2026-08-13T12:44:55-04:00"
    sentinel_summary = "CONFIDENTIAL_PATIENT_INTAKE_SUMMARY_333"
    sentinel_description = "CONFIDENTIAL_DIAGNOSTIC_BODY_222"
    sentinel_body_secret = "PROVIDER_INTERNAL_ERROR_DIAGNOSTIC_SECRET_111"
    sentinel_exc_msg = "SENTINEL_TRANSPORT_EXCEPTION_MESSAGE_B2_110"
    sentinel_url = f"{calendar.EVENTS_URL}/{sentinel_eid}"

    # --- Contractor dict: carries unique extra fields so whole-object logging fails ---
    contractor = _contractor(
        contractor_id=sentinel_cid,
        google_calendar_access_token=sentinel_access,
        google_calendar_refresh_token=sentinel_refresh,
        customer_id=sentinel_customer_id,
        customer_key=sentinel_customer_key,
        request_id=sentinel_request_id,
        logical_operation_id=sentinel_logical_op,
    )

    # --- Firestore doc: same extra fields so snapshot logging fails -------------
    import app.db.firestore_client as firestore_module
    db = firestore_module.get_firestore_client()
    db.collections.setdefault("contractors", {})[sentinel_cid] = _FakeDocRef({
        "contractor_id": sentinel_cid,
        "active": True,
        "google_calendar_access_token": sentinel_access,
        "google_calendar_refresh_token": sentinel_refresh,
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_connected": True,
        "customer_id": sentinel_customer_id,
        "customer_key": sentinel_customer_key,
        "request_id": sentinel_request_id,
        "logical_operation_id": sentinel_logical_op,
    })

    # --- Path 1: GET returns 200 with private event data; PATCH returns 403 -----
    calls_1 = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": sentinel_etag,
                    "start": {"dateTime": sentinel_base_start},
                    "end": {"dateTime": sentinel_base_end},
                    "summary": sentinel_summary,
                    "description": sentinel_description,
                },
            ),
            _FakeResponse(403, {"error": sentinel_body_secret}),
        ],
    )

    with caplog.at_level(logging.DEBUG):
        result_1 = await calendar.reschedule_appointment(
            contractor,
            sentinel_eid,
            base_start=sentinel_base_start,
            base_end=sentinel_base_end,
            desired_start=sentinel_desired_start,
            desired_end=sentinel_desired_end,
        )

    assert result_1 is False
    assert len(calls_1) == 2
    assert calls_1[0][0] == "GET"
    assert calls_1[1][0] == "PATCH"
    assert "status_code=403" in caplog.text

    # --- Path 2: GET raises RuntimeError containing a unique exception message --
    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **_kwargs):
            raise RuntimeError(sentinel_exc_msg)

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _RaisingClient)

    with caplog.at_level(logging.DEBUG):
        result_2 = await calendar.reschedule_appointment(
            contractor,
            sentinel_eid,
            base_start=sentinel_base_start,
            base_end=sentinel_base_end,
            desired_start=sentinel_desired_start,
            desired_end=sentinel_desired_end,
        )

    assert result_2 is False
    # Coarse exception_type is logged; the raw exception message must not be.
    assert "exception_type=RuntimeError" in caplog.text
    assert sentinel_exc_msg not in caplog.text

    # --- Every sentinel individually absent from all captured logs --------------
    all_sentinels = [
        sentinel_access,
        sentinel_refresh,
        sentinel_cid,
        sentinel_customer_id,
        sentinel_customer_key,
        sentinel_request_id,
        sentinel_logical_op,
        sentinel_eid,
        sentinel_etag,
        sentinel_base_start,
        sentinel_base_end,
        sentinel_desired_start,
        sentinel_desired_end,
        sentinel_summary,
        sentinel_description,
        sentinel_body_secret,
        sentinel_exc_msg,
        sentinel_url,
    ]
    for s in all_sentinels:
        assert s not in caplog.text, f"Sentinel leaked in logs: {s!r}"


@pytest.mark.asyncio
async def test_reschedule_appointment_call_exception_terminalizes_intent_and_allows_reconciliation(
    monkeypatch,
):
    import app.db.firestore_client as firestore_module
    from app.services.integration_tokens import parse_provider_operation_intent

    db = firestore_module.get_firestore_client()
    contractor_data = db.collection("contractors").document("contractor-1").get().to_dict()
    assert contractor_data is not None

    base_start = "2026-08-13T09:00:00-04:00"
    base_end = "2026-08-13T10:00:00-04:00"
    desired_start = "2026-08-13T11:00:00-04:00"
    desired_end = "2026-08-13T12:00:00-04:00"

    # 1. First attempt: GET base schedule, PATCH simulates timeout after server recorded write
    calls = []

    class _TimeoutAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-base-1"',
                    "start": {"dateTime": base_start},
                    "end": {"dateTime": base_end},
                },
            )

        async def patch(self, url, **kwargs):
            calls.append(("PATCH", url, kwargs))
            raise TimeoutError("Simulated transport timeout on PATCH")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _TimeoutAsyncClient)

    succeeded = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert succeeded is False
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PATCH"

    # Verify durable provider intent is NOT stuck in provider_request_started
    post_doc = db.collection("contractors").document("contractor-1").get().to_dict()
    status, intent, _ = parse_provider_operation_intent(post_doc, "google_calendar")
    assert status == "absent" or intent is None

    # 2. Second attempt: remote is now desired schedule, GET-only reconciliation succeeds
    reconcile_calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "etag": '"etag-desired-2"',
                    "start": {"dateTime": desired_start},
                    "end": {"dateTime": desired_end},
                },
            )
        ],
    )

    reconciled = await calendar.reschedule_appointment(
        _contractor(),
        "event-1",
        base_start=base_start,
        base_end=base_end,
        desired_start=desired_start,
        desired_end=desired_end,
    )

    assert reconciled is True
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0][0] == "GET"
    post_doc2 = db.collection("contractors").document("contractor-1").get().to_dict()
    status2, intent2, _ = parse_provider_operation_intent(post_doc2, "google_calendar")
    assert status2 == "absent" or intent2 is None


# ---------------------------------------------------------------------------
# P0 mutation-effective terminalization propagation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminalization_raises_after_normal_initial_response_propagates(monkeypatch):
    """P0 mutation guard: call _with_token_refresh directly so that a terminalizer exception
    after a normal (non-raising) initial provider 200 propagates to the caller.

    reschedule_appointment intentionally catches every _with_token_refresh exception and
    returns False, so we must test the invariant at the layer that owns it.

    Causal assertions:
    - Exact RuntimeError propagates from _with_token_refresh.
    - Provider callback called exactly once (initial path; no retry).
    - Terminalizer called exactly once (initial success branch; no retry).
    """
    import app.services.integration_token_mutations as mutations_module

    provider_calls: list[str] = []
    terminalize_calls: list[str] = []

    # --- Minimal durable snapshot returned for the initial authorization gate ---
    _SNAP = {
        "access_token": "access-token-initial",
        "access_token_raw": "access-token-initial",
        "refresh_token_raw": "refresh-token",
        "generation": 0,
        "lifecycle_epoch": 0,
    }

    async def _load_snap(contractor_id, *, provider):
        return _SNAP

    async def _acquire(**_kwargs):
        return ("claim-initial-1", None)

    async def _transition(**_kwargs):
        return None

    async def _terminalize(**_kwargs):
        terminalize_calls.append("terminalize")
        raise RuntimeError("terminalizer-boom-initial")

    monkeypatch.setattr(mutations_module, "load_durable_provider_snapshot", _load_snap)
    monkeypatch.setattr(mutations_module, "acquire_provider_operation_intent_cas", _acquire)
    monkeypatch.setattr(
        mutations_module, "transition_provider_operation_intent_to_started_cas", _transition
    )
    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _terminalize)

    class _FakeResp200:
        status_code = 200

    async def _call(token: str):
        provider_calls.append(f"call:{token}")
        return _FakeResp200()

    _exc = None
    try:
        await calendar._with_token_refresh(_contractor(), _call)
    except RuntimeError as e:
        _exc = e

    # Exact exception identity.
    assert _exc is not None, "Expected RuntimeError to propagate from _with_token_refresh"
    assert str(_exc) == "terminalizer-boom-initial", f"Wrong exception: {_exc}"

    # Provider called exactly once (initial path, no 401 retry triggered).
    assert provider_calls == ["call:access-token-initial"], (
        f"Expected exactly 1 initial provider call; got {provider_calls}"
    )

    # Terminalizer called exactly once (initial success branch only).
    assert terminalize_calls == ["terminalize"], (
        f"Expected exactly 1 terminalize call; got {terminalize_calls}"
    )


@pytest.mark.asyncio
async def test_terminalization_raises_after_normal_retry_response_propagates(monkeypatch):
    """P0 mutation guard: call _with_token_refresh directly so that a terminalizer exception
    after a normal (non-raising) 401-retry provider 200 propagates to the caller.

    Call order driven through the function:
      1. Initial provider call → 401 (terminal_outcome=True → initial terminalize called).
      2. refresh_access_token → truthy (refresh succeeds).
      3. load_durable_provider_snapshot (retry) → valid retry snap.
      4. Retry provider call → 200 (retry_terminal_outcome=True → retry terminalize called).
      5. Retry terminalize raises → RuntimeError propagates from _with_token_refresh.

    Causal assertions:
    - Exact RuntimeError from retry terminalize propagates.
    - Provider callback called exactly twice in order: initial-401, retry-200.
    - Terminalizer called exactly twice in order: initial (succeeds), retry (raises).
    """
    import app.services.integration_token_mutations as mutations_module

    provider_calls: list[str] = []
    terminalize_calls: list[str] = []
    snap_calls: list[int] = []

    _SNAP_INITIAL = {
        "access_token": "access-token-initial",
        "access_token_raw": "access-token-initial",
        "refresh_token_raw": "refresh-token",
        "generation": 0,
        "lifecycle_epoch": 0,
    }
    _SNAP_RETRY = {
        "access_token": "access-token-retry",
        "access_token_raw": "access-token-retry",
        "refresh_token_raw": "refresh-token-retry",
        "generation": 1,
        "lifecycle_epoch": 0,
    }

    async def _load_snap(contractor_id, *, provider):
        snap_calls.append(len(snap_calls) + 1)
        # First call: initial authorization gate. Second call: retry gate.
        return _SNAP_INITIAL if len(snap_calls) == 1 else _SNAP_RETRY

    claim_counter = {"n": 0}

    async def _acquire(**_kwargs):
        claim_counter["n"] += 1
        return (f"claim-{claim_counter['n']}", None)

    async def _transition(**_kwargs):
        return None

    async def _terminalize(**_kwargs):
        terminalize_calls.append("terminalize")
        if len(terminalize_calls) == 1:
            # Initial 401 path: terminalize succeeds so the retry branch is entered.
            return True
        # Retry path: raise to prove propagation.
        raise RuntimeError("terminalizer-boom-retry")

    monkeypatch.setattr(mutations_module, "load_durable_provider_snapshot", _load_snap)
    monkeypatch.setattr(mutations_module, "acquire_provider_operation_intent_cas", _acquire)
    monkeypatch.setattr(
        mutations_module, "transition_provider_operation_intent_to_started_cas", _transition
    )
    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _terminalize)

    # refresh_access_token is a module-level function on calendar; patch it directly.
    async def _fake_refresh(contractor_dict, *, force=False):
        return "access-token-retry"

    monkeypatch.setattr(calendar, "refresh_access_token", _fake_refresh)

    class _FakeResp401:
        status_code = 401

    class _FakeResp200:
        status_code = 200

    async def _call(token: str):
        provider_calls.append(f"call:{token}")
        if token == "access-token-initial":
            return _FakeResp401()
        return _FakeResp200()

    _exc = None
    try:
        await calendar._with_token_refresh(_contractor(), _call)
    except RuntimeError as e:
        _exc = e

    # Exact exception identity from the retry terminalizer.
    assert _exc is not None, "Expected RuntimeError to propagate from retry terminalize"
    assert str(_exc) == "terminalizer-boom-retry", f"Wrong exception: {_exc}"

    # Provider called exactly twice: initial (401) then retry (200).
    assert provider_calls == ["call:access-token-initial", "call:access-token-retry"], (
        f"Expected 2 provider calls in order; got {provider_calls}"
    )

    # Terminalizer called exactly twice: initial (succeeded), retry (raised).
    assert terminalize_calls == ["terminalize", "terminalize"], (
        f"Expected exactly 2 terminalize calls; got {terminalize_calls}"
    )


@pytest.mark.asyncio
async def test_terminalization_failure_absorbed_on_call_exception_opt_in(monkeypatch, caplog):
    """When the provider call itself raises AND terminalize_intent_on_exception=True (the opt-in
    path used by reschedule_appointment), a terminalizer failure must be caught/logged and the
    original transport exception must still propagate to the reschedule caller as False."""
    import app.services.integration_token_mutations as mutations_module

    async def _boom(**_kwargs) -> None:
        raise RuntimeError("terminalizer-boom-opt-in-absorbed")

    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _boom)

    class _TransportRaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **_kwargs):
            raise TimeoutError("simulated-transport-timeout-opt-in")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _TransportRaisingClient)

    with caplog.at_level(logging.ERROR):
        result = await calendar.reschedule_appointment(
            _contractor(),
            "event-opt-in-absorbed",
            base_start="2026-08-13T09:00:00-04:00",
            base_end="2026-08-13T10:00:00-04:00",
            desired_start="2026-08-13T11:00:00-04:00",
            desired_end="2026-08-13T12:00:00-04:00",
        )

    # reschedule_appointment uses terminalize_intent_on_exception=True for GET, so the transport
    # exception is caught and returned as False; the terminalizer failure is logged, not re-raised.
    assert result is False
    # Logged with exception_type only (no raw exception message).
    assert "exception_type=RuntimeError" in caplog.text
    assert "terminalizer-boom-opt-in-absorbed" not in caplog.text


# ---------------------------------------------------------------------------
# A1 terminalize-False causal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminalize_false_on_initial_200_raises_coarse_error(monkeypatch):
    """A1 guard: when the initial provider call returns 200 but terminalize returns False,
    _with_token_refresh must raise the fixed coarse RuntimeError instead of letting the
    caller consume the provider response.

    Causal assertions:
    - Exact coarse RuntimeError message propagates (not the provider response).
    - Provider called exactly once (initial path only).
    - Terminalizer called exactly once (initial normal-response branch).
    """
    import app.services.integration_token_mutations as mutations_module

    provider_calls: list[str] = []
    terminalize_calls: list[str] = []

    _SNAP = {
        "access_token": "access-token-a1-initial",
        "access_token_raw": "access-token-a1-initial",
        "refresh_token_raw": "refresh-token-a1",
        "generation": 0,
        "lifecycle_epoch": 0,
    }

    async def _load_snap(contractor_id, *, provider):
        return _SNAP

    async def _acquire(**_kwargs):
        return ("claim-a1-1", None)

    async def _transition(**_kwargs):
        return None

    async def _terminalize(**_kwargs):
        terminalize_calls.append("terminalize")
        return False  # Durability unconfirmed — must prevent response consumption.

    monkeypatch.setattr(mutations_module, "load_durable_provider_snapshot", _load_snap)
    monkeypatch.setattr(mutations_module, "acquire_provider_operation_intent_cas", _acquire)
    monkeypatch.setattr(
        mutations_module, "transition_provider_operation_intent_to_started_cas", _transition
    )
    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _terminalize)

    class _FakeResp200:
        status_code = 200

    async def _call(token: str):
        provider_calls.append(f"call:{token}")
        return _FakeResp200()

    _exc = None
    try:
        await calendar._with_token_refresh(_contractor(), _call)
    except RuntimeError as e:
        _exc = e

    # Must raise the coarse fixed error, not silently return the response.
    assert _exc is not None, "Expected RuntimeError when terminalize returns False"
    assert str(_exc) == "Google Calendar operation intent terminalization did not confirm", (
        f"Wrong exception message: {_exc}"
    )

    # Provider called exactly once (initial 200, no 401 retry path).
    assert provider_calls == ["call:access-token-a1-initial"], (
        f"Expected exactly 1 provider call; got {provider_calls}"
    )

    # Terminalizer called exactly once (initial normal-response branch).
    assert terminalize_calls == ["terminalize"], (
        f"Expected exactly 1 terminalize call; got {terminalize_calls}"
    )


@pytest.mark.asyncio
async def test_terminalize_false_on_retry_200_raises_coarse_error(monkeypatch):
    """A1 guard: 401-retry path — initial terminalize returns True (so the retry branch
    is entered), then retry terminalize returns False, which must raise the fixed coarse
    retry RuntimeError instead of letting the caller consume the retry response.

    Call order:
      1. Initial provider call → 401 (terminal_outcome=True → initial terminalize → True).
      2. refresh_access_token → truthy (refresh succeeds).
      3. load_durable_provider_snapshot (retry gate) → valid retry snap.
      4. Retry provider call → 200 (retry_terminal_outcome=True → retry terminalize → False).
      5. Coarse retry RuntimeError propagates from _with_token_refresh.

    Causal assertions:
    - Exact coarse retry RuntimeError message.
    - Provider called exactly twice in order: initial 401, retry 200.
    - Terminalizer called exactly twice in order: initial (True), retry (False).
    """
    import app.services.integration_token_mutations as mutations_module

    provider_calls: list[str] = []
    terminalize_calls: list[str] = []
    snap_calls: list[int] = []

    _SNAP_INITIAL = {
        "access_token": "access-token-a1-retry-initial",
        "access_token_raw": "access-token-a1-retry-initial",
        "refresh_token_raw": "refresh-token-a1-retry",
        "generation": 0,
        "lifecycle_epoch": 0,
    }
    _SNAP_RETRY = {
        "access_token": "access-token-a1-retry-after",
        "access_token_raw": "access-token-a1-retry-after",
        "refresh_token_raw": "refresh-token-a1-retry-after",
        "generation": 1,
        "lifecycle_epoch": 0,
    }

    async def _load_snap(contractor_id, *, provider):
        snap_calls.append(len(snap_calls) + 1)
        return _SNAP_INITIAL if len(snap_calls) == 1 else _SNAP_RETRY

    claim_counter = {"n": 0}

    async def _acquire(**_kwargs):
        claim_counter["n"] += 1
        return (f"claim-a1r-{claim_counter['n']}", None)

    async def _transition(**_kwargs):
        return None

    async def _terminalize(**_kwargs):
        terminalize_calls.append("terminalize")
        if len(terminalize_calls) == 1:
            # Initial 401 path: confirm so the retry branch is entered.
            return True
        # Retry path: False — must block response consumption.
        return False

    monkeypatch.setattr(mutations_module, "load_durable_provider_snapshot", _load_snap)
    monkeypatch.setattr(mutations_module, "acquire_provider_operation_intent_cas", _acquire)
    monkeypatch.setattr(
        mutations_module, "transition_provider_operation_intent_to_started_cas", _transition
    )
    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _terminalize)

    async def _fake_refresh(contractor_dict, *, force=False):
        return "access-token-a1-retry-after"

    monkeypatch.setattr(calendar, "refresh_access_token", _fake_refresh)

    class _FakeResp401:
        status_code = 401

    class _FakeResp200:
        status_code = 200

    async def _call(token: str):
        provider_calls.append(f"call:{token}")
        if token == "access-token-a1-retry-initial":
            return _FakeResp401()
        return _FakeResp200()

    _exc = None
    try:
        await calendar._with_token_refresh(_contractor(), _call)
    except RuntimeError as e:
        _exc = e

    # Coarse retry error must propagate.
    assert _exc is not None, "Expected RuntimeError when retry terminalize returns False"
    assert str(_exc) == "Google Calendar retry operation intent terminalization did not confirm", (
        f"Wrong exception message: {_exc}"
    )

    # Provider called exactly twice: initial (401) then retry (200).
    assert provider_calls == [
        "call:access-token-a1-retry-initial",
        "call:access-token-a1-retry-after",
    ], f"Expected 2 provider calls in order; got {provider_calls}"

    # Terminalizer called exactly twice: initial (True), retry (False).
    assert terminalize_calls == ["terminalize", "terminalize"], (
        f"Expected exactly 2 terminalize calls; got {terminalize_calls}"
    )


# ---------------------------------------------------------------------------
# A2 CancelledError causal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_error_during_initial_call_runs_cleanup_and_propagates(monkeypatch):
    """A2 guard: asyncio.CancelledError raised during the initial provider await
    must trigger the opt-in cleanup terminalizer (terminalize_intent_on_exception=True)
    and then re-raise the original cancellation unchanged.

    Causal assertions:
    - The exact original CancelledError (same message sentinel) propagates.
    - Provider coroutine called exactly once with the initial token.
    - Cleanup terminalizer called exactly once for the initial claim.
    """
    import asyncio
    import app.services.integration_token_mutations as mutations_module

    provider_calls: list[str] = []
    terminalize_calls: list[str] = []

    _SNAP = {
        "access_token": "access-token-a2-initial",
        "access_token_raw": "access-token-a2-initial",
        "refresh_token_raw": "refresh-token-a2",
        "generation": 0,
        "lifecycle_epoch": 0,
    }

    async def _load_snap(contractor_id, *, provider):
        return _SNAP

    async def _acquire(**_kwargs):
        return ("claim-a2-initial-1", None)

    async def _transition(**_kwargs):
        return None

    async def _terminalize(**_kwargs):
        terminalize_calls.append("terminalize")
        return True  # Cleanup succeeds; original CancelledError must still propagate.

    monkeypatch.setattr(mutations_module, "load_durable_provider_snapshot", _load_snap)
    monkeypatch.setattr(mutations_module, "acquire_provider_operation_intent_cas", _acquire)
    monkeypatch.setattr(
        mutations_module, "transition_provider_operation_intent_to_started_cas", _transition
    )
    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _terminalize)

    async def _call(token: str):
        provider_calls.append(f"call:{token}")
        raise asyncio.CancelledError("cancel-initial-sentinel")

    _exc = None
    try:
        await calendar._with_token_refresh(
            _contractor(), _call, terminalize_intent_on_exception=True
        )
    except asyncio.CancelledError as e:
        _exc = e

    # Original CancelledError must propagate with the same sentinel message.
    assert _exc is not None, "Expected CancelledError to propagate from _with_token_refresh"
    assert str(_exc) == "cancel-initial-sentinel", (
        f"Wrong cancellation message: {_exc!r}"
    )

    # Provider coroutine entered exactly once (initial path only).
    assert provider_calls == ["call:access-token-a2-initial"], (
        f"Expected exactly 1 provider call; got {provider_calls}"
    )

    # Cleanup terminalizer called exactly once for the initial claim.
    assert terminalize_calls == ["terminalize"], (
        f"Expected exactly 1 cleanup terminalize call; got {terminalize_calls}"
    )


@pytest.mark.asyncio
async def test_cancelled_error_during_retry_call_runs_cleanup_and_propagates(monkeypatch):
    """A2b guard: asyncio.CancelledError raised during the *retry* provider await
    (after a 401 + successful refresh) must trigger the retry opt-in cleanup terminalizer
    and re-raise the original cancellation unchanged.

    Call order driven through the function:
      1. Initial provider call with token A → 401 (terminal_outcome=True).
      2. Initial normal terminalizer for claim-a2b-1 → True (A1 check passes, retry entered).
      3. refresh_access_token called exactly once → truthy.
      4. load_durable_provider_snapshot (retry gate) → snap with token B.
      5. Retry provider call with token B → raises CancelledError('cancel-retry-sentinel').
      6. Retry cleanup terminalizer for claim-a2b-2 → True.
      7. Original retry CancelledError re-propagates.

    Causal assertions:
    - Exact retry CancelledError message propagates.
    - Provider calls exactly [token-A, token-B] in order.
    - Terminalizer calls exactly [claim-a2b-1, claim-a2b-2] in order.
    - Refresh called exactly once.
    """
    import asyncio
    import app.services.integration_token_mutations as mutations_module

    provider_calls: list[str] = []
    terminalize_calls: list[str] = []
    refresh_calls: list[int] = []
    snap_calls: list[int] = []

    _SNAP_INITIAL = {
        "access_token": "token-a2b-A",
        "access_token_raw": "token-a2b-A",
        "refresh_token_raw": "refresh-a2b",
        "generation": 0,
        "lifecycle_epoch": 0,
    }
    _SNAP_RETRY = {
        "access_token": "token-a2b-B",
        "access_token_raw": "token-a2b-B",
        "refresh_token_raw": "refresh-a2b-after",
        "generation": 1,
        "lifecycle_epoch": 0,
    }

    async def _load_snap(contractor_id, *, provider):
        snap_calls.append(len(snap_calls) + 1)
        return _SNAP_INITIAL if len(snap_calls) == 1 else _SNAP_RETRY

    claim_counter = {"n": 0}

    async def _acquire(**_kwargs):
        claim_counter["n"] += 1
        return (f"claim-a2b-{claim_counter['n']}", None)

    async def _transition(**_kwargs):
        return None

    async def _terminalize(*, claim_id, **_kwargs):
        terminalize_calls.append(claim_id)
        return True  # Both normal (initial 401) and cleanup (retry cancel) confirm.

    monkeypatch.setattr(mutations_module, "load_durable_provider_snapshot", _load_snap)
    monkeypatch.setattr(mutations_module, "acquire_provider_operation_intent_cas", _acquire)
    monkeypatch.setattr(
        mutations_module, "transition_provider_operation_intent_to_started_cas", _transition
    )
    monkeypatch.setattr(mutations_module, "terminalize_provider_operation_intent_cas", _terminalize)

    async def _fake_refresh(contractor_dict, *, force=False):
        refresh_calls.append(1)
        return "token-a2b-B"

    monkeypatch.setattr(calendar, "refresh_access_token", _fake_refresh)

    class _FakeResp401:
        status_code = 401

    async def _call(token: str):
        provider_calls.append(token)
        if token == "token-a2b-A":
            return _FakeResp401()
        raise asyncio.CancelledError("cancel-retry-sentinel")

    _exc = None
    try:
        await calendar._with_token_refresh(
            _contractor(), _call, terminalize_intent_on_exception=True
        )
    except asyncio.CancelledError as e:
        _exc = e

    # Original retry CancelledError must propagate with the exact sentinel message.
    assert _exc is not None, "Expected CancelledError to propagate from retry path"
    assert str(_exc) == "cancel-retry-sentinel", f"Wrong message: {_exc!r}"

    # Provider entered exactly twice: initial (401) then retry (cancelled).
    assert provider_calls == ["token-a2b-A", "token-a2b-B"], (
        f"Expected [A, B] provider calls; got {provider_calls}"
    )

    # Terminalizer called exactly twice: initial normal (claim-a2b-1), retry cleanup (claim-a2b-2).
    assert terminalize_calls == ["claim-a2b-1", "claim-a2b-2"], (
        f"Expected [claim-a2b-1, claim-a2b-2]; got {terminalize_calls}"
    )

    # Refresh called exactly once.
    assert refresh_calls == [1], f"Expected exactly 1 refresh call; got {refresh_calls}"


@pytest.mark.asyncio
async def test_reschedule_transport_exception_logging_privacy(monkeypatch, caplog):
    import app.db.firestore_client as firestore_module

    sentinels = {
        "access": "EXC_ACCESS_SECRET_901",
        "refresh": "EXC_REFRESH_SECRET_902",
        "contractor": "exc-contractor-903",
        "customer_id": "exc-customer-id-904",
        "customer_key": "exc-customer-key-905",
        "request_id": "exc-request-id-906",
        "logical_id": "exc-logical-id-907",
        "event_id": "exc-event-id-908",
        "base_start": "2026-08-18T09:17:31-04:00",
        "base_end": "2026-08-18T10:17:32-04:00",
        "desired_start": "2026-08-18T11:47:33-04:00",
        "desired_end": "2026-08-18T12:47:34-04:00",
        "exception": "EXC_PROVIDER_MESSAGE_SECRET_909",
    }
    event_url = f"{calendar.EVENTS_URL}/{sentinels['event_id']}"
    db = firestore_module.get_firestore_client()
    db.collections.setdefault("contractors", {})[sentinels["contractor"]] = _FakeDocRef(
        {
            "contractor_id": sentinels["contractor"],
            "customer_id": sentinels["customer_id"],
            "customer_key": sentinels["customer_key"],
            "request_id": sentinels["request_id"],
            "logical_operation_id": sentinels["logical_id"],
            "google_calendar_access_token": sentinels["access"],
            "google_calendar_refresh_token": sentinels["refresh"],
            "google_calendar_generation": 0,
            "google_calendar_lifecycle_epoch": 0,
            "google_calendar_connected": True,
            "active": True,
        }
    )

    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            assert url == event_url
            assert kwargs["headers"]["Authorization"] == f"Bearer {sentinels['access']}"
            raise RuntimeError(sentinels["exception"])

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _RaisingClient)
    contractor = _contractor(
        contractor_id=sentinels["contractor"],
        customer_id=sentinels["customer_id"],
        customer_key=sentinels["customer_key"],
        request_id=sentinels["request_id"],
        logical_operation_id=sentinels["logical_id"],
        google_calendar_access_token=sentinels["access"],
        google_calendar_refresh_token=sentinels["refresh"],
    )
    with caplog.at_level(logging.DEBUG):
        result = await calendar.reschedule_appointment(
            contractor,
            sentinels["event_id"],
            base_start=sentinels["base_start"],
            base_end=sentinels["base_end"],
            desired_start=sentinels["desired_start"],
            desired_end=sentinels["desired_end"],
        )

    assert result is False
    assert "operation=get_event" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    for sentinel in [*sentinels.values(), event_url]:
        assert sentinel not in caplog.text
