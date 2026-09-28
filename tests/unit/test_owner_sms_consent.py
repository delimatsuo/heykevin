"""Unit tests for owner SMS consent transitions, inbound webhooks, signed callbacks, and timing."""

import os
import time
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from twilio.request_validator import RequestValidator

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+12025550199")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+12025550123")
os.environ.setdefault("CLOUD_RUN_URL", "https://test.kevinai.app")

from app.config import settings
from app.db import calls as call_db
from app.db import owner_sms as owner_sms_db
from app.db import post_call_handoffs as handoff_db
from app.services import post_call, post_call_handoff
from app.webhooks.twilio_incoming import router as twilio_router


@pytest.fixture(autouse=True)
def fail_fast_on_real_firestore(monkeypatch):
    """Ensure no test ever accidentally touches a real Firestore instance."""
    def _fail(*args, **kwargs):
        raise RuntimeError("Unstubbed Firestore access in unit test")
    monkeypatch.setattr("google.cloud.firestore.Client", _fail)
    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", _fail)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", _fail)
    monkeypatch.setattr("app.db.owner_sms.get_firestore_client", _fail)
    monkeypatch.setattr(owner_sms_db.firestore, "transactional", lambda function: function)


# Mock Firestore for consent transitions
class FakeDocumentReference:
    def __init__(self, data=None):
        self._data = dict(data) if data is not None else None
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = FakeCollectionReference()
        return self._collections[name]

    def get(self, transaction=None):
        class Snap:
            def __init__(self, d):
                self._d = d
                self.exists = d is not None

            def to_dict(self):
                return dict(self._d) if self._d is not None else None

        return Snap(self._data)

    def update(self, updates, transaction=None):
        if self._data is None:
            self._data = {}
        self._data.update(updates)

    def set(self, data, merge=False, transaction=None):
        if merge and self._data is not None:
            self._data.update(data)
        else:
            self._data = dict(data)


class FakeTransaction:
    def get(self, ref):
        return ref.get(transaction=self)

    def update(self, ref, updates):
        ref.update(updates, transaction=self)

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge, transaction=self)


class FakeCollectionReference:
    def __init__(self):
        self._docs = {}

    def document(self, doc_id):
        if doc_id not in self._docs:
            self._docs[doc_id] = FakeDocumentReference()
        return self._docs[doc_id]


class FakeFirestoreClient:
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = FakeCollectionReference()
        return self._collections[name]

    def transaction(self):
        return FakeTransaction()


