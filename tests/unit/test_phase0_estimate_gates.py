import os

import httpx
import pytest
from fastapi import FastAPI

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.api import estimates
from app.services.gated_actions import ActionKey, GateContext, GateReason


class _Request:
    headers = {"content-type": "image/jpeg", "content-length": "4"}

    async def stream(self):
        yield b"data"


class _Doc:
    def __init__(self, updates):
        self.updates = updates

    def update(self, data):
        self.updates.append(data)


class _Collection:
    def __init__(self, updates):
        self.updates = updates

    def document(self, _key):
        return _Doc(self.updates)


class _DB:
    def __init__(self):
        self.updates = []

    def collection(self, _name):
        return _Collection(self.updates)


async def _estimate_doc(_token):
    return {
        "contractor_id": "c1",
        "caller_phone": "+15551234567",
        "upload_count": 0,
    }


async def _analyze_media(**_kwargs):
    return {"diagnosis": "leak", "estimate_min": 100, "estimate_max": 200, "confidence": "medium"}


def _set_common_estimate_fakes(monkeypatch):
    db = _DB()
    monkeypatch.setattr(estimates, "_get_estimate_doc", _estimate_doc)
    monkeypatch.setattr(estimates, "analyze_media", _analyze_media)
    monkeypatch.setattr(estimates, "get_firestore_client", lambda: db)
    return db


@pytest.mark.asyncio
async def test_estimate_result_sms_fails_closed_when_sms_gate_disabled(monkeypatch):
    sent = []
    audits = []
    logs = []
    _set_common_estimate_fakes(monkeypatch)

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
        }

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    def fake_logger_info(message, *args, **kwargs):
        logs.append((message, kwargs))

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)
    monkeypatch.setattr(estimates.logger, "info", fake_logger_info)

    result = await estimates.upload_and_analyze("token", request=_Request())

    assert result["status"] == "ok"
    assert len(sent) == 1
    owner_args, owner_kwargs = sent[0]
    assert owner_args[0] == "+15550000000"
    assert "AI ESTIMATE SENT" in owner_args[1]
    assert owner_kwargs == {"from_number": "+15559999999"}
    assert audits
    audit = audits[0]
    assert audit["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert audit["contractor_id"] == "c1"
    assert audit["source"] == "estimate"
    assert audit["resource_id"] == estimates._hash_token("token")[:12]
    assert audit["decision"].allowed is False

    blocked_logs = [entry for entry in logs if entry[0] == "Estimate caller SMS blocked by gate"]
    assert blocked_logs == [
        (
            "Estimate caller SMS blocked by gate",
            {"extra": {"action": ActionKey.ESTIMATE_RESULT_SMS.value, "reason": audit["decision"].reason.value}},
        )
    ]
    blocked_payload = str(blocked_logs)
    assert "+15551234567" not in blocked_payload
    assert "leak" not in blocked_payload


@pytest.mark.asyncio
async def test_estimate_result_sms_allowed_passes_gate_context_to_caller_sms_only(monkeypatch):
    sent = []
    audits = []
    _set_common_estimate_fakes(monkeypatch)
    raw_token = "raw-estimate-bearer-token"
    token_hash_prefix = estimates._hash_token(raw_token)[:12]

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "gated_actions": {ActionKey.ESTIMATE_RESULT_SMS.value: True},
            "sms_compliance_status": "approved",
        }

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)

    result = await estimates.upload_and_analyze(raw_token, request=_Request())

    assert result["status"] == "ok"
    assert len(sent) == 2

    caller_args, caller_kwargs = sent[0]
    assert caller_args[0] == "+15551234567"
    assert "AI Diagnosis: leak" in caller_args[1]
    assert caller_kwargs["from_number"] == "+15559999999"
    assert caller_kwargs["contractor"]["contractor_id"] == "c1"
    assert caller_kwargs["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert caller_kwargs["gate_context"] == GateContext(
        source="estimate",
        actor="system",
        idempotency_key=f"{token_hash_prefix}:caller_sms",
    )
    assert raw_token not in caller_kwargs["gate_context"].idempotency_key
    assert token_hash_prefix in caller_kwargs["gate_context"].idempotency_key

    owner_args, owner_kwargs = sent[1]
    assert owner_args[0] == "+15550000000"
    assert "AI ESTIMATE SENT" in owner_args[1]
    assert owner_kwargs == {"from_number": "+15559999999"}

    assert audits
    audit = audits[0]
    assert audit["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert audit["contractor_id"] == "c1"
    assert audit["source"] == "estimate"
    assert audit["resource_id"] == token_hash_prefix
    assert audit["decision"].allowed is True


@pytest.mark.asyncio
async def test_estimate_upload_route_injects_request_and_executes_sms_gate(monkeypatch):
    sent = []
    audits = []
    _set_common_estimate_fakes(monkeypatch)

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "gated_actions": {ActionKey.ESTIMATE_RESULT_SMS.value: True},
            "sms_compliance_status": "approved",
        }

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)

    app = FastAPI()
    app.include_router(estimates.router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/estimates/route-token/upload",
            content=b"data",
            headers={"content-type": "image/jpeg"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(sent) == 2
    assert audits
    assert audits[0]["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert audits[0]["decision"].allowed is True


@pytest.mark.asyncio
async def test_estimate_result_sms_fails_closed_when_sms_compliance_not_approved(monkeypatch):
    sent = []
    audits = []
    _set_common_estimate_fakes(monkeypatch)

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "gated_actions": {ActionKey.ESTIMATE_RESULT_SMS.value: True},
            "sms_compliance_status": "pending",
        }

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)

    result = await estimates.upload_and_analyze("token", request=_Request())

    assert result["status"] == "ok"
    assert len(sent) == 1
    owner_args, owner_kwargs = sent[0]
    assert owner_args[0] == "+15550000000"
    assert "AI ESTIMATE SENT" in owner_args[1]
    assert owner_kwargs == {"from_number": "+15559999999"}

    assert audits
    audit = audits[0]
    assert audit["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert audit["decision"].allowed is False
    assert audit["decision"].reason == GateReason.COMPLIANCE_NOT_APPROVED
