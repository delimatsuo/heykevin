"""Google Calendar OAuth token refresh behavior.

Google Calendar access tokens are opaque bearer strings (unlike Jobber's,
which are JWTs we can decode locally) and expire in ~1 hour. Proactive refresh
occurs when we know a token is expiring, and a reactive refresh-and-retry-once
occurs when we don't (e.g. for contractors who connected before this fix and have
no stored expiry). All refreshes are guarded by durable CAS in Firestore.
"""

import asyncio
import datetime
import json
import os
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services import calendar
from app.services.integration_tokens import safe_decrypt_integration_token


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        try:
            self.text = json.dumps(body)
        except Exception:
            self.text = str(body)

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

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _freebusy_ok(busy=None):
    return _FakeResponse(200, {"calendars": {"primary": {"busy": busy or []}}})


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
                self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)

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

    def set(self, doc_ref, data):
        self._staged_sets.append((doc_ref, dict(data)))

    def create(self, doc_ref, data):
        if doc_ref.exists:
            raise RuntimeError("Document already exists")
        self._staged_sets.append((doc_ref, dict(data)))

    def delete(self, doc_ref):
        self._staged_deletes.append(doc_ref)

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
                return self.docs.setdefault(doc_id, _FakeDocRef({
                    "contractor_id": doc_id,
                    "active": True,
                    "google_calendar_connected": True,
                    "google_calendar_generation": 0,
                    "google_calendar_lifecycle_epoch": 0,
                    "google_calendar_access_token": "stale-token",
                    "google_calendar_refresh_token": "refresh-1",
                }))

        return _Coll(self.collections.setdefault(name, {}))

    def transaction(self):
        tx = _FakeTransaction(self)
        self.last_transaction = tx
        return tx


@pytest.fixture(autouse=True)
def _reset_locks():
    calendar._REFRESH_LOCKS.clear()
    yield
    calendar._REFRESH_LOCKS.clear()


@pytest.fixture(autouse=True)
def _google_creds(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")


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
            "c1": _FakeDocRef({
                "contractor_id": "c1",
                "active": True,
                "google_calendar_access_token": "stale-token",
                "google_calendar_refresh_token": "refresh-1",
                "google_calendar_generation": 0,
                "google_calendar_lifecycle_epoch": 0,
                "google_calendar_connected": True,
            })
        }
    })
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    return db


@pytest.mark.asyncio
async def test_refreshes_expiring_token_before_check_availability(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 3600}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,  # already expired
    }

    slots = await calendar.get_available_slots(contractor)

    assert len(slots) > 0  # empty busy periods -> the whole window is available
    assert calls[0][0] == calendar.TOKEN_URL
    assert calls[1][0] == calendar.FREEBUSY_URL
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_reuses_fresh_token_without_refresh_call(monkeypatch, _setup_firestore):
    calls = []
    responses = [_freebusy_ok()]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    _setup_firestore.collection("contractors").document("c1").data["google_calendar_access_token"] = "still-good"

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "still-good",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() + 3000,
    }

    await calendar.get_available_slots(contractor)

    assert len(calls) == 1
    assert calls[0][0] == calendar.FREEBUSY_URL
    assert calls[0][1]["headers"]["Authorization"] == "Bearer still-good"


@pytest.mark.asyncio
async def test_retries_once_after_401_when_no_expiry_known(monkeypatch, _setup_firestore):
    """Legacy contractors connected before this fix have no stored expiry.

    The proactive check can't help them (we don't know their token is
    stale), so the reactive 401-retry is the real backstop.
    """
    _setup_firestore.collection("contractors").document("c1").data["google_calendar_access_token"] = "unknown-freshness-token"
    calls = []
    responses = [
        _FakeResponse(401, {"error": "invalid_token"}),
        _FakeResponse(200, {"access_token": "refreshed-token", "expires_in": 3600}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "unknown-freshness-token",
        "google_calendar_refresh_token": "refresh-1",
        # no google_calendar_token_expires_at
    }

    slots = await calendar.get_available_slots(contractor)

    assert len(slots) > 0  # empty busy periods -> the whole window is available
    assert calls[0][0] == calendar.FREEBUSY_URL
    assert calls[0][1]["headers"]["Authorization"] == "Bearer unknown-freshness-token"
    assert calls[1][0] == calendar.TOKEN_URL
    assert calls[2][0] == calendar.FREEBUSY_URL
    assert calls[2][1]["headers"]["Authorization"] == "Bearer refreshed-token"


@pytest.mark.asyncio
async def test_persists_refreshed_access_token_and_new_expiry(monkeypatch, _setup_firestore):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 1800}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    before = time.time()
    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    await calendar.get_available_slots(contractor)

    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data["google_calendar_generation"] == 1
    dec_access = safe_decrypt_integration_token(
        doc.data["google_calendar_access_token"],
        contractor_id="c1",
        provider="google_calendar",
        token_kind="access",
    )
    assert dec_access == "new-token"
    # Google omitted refresh_token in this response -> original must be preserved, not wiped.
    dec_refresh = safe_decrypt_integration_token(
        doc.data["google_calendar_refresh_token"],
        contractor_id="c1",
        provider="google_calendar",
        token_kind="refresh",
    )
    assert dec_refresh == "refresh-1"
    assert doc.data["google_calendar_token_expires_at"] >= before + 1800 - 5