@pytest.mark.asyncio
async def test_consent_transition_stop_and_start_and_deduplication(monkeypatch):
    client = FakeFirestoreClient()
    monkeypatch.setattr("app.db.owner_sms.get_firestore_client", lambda: client)

    # Setup contractor doc with resolved identities
    contractor_ref = client.collection("contractors").document("c123")
    contractor_ref.set({
        "contractor_id": "c123",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "owner_sms_enabled": False,  # app switch is independently False
        "owner_sms_opted_out": False,
        "owner_sms_opt_out_revision": 0,
    })

    # 1. Apply STOP transition
    ok, details = await owner_sms_db.apply_owner_sms_consent_transition(
        "c123",
        opt_out=True,
        source="inbound_sms",
        dedupe_key="inbound:SM11111111111111111111111111111111",
        expected_owner_phone="+12025550123",
        expected_twilio_number="+12025550199",
    )
    assert ok is True
    assert details["outcome"] == "applied"
    doc_after_stop = contractor_ref._data
    assert doc_after_stop["owner_sms_opted_out"] is True
    assert doc_after_stop["owner_sms_opt_out_revision"] == 1
    assert doc_after_stop["owner_sms_opt_out_source"] == "inbound_sms"
    # App switch must NOT be changed
    assert doc_after_stop["owner_sms_enabled"] is False

    # 2. Replaying same MessageSid should deduplicate and return True without bumping revision
    ok_dedup, details_dedup = await owner_sms_db.apply_owner_sms_consent_transition(
        "c123",
        opt_out=True,
        source="inbound_sms",
        dedupe_key="inbound:SM11111111111111111111111111111111",
        expected_owner_phone="+12025550123",
        expected_twilio_number="+12025550199",
    )
    assert ok_dedup is True
    assert details_dedup["outcome"] == "deduplicated"
    assert contractor_ref._data["owner_sms_opt_out_revision"] == 1

    # 3. Same-state STOP with a new message increments revision
    ok_same_state, details_same = await owner_sms_db.apply_owner_sms_consent_transition(
        "c123",
        opt_out=True,
        source="inbound_sms",
        dedupe_key="inbound:SM22222222222222222222222222222222",
        expected_owner_phone="+12025550123",
        expected_twilio_number="+12025550199",
    )
    assert ok_same_state is True
    assert details_same["outcome"] == "applied"
    assert contractor_ref._data["owner_sms_opt_out_revision"] == 2

    # 4. Apply START transition
    ok_start, details_start = await owner_sms_db.apply_owner_sms_consent_transition(
        "c123",
        opt_out=False,
        source="inbound_sms",
        dedupe_key="inbound:SM33333333333333333333333333333333",
        expected_owner_phone="+12025550123",
        expected_twilio_number="+12025550199",
    )
    assert ok_start is True
    assert details_start["outcome"] == "applied"
    doc_after_start = contractor_ref._data
    assert doc_after_start["owner_sms_opted_out"] is False
    assert doc_after_start["owner_sms_opt_out_revision"] == 3
    # START must NOT flip app switch back to True if it was False
    assert doc_after_start["owner_sms_enabled"] is False

    # 5. Outdated revision rejection: An old 21610 delivery failure with expected_revision=1 arrives
    ok_stale, details_stale = await owner_sms_db.apply_owner_sms_consent_transition(
        "c123",
        opt_out=True,
        source="provider_21610_async",
        dedupe_key="delivery:SM44444444444444444444444444444444",
        expected_revision=1,  # current is 3
        expected_owner_phone="+12025550123",
        expected_twilio_number="+12025550199",
    )
    assert ok_stale is True
    assert details_stale["outcome"] == "superseded"
    assert contractor_ref._data["owner_sms_opted_out"] is False
    assert contractor_ref._data["owner_sms_opt_out_revision"] == 3


@pytest.mark.asyncio
async def test_consent_transition_rejects_tenant_phone_mismatch(monkeypatch):
    client = FakeFirestoreClient()
    monkeypatch.setattr("app.db.owner_sms.get_firestore_client", lambda: client)

    contractor_ref = client.collection("contractors").document("c123")
    contractor_ref.set({
        "contractor_id": "c123",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "owner_sms_opted_out": False,
        "owner_sms_opt_out_revision": 0,
    })

    # Call with mismatched expected owner phone
    ok, details = await owner_sms_db.apply_owner_sms_consent_transition(
        "c123",
        opt_out=True,
        source="inbound_sms",
        dedupe_key="inbound:SM55555555555555555555555555555555",
        expected_owner_phone="+12025550124",
        expected_twilio_number="+12025550199",
    )
    assert ok is False
    assert details["error"] == "identity_mismatch"
    assert contractor_ref._data["owner_sms_opted_out"] is False
    assert contractor_ref._data["owner_sms_opt_out_revision"] == 0


def _build_signed_post(url: str, form_dict: dict) -> tuple[dict, str]:
    validator = RequestValidator(settings.twilio_auth_token)
    signature = validator.compute_signature(url, form_dict)
    return form_dict, signature


