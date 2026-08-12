"""Tenant provenance contracts for contact-backed receptionist greetings."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.routing import Route
from app.services.state_machine import ActiveCall, CallState
from app.utils.phone import phone_hash
from app.webhooks import media_stream, twilio_incoming
from tests.unit.test_security_audit_medium import _install_fake_firestore

CALLER_PHONE = "+15551234567"
TENANT_ID = "tenant-b"


class _FakeFormRequest:
    async def form(self) -> dict[str, str]:
        return {
            "CallSid": "CA_contact_provenance",
            "From": CALLER_PHONE,
            "To": "+15550009999",
        }


def _contractor(*, voice_engine: str, personalization_flag: bool | None) -> dict:
    contractor = {
        "contractor_id": TENANT_ID,
        "owner_name": "Owner",
        "owner_phone": "+15550000001",
        "business_name": "Tenant B Plumbing",
        "voice_engine": voice_engine,
        "ring_through_contacts": False,
        "mode": "business",
        "subscription_status": "active",
        "subscription_tier": "business",
    }
    if personalization_flag is not None:
        contractor["customer_memory_personalization_enabled"] = personalization_flag
    return contractor


async def _run_incoming_call(
    monkeypatch,
    contractor: dict | None,
) -> tuple[str, dict]:
    captured: dict = {}
    recorded = asyncio.Event()

    async def get_contractor_by_twilio_number(_number: str) -> dict | None:
        return dict(contractor) if contractor else None

    async def get_call_history(_phone: str, limit: int = 10) -> list[dict]:
        assert limit == 10
        return []

    async def register_conference(
        _conference_name: str,
        _contractor_id: str,
        _call_sid: str,
    ) -> None:
        return None

    async def record_routing(**kwargs) -> None:
        captured.update(kwargs)
        recorded.set()

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_twilio_number",
        get_contractor_by_twilio_number,
    )
    monkeypatch.setattr("app.db.calls.get_call_history", get_call_history)
    monkeypatch.setattr("app.services.circuit_breaker.is_circuit_open", lambda: False)
    monkeypatch.setattr(
        "app.services.quiet_hours.get_quiet_hours_routing_override",
        lambda _score: None,
    )
    monkeypatch.setattr(
        "app.services.conference_registry.new_conference_name",
        lambda _prefix: "opaque_conference",
    )
    monkeypatch.setattr(
        "app.services.conference_registry.register_conference",
        register_conference,
    )
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", record_routing)

    response = await twilio_incoming.handle_incoming_call(_FakeFormRequest())
    await asyncio.wait_for(recorded.wait(), timeout=1)
    return bytes(response.body).decode(), captured


@pytest.mark.asyncio
async def test_unmatched_twilio_number_never_consults_global_contacts(monkeypatch):
    _install_fake_firestore(monkeypatch)

    async def fail_if_contact_is_read(*_args, **_kwargs):
        raise AssertionError("an unbound call must not perform a contact lookup")

    monkeypatch.setattr("app.db.contacts.get_contact", fail_if_contact_is_read)

    _twiml, captured = await _run_incoming_call(monkeypatch, None)

    assert captured["contractor_id"] == ""
    assert captured["lookups"]["contact"] is None
    assert captured["caller_name"] == ""
    assert captured["caller_name_trusted"] is False


@pytest.mark.asyncio
async def test_tenant_contact_api_returns_not_found_for_global_only_record(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    valid_phone = "+16175550123"
    fake._docs[f"contacts/{phone_hash(valid_phone)}"] = {
        "name": "Legacy Carol",
        "is_whitelisted": True,
    }

    from app.api.contacts import api_get_contact

    class _State:
        contractor_id = TENANT_ID
        is_admin = False

    class _Request:
        state = _State()

    result = await api_get_contact(valid_phone, _Request(), contractor_id=TENANT_ID)

    assert result == ({"error": "Not found"}, 404)


@pytest.mark.parametrize("personalization_flag", [None, False])
@pytest.mark.asyncio
async def test_relay_ingress_never_uses_global_contact_or_caller_contact(
    monkeypatch,
    personalization_flag,
):
    fake = _install_fake_firestore(monkeypatch)
    fake._docs[f"contacts/{phone_hash(CALLER_PHONE)}"] = {
        "name": "Legacy Carol",
        "is_whitelisted": True,
    }
    fake._docs["caller_contacts/15551234567"] = {
        "caller_name": "Legacy Carol",
    }
    fake._docs[f"contractors/{TENANT_ID}/caller_contacts/15551234567"] = {
        "caller_name": "Possibly Migrated Carol",
    }

    twiml, captured = await _run_incoming_call(
        monkeypatch,
        _contractor(
            voice_engine="relay",
            personalization_flag=personalization_flag,
        ),
    )

    assert captured["route"] is Route.AI_SCREENING
    assert captured["lookups"]["contact"] is None
    assert captured["caller_name"] == ""
    assert captured["caller_name_trusted"] is False
    assert "Legacy Carol" not in twiml
    assert "Hello, Legacy" not in twiml
    assert "thank you for calling Tenant B Plumbing" in twiml


@pytest.mark.parametrize("personalization_flag", [None, False])
@pytest.mark.asyncio
async def test_relay_ingress_preserves_tenant_owner_contact_greeting(
    monkeypatch,
    personalization_flag,
):
    fake = _install_fake_firestore(monkeypatch)
    fake._docs[f"contacts/{phone_hash(CALLER_PHONE)}"] = {
        "name": "Wrong Global Name",
        "is_whitelisted": True,
    }
    fake._docs[f"contractors/{TENANT_ID}/contacts/{phone_hash(CALLER_PHONE)}"] = {
        "name": "Alice Tenant",
        "is_whitelisted": True,
    }

    twiml, captured = await _run_incoming_call(
        monkeypatch,
        _contractor(
            voice_engine="relay",
            personalization_flag=personalization_flag,
        ),
    )

    assert captured["route"] is Route.AI_SCREENING
    assert captured["caller_name"] == "Alice Tenant"
    assert captured["caller_name_trusted"] is True
    assert "Hello, Alice. How can I help you today?" in twiml
    assert "Wrong Global Name" not in twiml


class _RtdbReference:
    def __init__(self, call_data: dict):
        self._call_data = call_data

    def get(self) -> dict:
        return dict(self._call_data)


class _MediaWebSocket:
    def __init__(self, ws_token: str):
        self._ws_token = ws_token
        self._block_ingress = asyncio.Event()
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        return json.dumps(
            {
                "event": "start",
                "streamSid": "MZ_contact_provenance",
                "start": {"customParameters": {"ws_token": self._ws_token}},
            }
        )

    async def iter_text(self):
        await self._block_ingress.wait()
        if False:  # pragma: no cover - makes this an async generator
            yield ""

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


async def _run_gemini_setup(
    monkeypatch,
    *,
    contractor: dict,
    captured_routing: dict,
) -> tuple[str, str, dict]:
    ws_token = "tenant-bound-token"
    active_call = ActiveCall(
        call_sid=captured_routing["call_sid"],
        caller_phone=captured_routing["caller_phone"],
        state=CallState.SCREENING,
        contractor_id=captured_routing["contractor_id"],
        ws_token=ws_token,
        caller_name=captured_routing["caller_name"],
        caller_name_trusted=captured_routing["caller_name_trusted"],
    )
    call_data = active_call.to_dict()
    websocket = _MediaWebSocket(ws_token)
    setup: dict = {}

    async def resolve_active_call(_call_sid: str, _call_data: dict) -> ActiveCall:
        return active_call

    async def get_contractor(_contractor_id: str) -> dict:
        return dict(contractor)

    async def capture_pipeline(pipeline, _ingress, **_kwargs) -> bool:
        setup["config"] = dict(pipeline._contractor_config)
        setup["greeting"] = pipeline._build_greeting_text()
        setup["prompt"] = pipeline._system_prompt
        return True

    monkeypatch.setattr(media_stream, "_init_firebase", lambda: None)
    monkeypatch.setattr(
        "firebase_admin.db.reference",
        lambda _path: _RtdbReference(call_data),
    )
    monkeypatch.setattr(media_stream, "_resolve_active_call", resolve_active_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", get_contractor)
    monkeypatch.setattr(media_stream.settings, "gemini_api_key", "test-gemini-key")
    monkeypatch.setattr(media_stream, "_serve_pipeline_ingress", capture_pipeline)

    await media_stream.media_stream_ws(websocket, captured_routing["call_sid"])
    return setup["greeting"], setup["prompt"], setup["config"]


@pytest.mark.parametrize("personalization_flag", [None, False])
@pytest.mark.asyncio
async def test_gemini_ingress_never_speaks_global_contact(
    monkeypatch,
    personalization_flag,
):
    fake = _install_fake_firestore(monkeypatch)
    fake._docs[f"contacts/{phone_hash(CALLER_PHONE)}"] = {
        "name": "Legacy Carol",
        "is_whitelisted": True,
    }
    fake._docs["caller_contacts/15551234567"] = {
        "caller_name": "Legacy Carol",
    }
    fake._docs[f"contractors/{TENANT_ID}/caller_contacts/15551234567"] = {
        "caller_name": "Possibly Migrated Carol",
    }
    contractor = _contractor(
        voice_engine="gemini",
        personalization_flag=personalization_flag,
    )
    _twiml, captured = await _run_incoming_call(monkeypatch, contractor)

    greeting, prompt, pipeline_config = await _run_gemini_setup(
        monkeypatch,
        contractor=contractor,
        captured_routing=captured,
    )

    assert greeting == (
        "Hi, thank you for calling Tenant B Plumbing. My name is Kevin. How can I help you?"
    )
    assert "Legacy Carol" not in greeting
    assert "Legacy Carol" not in prompt
    assert "Possibly Migrated Carol" not in greeting
    assert "Possibly Migrated Carol" not in prompt
    assert "known_caller_name" not in pipeline_config
    assert "known_caller_name_trusted" not in pipeline_config


@pytest.mark.asyncio
async def test_gemini_ingress_preserves_tenant_owner_contact_greeting(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    fake._docs[f"contacts/{phone_hash(CALLER_PHONE)}"] = {
        "name": "Wrong Global Name",
        "is_whitelisted": True,
    }
    fake._docs[f"contractors/{TENANT_ID}/contacts/{phone_hash(CALLER_PHONE)}"] = {
        "name": "Alice Tenant",
        "is_whitelisted": True,
    }
    contractor = _contractor(voice_engine="gemini", personalization_flag=False)
    _twiml, captured = await _run_incoming_call(monkeypatch, contractor)

    greeting, prompt, pipeline_config = await _run_gemini_setup(
        monkeypatch,
        contractor=contractor,
        captured_routing=captured,
    )

    assert greeting == "Hello, Alice. How can I help you today?"
    assert pipeline_config["known_caller_name"] == "Alice Tenant"
    assert pipeline_config["known_caller_name_trusted"] is True
    assert "Wrong Global Name" not in prompt