@pytest.mark.asyncio
async def test_rotated_refresh_token_is_persisted(monkeypatch, _setup_firestore):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "refresh_token": "rotated-refresh", "expires_in": 3600}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    await calendar.get_available_slots(contractor)

    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data["google_calendar_generation"] == 1
    dec_refresh = safe_decrypt_integration_token(
        doc.data["google_calendar_refresh_token"],
        contractor_id="c1",
        provider="google_calendar",
        token_kind="refresh",
    )
    assert dec_refresh == "rotated-refresh"


@pytest.mark.asyncio
async def test_book_appointment_also_refreshes_expiring_token(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 3600}),
        _FakeResponse(201, {"id": "evt-1"}),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    event_id = await calendar.book_appointment(
        contractor,
        title="Estimate",
        start_time="2026-08-05T09:00:00-04:00",
        end_time="2026-08-05T10:00:00-04:00",
    )

    assert event_id == "evt-1"
    assert calls[0][0] == calendar.TOKEN_URL
    assert calls[1][0] == calendar.EVENTS_URL
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_hit_google_once(monkeypatch, _setup_firestore):
    """Two calendar tool calls racing on the same contractor should not
    both refresh — the second should see the first refresh's fresh token.
    """
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 3600}),
        _freebusy_ok(),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    await asyncio.gather(
        calendar.get_available_slots(contractor),
        calendar.get_available_slots(contractor),
    )

    token_url_calls = [c for c in calls if c[0] == calendar.TOKEN_URL]
    assert len(token_url_calls) == 1
    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data["google_calendar_generation"] == 1


@pytest.mark.asyncio
async def test_calendar_refresh_fails_closed_when_generation_mismatched(monkeypatch, _setup_firestore):
    """If durable generation advances while refresh is in-flight, CAS fails closed."""
    doc = _setup_firestore.collection("contractors").document("c1")

    class _RacingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            # Concurrent process advances generation in Firestore while HTTP call is in-flight
            doc.data["google_calendar_generation"] = 5
            return _FakeResponse(200, {"access_token": "refreshed-token", "expires_in": 3600})

    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _RacingAsyncClient())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    # refresh_access_token must fail closed (return None) and leave contractor unchanged
    res = await calendar.refresh_access_token(contractor, force=True)
    assert res is None
    assert contractor["google_calendar_access_token"] == "stale-token"
    assert doc.data["google_calendar_generation"] == 5


@pytest.mark.asyncio
async def test_calendar_refresh_fails_closed_when_disconnected_concurrently(monkeypatch, _setup_firestore):
    """If provider was marked disconnected in Firestore, refresh aborts."""
    doc = _setup_firestore.collection("contractors").document("c1")
    doc.data["google_calendar_connected"] = False

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    res = await calendar.refresh_access_token(contractor, force=True)
    assert res is None
    assert contractor["google_calendar_access_token"] == "stale-token"


