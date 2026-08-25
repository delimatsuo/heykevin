"""Jobber OAuth token refresh behavior."""

import base64
import datetime
import json
import logging
import os
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services import jobber


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}."


def _contractor_auth(cid: str = "test-contractor") -> dict:
    return {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "valid-jobber-access",
        "jobber_refresh_token": "valid-jobber-refresh",
    }


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
        if "variables" not in kwargs and isinstance(kwargs.get("json"), dict):
            kwargs["variables"] = kwargs["json"].get("variables")
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class _RaisingAsyncClient:
    def __init__(self, exc: Exception):
        self.exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *_args, **_kwargs):
        raise self.exc


class _FakeDocRef:
    def __init__(self, data=None, doc_id=None):
        self.id = doc_id
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []

    @property
    def exists(self) -> bool:
        return (self.data is not None) and (not self.deleted)

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
                if doc_id not in self.docs:
                    self.docs[doc_id] = _FakeDocRef(None, doc_id=doc_id)
                return self.docs[doc_id]

        return _Coll(self.collections.setdefault(name, {}))

    def transaction(self):
        tx = _FakeTransaction(self)
        self.last_transaction = tx
        return tx


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
                "jobber_access_token": "old-token",
                "jobber_refresh_token": "old-refresh",
                "jobber_generation": 0,
                "jobber_lifecycle_epoch": 0,
                "jobber_connected": True,
            }),
            "test-contractor": _FakeDocRef({
                "contractor_id": "test-contractor",
                "active": True,
                "jobber_access_token": "valid-jobber-access",
                "jobber_refresh_token": "valid-jobber-refresh",
                "jobber_generation": 0,
                "jobber_lifecycle_epoch": 0,
                "jobber_connected": True,
            }),
        }
    })
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    return db


@pytest.mark.asyncio
async def test_refreshes_expiring_token_before_graphql(monkeypatch, _setup_firestore):
    calls = []
    new_token = _jwt(int(time.time()) + 3600)
    initial_access = _jwt(int(time.time()) - 10)
    responses = [
        _FakeResponse(200, {"access_token": new_token, "refresh_token": "new-refresh"}),
        _FakeResponse(200, {"data": {"clients": {"nodes": [{"id": "client-1"}]}}}),
    ]

    _setup_firestore.collection("contractors").document("c1").data.update({
        "contractor_id": "c1",
        "active": True,
        "jobber_access_token": initial_access,
        "jobber_refresh_token": "old-refresh",
        "jobber_generation": 0,
        "jobber_connected": True,
    })

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    from app import config
    monkeypatch.setattr(config.settings, "jobber_client_id", "client-id")
    monkeypatch.setattr(config.settings, "jobber_client_secret", "client-secret")

    contractor = {
        "contractor_id": "c1",
        "jobber_access_token": initial_access,
        "jobber_refresh_token": "old-refresh",
    }

    customer = await jobber.lookup_customer(contractor, "+16505550100")

    assert customer == {"id": "client-1"}
    assert calls[0][0] == jobber.JOBBER_TOKEN_URL
    assert calls[0][1]["data"]["grant_type"] == "refresh_token"
    assert calls[0][1]["data"]["refresh_token"] == "old-refresh"
    assert calls[1][0] == jobber.JOBBER_GRAPHQL_URL
    assert calls[1][1]["headers"]["Authorization"] == f"Bearer {new_token}"
    assert contractor["jobber_access_token"] == new_token
    assert contractor["jobber_refresh_token"] == "new-refresh"

    # Confirm durable record updated with CAS
    doc = _setup_firestore.collection("contractors").document("c1")
    assert doc.data["jobber_generation"] == 1


@pytest.mark.asyncio
async def test_retries_once_after_jobber_401(monkeypatch, _setup_firestore):
    calls = []
    old_token = _jwt(int(time.time()) + 3600)
    new_token = _jwt(int(time.time()) + 7200)
    responses = [
        _FakeResponse(401, {"error": "invalid_request"}),
        _FakeResponse(200, {"access_token": new_token, "refresh_token": "new-refresh"}),
        _FakeResponse(200, {"data": {"jobCreate": {"job": {"id": "job-1"}}}}),
    ]

    _setup_firestore.collection("contractors").document("c1").data.update({
        "contractor_id": "c1",
        "active": True,
        "jobber_access_token": old_token,
        "jobber_refresh_token": "old-refresh",
        "jobber_generation": 0,
        "jobber_connected": True,
    })

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    from app import config
    monkeypatch.setattr(config.settings, "jobber_client_id", "client-id")
    monkeypatch.setattr(config.settings, "jobber_client_secret", "client-secret")

    contractor = {
        "contractor_id": "c1",
        "jobber_access_token": old_token,
        "jobber_refresh_token": "old-refresh",
    }

    job_id = await jobber.create_job(contractor, {"title": "Phone inquiry"})

    assert job_id == "job-1"
    assert calls[0][0] == jobber.JOBBER_GRAPHQL_URL
    assert calls[0][1]["headers"]["Authorization"] == f"Bearer {old_token}"
    assert calls[1][0] == jobber.JOBBER_TOKEN_URL
    assert calls[2][0] == jobber.JOBBER_GRAPHQL_URL
    assert calls[2][1]["headers"]["Authorization"] == f"Bearer {new_token}"


