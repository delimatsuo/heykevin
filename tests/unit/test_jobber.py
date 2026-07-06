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


@pytest.mark.asyncio
async def test_refreshes_expiring_token_before_graphql(monkeypatch):
    calls = []
    new_token = _jwt(int(time.time()) + 3600)
    responses = [
        _FakeResponse(200, {"access_token": new_token, "refresh_token": "new-refresh"}),
        _FakeResponse(200, {"data": {"clients": {"nodes": [{"id": "client-1"}]}}}),
    ]

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    monkeypatch.setattr(jobber, "_write_jobber_tokens", lambda contractor_id, updates: _noop_async())

    from app import config
    monkeypatch.setattr(config.settings, "jobber_client_id", "client-id")
    monkeypatch.setattr(config.settings, "jobber_client_secret", "client-secret")

    contractor = {
        "contractor_id": "",
        "jobber_access_token": _jwt(int(time.time()) - 10),
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


@pytest.mark.asyncio
async def test_retries_once_after_jobber_401(monkeypatch):
    calls = []
    old_token = _jwt(int(time.time()) + 3600)
    new_token = _jwt(int(time.time()) + 7200)
    responses = [
        _FakeResponse(401, {"error": "invalid_request"}),
        _FakeResponse(200, {"access_token": new_token, "refresh_token": "new-refresh"}),
        _FakeResponse(200, {"data": {"jobCreate": {"job": {"id": "job-1"}}}}),
    ]

    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    monkeypatch.setattr(jobber, "_write_jobber_tokens", lambda contractor_id, updates: _noop_async())

    from app import config
    monkeypatch.setattr(config.settings, "jobber_client_id", "client-id")
    monkeypatch.setattr(config.settings, "jobber_client_secret", "client-secret")

    contractor = {
        "contractor_id": "",
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
        job_id = await jobber.create_job("jobber-token", {"title": "Phone inquiry"})

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
        quote_id = await jobber.create_quote("jobber-token", {"title": "Phone inquiry"})

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

    customer = await jobber.lookup_customer("jobber-token", "+15551234567")

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

    customer = await jobber.lookup_customer("jobber-token", "+15551234567")

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
        "jobber-token",
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
        "jobber-token",
        {
            "client_id": "client-1",
            "property_id": "property-1",
            "title": "Leaking sink",
        },
    )
    long_message = "Call summary " + ("x" * 6000)
    expected_note_message = long_message[:5000]
    note_id = await jobber.create_request_note("jobber-token", "request-1", long_message)

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


async def _noop_async():
    return None
