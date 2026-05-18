"""F-07 / F-13 — conference name and call-action ownership hardening.

These tests cover the post-audit fixes to ``app/webhooks/twilio_incoming.py``
and ``app/api/voip.py``:

* The conference friendly name is now an opaque ``secrets.token_urlsafe(16)``
  suffix (no longer ``call_<CallSid>`` / ``direct_<CallSid>`` /
  ``expired_<CallSid>``), so an attacker who learns or guesses a Twilio
  ``CallSid`` cannot derive a valid conference to join.
* The conference-name → ``{contractor_id, call_sid}`` binding is persisted in
  Firestore (``conference_bindings`` collection) so that downstream webhooks
  (``/webhooks/twilio/ios-voice``) can resolve ownership.
* The ``/api/call-action`` REST endpoint refuses (HTTP 403) any action whose
  ``call_sid`` is owned by another contractor, even if the in-memory RTDB
  record has been cleaned up.
* ``_generate_access_token`` is now invoked with ``contractor_id`` everywhere
  in the Twilio webhook layer; no path falls back to the legacy ``contractor_``
  unbound identity.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, Optional

import jwt
import pytest
from fastapi import HTTPException

from app.api import voip as voip_module
from app.services import conference_registry
from app.webhooks import twilio_incoming as twilio_module


# ---------------------------------------------------------------------------
# Fakes — minimal Firestore stand-in (mirrors tests/unit/test_subscription_security.py)
# ---------------------------------------------------------------------------


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict]):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class _DocRef:
    def __init__(self, fs: "_InMemoryFirestore", path: str):
        self._fs = fs
        self._path = path

    def get(self, transaction=None):
        data = self._fs._docs.get(self._path)
        return _Snapshot(data is not None, data)

    def set(self, value: dict, merge: bool = False):
        if merge and self._path in self._fs._docs:
            current = self._fs._docs[self._path]
            current.update(value)
        else:
            self._fs._docs[self._path] = dict(value)

    def update(self, value: dict):
        self._fs._docs.setdefault(self._path, {}).update(value)


class _Collection:
    def __init__(self, fs: "_InMemoryFirestore", name: str):
        self._fs = fs
        self._name = name

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}")


class _InMemoryFirestore:
    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}

    def collection(self, name: str):
        return _Collection(self, name)

    def document(self, path: str):
        return _DocRef(self, path)


def _install_fake_firestore(monkeypatch) -> _InMemoryFirestore:
    fake = _InMemoryFirestore()
    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        lambda: fake,
    )
    return fake


# ---------------------------------------------------------------------------
# Conference name unguessability + registry round-trip
# ---------------------------------------------------------------------------


def test_new_conference_name_is_opaque_and_unique():
    """The name must be high-entropy and must NOT contain a Twilio CallSid."""
    n1 = conference_registry.new_conference_name("call")
    n2 = conference_registry.new_conference_name("call")
    assert n1 != n2

    # 16 url-safe bytes ≈ 22 base64 characters → name is at least 27 chars
    # ("call_" + suffix). Way above any reasonable guess budget.
    assert len(n1) >= 20

    # A Twilio CallSid begins with "CA" and is 34 chars. Conference name
    # must not embed one.
    sample_call_sid = "CA" + "0" * 32
    assert sample_call_sid not in n1

    # Without prefix the name is just the entropy.
    n3 = conference_registry.new_conference_name()
    assert len(n3) >= 20
    assert sample_call_sid not in n3
    assert not n3.startswith(("call_", "direct_", "expired_", "pickup_", "urgent_"))


@pytest.mark.asyncio
async def test_register_and_resolve_conference_owner(monkeypatch):
    _install_fake_firestore(monkeypatch)

    name = conference_registry.new_conference_name("call")
    await conference_registry.register_conference(name, "contractor-alpha", "CA-call-1")

    owner = await conference_registry.resolve_conference_owner(name)
    assert owner == "contractor-alpha"

    binding = await conference_registry.get_conference_binding(name)
    assert binding is not None
    assert binding["contractor_id"] == "contractor-alpha"
    assert binding["call_sid"] == "CA-call-1"


@pytest.mark.asyncio
async def test_resolve_conference_owner_returns_none_for_unknown(monkeypatch):
    _install_fake_firestore(monkeypatch)
    assert await conference_registry.resolve_conference_owner("does-not-exist") is None


# ---------------------------------------------------------------------------
# F-07: webhook layer threads contractor_id into _generate_access_token
# ---------------------------------------------------------------------------


def _set_twilio_settings(monkeypatch):
    monkeypatch.setattr(voip_module.settings, "twilio_account_sid", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(voip_module.settings, "twilio_api_key_sid", "SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(voip_module.settings, "twilio_api_key_secret", "test-secret-not-real")
    monkeypatch.setattr(voip_module.settings, "twilio_twiml_app_sid", "APxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def _decode_identity(token: str) -> str:
    decoded = jwt.decode(token, options={"verify_signature": False})
    return decoded.get("grants", {}).get("identity", "")


@pytest.mark.asyncio
async def test_ring_contractor_passes_contractor_id_into_access_token(monkeypatch):
    """The whitelisted-fast-track ring path must mint a contractor-bound token.

    Regression for the F-07 follow-up: the prior code called
    ``_generate_access_token()`` with no argument here, producing tokens with
    identity ``contractor_`` (empty contractor).
    """
    _set_twilio_settings(monkeypatch)

    # Capture what contractor_id is threaded through to _generate_access_token
    # by wrapping the real function (no recursion: we hold a reference to the
    # original before monkeypatching).
    real_generate = voip_module._generate_access_token
    captured = {}

    def wrapper(contractor_id: str = ""):
        captured["contractor_id"] = contractor_id
        return real_generate(contractor_id=contractor_id)

    monkeypatch.setattr(voip_module, "_generate_access_token", wrapper)

    async def fake_get_device_token(token_type: str = "push", contractor_id: str = ""):
        return "device-tok"

    sent = {}

    async def fake_send_voip_push(**kwargs):
        sent.update(kwargs)
        return True

    async def fake_redirect(_call_sid):
        return None

    # Avoid Twilio + RTDB side effects from the post-push polling loop.
    async def short_sleep(_):
        # Force the polling loop to exit quickly by raising so we never hit
        # the live Twilio client.
        raise asyncio.CancelledError()

    from app.services import push_notification
    monkeypatch.setattr(push_notification, "send_voip_push", fake_send_voip_push)
    monkeypatch.setattr(push_notification, "get_device_token", fake_get_device_token)
    monkeypatch.setattr(twilio_module, "_async_redirect_to_kevin", fake_redirect)
    monkeypatch.setattr(twilio_module.asyncio, "sleep", short_sleep)

    try:
        await twilio_module._ring_contractor(
            call_sid="CA-test-1",
            caller_phone="+15551112222",
            caller_name="Friend",
            conference_name="opaque-conf-abc",
            contractor_id="contractor-bob",
        )
    except asyncio.CancelledError:
        pass

    assert captured.get("contractor_id") == "contractor-bob"
    issued_token = sent.get("access_token")
    assert issued_token, sent
    assert _decode_identity(issued_token) == "contractor_contractor-bob"


# ---------------------------------------------------------------------------
# F-13: /api/call-action rejects cross-contractor call_sids
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, contractor_id: str, is_admin: bool = False):
        class _State:
            pass
        self.state = _State()
        self.state.contractor_id = contractor_id
        self.state.is_admin = is_admin


class _FakeActiveCall:
    def __init__(self, contractor_id: str):
        self.contractor_id = contractor_id


@pytest.mark.asyncio
async def test_call_action_rejects_cross_contractor_when_rtdb_present(monkeypatch):
    async def fake_get_active_call(_sid):
        return _FakeActiveCall(contractor_id="contractor-victim")

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)

    body = voip_module.CallAction(call_sid="CA-victim-1", action="voicemail")
    request = _FakeRequest("contractor-attacker")

    with pytest.raises(HTTPException) as exc:
        await voip_module.handle_call_action(
            request=request, body=body, contractor_id="contractor-attacker"
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_call_action_rejects_when_rtdb_missing_but_firestore_owned_elsewhere(monkeypatch):
    """F-13: an attacker leaking another contractor's call_sid cannot no-op
    route the call once the RTDB record has been cleaned up — Firestore
    is consulted as a fallback."""

    async def fake_get_active_call(_sid):
        return None

    async def fake_get_call(_sid):
        return {"contractor_id": "contractor-victim"}

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.calls.get_call", fake_get_call)

    body = voip_module.CallAction(call_sid="CA-victim-1", action="voicemail")
    request = _FakeRequest("contractor-attacker")

    with pytest.raises(HTTPException) as exc:
        await voip_module.handle_call_action(
            request=request, body=body, contractor_id="contractor-attacker"
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_call_action_returns_404_when_no_record_exists(monkeypatch):
    """F-13: with neither RTDB nor Firestore record, refuse rather than
    blindly hitting the Twilio API with the supplied call_sid."""

    async def fake_get_active_call(_sid):
        return None

    async def fake_get_call(_sid):
        return None

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.calls.get_call", fake_get_call)

    body = voip_module.CallAction(call_sid="CA-unknown", action="voicemail")
    request = _FakeRequest("contractor-attacker")

    with pytest.raises(HTTPException) as exc:
        await voip_module.handle_call_action(
            request=request, body=body, contractor_id="contractor-attacker"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_call_action_allows_owner_when_rtdb_record_present(monkeypatch):
    """Sanity: legitimate contractor can still operate on their own call."""

    async def fake_get_active_call(_sid):
        return _FakeActiveCall(contractor_id="contractor-owner")

    async def fake_decline(call_sid):
        return {"status": "ok"}

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)
    monkeypatch.setattr(voip_module, "_handle_decline", fake_decline)

    body = voip_module.CallAction(call_sid="CA-owner-1", action="decline")
    request = _FakeRequest("contractor-owner")

    response = await voip_module.handle_call_action(
        request=request, body=body, contractor_id="contractor-owner"
    )
    assert response == {"status": "ok"}


# ---------------------------------------------------------------------------
# F-13: /webhooks/twilio/ios-voice rejects unknown / cross-contractor confs
# ---------------------------------------------------------------------------


class _FakeFormRequest:
    """FastAPI Request stand-in that returns a fixed form dict."""

    def __init__(self, form: Dict[str, str]):
        self._form = form

    async def form(self):
        return self._form


@pytest.mark.asyncio
async def test_ios_voice_rejects_unregistered_conference(monkeypatch):
    _install_fake_firestore(monkeypatch)

    request = _FakeFormRequest({
        "conference": "totally-made-up-conf-name",
        "From": "client:contractor_attacker",
    })
    response = await twilio_module.handle_ios_voice(request)
    body = bytes(response.body).decode()
    assert "expired" in body.lower() or "hangup" in body.lower()
    # Conference verb must NOT be in the response.
    assert "<Conference" not in body


@pytest.mark.asyncio
async def test_ios_voice_rejects_cross_contractor(monkeypatch):
    fake_fs = _install_fake_firestore(monkeypatch)

    name = conference_registry.new_conference_name("call")
    await conference_registry.register_conference(name, "contractor-victim", "CA-1")

    request = _FakeFormRequest({
        "conference": name,
        "From": "client:contractor_attacker",
    })
    response = await twilio_module.handle_ios_voice(request)
    body = bytes(response.body).decode()
    assert "denied" in body.lower() or "hangup" in body.lower()
    assert "<Conference" not in body


@pytest.mark.asyncio
async def test_ios_voice_allows_owner(monkeypatch):
    _install_fake_firestore(monkeypatch)

    name = conference_registry.new_conference_name("call")
    await conference_registry.register_conference(name, "contractor-owner", "CA-1")

    request = _FakeFormRequest({
        "conference": name,
        "From": "client:contractor_contractor-owner",
    })
    response = await twilio_module.handle_ios_voice(request)
    body = bytes(response.body).decode()
    assert "<Conference" in body
    assert name in body
