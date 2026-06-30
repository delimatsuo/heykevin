import os

import httpx
import pytest
from fastapi import FastAPI, HTTPException

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.api import estimates
from app.services.gated_actions import ActionKey, GateContext, GateReason


class _Request:
    def __init__(self, body=b"data", headers=None):
        self.body = body
        self.headers = headers or {"content-type": "image/jpeg", "content-length": str(len(body))}
        self.stream_calls = 0

    async def stream(self):
        self.stream_calls += 1
        yield self.body


class _UnreadableRequest(_Request):
    async def stream(self):
        self.stream_calls += 1
        raise AssertionError("upload body must not be read when the estimate gate denies")
        yield b""


class _AuthState:
    is_admin = True
    contractor_id = ""


class _AuthedRequest:
    state = _AuthState()


class _Doc:
    def __init__(self, db, key):
        self.db = db
        self.key = key

    def update(self, data):
        self.db.updates.append({"key": self.key, "data": data})

    def set(self, data):
        self.db.sets.append({"key": self.key, "data": data})


class _Collection:
    def __init__(self, db):
        self.db = db

    def document(self, key):
        return _Doc(self.db, key)


class _DB:
    def __init__(self):
        self.sets = []
        self.updates = []

    def collection(self, _name):
        return _Collection(self)


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
async def test_create_token_gate_disabled_does_not_write_or_return_public_url(monkeypatch):
    audits = []
    contexts = []
    body = estimates.CreateTokenRequest(
        contractor_id="contractor-1234567890",
        caller_phone="+15551234567",
        call_sid="CA123",
    )
    real_check_gated_action = estimates.check_gated_action

    async def fake_get_contractor(_cid):
        return {"contractor_id": "contractor-1234567890"}

    def fail_token_urlsafe(_length):
        raise AssertionError("token generation must not run when the gate denies")

    def fail_get_firestore_client():
        raise AssertionError("Firestore must not be touched when the gate denies")

    def capture_check(contractor, action, context):
        contexts.append(context)
        return real_check_gated_action(contractor, action, context)

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates.secrets, "token_urlsafe", fail_token_urlsafe)
    monkeypatch.setattr(estimates, "get_firestore_client", fail_get_firestore_client)
    monkeypatch.setattr(estimates, "check_gated_action", capture_check)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await estimates.create_estimate_token(body, request=_AuthedRequest())

    assert exc_info.value.status_code == 403
    assert "token" not in str(exc_info.value.detail).lower()
    assert "url" not in str(exc_info.value.detail).lower()
    assert contexts == [
        GateContext(source="estimate", actor="system", idempotency_key="CA123:estimate_token")
    ]
    assert "+15551234567" not in contexts[0].idempotency_key
    assert len(audits) == 1
    assert audits[0]["action"] == ActionKey.ESTIMATE_TOKEN_CREATE
    assert audits[0]["source"] == "estimate"
    assert audits[0]["resource_id"] == "CA123"
    assert audits[0]["decision"].allowed is False


@pytest.mark.asyncio
async def test_create_token_gate_allowed_writes_and_returns_public_url(monkeypatch):
    db = _DB()
    audits = []
    contexts = []
    body = estimates.CreateTokenRequest(
        contractor_id="contractor-1234567890",
        caller_phone="+15551234567",
        call_sid="CA123",
    )
    real_check_gated_action = estimates.check_gated_action

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "contractor-1234567890",
            "gated_actions": {ActionKey.ESTIMATE_TOKEN_CREATE.value: True},
            "automation_approvals": {ActionKey.ESTIMATE_TOKEN_CREATE.value: True},
        }

    def capture_check(contractor, action, context):
        contexts.append(context)
        return real_check_gated_action(contractor, action, context)

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates.secrets, "token_urlsafe", lambda _length: "created-estimate-token")
    monkeypatch.setattr(estimates, "get_firestore_client", lambda: db)
    monkeypatch.setattr(estimates, "check_gated_action", capture_check)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)

    result = await estimates.create_estimate_token(body, request=_AuthedRequest())

    token_hash = estimates._hash_token("created-estimate-token")
    assert result == {
        "status": "ok",
        "token": "created-estimate-token",
        "url": "https://heykevin.one/estimate/created-estimate-token",
    }
    assert len(db.sets) == 1
    assert db.sets[0]["key"] == token_hash
    assert db.sets[0]["data"]["token_hash"] == token_hash
    assert db.sets[0]["data"]["contractor_id"] == "contractor-1234567890"
    assert db.sets[0]["data"]["caller_phone"] == "+15551234567"
    assert db.sets[0]["data"]["status"] == "pending"
    assert contexts == [
        GateContext(source="estimate", actor="system", idempotency_key="CA123:estimate_token")
    ]
    assert "+15551234567" not in contexts[0].idempotency_key
    assert len(audits) == 1
    assert audits[0]["action"] == ActionKey.ESTIMATE_TOKEN_CREATE
    assert audits[0]["source"] == "estimate"
    assert audits[0]["resource_id"] == "CA123"
    assert audits[0]["decision"].allowed is True


@pytest.mark.asyncio
async def test_estimate_result_sms_fails_closed_when_sms_gate_disabled(monkeypatch):
    sent = []
    audits = []
    logs = []
    db = _set_common_estimate_fakes(monkeypatch)
    request = _UnreadableRequest()

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
        }

    async def fake_send_sms(*args, **kwargs):
        raise AssertionError("SMS must not be sent when the upload gate denies")

    async def fail_analyze_media(**_kwargs):
        raise AssertionError("analysis must not run when the upload gate denies")

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    def fake_logger_info(message, *args, **kwargs):
        logs.append((message, kwargs))

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)
    monkeypatch.setattr(estimates, "analyze_media", fail_analyze_media)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)
    monkeypatch.setattr(estimates.logger, "info", fake_logger_info)

    with pytest.raises(HTTPException) as exc_info:
        await estimates.upload_and_analyze("token", request=request)

    assert exc_info.value.status_code == 403
    assert request.stream_calls == 0
    assert sent == []
    assert db.updates == []
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert audit["contractor_id"] == "c1"
    assert audit["source"] == "estimate"
    assert audit["resource_id"] == estimates._hash_token("token")[:12]
    assert audit["decision"].allowed is False

    blocked_logs = [entry for entry in logs if entry[0] == "Estimate upload blocked by gate"]
    assert blocked_logs == [
        (
            "Estimate upload blocked by gate",
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
        idempotency_key=f"{token_hash_prefix}:result",
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
    db = _set_common_estimate_fakes(monkeypatch)
    request = _UnreadableRequest()

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "gated_actions": {ActionKey.ESTIMATE_RESULT_SMS.value: True},
            "sms_compliance_status": "pending",
        }

    async def fake_send_sms(*args, **kwargs):
        raise AssertionError("SMS must not be sent when the upload gate denies")

    async def fail_analyze_media(**_kwargs):
        raise AssertionError("analysis must not run when the upload gate denies")

    def fake_record_gate_decision(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)
    monkeypatch.setattr(estimates, "analyze_media", fail_analyze_media)
    monkeypatch.setattr(estimates, "record_gate_decision", fake_record_gate_decision, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await estimates.upload_and_analyze("token", request=request)

    assert exc_info.value.status_code == 403
    assert request.stream_calls == 0
    assert sent == []
    assert db.updates == []
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == ActionKey.ESTIMATE_RESULT_SMS
    assert audit["decision"].allowed is False
    assert audit["decision"].reason == GateReason.COMPLIANCE_NOT_APPROVED