@pytest.mark.asyncio
async def test_jobber_refresh_fails_closed_when_generation_mismatched(monkeypatch, _setup_firestore):
    """If durable generation advances while refresh is in-flight, CAS fails closed."""
    doc = _setup_firestore.collection("contractors").document("c1")
    old_tok = _jwt(int(time.time()) - 10)
    new_tok = _jwt(int(time.time()) + 3600)
    doc.data.update({
        "contractor_id": "c1",
        "active": True,
        "jobber_access_token": old_tok,
        "jobber_refresh_token": "old-refresh",
        "jobber_generation": 0,
        "jobber_connected": True,
    })

    class _RacingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            # Concurrent reconnect advances generation
            doc.data["jobber_generation"] = 5
            return _FakeResponse(200, {"access_token": new_tok, "refresh_token": "new-refresh"})

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _RacingAsyncClient())

    from app import config
    monkeypatch.setattr(config.settings, "jobber_client_id", "client-id")
    monkeypatch.setattr(config.settings, "jobber_client_secret", "client-secret")

    contractor = {
        "contractor_id": "c1",
        "jobber_access_token": old_tok,
        "jobber_refresh_token": "old-refresh",
    }

    res = await jobber.refresh_access_token(contractor, force=True)
    assert res is None
    assert contractor["jobber_access_token"] == old_tok
    assert doc.data["jobber_generation"] == 5


@pytest.mark.asyncio
async def test_jobber_refresh_fails_closed_when_disconnected_concurrently(monkeypatch, _setup_firestore):
    """If provider was marked disconnected in Firestore, refresh aborts."""
    doc = _setup_firestore.collection("contractors").document("c1")
    doc.data.update({
        "contractor_id": "c1",
        "jobber_connected": False,
    })

    contractor = {
        "contractor_id": "c1",
        "jobber_access_token": _jwt(int(time.time()) - 10),
        "jobber_refresh_token": "old-refresh",
    }

    res = await jobber.refresh_access_token(contractor, force=True)
    assert res is None
    assert contractor["jobber_access_token"] == contractor["jobber_access_token"]


