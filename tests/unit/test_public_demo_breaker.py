"""Isolation, binding, and timeout tests for the private Twilio breaker."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app import public_demo_breaker_main
from app.services import (
    public_demo_breaker,
    public_demo_breaker_client,
)

PARENT_SID = "AC" + "a" * 32
CHILD_SID = "AC" + "b" * 32
KEY_SID = "SK" + "c" * 32
TRIGGER_SID = "UT" + "d" * 32
SHARED_SECRET = "breaker-test-secret-that-is-long-enough"
CALLER_IDENTITY = "public-demo@example.iam.gserviceaccount.com"
AUDIENCE = "https://private-breaker.example.run.app"


def _breaker_configuration() -> public_demo_breaker.PublicDemoBreakerSettings:
    return public_demo_breaker.PublicDemoBreakerSettings(
        environment="demo-breaker",
        public_demo_breaker_audience=AUDIENCE,
        public_demo_breaker_hmac_secret=SHARED_SECRET,
        public_demo_breaker_caller_service_account=CALLER_IDENTITY,
        public_demo_twilio_usage_trigger_sid=TRIGGER_SID,
        public_demo_twilio_daily_spend_limit_usd=5,
        public_demo_breaker_twilio_parent_account_sid=PARENT_SID,
        public_demo_breaker_twilio_parent_main_api_key_sid=KEY_SID,
        public_demo_breaker_twilio_parent_main_api_key_secret="s" * 32,
        public_demo_breaker_twilio_child_account_sid=CHILD_SID,
    )


def _install_breaker_configuration(monkeypatch) -> None:
    configured = _breaker_configuration()
    for name in type(configured).model_fields:
        monkeypatch.setattr(
            public_demo_breaker.breaker_settings,
            name,
            getattr(configured, name),
        )


def _signed_body(*, fired_at: datetime | None = None, **overrides):
    payload = {
        "action": "suspend_public_demo",
        "caller_service_account": CALLER_IDENTITY,
        "child_account_sid": CHILD_SID,
        "current_value": "5.01",
        "date_fired": (fired_at or datetime.now(UTC)).isoformat(),
        "request_key": "e" * 64,
        "trigger_value": "5",
        "usage_trigger_sid": TRIGGER_SID,
        "version": 1,
    }
    payload.update(overrides)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = public_demo_breaker_client._breaker_signature(body, SHARED_SECRET)
    return body, signature


def _clear_forbidden_breaker_environment(monkeypatch) -> None:
    for name in public_demo_breaker._FORBIDDEN_BREAKER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (-60, True),
        (-60.001, False),
        (899.999, True),
        (900, False),
    ],
)
def test_private_callback_freshness_boundaries(age, expected):
    assert public_demo_breaker_main._callback_age_is_fresh(age) is expected


def test_private_runtime_requires_exact_separate_parent_child_and_no_auth_token(monkeypatch):
    _clear_forbidden_breaker_environment(monkeypatch)
    configured = _breaker_configuration()
    public_demo_breaker.validate_public_demo_breaker_runtime(configured)

    configured.public_demo_breaker_twilio_child_account_sid = PARENT_SID
    with pytest.raises(RuntimeError, match="parent and child.*must differ"):
        public_demo_breaker.validate_public_demo_breaker_runtime(configured)

    configured = _breaker_configuration()
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "forbidden-parent-auth-token")
    with pytest.raises(RuntimeError, match="forbidden.*AuthToken"):
        public_demo_breaker.validate_public_demo_breaker_runtime(configured)


def test_private_runtime_rejects_debug_logging(monkeypatch):
    _clear_forbidden_breaker_environment(monkeypatch)
    configured = _breaker_configuration()
    configured.log_level = "DEBUG"

    with pytest.raises(RuntimeError, match="LOG_LEVEL must be INFO or WARNING"):
        public_demo_breaker.validate_public_demo_breaker_runtime(configured)


@pytest.mark.asyncio
async def test_private_endpoint_rejects_unauthenticated_even_with_valid_hmac(monkeypatch):
    _install_breaker_configuration(monkeypatch)
    body, signature = _signed_body()

    async def unexpected():
        raise AssertionError("unauthenticated request must not reach Twilio")

    monkeypatch.setattr(public_demo_breaker_main, "trip_twilio_parent_breaker", unexpected)
    transport = httpx.ASGITransport(app=public_demo_breaker_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://breaker") as client:
        response = await client.post(
            "/v1/public-demo/suspend",
            content=body,
            headers={"X-Kevin-Breaker-Signature": signature},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_override",
    [
        {"child_account_sid": "AC" + "9" * 32},
        {"usage_trigger_sid": "UT" + "9" * 32},
        {"caller_service_account": "unexpected@example.iam.gserviceaccount.com"},
        {"current_value": "4.99"},
    ],
)
async def test_private_endpoint_rejects_misbound_signed_payload(monkeypatch, body_override):
    _install_breaker_configuration(monkeypatch)
    body, signature = _signed_body(**body_override)

    async def unexpected():
        raise AssertionError("misbound request must not reach Twilio")

    monkeypatch.setattr(public_demo_breaker_main, "trip_twilio_parent_breaker", unexpected)
    transport = httpx.ASGITransport(app=public_demo_breaker_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://breaker") as client:
        response = await client.post(
            "/v1/public-demo/suspend",
            content=body,
            headers={
                "Authorization": "Bearer header.payload.signature",
                "X-Kevin-Breaker-Signature": signature,
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_private_endpoint_rejects_stale_replay_and_accepts_fresh_retry(monkeypatch):
    _install_breaker_configuration(monkeypatch)
    tripped = []

    async def trip():
        tripped.append(True)
        return True

    monkeypatch.setattr(public_demo_breaker_main, "trip_twilio_parent_breaker", trip)
    transport = httpx.ASGITransport(app=public_demo_breaker_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://breaker") as client:
        stale_body, stale_signature = _signed_body(
            fired_at=datetime.now(UTC) - timedelta(hours=1)
        )
        stale = await client.post(
            "/v1/public-demo/suspend",
            content=stale_body,
            headers={
                "Authorization": "Bearer header.payload.signature",
                "X-Kevin-Breaker-Signature": stale_signature,
            },
        )
        fresh_body, fresh_signature = _signed_body()
        first = await client.post(
            "/v1/public-demo/suspend",
            content=fresh_body,
            headers={
                "Authorization": "Bearer header.payload.signature",
                "X-Kevin-Breaker-Signature": fresh_signature,
            },
        )
        replay = await client.post(
            "/v1/public-demo/suspend",
            content=fresh_body,
            headers={
                "Authorization": "Bearer header.payload.signature",
                "X-Kevin-Breaker-Signature": fresh_signature,
            },
        )

    assert stale.status_code == 403
    assert first.json() == {"status": "suspended"}
    assert replay.json() == {"status": "suspended"}
    assert tripped == [True, True]


class _FakeHttpClient:
    observed_kwargs: ClassVar[list[dict]] = []

    def __init__(self, **kwargs):
        self.observed_kwargs.append(kwargs)

    async def close(self):
        return None


class _FakeAccounts:
    def __init__(self, events, *, owner_sid=PARENT_SID, returned_sid=CHILD_SID):
        self.events = events
        self.owner_sid = owner_sid
        self.returned_sid = returned_sid

    def __call__(self, sid):
        self.events.append(("select-account", sid))
        return self

    async def update_async(self, *, status):
        self.events.append(("suspend", status))
        return SimpleNamespace(
            status=status,
            sid=self.returned_sid,
            owner_account_sid=self.owner_sid,
        )

    async def fetch_async(self):
        self.events.append(("fetch-account",))
        return SimpleNamespace(
            status="suspended",
            sid=self.returned_sid,
            owner_account_sid=self.owner_sid,
        )


@pytest.mark.asyncio
async def test_parent_main_key_only_suspends_exact_child(monkeypatch):
    configured = _breaker_configuration()
    events = []
    client_calls = []
    accounts = _FakeAccounts(events)

    def client_factory(username, password, *, account_sid, http_client):
        client_calls.append((username, password, account_sid, http_client))
        events.append(("client", account_sid))
        assert account_sid == PARENT_SID
        return SimpleNamespace(api=SimpleNamespace(accounts=accounts))

    monkeypatch.setattr("twilio.rest.Client", client_factory)
    monkeypatch.setattr(
        "twilio.http.async_http_client.AsyncTwilioHttpClient",
        _FakeHttpClient,
    )

    assert await public_demo_breaker.trip_twilio_parent_breaker(configured) is True
    assert [call[0:3] for call in client_calls] == [(KEY_SID, "s" * 32, PARENT_SID)]
    assert events == [
        ("client", PARENT_SID),
        ("select-account", CHILD_SID),
        ("suspend", "suspended"),
    ]
    assert "max_retries" not in _FakeHttpClient.observed_kwargs[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_sid", "owner_sid"),
    [("AC" + "9" * 32, PARENT_SID), (CHILD_SID, "AC" + "9" * 32)],
)
async def test_parent_response_must_bind_exact_child_and_owner(
    monkeypatch,
    returned_sid,
    owner_sid,
):
    configured = _breaker_configuration()
    events = []
    accounts = _FakeAccounts(events, owner_sid=owner_sid, returned_sid=returned_sid)

    def client_factory(*_args, account_sid, **_kwargs):
        assert account_sid == PARENT_SID
        return SimpleNamespace(api=SimpleNamespace(accounts=accounts))

    monkeypatch.setattr("twilio.rest.Client", client_factory)
    monkeypatch.setattr(
        "twilio.http.async_http_client.AsyncTwilioHttpClient",
        _FakeHttpClient,
    )

    assert await public_demo_breaker.trip_twilio_parent_breaker(configured) is False


@pytest.mark.asyncio
async def test_provider_errors_never_log_parent_child_or_key_identity(monkeypatch, caplog):
    configured = _breaker_configuration()

    class SensitiveFailure(RuntimeError):
        pass

    class Accounts:
        def __call__(self, _sid):
            return self

        async def update_async(self, **_kwargs):
            raise SensitiveFailure(f"{PARENT_SID} {CHILD_SID} {KEY_SID}")

        async def fetch_async(self):
            raise SensitiveFailure(f"{PARENT_SID} {CHILD_SID} {KEY_SID}")

    monkeypatch.setattr(
        "twilio.rest.Client",
        lambda *_args, **_kwargs: SimpleNamespace(api=SimpleNamespace(accounts=Accounts())),
    )
    monkeypatch.setattr(
        "twilio.http.async_http_client.AsyncTwilioHttpClient",
        _FakeHttpClient,
    )
    caplog.set_level(logging.WARNING)

    assert await public_demo_breaker.trip_twilio_parent_breaker(configured) is False
    rendered = caplog.text
    assert PARENT_SID not in rendered
    assert CHILD_SID not in rendered
    assert KEY_SID not in rendered
    assert "SensitiveFailure" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [503, "timeout"])
async def test_public_client_returns_false_on_private_5xx_or_timeout(monkeypatch, outcome):
    monkeypatch.setattr(
        public_demo_breaker_client.settings,
        "public_demo_breaker_url",
        AUDIENCE,
    )
    monkeypatch.setattr(
        public_demo_breaker_client.settings,
        "public_demo_breaker_audience",
        AUDIENCE,
    )
    monkeypatch.setattr(
        public_demo_breaker_client.settings,
        "public_demo_breaker_hmac_secret",
        SHARED_SECRET,
    )
    monkeypatch.setattr(
        public_demo_breaker_client.settings,
        "public_demo_breaker_caller_service_account",
        CALLER_IDENTITY,
    )
    monkeypatch.setattr(
        public_demo_breaker_client,
        "_fetch_cloud_run_id_token",
        lambda _audience: _async_value("header.payload.signature"),
    )

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            if outcome == "timeout":
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(outcome, json={"detail": "Retry later"})

    monkeypatch.setattr(public_demo_breaker_client.httpx, "AsyncClient", Client)

    assert await public_demo_breaker_client.trip_public_demo_breaker(
        child_account_sid=CHILD_SID,
        usage_trigger_sid=TRIGGER_SID,
        trigger_value=Decimal(5),
        current_value=Decimal("5.01"),
        date_fired=datetime.now(UTC),
        idempotency_token="provider-idempotency-token",
    ) is False


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_metadata_token_request_uses_exact_audience_full_format(monkeypatch):
    observed = []

    class Client:
        def __init__(self, **kwargs):
            observed.append(("init", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params, headers):
            observed.append((url, params, headers))
            return httpx.Response(
                200,
                text="header.payload.signature",
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(public_demo_breaker_client.httpx, "AsyncClient", Client)

    token = await public_demo_breaker_client._fetch_cloud_run_id_token(AUDIENCE)
    assert token == "header.payload.signature"
    assert observed[1][1] == {"audience": AUDIENCE, "format": "full"}
    assert observed[1][2] == {"Metadata-Flavor": "Google"}


def test_canonical_public_request_never_contains_provider_idempotency_token():
    body = public_demo_breaker_client._canonical_breaker_body(
        child_account_sid=CHILD_SID,
        usage_trigger_sid=TRIGGER_SID,
        trigger_value=Decimal(5),
        current_value=Decimal("5.01"),
        date_fired=datetime.now(UTC),
        idempotency_token="provider-secret-looking-idempotency-token",
        shared_secret=SHARED_SECRET,
        caller_service_account=CALLER_IDENTITY,
    )
    assert b"provider-secret-looking-idempotency-token" not in body
    assert len(json.loads(body)["request_key"]) == 64