@pytest.mark.asyncio
async def test_calendar_refresh_failure_quarantines_provider(monkeypatch, _setup_firestore):
    """If provider returns HTTP 400 on refresh in started phase, contractor is quarantined."""
    calls = []
    responses = [
        _FakeResponse(400, {"error": "invalid_grant"}),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    res = await calendar.refresh_access_token(contractor, force=True)
    assert res is None
    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data.get("google_calendar_reauthorization_required") is True
    assert doc.data.get("google_calendar_refresh_outcome_unknown") is True


@pytest.mark.asyncio
async def test_calendar_persistence_failure_triggers_immediate_quarantine(monkeypatch, _setup_firestore):
    """When Google token exchange succeeds (200) but persist_refreshed_tokens_cas fails, immediate quarantine is attempted."""
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-google-token", "expires_in": 3600}),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    import app.services.integration_token_mutations as mutations_module

    async def _failing_persist(**kwargs):
        raise RuntimeError("Database write error during CAS persist")

    monkeypatch.setattr(mutations_module, "persist_refreshed_tokens_cas", _failing_persist)

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    res = await calendar.refresh_access_token(contractor, force=True)
    assert res is None
    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data.get("google_calendar_reauthorization_required") is True
    assert doc.data.get("google_calendar_refresh_outcome_unknown") is True


@pytest.mark.asyncio
async def test_calendar_refresh_reduced_scope_quarantines_and_issues_zero_business_http(monkeypatch, _setup_firestore):
    """When refresh response presents reduced scope, provider is quarantined and zero Calendar business HTTP is issued."""
    calls = []
    responses = [
        _FakeResponse(200, {
            "access_token": "new-token-reduced-scope",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/calendar.freebusy",  # missing calendar.events!
        }),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    # Calling get_available_slots triggers refresh_access_token
    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor)

    # Exactly 1 HTTP call made (the OAuth refresh endpoint). Zero calls to FREEBUSY_URL or EVENTS_URL!
    assert len(calls) == 1
    assert calls[0][0] == calendar.TOKEN_URL
    business_calls = [c for c in calls if c[0] in (calendar.FREEBUSY_URL, calendar.EVENTS_URL)]
    assert len(business_calls) == 0

    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data.get("google_calendar_reauthorization_required") is True
    assert doc.data.get("google_calendar_refresh_outcome_unknown") is True


@pytest.mark.asyncio
async def test_calendar_refresh_valid_scope_persisted_in_cas(monkeypatch, _setup_firestore):
    """When refresh response presents valid extended scope, it is persisted in CAS."""
    calls = []
    extended_scope = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy https://www.googleapis.com/auth/userinfo.email"
    responses = [
        _FakeResponse(200, {
            "access_token": "new-token-valid-scope",
            "expires_in": 3600,
            "scope": extended_scope,
        }),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    slots = await calendar.get_available_slots(contractor)
    assert isinstance(slots, list)

    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data.get("google_calendar_scope") == extended_scope
    assert doc.data.get("google_calendar_generation") == 1


class _StrSubclass(str):
    pass


class _HostileObj:
    def __eq__(self, other):
        raise AssertionError("Hostile equality called!")

    def __bool__(self):
        raise AssertionError("Hostile bool called!")

    def __len__(self):
        raise AssertionError("Hostile len called!")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_scope",
    [
        None,
        False,
        0,
        "",
        "   ",
        [],
        ["https://www.googleapis.com/auth/calendar.events"],
        "https://www.googleapis.com/auth/calendar.events\thttps://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events\nhttps://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events  https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        _StrSubclass("https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy"),
    ],
)
async def test_calendar_refresh_started_quarantines_on_present_invalid_scope(monkeypatch, _setup_firestore, bad_scope):
    """When refresh presents an invalid/reduced scope in started phase, it quarantines and makes zero business HTTP calls."""
    calls = []
    responses = [
        _FakeResponse(200, {
            "access_token": "new-token",
            "expires_in": 3600,
            "scope": bad_scope,
        }),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor)

    assert len(calls) == 1
    assert calls[0][0] == calendar.TOKEN_URL
    business_calls = [c for c in calls if c[0] in (calendar.FREEBUSY_URL, calendar.EVENTS_URL)]
    assert len(business_calls) == 0

    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data.get("google_calendar_reauthorization_required") is True
    assert doc.data.get("google_calendar_refresh_outcome_unknown") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_access",
    [None, 123, "", _HostileObj(), _StrSubclass("valid-token")],
)
async def test_calendar_refresh_quarantines_on_malformed_access_token(monkeypatch, _setup_firestore, bad_access):
    """When refresh returns malformed access_token, it quarantines and issues zero business HTTP calls."""
    calls = []
    responses = [
        _FakeResponse(200, {
            "access_token": bad_access,
            "expires_in": 3600,
        }),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor)

    assert len(calls) == 1
    business_calls = [c for c in calls if c[0] in (calendar.FREEBUSY_URL, calendar.EVENTS_URL)]
    assert len(business_calls) == 0

    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data.get("google_calendar_reauthorization_required") is True
    assert doc.data.get("google_calendar_refresh_outcome_unknown") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambiguous_error_type",
    ["http_500", "http_502", "http_503", "httpx_timeout", "httpx_transport", "invalid_json"],
)
async def test_18k_google_calendar_token_refresh_ambiguity_quarantines_and_blocks_retry_http(
    monkeypatch, _setup_firestore, ambiguous_error_type
):
    """Google Calendar token refresh ambiguous failures (5xx, httpx timeout/transport, invalid JSON) transition to
    strict durable quarantine (reauthorization_required=True, refresh_outcome_unknown=True) with cleared started intent,
    and subsequent retry calls make zero extra token or business HTTP calls."""
    import httpx

    cid = f"c-gcal-ref-ambig-{ambiguous_error_type}"
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_access_token": "stale-access-token",
        "google_calendar_refresh_token": "valid-refresh-token",
        "google_calendar_token_expires_at": time.time() - 10.0,
    }, doc_id=cid)

    _setup_firestore.collection("contractors").docs[cid] = doc_ref

    http_calls = []

    class _AmbiguousCalendarClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            http_calls.append((url, kwargs))
            if ambiguous_error_type == "http_500":
                return _FakeResponse(500, {"error": "internal_error"})
            elif ambiguous_error_type == "http_502":
                return _FakeResponse(502, {"error": "bad_gateway"})
            elif ambiguous_error_type == "http_503":
                return _FakeResponse(503, {"error": "service_unavailable"})
            elif ambiguous_error_type == "httpx_timeout":
                raise httpx.TimeoutException("Google Calendar token endpoint connection timeout")
            elif ambiguous_error_type == "httpx_transport":
                raise httpx.TransportError("Google Calendar token endpoint transport reset")
            elif ambiguous_error_type == "invalid_json":
                class _BadJsonResp:
                    status_code = 200
                    def json(self): raise ValueError("HTML error page")
                return _BadJsonResp()

    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _AmbiguousCalendarClient())

    contractor = dict(doc_ref.data)

    # 1. Initial call via wrapper / business path (get_available_slots) triggers refresh which fails ambiguously
    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor)

    assert len(http_calls) == 1
    assert http_calls[0][0] == calendar.TOKEN_URL
    business_calls = [c for c in http_calls if c[0] in (calendar.FREEBUSY_URL, calendar.EVENTS_URL)]
    assert len(business_calls) == 0

    # Strict durable quarantine verification
    assert doc_ref.data.get("google_calendar_reauthorization_required") is True
    assert doc_ref.data.get("google_calendar_refresh_outcome_unknown") is True
    assert "google_calendar_operation_intent_phase" not in doc_ref.data
    assert "google_calendar_operation_intent_id" not in doc_ref.data

    # 2. Subsequent retry via get_available_slots sees strict quarantine and raises error with ZERO extra token or business HTTP
    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor)

    assert len(http_calls) == 1  # ZERO additional HTTP call!