@pytest.mark.asyncio
async def test_graphql_error_payload_logging_omits_sensitive_values(monkeypatch, caplog):
    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
    )
    responses = [
        _FakeResponse(
            200,
            {
                "errors": [
                    {
                        "message": (
                            "Cannot create job for Jane Private at 123 Secret Lane, "
                            "call +15551234567, gate code 2468."
                        ),
                        "extensions": {
                            "input": {
                                "title": "Jane Private repair",
                                "description": "123 Secret Lane callback +15551234567",
                            }
                        },
                    },
                    {"message": "Validation failed"},
                ],
                "data": None,
            },
        )
    ]

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient([], responses))

    with caplog.at_level(logging.WARNING):
        result = await jobber._graphql_request(
            "jobber-token",
            "mutation CreateJob($input: JobCreateInput!) { jobCreate(input: $input) { job { id } } }",
            {
                "input": {
                    "title": "Jane Private repair",
                    "description": "123 Secret Lane callback +15551234567",
                }
            },
        )

    assert result is None
    assert "Jobber GraphQL errors" in caplog.text
    assert "error_count=2" in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_graphql_request_exception_logging_uses_exception_type_only(monkeypatch, caplog):
    sensitive_values = (
        "Jane Private",
        "123 Secret Lane",
        "+15551234567",
        "gate code 2468",
    )
    monkeypatch.setattr(
        jobber.httpx,
        "AsyncClient",
        lambda: _RaisingAsyncClient(
            RuntimeError(
                "Transport failed for Jane Private at 123 Secret Lane, "
                "call +15551234567, gate code 2468."
            )
        ),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(jobber.JobberNetworkError):
            await jobber._graphql_request(
                "jobber-token",
                "mutation CreateJob($input: JobCreateInput!) { jobCreate(input: $input) { job { id } } }",
                {
                    "input": {
                        "title": "Jane Private repair",
                        "description": "123 Secret Lane callback +15551234567",
                    }
                },
            )

    assert "Jobber request failed" in caplog.text
    assert "provider=jobber operation=graphql_request result=error" in caplog.text
    assert "RuntimeError" not in caplog.text
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_graphql_request_sends_jobber_version_header(monkeypatch):
    calls = []
    responses = [_FakeResponse(200, {"data": {"viewer": {"id": "viewer-1"}}})]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    data = await jobber._graphql_request("jobber-token", "query { viewer { id } }")

    assert data == {"viewer": {"id": "viewer-1"}}
    assert calls[0][1]["headers"]["X-JOBBER-GRAPHQL-VERSION"] == "2025-04-16"


def test_extract_mutation_payload_rejects_user_errors(caplog):
    payload = {
        "requestCreate": {
            "request": None,
            "userErrors": [{"message": "Client is required", "path": ["clientId"]}],
        }
    }

    with caplog.at_level(logging.WARNING):
        result = jobber._extract_mutation_object(payload, "requestCreate", "request")

    assert result is None
    assert "Jobber mutation returned user errors" in caplog.text
    assert "mutation=requestCreate" in caplog.text
    assert "error_count=1" in caplog.text
    assert "Client is required" not in caplog.text


@pytest.mark.asyncio
async def test_create_job_logs_sanitized_user_errors(monkeypatch, caplog):
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "jobCreate": {
                        "job": None,
                        "userErrors": [{"message": "Client is required", "path": ["clientId"]}],
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient([], responses))

    with caplog.at_level(logging.WARNING):
        job_id = await jobber.create_job(_contractor_auth(), {"title": "Phone inquiry"})

    assert job_id is None
    assert "Jobber mutation returned user errors" in caplog.text
    assert "mutation=jobCreate" in caplog.text
    assert "error_count=1" in caplog.text
    assert "Client is required" not in caplog.text


@pytest.mark.asyncio
async def test_create_quote_logs_sanitized_user_errors(monkeypatch, caplog):
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "quoteCreate": {
                        "quote": None,
                        "userErrors": [{"message": "Property is required", "path": ["propertyId"]}],
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient([], responses))

    with caplog.at_level(logging.WARNING):
        quote_id = await jobber.create_quote(_contractor_auth(), {"title": "Phone inquiry"})

    assert quote_id is None
    assert "Jobber mutation returned user errors" in caplog.text
    assert "mutation=quoteCreate" in caplog.text
    assert "error_count=1" in caplog.text
    assert "Property is required" not in caplog.text


@pytest.mark.asyncio
async def test_lookup_customer_searches_phone_fields(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "clients": {
                        "nodes": [
                            {
                                "id": "client-1",
                                "name": "Jane Private",
                                "firstName": "Jane",
                                "lastName": "Private",
                                "phones": [{"number": "+15551234567"}],
                                "emails": [],
                                "billingAddress": None,
                                "clientProperties": {"nodes": []},
                            }
                        ]
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    customer = await jobber.lookup_customer(_contractor_auth(), "+15551234567")

    assert customer["id"] == "client-1"
    assert calls[0][1]["variables"] == {"phone": "+15551234567"}
    query = calls[0][1]["json"]["query"]
    assert "clients(searchTerm: $phone" in query
    assert "searchFields: [PHONES]" in query
    assert "first: 1" in query


@pytest.mark.asyncio
async def test_lookup_customer_preserves_billing_address_street_compatibility(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "clients": {
                        "nodes": [
                            {
                                "id": "client-1",
                                "name": "Jane Private",
                                "firstName": "Jane",
                                "lastName": "Private",
                                "phones": [{"number": "+15551234567"}],
                                "emails": [],
                                "billingAddress": {
                                    "street1": "123 Main Street",
                                    "street2": "Suite 4",
                                    "city": "Denver",
                                    "province": "CO",
                                    "postalCode": "80202",
                                },
                                "clientProperties": {"nodes": []},
                            }
                        ]
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    customer = await jobber.lookup_customer(_contractor_auth(), "+15551234567")

    assert customer["billingAddress"]["street"] == "123 Main Street Suite 4"
    assert customer["billingAddress"]["street1"] == "123 Main Street"
    assert customer["billingAddress"]["street2"] == "Suite 4"


@pytest.mark.asyncio
async def test_create_client_builds_jobber_client_payload(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "clientCreate": {
                        "client": {
                            "id": "client-1",
                            "name": "Jane Private",
                            "clientProperties": {"nodes": [{"id": "property-1"}]},
                        },
                        "userErrors": [],
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    result = await jobber.create_client(
        _contractor_auth(),
        {
            "caller_name": "Jane Private",
            "caller_phone": "+15551234567",
            "address": "123 Main Street, Denver CO",
        },
    )

    assert result == {"id": "client-1", "name": "Jane Private", "property_id": "property-1"}
    input_payload = calls[0][1]["json"]["variables"]["input"]
    assert input_payload["firstName"] == "Jane"
    assert input_payload["lastName"] == "Private"
    assert input_payload["phones"] == [{"number": "+15551234567", "primary": True}]
    assert input_payload["sourceAttribution"] == {"sourceText": "Hey Kevin"}
    assert input_payload["properties"] == [{"address": {"street1": "123 Main Street, Denver CO"}}]


@pytest.mark.asyncio
async def test_create_request_and_note(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "requestCreate": {
                        "request": {"id": "request-1", "title": "Leaking sink", "jobberWebUri": "https://example.test/request"},
                        "userErrors": [],
                    }
                }
            },
        ),
        _FakeResponse(
            200,
            {
                "data": {
                    "requestCreateNote": {
                        "request": {"id": "request-1"},
                        "requestNote": {"id": "note-1"},
                        "userErrors": [],
                    }
                }
            },
        ),
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    request = await jobber.create_request(
        _contractor_auth(),
        {
            "client_id": "client-1",
            "property_id": "property-1",
            "title": "Leaking sink",
        },
    )
    long_message = "Call summary " + ("x" * 6000)
    expected_note_message = long_message[:5000]
    note_id = await jobber.create_request_note(_contractor_auth(), "request-1", long_message)

    assert request == {"id": "request-1", "title": "Leaking sink", "jobberWebUri": "https://example.test/request"}
    assert note_id == "note-1"
    assert calls[0][1]["json"]["variables"]["input"] == {
        "clientId": "client-1",
        "propertyId": "property-1",
        "title": "Leaking sink",
    }
    note_variables = calls[1][1]["json"]["variables"]
    assert len(note_variables["input"]["message"]) == 5000
    assert note_variables["input"]["message"] == expected_note_message
    assert calls[1][1]["json"]["variables"] == {
        "requestId": "request-1",
        "input": {"message": expected_note_message, "pinned": False},
    }


@pytest.mark.asyncio
async def test_jobber_raw_string_auth_refused_with_zero_http(monkeypatch):
    """Proves raw-string auth passed to Jobber APIs is rejected with zero provider HTTP calls."""
    http_called = False

    class _FailHttp:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            nonlocal http_called
            http_called = True
            return _FakeResponse(200, {})

    monkeypatch.setattr(jobber.httpx, "AsyncClient", _FailHttp)

    # 1. create_job with raw string auth
    assert await jobber.create_job("raw-string-token", {"title": "Test"}) is None
    assert not http_called

    # 2. _graphql_request_with_refresh with raw string auth
    assert await jobber._graphql_request_with_refresh("raw-string-token", "query { viewer { id } }") is None
    assert not http_called

    # 3. _resolve_access_token with raw string auth
    assert await jobber._resolve_access_token("raw-string-token") == ""
    assert not http_called


@pytest.mark.asyncio
async def test_jobber_envelope_floor_enforcement_with_zero_http(monkeypatch, _setup_firestore):
    """Proves jobber_token_envelope_required=True with plaintext pair fails closed with zero HTTP calls."""
    http_called = False

    class _FailHttp:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            nonlocal http_called
            http_called = True
            return _FakeResponse(200, {})

    monkeypatch.setattr(jobber.httpx, "AsyncClient", _FailHttp)

    contractor_downgraded = {
        "contractor_id": "c-floor-downgrade",
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_token_envelope_required": True,
        "jobber_access_token": "plaintext-access",
        "jobber_refresh_token": "plaintext-refresh",
    }
    _setup_firestore.collection("contractors").document("c-floor-downgrade").set(contractor_downgraded)

    # API call must fail closed without making HTTP calls
    assert await jobber.create_job(contractor_downgraded, {"title": "Test"}) is None
    assert not http_called

    # Malformed floor value must also fail closed with zero HTTP calls
    contractor_bad_floor = dict(contractor_downgraded, jobber_token_envelope_required="not-a-bool")
    _setup_firestore.collection("contractors").document("c-floor-downgrade").set(contractor_bad_floor)
    assert await jobber.create_job(contractor_bad_floor, {"title": "Test"}) is None
    assert not http_called


@pytest.mark.asyncio
async def test_jobber_refresh_failure_quarantines_provider(monkeypatch):
    """If Jobber returns HTTP 400 on refresh in started phase, contractor is quarantined."""
    import base64

    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "jobber_client_id", "test-jobber-client-id")
    monkeypatch.setattr(settings, "jobber_client_secret", "test-jobber-client-secret")

    doc_ref = _FakeDocRef({
        "contractor_id": "c-jobber-q",
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "stale-token",
        "jobber_refresh_token": "refresh-1",
        "jobber_token_expires_at": time.time() - 10,
    })
    db = _FakeFirestore({"contractors": {"c-jobber-q": doc_ref}})

    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    calls = []
    responses = [
        _FakeResponse(400, {"error": "invalid_grant"}),
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": "c-jobber-q",
        "jobber_access_token": "stale-token",
        "jobber_refresh_token": "refresh-1",
        "jobber_token_expires_at": time.time() - 10,
    }

    res = await jobber.refresh_access_token(contractor, force=True)
    assert res is None
    assert doc_ref.data.get("jobber_reauthorization_required") is True
    assert doc_ref.data.get("jobber_refresh_outcome_unknown") is True


@pytest.mark.asyncio
async def test_jobber_persistence_failure_triggers_immediate_quarantine(monkeypatch):
    """When Jobber token exchange succeeds (200) but persist_refreshed_tokens_cas fails, immediate quarantine is attempted."""
    cid = "c-jobber-persist-fail"
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "stale-access",
        "jobber_refresh_token": "stale-refresh",
        "jobber_token_expires_at": time.time() - 10,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    calls = []
    responses = [
        _FakeResponse(200, {
            "access_token": "new-jobber-access",
            "refresh_token": "new-jobber-refresh",
            "expires_in": 3600,
        }),
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    async def _failing_persist(**kwargs):
        raise RuntimeError("Database write error during CAS persist")

    monkeypatch.setattr(mutations_module, "persist_refreshed_tokens_cas", _failing_persist)

    contractor = {
        "contractor_id": cid,
        "jobber_access_token": "stale-access",
        "jobber_refresh_token": "stale-refresh",
        "jobber_token_expires_at": time.time() - 10,
    }

    res = await jobber.refresh_access_token(contractor, force=True)
    assert res is None
    # Quarantine must be set because persist failed after provider 200
    assert doc_ref.data.get("jobber_reauthorization_required") is True
    assert doc_ref.data.get("jobber_refresh_outcome_unknown") is True


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
    "bad_access",
    [None, 123, "", _HostileObj(), _StrSubclass("valid-jwt-looking-token")],
)
async def test_jobber_refresh_quarantines_on_malformed_access_token(monkeypatch, bad_access):
    """When Jobber refresh returns malformed access_token, it quarantines and issues zero business HTTP calls."""
    cid = "c-jobber-malformed"
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "stale-access",
        "jobber_refresh_token": "stale-refresh",
        "jobber_token_expires_at": time.time() - 10,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    calls = []
    responses = [
        _FakeResponse(200, {
            "access_token": bad_access,
            "refresh_token": "valid-new-refresh",
            "expires_in": 3600,
        }),
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    contractor = {
        "contractor_id": cid,
        "jobber_access_token": "stale-access",
        "jobber_refresh_token": "stale-refresh",
        "jobber_token_expires_at": time.time() - 10,
    }

    res = await jobber.refresh_access_token(contractor, force=True)
    assert res is None
    assert len(calls) == 1
    assert calls[0][0] == jobber.JOBBER_TOKEN_URL
    # Zero GraphQL calls made
    assert doc_ref.data.get("jobber_reauthorization_required") is True
    assert doc_ref.data.get("jobber_refresh_outcome_unknown") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_access",
    [None, 123, "", _HostileObj(), _StrSubclass("valid-looking-token")],
)
async def test_jobber_callback_rejects_malformed_access_token(monkeypatch, bad_access):
    """Jobber OAuth callback rejects malformed access_token with HTTP 502 and commits zero credentials."""
    from fastapi import HTTPException

    from app.api import integrations
    from app.services.integration_tokens import compute_raw_credentials_fingerprint

    state_data = {
        "contractor_id": "c-jobber-cb",
        "provider": "jobber",
        "created_at": 1000.0,
        "expires_at": 1600.0,
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
    }
    state = _FakeDocRef(state_data, doc_id="opaque-jobber-state")
    contractor = _FakeDocRef({"contractor_id": "c-jobber-cb", "active": True, "jobber_connected": False, "jobber_generation": 0, "jobber_lifecycle_epoch": 0}, doc_id="c-jobber-cb")
    db = _FakeFirestore({
        "jobber_oauth_states": {"opaque-jobber-state": state},
        "contractors": {"c-jobber-cb": contractor},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

    response = _FakeResponse(200, {
        "access_token": bad_access,
        "refresh_token": "valid-refresh",
        "expires_in": 3600,
    })
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient([], [response]))
    monkeypatch.setattr("time.time", lambda: 1000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.jobber_callback(code="jobber-code", state="opaque-jobber-state")

    assert exc.value.status_code == 502
    assert "jobber_access_token" not in contractor.data


async def _noop_async():
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ambiguous_error_type",
    [
        "http_408",
        "http_425",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_599",
        "httpx_timeout",
        "httpx_transport",
        "invalid_json",
        "non_dict_json",
    ],
)
async def test_18j_jobber_refresh_ambiguous_failures_retain_started_fence_and_block_retry_http(
    monkeypatch, _setup_firestore, ambiguous_error_type
):
    """Causal proof: Jobber refresh ambiguous failures (HTTP 408/425/429/5xx/599, httpx timeout/transport, invalid/non-dict JSON)
    transition to strict durable quarantine (reauthorization_required=True, refresh_outcome_unknown=True),
    and subsequent retry calls make zero additional provider HTTP calls."""
    import httpx

    import app.db.contractors as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import encrypt_integration_token

    cid = f"c-jobber-ambig-{ambiguous_error_type}"
    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)

    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    caller_contractor = dict(doc_ref.data)
    http_call_count = [0]

    class _AmbiguousClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            http_call_count[0] += 1
            if ambiguous_error_type.startswith("http_"):
                code = int(ambiguous_error_type.split("_")[1])
                return _FakeResponse(code, {"error": f"HTTP {code}"})
            elif ambiguous_error_type == "httpx_timeout":
                raise httpx.TimeoutException("Jobber token endpoint connection timeout")
            elif ambiguous_error_type == "httpx_transport":
                raise httpx.TransportError("Jobber token endpoint connection reset")
            elif ambiguous_error_type == "invalid_json":
                class _BadJsonResp:
                    status_code = 200
                    def json(self): raise ValueError("HTML error page")
                return _BadJsonResp()
            elif ambiguous_error_type == "non_dict_json":
                class _NonDictJsonResp:
                    status_code = 200
                    def json(self): return ["array", "not", "dict"]
                return _NonDictJsonResp()

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _AmbiguousClient())

    # 1. Initial refresh attempt
    res = await jobber.refresh_access_token(caller_contractor, force=True)
    assert res is None
    assert http_call_count[0] == 1

    # Fresh durable reread of contractor document
    durable_reread = db.collections["contractors"][cid].data
    assert durable_reread.get("jobber_reauthorization_required") is True
    assert durable_reread.get("jobber_refresh_outcome_unknown") is True
    assert "jobber_operation_intent_phase" not in durable_reread
    assert "jobber_operation_intent_id" not in durable_reread

    # 2. Retry refresh attempt against fresh durable reread -> makes ZERO additional HTTP call!
    res_retry = await jobber.refresh_access_token(dict(durable_reread), force=True)
    assert res_retry is None
    assert http_call_count[0] == 1  # ZERO additional HTTP call!


@pytest.mark.asyncio
async def test_18j_jobber_refresh_quarantine_write_failure_preserves_started_fence(
    monkeypatch, _setup_firestore
):
    """Causal proof: when quarantine write fails following a token refresh failure, the started fence is NOT cleared or falsely terminalized."""
    import app.db.contractors as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import encrypt_integration_token

    cid = "c-jobber-quarantine-fail"
    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)

    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    contractor = doc_ref.data

    # Return HTTP 400 (invalid_grant) which triggers quarantine_provider_reauth_cas
    resp_400 = _FakeResponse(400, {"error": "invalid_grant", "error_description": "Token revoked"})
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient([], [resp_400]))

    # Inject failure into quarantine_provider_reauth_cas
    async def _failing_quarantine(*args, **kwargs):
        raise RuntimeError("Simulated quarantine CAS write failure")

    monkeypatch.setattr(mutations_module, "quarantine_provider_reauth_cas", _failing_quarantine)

    with pytest.raises(RuntimeError, match="quarantine CAS write failure"):
        await jobber.refresh_access_token(contractor, force=True)

    # Started fence must remain present on document, not cleared or falsely terminalized!
    assert doc_ref.data.get("jobber_operation_intent_phase") == "provider_request_started"


@pytest.mark.asyncio
async def test_18j_jobber_wrapper_path_ambiguous_failure_fencing(monkeypatch, _setup_firestore):
    """Causal proof: when _graphql_request_with_refresh triggers a token refresh that fails ambiguously,
    successful quarantine write produces strict durable quarantine (reauthorization_required=True, refresh_outcome_unknown=True)
    with cleared operation intent fields, and subsequent GraphQL wrapper calls make zero additional token or GraphQL HTTP calls."""
    import httpx

    import app.db.contractors as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import encrypt_integration_token

    cid = "c-jobber-wrapper-fencing"
    enc_acc = encrypt_integration_token("expired-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
        "jobber_access_token_expires_at": 100.0,
    }, doc_id=cid)

    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    contractor = doc_ref.data
    http_token_call_count = [0]

    class _TimingOutTokenClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, *args, **kwargs):
            if "oauth/token" in url or "token" in url:
                http_token_call_count[0] += 1
                raise httpx.TimeoutException("Token endpoint timeout")
            # GraphQL request returns 401 (unauthorized) to trigger auto-refresh flow
            return _FakeResponse(401, {"error": "unauthorized"})

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _TimingOutTokenClient())
    monkeypatch.setattr(jobber.time, "time", lambda: 1000.0)

    # First wrapper call triggers auto-refresh which times out
    res1 = await jobber._graphql_request_with_refresh(contractor, "query { viewer { id } }")
    assert res1 is None
    assert http_token_call_count[0] == 1

    # Strict durable quarantine verification after successful quarantine write
    assert doc_ref.data.get("jobber_reauthorization_required") is True
    assert doc_ref.data.get("jobber_refresh_outcome_unknown") is True
    assert "jobber_operation_intent_phase" not in doc_ref.data
    assert "jobber_operation_intent_id" not in doc_ref.data

    # Second wrapper call sees strict quarantine and blocks before making another token or GraphQL HTTP request
    res2 = await jobber._graphql_request_with_refresh(contractor, "query { viewer { id } }")
    assert res2 is None
    assert http_token_call_count[0] == 1  # ZERO additional token or GraphQL HTTP calls


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_error_type", [
    "http_408", "http_425", "http_429", "http_500", "http_502", "http_503", "http_599", "httpx_timeout", "httpx_transport", "invalid_json", "non_dict_json"
])
async def test_18k_jobber_business_graphql_ambiguity_retains_started_fence_and_blocks_retry_http(
    monkeypatch, _setup_firestore, ambiguous_error_type
):
    """Causal proof: direct Jobber business GraphQL ambiguity (408/425/429/5xx/599, timeout/transport, invalid/non-dict JSON) after business
    intent becomes started raises JobberNetworkError, retains provider_request_started, and a retry
    fails on the fence with zero second GraphQL HTTP call."""
    import httpx

    import app.db.contractors as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import encrypt_integration_token

    cid = f"c-jobber-biz-ambig-{ambiguous_error_type}"
    enc_acc = encrypt_integration_token("valid-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("valid-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)

    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(jobber, "get_firestore_client", lambda: db, raising=False)

    caller_contractor = dict(doc_ref.data)
    http_call_count = [0]

    class _AmbiguousBizClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            http_call_count[0] += 1
            if ambiguous_error_type.startswith("http_"):
                code = int(ambiguous_error_type.split("_")[1])
                return _FakeResponse(code, {"error": f"HTTP {code}"})
            elif ambiguous_error_type == "httpx_timeout":
                raise httpx.TimeoutException("Jobber GraphQL timeout")
            elif ambiguous_error_type == "httpx_transport":
                raise httpx.TransportError("Jobber GraphQL transport error")
            elif ambiguous_error_type == "invalid_json":
                class _BadJsonResp:
                    status_code = 200
                    def json(self): raise ValueError("HTML error page from Jobber GraphQL")
                return _BadJsonResp()
            elif ambiguous_error_type == "non_dict_json":
                class _NonDictJsonResp:
                    status_code = 200
                    def json(self): return ["array", "not", "dict"]
                return _NonDictJsonResp()

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _AmbiguousBizClient())

    # 1. Initial business GraphQL request raises precise network/ambiguity error
    with pytest.raises(jobber.JobberNetworkError):
        await jobber._graphql_request_with_refresh(caller_contractor, "{ client { id } }")

    assert http_call_count[0] == 1
    # Contractor document MUST retain provider_request_started phase fence!
    durable_reread = db.collections["contractors"][cid].data
    assert durable_reread.get("jobber_operation_intent_phase") == "provider_request_started"

    # 2. Retry business request against fresh durable reread fails on the fence (returns None) with ZERO second GraphQL HTTP call!
    res_retry = await jobber._graphql_request_with_refresh(dict(durable_reread), "{ client { id } }")
    assert res_retry is None
    assert http_call_count[0] == 1  # ZERO second GraphQL HTTP call!


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_type",
    [
        "success",
        "pre_dispatch_fail",
        "terminal_400",
        "timeout",
        "http_429",
        "invalid_json",
        "non_dict_json",
        "missing_access_token",
        "persistence_fail",
    ],
)
async def test_18q_jobber_callback_quarantine_reauth_matrix(monkeypatch, _setup_firestore, case_type):
    """Causal proof for jobber_callback under True/True quarantine reauthorization:
    - success: 1 HTTP, fresh credentials installed, gen+epoch advance, quarantine+attempt removed.
    - pre_dispatch_fail / terminal_400: attempt terminalized, quarantine retained.
    - ambiguity (timeout, 429, invalid/non-dict JSON, missing token, persist fail): attempt retained in provider_request_started, quarantine retained, retry makes ZERO second HTTP call.
    """
    import base64
    import httpx
    from fastapi import HTTPException
    import app.api.integrations as integrations
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import compute_raw_credentials_fingerprint, encrypt_integration_token, IntegrationTokenConfigError
    from app.services.integration_token_mutations import IntegrationTokenCASConflict

    k1 = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("INTEGRATION_TOKEN_ENCRYPTION_KEYS", f'{{"1": "{k1}"}}')
    monkeypatch.setenv("INTEGRATION_TOKEN_ACTIVE_KEY_VERSION", "1")
    monkeypatch.setenv("INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED", "true")

    cid = f"c-jobber-cb-qreauth-{case_type}"
    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")
    state_id = "s" * 32
    fp = compute_raw_credentials_fingerprint(enc_acc, enc_ref)

    c_doc = _FakeDocRef(
        {
            "contractor_id": cid,
            "active": True,
            "jobber_connected": True,
            "jobber_access_token": enc_acc,
            "jobber_refresh_token": enc_ref,
            "jobber_generation": 1,
            "jobber_lifecycle_epoch": 1,
            "jobber_token_envelope_required": True,
            "jobber_reauthorization_required": True,
            "jobber_refresh_outcome_unknown": True,
        },
        doc_id=cid,
    )
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {},
        "integration_lifecycle_audit": {},
    })
    await mutations_module.create_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state=state_id,
        contractor_id=cid,
        provider="jobber",
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    http_count = [0]

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            if case_type == "success":
                return _FakeResponse(200, {"access_token": "new-acc-tok", "refresh_token": "new-ref-tok", "expires_in": 3600})
            elif case_type == "terminal_400":
                return _FakeResponse(400, {"error": "invalid_grant"})
            elif case_type == "timeout":
                raise httpx.TimeoutException("Timeout")
            elif case_type == "http_429":
                return _FakeResponse(429, {"error": "rate limited"})
            elif case_type == "invalid_json":
                class _BadJsonResp:
                    status_code = 200
                    def json(self): raise ValueError("Not JSON")
                return _BadJsonResp()
            elif case_type == "non_dict_json":
                class _NonDictResp:
                    status_code = 200
                    def json(self): return ["array"]
                return _NonDictResp()
            elif case_type == "missing_access_token":
                return _FakeResponse(200, {"refresh_token": "new-ref-only"})
            elif case_type == "persistence_fail":
                return _FakeResponse(200, {"access_token": "new-acc-tok", "refresh_token": "new-ref-tok"})

    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _MockClient())

    if case_type == "pre_dispatch_fail":
        monkeypatch.setattr("app.services.integration_tokens.determine_write_format", lambda **kwargs: (_ for _ in ()).throw(IntegrationTokenConfigError("Unconfigured")))

    if case_type == "persistence_fail":
        async def _fail_connect(*args, **kwargs):
            raise IntegrationTokenCASConflict("Simulated persistence failure")
        monkeypatch.setattr(mutations_module, "connect_provider_cas", _fail_connect)

    if case_type == "success":
        res = await integrations.jobber_callback(code="jobber-code", state=state_id)
        assert res.status_code == 200
        assert http_count[0] == 1
        durable = c_doc.data
        assert durable.get("jobber_generation") == 2
        assert durable.get("jobber_lifecycle_epoch") == 2
        assert "jobber_reauthorization_required" not in durable
        assert "jobber_refresh_outcome_unknown" not in durable
        assert "jobber_reauthorization_attempt_id" not in durable
    elif case_type in ("pre_dispatch_fail", "terminal_400"):
        with pytest.raises(HTTPException):
            await integrations.jobber_callback(code="jobber-code", state=state_id)
        durable = c_doc.data
        assert durable.get("jobber_reauthorization_required") is True
        assert durable.get("jobber_refresh_outcome_unknown") is True
        assert "jobber_reauthorization_attempt_id" not in durable
    else:
        # Ambiguous failure: started attempt retained alongside quarantine, subsequent callback with fresh state blocks before second HTTP
        with pytest.raises(HTTPException):
            await integrations.jobber_callback(code="jobber-code", state=state_id)
        durable = c_doc.data
        assert durable.get("jobber_reauthorization_required") is True
        assert durable.get("jobber_refresh_outcome_unknown") is True
        assert durable.get("jobber_reauthorization_attempt_phase") == "provider_request_started"


