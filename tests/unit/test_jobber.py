"""Jobber OAuth token refresh behavior."""

import base64
import json
import time

import pytest

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
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


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


async def _noop_async():
    return None
