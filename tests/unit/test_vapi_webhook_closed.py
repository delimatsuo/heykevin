"""The legacy Vapi webhook stays closed, and an unset Vapi secret never fires the error alert.

Every Cloud Run instance start logged CRITICAL "vapi_webhook_secret is not set",
which matched the production error alert and emailed the owner, though
app.main never mounts the Vapi router.
"""

import hashlib
import hmac
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import app.main as app_main
from app.webhooks import vapi_events

SECRET = "fictional-vapi-secret"
EVENT = json.dumps({"message": {"type": "status-update"}}).encode()


class _Request:
    def __init__(self, body: bytes, signature: str | None = None):
        self._body = body
        self.headers = {} if signature is None else {"X-Vapi-Signature": signature}

    async def body(self):
        return self._body

    async def json(self):
        return json.loads(self._body)


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def handled(monkeypatch):
    events = []

    async def handle_status_update(data):
        events.append(data)

    monkeypatch.setattr(vapi_events, "_handle_status_update", handle_status_update)
    return events


def _secret(monkeypatch, value: str):
    monkeypatch.setattr(vapi_events, "settings", SimpleNamespace(vapi_webhook_secret=value))


@pytest.mark.asyncio
async def test_events_are_rejected_while_no_secret_is_set(monkeypatch, handled):
    _secret(monkeypatch, "")
    # Signed with the empty key, which is what an unset secret would check against.
    signature = hmac.new(b"", EVENT, hashlib.sha256).hexdigest()

    response = await vapi_events.handle_vapi_event(_Request(EVENT, signature=signature))

    assert response.status_code == 401
    assert handled == []


@pytest.mark.asyncio
async def test_events_with_a_wrong_signature_are_rejected(monkeypatch, handled):
    _secret(monkeypatch, SECRET)

    response = await vapi_events.handle_vapi_event(_Request(EVENT, signature="0" * 64))

    assert response.status_code == 401
    assert handled == []


@pytest.mark.asyncio
async def test_signed_events_are_handled(monkeypatch, handled):
    _secret(monkeypatch, SECRET)

    response = await vapi_events.handle_vapi_event(_Request(EVENT, signature=_sign(EVENT)))

    assert response == {"status": "ok"}
    assert handled == [json.loads(EVENT)]


def test_the_vapi_webhook_is_not_served():
    paths = {getattr(route, "path", "") for route in app_main.app.routes}

    assert not any(path.startswith("/webhooks/vapi") for path in paths)


@pytest.mark.asyncio
async def test_startup_without_a_vapi_secret_logs_no_error(monkeypatch):
    records = []
    logger = SimpleNamespace(**{
        level: (lambda *args, _level=level, **kwargs: records.append((_level, args)))
        for level in ("debug", "info", "warning", "error", "critical")
    })
    required = ["twilio_account_sid", "twilio_auth_token", "anthropic_api_key",
                "deepgram_api_key", "elevenlabs_api_key", "api_bearer_token"]
    monkeypatch.setattr(app_main, "settings", SimpleNamespace(
        **{key: "fictional" for key in required},
        vapi_webhook_secret="", environment="production",
    ))
    monkeypatch.setattr(app_main, "validate_runtime_safety", lambda: None)

    async def no_workers():
        return None

    monkeypatch.setattr(app_main, "_configure_background_workers", no_workers)
    monkeypatch.setattr(app_main, "logger", logger)

    await app_main.startup()

    assert [level for level, _ in records if level in ("error", "critical")] == []
    assert ("info", ("Kevin starting up",)) in records