@pytest.mark.asyncio
async def test_18qc_jobber_quarantined_callback_retry_zero_http(monkeypatch):
    """Prove Jobber callback under started attempt phase blocks on retry from fresh state with 0 HTTP calls."""
    import base64
    from fastapi import HTTPException
    import app.api.integrations as integrations
    import app.services.integration_token_mutations as mutations_module
    import app.services.integration_token_mutations as it_mutations
    from app.config import settings
    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    cid = "test_jobber_callback_retry_zero_http"
    state_id = "state_retry_j_" + "c" * 20
    fp = it_mutations.compute_raw_credentials_fingerprint("acc_jobber_old", "ref_jobber_old")

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": "acc_jobber_old",
        "jobber_refresh_token": "ref_jobber_old",
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_reauthorization_required": True,
        "jobber_refresh_outcome_unknown": True,
        "jobber_reauthorization_attempt_id": "attempt-j1",
        "jobber_reauthorization_attempt_kind": "reconnect",
        "jobber_reauthorization_attempt_phase": "provider_request_started",
        "jobber_reauthorization_attempt_expires_at": time.time() + 300.0,
        "jobber_reauthorization_attempt_acquired_at": time.time(),
        "jobber_reauthorization_attempt_generation": 1,
        "jobber_reauthorization_attempt_lifecycle_epoch": 1,
        "jobber_reauthorization_attempt_credentials_fingerprint": fp,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 1,
        "generation": 1,
        "credentials_fingerprint": fp,
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
    }, doc_id=state_id)
    db = _FakeFirestore({"contractors": {cid: c_doc}, "jobber_oauth_states": {state_id: s_doc}})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    http_count = [0]
    class _FailingJobberClient:
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            raise AssertionError("HTTP call strictly forbidden for Jobber retry under started attempt!")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FailingJobberClient())

    with pytest.raises(HTTPException) as exc_info:
        await integrations.jobber_callback(code="jobber-code", state=state_id)

    assert exc_info.value.status_code in (400, 409)
    assert http_count[0] == 0


