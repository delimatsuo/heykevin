"""Duplicate-booking protection for Google Calendar event creation.

`check_gated_action` only asserts that an idempotency key is non-empty
(app/services/gated_actions.py:151) — it never stores or compares one. So
before this change, two `book_appointment` tool calls in a single phone
call both passed the gate and created two real customer appointments.

The fix sends a deterministic, content-derived event id to Google, which
enforces uniqueness per calendar and answers a repeat insert with
409 / reason "duplicate". A retry therefore collapses onto the existing
event instead of creating a second one.

Google does not guarantee collision detection at creation time ("Due to
the globally distributed nature of the system, we cannot guarantee that
ID collisions will be detected at event creation time"), so this is
strong best-effort protection rather than an absolute guarantee.
"""

import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import calendar

BASE32HEX_ALPHABET = set("0123456789abcdefghijklmnopqrstuv")


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, calls: list, responses: list):
        self.calls = calls
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        resp = self.responses.pop(0)
        # Google honours a client-supplied event id and echoes it back, so a
        # successful insert never returns some other id. Modelling that is
        # what makes "a retry resolves to the same appointment" meaningful.
        sent_id = (kwargs.get("json") or {}).get("id")
        if sent_id and resp.status_code in (200, 201):
            resp._body = {**resp._body, "id": sent_id}
        return resp


def _patch_client(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    return calls


import datetime
import time

class _FakeDocRef:
    def __init__(self, data=None, doc_id=None):
        self.id = doc_id
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []

    def get(self, *args, transaction=None, **kwargs):
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
                    "google_calendar_access_token": "access-token",
                    "google_calendar_refresh_token": "refresh-token",
                }))

        return _Coll(self.collections.setdefault(name, {}))

    def transaction(self):
        return _FakeTransaction(self)


@pytest.fixture(autouse=True)
def _setup_firestore(monkeypatch):
    import base64
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")

    db = _FakeFirestore({
        "contractors": {
            "contractor-1": _FakeDocRef({
                "contractor_id": "contractor-1",
                "active": True,
                "google_calendar_access_token": "access-token",
                "google_calendar_refresh_token": "refresh-token",
                "google_calendar_generation": 0,
                "google_calendar_lifecycle_epoch": 0,
                "google_calendar_connected": True,
            })
        }
    })
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    return db


@pytest.fixture(autouse=True)
def _reset_locks():
    calendar._REFRESH_LOCKS.clear()
    yield
    calendar._REFRESH_LOCKS.clear()


def _contractor():
    return {
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
    }


def _booking(**overrides):
    booking = {
        "title": "Estimate",
        "start_time": "2026-08-06T09:00:00Z",
        "end_time": "2026-08-06T10:00:00Z",
        "description": "Leaking water heater",
        "call_sid": "CA_TEST_CALL",
    }
    booking.update(overrides)
    return booking


@pytest.mark.asyncio
async def test_booking_sends_deterministic_event_id_to_google(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])

    await calendar.book_appointment(_contractor(), **_booking())

    sent_id = calls[0][1]["json"]["id"]
    assert sent_id, "an explicit event id must be sent so Google can enforce uniqueness"
    assert 5 <= len(sent_id) <= 1024, "Google requires ids between 5 and 1024 characters"
    assert set(sent_id) <= BASE32HEX_ALPHABET, "Google requires base32hex (0-9, a-v)"


@pytest.mark.asyncio
async def test_identical_booking_requests_produce_the_same_event_id(monkeypatch):
    first = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking())

    second = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking())

    assert first[0][1]["json"]["id"] == second[0][1]["json"]["id"]


@pytest.mark.asyncio
async def test_duplicate_insert_is_reported_as_success_not_failure(monkeypatch, caplog):
    """A 409 means our own earlier insert won — the appointment exists.

    Returning None here would tell the caller booking failed, and Kevin
    would apologise for a booking that is actually on the calendar.
    """
    _patch_client(
        monkeypatch,
        [_FakeResponse(409, {"error": {"errors": [{"reason": "duplicate"}]}})],
    )

    with caplog.at_level(logging.INFO):
        result = await calendar.book_appointment(_contractor(), **_booking())

    assert result, "a duplicate insert must resolve to the existing event, not a failure"