@pytest.mark.asyncio
async def test_inbound_sms_webhook_owner_stop_and_start(monkeypatch):
    contractor = {
        "contractor_id": "c123",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "business_name": "Test Co",
    }

    async def mock_get_by_number(phone):
        if phone in ("+12025550199", "+1 202-555-0199"):
            return contractor
        return None

    transitions = []

    async def mock_apply_transition(contractor_id, **kwargs):
        transitions.append((contractor_id, kwargs))
        return True, {"outcome": "applied"}

    inbound_records = []

    async def mock_record_inbound(cid, payload):
        inbound_records.append((cid, payload))
        return True

    async def mock_notify(cid):
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_twilio_number", mock_get_by_number)
    monkeypatch.setattr("app.db.owner_sms.apply_owner_sms_consent_transition", mock_apply_transition)
    monkeypatch.setattr("app.webhooks.twilio_incoming._record_inbound_message", mock_record_inbound)
    monkeypatch.setattr("app.webhooks.twilio_incoming._notify_owner_of_inbound_message", mock_notify)

    app = FastAPI()
    app.include_router(twilio_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test.kevinai.app") as ac:
        url = "https://test.kevinai.app/webhooks/twilio/mms-incoming"

        # 1. Owner sends "STOP" with valid signature
        form_data = {
            "To": "+12025550199",
            "From": "+12025550123",
            "Body": "STOP",
            "MessageSid": "SM11111111111111111111111111111111",
        }
        data, sig = _build_signed_post(url, form_data)
        resp_stop = await ac.post(
            "/webhooks/twilio/mms-incoming",
            data=data,
            headers={"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https"},
        )
        assert resp_stop.status_code == 200
        assert "<Response" in resp_stop.text
        assert len(transitions) == 1
        assert transitions[0][0] == "c123"
        assert transitions[0][1]["opt_out"] is True
        assert transitions[0][1]["dedupe_key"] == "inbound:SM11111111111111111111111111111111"
        assert len(inbound_records) == 0  # no generic inbound record for owner consent!

        # 2. Owner sends "START"
        form_data_start = {
            "To": "+12025550199",
            "From": "+12025550123",
            "Body": "start ",
            "MessageSid": "SM22222222222222222222222222222222",
        }
        data, sig = _build_signed_post(url, form_data_start)
        resp_start = await ac.post(
            "/webhooks/twilio/mms-incoming",
            data=data,
            headers={"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https"},
        )
        assert resp_start.status_code == 200
        assert len(transitions) == 2
        assert transitions[1][1]["opt_out"] is False
        assert transitions[1][1]["dedupe_key"] == "inbound:SM22222222222222222222222222222222"

        # 3. Non-owner caller sends "STOP" -> should NOT trigger owner consent transition
        form_data_caller = {
            "To": "+12025550199",
            "From": "+12025550124",
            "Body": "STOP",
            "MessageSid": "SM33333333333333333333333333333333",
        }
        data, sig = _build_signed_post(url, form_data_caller)
        resp_caller = await ac.post(
            "/webhooks/twilio/mms-incoming",
            data=data,
            headers={"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https"},
        )
        assert resp_caller.status_code == 200
        assert len(transitions) == 2  # unchanged
        assert len(inbound_records) == 1  # caller message recorded

        # 4. Owner sends sentence containing "stop" -> should NOT trigger consent transition
        form_data_sentence = {
            "To": "+12025550199",
            "From": "+12025550123",
            "Body": "Please stop by our job site today",
            "MessageSid": "SM44444444444444444444444444444444",
        }
        data, sig = _build_signed_post(url, form_data_sentence)
        resp_sentence = await ac.post(
            "/webhooks/twilio/mms-incoming",
            data=data,
            headers={"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https"},
        )
        assert resp_sentence.status_code == 200
        assert len(transitions) == 2

        # 5. Unknown OptOutType does not trigger START fallback
        form_data_unknown_opt = {
            "To": "+12025550199",
            "From": "+12025550123",
            "Body": "START",
            "OptOutType": "UNKNOWN_ACTION",
            "MessageSid": "SM55555555555555555555555555555555",
        }
        data, sig = _build_signed_post(url, form_data_unknown_opt)
        resp_unknown = await ac.post(
            "/webhooks/twilio/mms-incoming",
            data=data,
            headers={"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https"},
        )
        assert resp_unknown.status_code == 200
        assert len(transitions) == 2  # no transition triggered!


@pytest.mark.asyncio
async def test_owner_sms_status_callback_and_signature_security(monkeypatch):
    contractor = {
        "contractor_id": "c123",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "owner_sms_opt_out_revision": 1,
    }

    async def mock_get_contractor(cid):
        return contractor if cid == "c123" else None

    transitions = []

    async def mock_apply_transition(contractor_id, **kwargs):
        transitions.append((contractor_id, kwargs))
        return True, {"outcome": "applied"}

    monkeypatch.setattr("app.db.contractors.get_contractor", mock_get_contractor)
    monkeypatch.setattr("app.db.owner_sms.apply_owner_sms_consent_transition", mock_apply_transition)

    app = FastAPI()
    app.include_router(twilio_router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test.kevinai.app") as ac:
        url = "https://test.kevinai.app/webhooks/twilio/owner-sms-status?contractor_id=c123&revision=1"

        # 1. Valid 21610 delivery failure with valid Twilio signature
        form_data = {
            "MessageSid": "SM66666666666666666666666666666666",
            "MessageStatus": "failed",
            "ErrorCode": "21610",
            "To": "+12025550123",
            "From": "+12025550199",
        }
        data, sig = _build_signed_post(url, form_data)
        resp = await ac.post(
            "/webhooks/twilio/owner-sms-status?contractor_id=c123&revision=1",
            data=data,
            headers={"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert len(transitions) == 1
        assert transitions[0][0] == "c123"
        assert transitions[0][1]["opt_out"] is True
        assert transitions[0][1]["expected_revision"] == 1
        assert transitions[0][1]["dedupe_key"] == "delivery:SM66666666666666666666666666666666"

        # 2. Invalid signature rejected with 403
        resp_bad_sig = await ac.post(
            "/webhooks/twilio/owner-sms-status?contractor_id=c123&revision=1",
            data=data,
            headers={"X-Twilio-Signature": "invalidsig", "X-Forwarded-Proto": "https"},
        )
        assert resp_bad_sig.status_code == 403

        # 3. Non-21610 error code does not apply opt-out
        form_other = {
            "MessageSid": "SM77777777777777777777777777777777",
            "MessageStatus": "failed",
            "ErrorCode": "30008",
            "To": "+12025550123",
            "From": "+12025550199",
        }
        data_other, sig_other = _build_signed_post(url, form_other)
        resp_other = await ac.post(
            "/webhooks/twilio/owner-sms-status?contractor_id=c123&revision=1",
            data=data_other,
            headers={"X-Twilio-Signature": sig_other, "X-Forwarded-Proto": "https"},
        )
        assert resp_other.status_code == 200
        assert len(transitions) == 1  # unchanged


@pytest.mark.asyncio
async def test_post_call_timing_and_gating(monkeypatch):
    # 1. Nonterminal call status defers without claim or send
    async def mock_get_handoff(call_sid):
        return {
            "call_sid": call_sid,
            "status": "pending",
            "contractor_id": "c123",
            "created_at": time.time(),
        }

    async def mock_get_call_nonterminal(call_sid):
        return {
            "call_sid": call_sid,
            "call_status": "in-progress",  # Nonterminal!
            "contractor_id": "c123",
        }

    claims = []

    async def mock_claim(call_sid):
        claims.append(call_sid)
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", mock_get_handoff)
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", mock_get_call_nonterminal)
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", mock_claim)

    status = await post_call_handoff.run_post_call_handoff(
        "CA_nonterminal",
        transcript_lines=["Caller: hi"],
    )
    assert status == "pending"
    assert len(claims) == 0  # Not claimed

    # 2. Conflicting contractor IDs -> quarantines with contractor_mismatch
    async def mock_get_call_mismatch(call_sid):
        return {
            "call_sid": call_sid,
            "call_status": "completed",
            "contractor_id": "c_DIFFERENT",
        }

    quarantines = []

    async def mock_quarantine(call_sid, code, **kwargs):
        quarantines.append((call_sid, code))
        return True

    async def mock_save_call(call_sid, updates):
        return True

    monkeypatch.setattr(post_call_handoff.call_db, "get_call", mock_get_call_mismatch)
    monkeypatch.setattr(
        post_call_handoff.handoff_db,
        "quarantine_pending_handoff",
        mock_quarantine,
    )
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", mock_save_call)

    status_mismatch = await post_call_handoff.run_post_call_handoff(
        "CA_mismatch",
        transcript_lines=["Caller: hi"],
    )
    assert status_mismatch == "needs_attention"
    assert len(quarantines) == 1
    assert quarantines[0] == ("CA_mismatch", "contractor_mismatch")

    # 3. 24h old nonterminal call -> quarantines with call_end_unconfirmed
    async def mock_get_handoff_stale(call_sid):
        return {
            "call_sid": call_sid,
            "status": "pending",
            "contractor_id": "c123",
            "created_at": time.time() - 90000,  # >24h old
        }

    monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", mock_get_handoff_stale)
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", mock_get_call_nonterminal)

    quarantines.clear()
    status_stale = await post_call_handoff.run_post_call_handoff(
        "CA_stale",
        transcript_lines=["Caller: hi"],
    )
    assert status_stale == "needs_attention"
    assert len(quarantines) == 1
    assert quarantines[0] == ("CA_stale", "call_end_unconfirmed")


@pytest.mark.asyncio
async def test_new_same_state_start_fences_old_failure_and_old_start_cannot_replay(monkeypatch):
    client = FakeFirestoreClient()
    monkeypatch.setattr(owner_sms_db, "get_firestore_client", lambda: client)
    ref = client.collection("contractors").document("c1")
    ref.set({"owner_phone": "+12025550123", "twilio_number": "+12025550199", "owner_sms_opted_out": False})
    async def apply(opt_out, digit, **kwargs):
        return await owner_sms_db.apply_owner_sms_consent_transition(
            "c1", opt_out=opt_out, source="test", dedupe_key="inbound:SM" + digit * 32,
            expected_owner_phone="+12025550123", expected_twilio_number="+12025550199", **kwargs,
        )
    assert (await apply(False, "1"))[1]["revision"] == 1
    assert (await apply(True, "2", expected_revision=0))[1]["outcome"] == "superseded"
    assert ref._data["owner_sms_opted_out"] is False
    assert (await apply(True, "3"))[1]["revision"] == 2
    assert (await apply(False, "1"))[1]["outcome"] == "deduplicated"
    assert ref._data["owner_sms_opted_out"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path,form", [
    ("/webhooks/twilio/mms-incoming", {"To": "+12025550199", "From": "+12025550123", "Body": "STOP", "MessageSid": "SM" + "a" * 32}),
    ("/webhooks/twilio/owner-sms-status?contractor_id=c1&revision=0", {"To": "+12025550123", "From": "+12025550199", "MessageStatus": "failed", "ErrorCode": "21610", "MessageSid": "SM" + "a" * 32}),
    ("/webhooks/twilio/status", {"CallSid": "CA_test", "CallStatus": "completed"}),
])
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_persistence_returns_real_http_500(monkeypatch, path, form, raises):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    account = {"contractor_id": "c1", "owner_phone": "+12025550123", "twilio_number": "+12025550199"}
    monkeypatch.setattr("app.db.contractors.get_contractor_by_twilio_number", AsyncMock(return_value=account))
    transition = AsyncMock(side_effect=RuntimeError("unavailable")) if raises else AsyncMock(return_value=(False, {"error": "test"}))
    save = AsyncMock(side_effect=RuntimeError("unavailable")) if raises else AsyncMock(return_value=False)
    monkeypatch.setattr(owner_sms_db, "apply_owner_sms_consent_transition", transition)
    monkeypatch.setattr(call_db, "save_call", save)
    cleanup = Mock()
    monkeypatch.setattr("app.db.cache._init_firebase", lambda: None)
    monkeypatch.setattr("firebase_admin.db.reference", lambda path: SimpleNamespace(delete=cleanup))
    app = FastAPI()
    app.include_router(twilio_router)
    url = "https://test.kevinai.app" + path
    _, sig = _build_signed_post(url, form)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test.kevinai.app") as client:
        response = await client.post(path, data=form, headers={"X-Twilio-Signature": sig})
    assert response.status_code == 500
    if path == "/webhooks/twilio/status":
        cleanup.assert_called_once()
    else:
        cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_status_callback_query_tampering_and_missing_signature_rejected(monkeypatch):
    from unittest.mock import AsyncMock
    transition = AsyncMock()
    monkeypatch.setattr(owner_sms_db, "apply_owner_sms_consent_transition", transition)
    app = FastAPI()
    app.include_router(twilio_router)
    path = "/webhooks/twilio/owner-sms-status?contractor_id=c1&revision=0"
    form = {"To": "+12025550123", "From": "+12025550199", "MessageStatus": "failed", "ErrorCode": "21610", "MessageSid": "SM" + "a" * 32}
    _, sig = _build_signed_post("https://test.kevinai.app" + path, form)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test.kevinai.app") as client:
        assert (await client.post(path.replace("c1", "other"), data=form, headers={"X-Twilio-Signature": sig})).status_code == 403
        assert (await client.post(path, data=form)).status_code == 403
    transition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,call,expected", [
    ("in_progress", None, False), ("completed", None, False),
    ("pending", {"call_status": "completed", "contractor_id": "c1"}, False),
    ("pending", {"call_status": "in-progress", "contractor_id": "c1"}, True),
])
@pytest.mark.parametrize("created", [float("nan"), float("inf"), float("-inf"), 1.0])
async def test_quarantine_rechecks_pending_and_terminal_state_in_transaction(monkeypatch, status, call, expected, created):
    client = FakeFirestoreClient()
    monkeypatch.setattr(handoff_db, "get_firestore_client", lambda: client)
    ref = client.collection("post_call_handoffs").document("CA_test")
    ref.set({"status": status, "contractor_id": "c1", "created_at": created})
    if call is not None:
        client.collection("calls").document("CA_test").set(call)
    assert await handoff_db.quarantine_pending_handoff("CA_test", "call_end_unconfirmed") is expected
    assert ref._data["status"] == ("needs_attention" if expected else status)


@pytest.mark.asyncio
async def test_pending_scan_rotates_past_more_active_calls_than_limit(monkeypatch):
    ids = ["CA_1", "CA_2", "CA_3", "CA_4", "CA_5"]
    visited = []
    scans = []
    async def list_ids(status, *, limit, start_after=None):
        scans.append((status, limit, start_after))
        return [sid for sid in ids if start_after is None or sid > start_after][:limit] if status == "pending" else []
    async def process(sid):
        visited.append(sid)
        return "completed" if sid == "CA_5" else "pending"
    monkeypatch.setattr(post_call_handoff, "_pending_cursor", None)
    monkeypatch.setattr(handoff_db, "list_handoff_ids", list_ids)
    monkeypatch.setattr(post_call_handoff, "run_post_call_handoff", process)
    for _ in range(3):
        await post_call_handoff.run_pending_post_calls_once(limit=2)
    assert "CA_5" in visited
    assert len(visited) == 6
    assert all(limit <= 2 for status, limit, cursor in scans if status == "pending")


@pytest.mark.asyncio
async def test_voicemail_recap_respects_owner_opt_out(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from app.services import owner_sms
    account = {"contractor_id": "c1", "owner_phone": "+12025550123", "twilio_number": "+12025550199", "owner_sms_enabled": False}
    monkeypatch.setattr("app.db.contractors.get_contractor_by_twilio_number", AsyncMock(return_value=account))
    read = AsyncMock(return_value=account)
    monkeypatch.setattr("app.db.contractors.get_contractor", read)
    provider = Mock(side_effect=AssertionError("Voicemail opt-out must not reach provider"))
    monkeypatch.setattr(owner_sms, "Client", provider)
    app = FastAPI()
    app.include_router(twilio_router)
    path = "/webhooks/twilio/voicemail-transcription"
    form = {"To": "+12025550199", "From": "+12025550124", "TranscriptionText": "Please call back"}
    _, sig = _build_signed_post("https://test.kevinai.app" + path, form)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test.kevinai.app") as client:
        assert (await client.post(path, data=form, headers={"X-Twilio-Signature": sig})).status_code == 200
    read.assert_awaited_once_with("c1")
    provider.assert_not_called()
