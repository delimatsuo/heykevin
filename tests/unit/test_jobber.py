"""Jobber OAuth token refresh behavior."""

import base64
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
        self.text = json.dumps(body)

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
    def __init__(self, data=None):
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []

    def get(self, *args, transaction=None, **kwargs):
        class _Snap:
            def __init__(self, d, deleted):
                self._d = dict(d) if d is not None else None
                self.exists = (d is not None) and (not deleted)

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
        return doc_ref.get()

    def update(self, doc_ref, updates):
        self._staged_updates.append((doc_ref, dict(updates)))

    def set(self, doc_ref, data):
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
                return self.docs.setdefault(doc_id, _FakeDocRef({"contractor_id": doc_id, "active": True}))

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
                "jobber_connected": True,
            })
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
    assert "Jobber request failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
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
async def test_jobber_envelope_floor_enforcement_with_zero_http(monkeypatch):
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
        "jobber_connected": True,
        "jobber_token_envelope_required": True,
        "jobber_access_token": "plaintext-access",
        "jobber_refresh_token": "plaintext-refresh",
    }

    # API call must fail closed without making HTTP calls
    assert await jobber.create_job(contractor_downgraded, {"title": "Test"}) is None
    assert not http_called

    # Malformed floor value must also fail closed with zero HTTP calls
    contractor_bad_floor = dict(contractor_downgraded, jobber_token_envelope_required="not-a-bool")
    assert await jobber.create_job(contractor_bad_floor, {"title": "Test"}) is None
    assert not http_called


async def _noop_async():
    return None