@pytest.mark.asyncio
async def test_retry_of_the_same_booking_does_not_create_a_second_event(monkeypatch):
    """The scenario this exists for: one tool call retried after a timeout."""
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(200, {"id": "server-assigned"}),
            _FakeResponse(409, {"error": {"errors": [{"reason": "duplicate"}]}}),
        ],
    )

    first = await calendar.book_appointment(_contractor(), **_booking())
    second = await calendar.book_appointment(_contractor(), **_booking())

    assert first and second
    assert first == second, "a retry must resolve to the same appointment"
    assert calls[0][1]["json"]["id"] == calls[1][1]["json"]["id"]


@pytest.mark.asyncio
async def test_two_different_appointments_in_one_call_are_both_bookable(monkeypatch):
    """Idempotency must key on the appointment, not merely on the call.

    The gate's own key is `f"{call_sid}:{action}"`, constant for the whole
    call. Deriving the event id from that alone would make the second
    genuine booking of a call collide with the first and be silently
    swallowed as a duplicate.
    """
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(200, {"id": "server-assigned-1"}),
            _FakeResponse(200, {"id": "server-assigned-2"}),
        ],
    )

    await calendar.book_appointment(_contractor(), **_booking())
    await calendar.book_appointment(
        _contractor(),
        **_booking(
            title="Follow-up",
            start_time="2026-08-08T14:00:00Z",
            end_time="2026-08-08T15:00:00Z",
        ),
    )

    assert calls[0][1]["json"]["id"] != calls[1][1]["json"]["id"]


@pytest.mark.asyncio
async def test_bookings_from_different_calls_never_share_an_event_id(monkeypatch):
    """Two callers requesting the same slot must not collapse into one event."""
    first = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking(call_sid="CA_CALLER_ONE"))

    second = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking(call_sid="CA_CALLER_TWO"))

    assert first[0][1]["json"]["id"] != second[0][1]["json"]["id"]


@pytest.mark.asyncio
async def test_booking_without_a_call_sid_still_succeeds(monkeypatch):
    """Absent a call sid there is nothing stable to dedupe on.

    Fail open rather than closed: refusing to book would break the tool
    outright, which is worse than the duplicate risk it guards against.
    """
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])

    result = await calendar.book_appointment(_contractor(), **_booking(call_sid=""))

    assert result == "server-assigned"
    assert "id" not in calls[0][1]["json"], "let Google assign an id when we cannot derive one"


def test_managed_booking_identity_is_stable_across_calls_and_phone_formatting():
    """Provider identity is semantic product state, never a call/tool ID."""
    from app.services.voice_pipeline import _managed_booking_identity

    first = _managed_booking_identity(
        "contractor-1",
        "+16175550123",
        "Estimate",
        "2026-08-06T09:00:00Z",
        "2026-08-06T10:00:00Z",
    )
    later_call = _managed_booking_identity(
        "contractor-1",
        "617-555-0123",
        "  Estimate  ",
        "2026-08-06T05:00:00-04:00",
        "2026-08-06T06:00:00-04:00",
    )

    assert later_call == first
    request_id, event_id, _title, _start, _end = first
    assert request_id.startswith("sr_")
    assert len(event_id) == 32
    assert set(event_id) <= BASE32HEX_ALPHABET


@pytest.mark.asyncio
async def test_non_duplicate_conflict_is_still_a_failure(monkeypatch, caplog):
    """A 409 that isn't our duplicate must not be laundered into success."""
    _patch_client(
        monkeypatch,
        [_FakeResponse(409, {"error": {"errors": [{"reason": "conflict"}]}})],
    )

    with caplog.at_level(logging.ERROR):
        result = await calendar.book_appointment(_contractor(), **_booking())

    assert result is None