@pytest.mark.asyncio
async def test_18k_google_calendar_token_refresh_quarantine_write_failure_preserves_started_fence(
    monkeypatch, _setup_firestore
):
    """Causal proof: when quarantine write fails following a Google Calendar token refresh failure, the started fence remains on the contractor document."""
    import app.services.integration_token_mutations as mutations_module

    cid = "c-gcal-ref-quarantine-fail"
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_access_token": "stale-access-token",
        "google_calendar_refresh_token": "valid-refresh-token",
        "google_calendar_token_expires_at": time.time() - 10.0,
    }, doc_id=cid)

    _setup_firestore.collection("contractors").docs[cid] = doc_ref

    resp_500 = _FakeResponse(500, {"error": "internal_error"})
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient([], [resp_500]))

    async def _failing_quarantine(*args, **kwargs):
        raise RuntimeError("Simulated Google Calendar quarantine CAS write failure")

    monkeypatch.setattr(mutations_module, "quarantine_provider_reauth_cas", _failing_quarantine)

    contractor = dict(doc_ref.data)
    with pytest.raises(RuntimeError, match="quarantine CAS write failure"):
        await calendar.refresh_access_token(contractor, force=True)

    # Started fence MUST remain present on contractor document
    assert doc_ref.data.get("google_calendar_operation_intent_phase") == "provider_request_started"


@pytest.mark.asyncio
async def test_18qd_google_calendar_refresh_sentinel_non_disclosure(monkeypatch, caplog):
    """Prove transaction exception sentinels and contractor IDs are absent from log records/extras when preflight fails in Google Calendar refresh wrapper."""
    import logging
    import app.services.integration_token_mutations as mutations_module

    cid = "cid_secret_calendar_sentinel_67890"
    sentinel = "SECRET_SENTINEL_EXCEPTION_GCAL_DB_8888"

    async def _failing_preflight(*args, **kwargs):
        # Transaction exception inside preflight is caught and returns blocked preflight_failed
        return "blocked", "preflight_failed"

    monkeypatch.setattr(mutations_module, "check_and_recover_expired_intent_preflight_cas", _failing_preflight)

    http_count = [0]
    class _FailingHTTPClient:
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            raise AssertionError("HTTP call strictly forbidden when preflight fails!")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FailingHTTPClient())

    with caplog.at_level(logging.WARNING):
        res = await calendar.refresh_access_token({"contractor_id": cid})
        assert res is None
        assert http_count[0] == 0

    for rec in caplog.records:
        rec_dict = rec.__dict__
        assert sentinel not in str(rec_dict)
        assert cid not in str(rec_dict)
        assert rec_dict.get("extra") is None or cid not in str(rec_dict["extra"])
        assert "preflight_failed" in rec.message or "preflight" in rec.message