@pytest.mark.asyncio
async def test_18qd_jobber_refresh_wrapper_sentinel_non_disclosure_and_no_http(monkeypatch, caplog):
    """Prove transaction exception sentinels and contractor IDs are absent from log records/extras when preflight fails in Jobber refresh wrapper."""
    import logging
    import app.services.jobber as jobber_service
    import app.services.integration_token_mutations as mutations_module

    cid = "cid_secret_jobber_sentinel_12345"
    sentinel = "SECRET_SENTINEL_EXCEPTION_DB_9999"

    async def _failing_preflight(*args, **kwargs):
        # Transaction exception inside preflight is caught and returns blocked preflight_failed
        return "blocked", "preflight_failed"

    monkeypatch.setattr(mutations_module, "check_and_recover_expired_intent_preflight_cas", _failing_preflight)

    http_count = [0]
    class _FailingHTTPClient:
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            raise AssertionError("HTTP call strictly forbidden when preflight fails!")

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", lambda: _FailingHTTPClient())

    with caplog.at_level(logging.WARNING):
        res = await jobber_service.refresh_access_token({"contractor_id": cid})
        assert res is None
        assert http_count[0] == 0

    for rec in caplog.records:
        rec_dict = rec.__dict__
        assert sentinel not in str(rec_dict)
        assert cid not in str(rec_dict)
        assert rec_dict.get("extra") is None or cid not in str(rec_dict["extra"])
        assert "preflight_failed" in rec.message or "preflight" in rec.message


@pytest.mark.asyncio
async def test_18qd_jobber_lead_capture_noop(monkeypatch):
    """Prove update_jobber_lead_capture_cas performs zero updates/sets/creates/deletes on no-op same-value update."""
    import app.services.integration_token_mutations as mutations_module

    cid = "cid_lead_noop_123"

    initial_doc = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "acc_jobber",
        "jobber_refresh_token": "ref_jobber",
        "jobber_lead_capture_enabled": True,
        "jobber_lead_capture_updated_at": 100.0,
    }
    c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: c_doc}, "admin_audit_events": {}})

    txn_counts = {"update": 0, "set": 0, "create": 0, "delete": 0}
    real_transaction_func = db.transaction

    def _instrumented_transaction():
        txn = real_transaction_func()
        orig_update = txn.update
        orig_set = txn.set
        orig_create = txn.create
        orig_delete = txn.delete

        def _count_update(*args, **kwargs):
            txn_counts["update"] += 1
            return orig_update(*args, **kwargs)

        def _count_set(*args, **kwargs):
            txn_counts["set"] += 1
            return orig_set(*args, **kwargs)

        def _count_create(*args, **kwargs):
            txn_counts["create"] += 1
            return orig_create(*args, **kwargs)

        def _count_delete(*args, **kwargs):
            txn_counts["delete"] += 1
            return orig_delete(*args, **kwargs)

        txn.update = _count_update
        txn.set = _count_set
        txn.create = _count_create
        txn.delete = _count_delete
        return txn

    monkeypatch.setattr(db, "transaction", _instrumented_transaction)

    res = await mutations_module.update_jobber_lead_capture_cas(
        contractor_id=cid,
        enabled=True,
        db=db,
    )

    assert res.enabled is True
    assert res.previous_enabled is True
    assert res.updated_at == 100.0
    assert txn_counts["update"] == 0
    assert txn_counts["set"] == 0
    assert txn_counts["create"] == 0
    assert txn_counts["delete"] == 0
    assert c_doc.get().to_dict() == initial_doc
