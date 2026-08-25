import asyncio
import os
import sys

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services import post_call
from app.services.gated_actions import ActionKey


CALLER_PHONE = "+15551234567"
CONTRACTOR_PHONE = "+15550000000"
TWILIO_NUMBER = "+15559999999"


class _FakeEstimateResponse:
    status_code = 200

    def json(self):
        return {"url": "https://example.com/estimate-token"}


class _FakeEstimateClient:
    constructed = 0
    posts = []

    def __init__(self):
        type(self).constructed += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *args, **kwargs):
        type(self).posts.append((args, kwargs))
        return _FakeEstimateResponse()


def _contractor(**overrides):
    data = {
        "contractor_id": "c1",
        "owner_name": "Owner",
        "business_name": "Owner Plumbing",
        "sms_compliance_status": "approved",
        "gated_actions": {},
        "automation_approvals": {},
    }
    data.update(overrides)
    return data


def _allow(contractor, *actions):
    contractor["gated_actions"] = {
        **contractor.get("gated_actions", {}),
        **{action.value: True for action in actions},
    }
    return contractor


def _approve_automation(contractor, *actions):
    contractor["automation_approvals"] = {
        **contractor.get("automation_approvals", {}),
        **{action.value: True for action in actions},
    }
    return contractor


def _job_card(call_type, **overrides):
    data = {
        "caller_phone": CALLER_PHONE,
        "caller_name": "Pat",
        "call_type": call_type,
        "issue_description": "leaky faucet",
    }
    data.update(overrides)
    return data


async def _run_business(monkeypatch, *, job_data, contractor, vcard_url="", created_jobs=None, sent_sms=None, sent_mms=None):
    created_jobs = created_jobs if created_jobs is not None else []
    sent_sms = sent_sms if sent_sms is not None else []
    sent_mms = sent_mms if sent_mms is not None else []

    async def fake_extract_job_card(*_args, **_kwargs):
        return dict(job_data)

    async def fake_save_call(*_args, **_kwargs):
        return None

    async def fake_get_job_by_call_sid(_sid):
        return None

    async def fake_save_job(data):
        return "job-1"

    async def fake_send_sms(*args, **kwargs):
        sent_sms.append((args, kwargs))
        return True

    async def fake_send_mms(*args, **kwargs):
        sent_mms.append((args, kwargs))
        return True

    def fake_capture_jobber_lead(*args, **kwargs):
        created_jobs.append((args, kwargs))

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract_job_card)
    monkeypatch.setattr(post_call.call_db, "save_call", fake_save_call)
    monkeypatch.setattr(post_call.job_db, "get_job_by_call_sid", fake_get_job_by_call_sid)
    monkeypatch.setattr(post_call.job_db, "save_job", fake_save_job)
    monkeypatch.setattr(post_call, "send_sms", fake_send_sms)
    monkeypatch.setattr(post_call, "send_mms", fake_send_mms)
    monkeypatch.setattr(post_call, "_capture_jobber_lead", fake_capture_jobber_lead)
    monkeypatch.setattr(post_call, "_send_summary_push", fake_save_call)
    monkeypatch.setattr(post_call, "_get_vcard_url", lambda _contractor: vcard_url)
    monkeypatch.setattr(
        post_call,
        "_update_caller_contact",
        lambda *_args, **_kwargs: fake_save_call(),
    )

    await post_call._process_business(
        transcript_text="Caller: I need a leaky faucet fixed",
        caller_phone=CALLER_PHONE,
        call_sid="CA123",
        contractor_phone=CONTRACTOR_PHONE,
        twilio_number=TWILIO_NUMBER,
        contractor=contractor,
    )
    await asyncio.sleep(0)
    return sent_sms, sent_mms, created_jobs


def _caller_sends(sent):
    return [item for item in sent if item[0] and item[0][0] == CALLER_PHONE]


@pytest.mark.asyncio
async def test_disabled_service_request_confirmation_sms_does_not_contact_caller(monkeypatch):
    sent_sms = []

    await _run_business(
        monkeypatch,
        job_data=_job_card("service_request"),
        contractor=_contractor(),
        sent_sms=sent_sms,
    )

    assert _caller_sends(sent_sms) == []


@pytest.mark.asyncio
async def test_allowed_service_request_confirmation_sms_uses_caller_confirmation_sms_action(monkeypatch):
    sent_sms = []
    contractor = _allow(_contractor(), ActionKey.CALLER_CONFIRMATION_SMS)

    await _run_business(
        monkeypatch,
        job_data=_job_card("service_request"),
        contractor=contractor,
        sent_sms=sent_sms,
    )

    caller_sends = _caller_sends(sent_sms)
    assert len(caller_sends) == 1
    assert caller_sends[0][1]["action"] == ActionKey.CALLER_CONFIRMATION_SMS
    assert caller_sends[0][1]["gate_context"].idempotency_key == "CA123:caller_confirmation_sms"


@pytest.mark.asyncio
async def test_disabled_service_request_confirmation_mms_does_not_contact_caller_or_mms_side_effect(monkeypatch):
    sent_mms = []

    await _run_business(
        monkeypatch,
        job_data=_job_card("service_request"),
        contractor=_contractor(),
        vcard_url="https://example.com/card.vcf",
        sent_mms=sent_mms,
    )

    assert _caller_sends(sent_mms) == []
    assert sent_mms == []


@pytest.mark.asyncio
async def test_allowed_service_request_confirmation_mms_uses_caller_confirmation_mms_action(monkeypatch):
    sent_mms = []
    contractor = _allow(_contractor(), ActionKey.CALLER_CONFIRMATION_MMS)

    await _run_business(
        monkeypatch,
        job_data=_job_card("service_request"),
        contractor=contractor,
        vcard_url="https://example.com/card.vcf",
        sent_mms=sent_mms,
    )

    caller_sends = _caller_sends(sent_mms)
    assert len(caller_sends) == 1
    assert caller_sends[0][1]["action"] == ActionKey.CALLER_CONFIRMATION_MMS
    assert caller_sends[0][1]["gate_context"].idempotency_key == "CA123:caller_confirmation_mms"


@pytest.mark.asyncio
async def test_disabled_non_service_vcard_mms_does_not_contact_caller(monkeypatch):
    sent_mms = []

    await _run_business(
        monkeypatch,
        job_data=_job_card("business"),
        contractor=_contractor(),
        vcard_url="https://example.com/card.vcf",
        sent_mms=sent_mms,
    )

    assert _caller_sends(sent_mms) == []
    assert sent_mms == []


@pytest.mark.asyncio
async def test_allowed_non_service_vcard_mms_uses_caller_vcard_mms_action(monkeypatch):
    sent_mms = []
    contractor = _allow(_contractor(), ActionKey.CALLER_VCARD_MMS)

    await _run_business(
        monkeypatch,
        job_data=_job_card("business"),
        contractor=contractor,
        vcard_url="https://example.com/card.vcf",
        sent_mms=sent_mms,
    )

    caller_sends = _caller_sends(sent_mms)
    assert len(caller_sends) == 1
    assert caller_sends[0][1]["action"] == ActionKey.CALLER_VCARD_MMS
    assert caller_sends[0][1]["gate_context"].idempotency_key == "CA123:caller_vcard_mms"


@pytest.mark.asyncio
async def test_disabled_auto_reply_does_not_call_auto_reply_helper(monkeypatch):
    async def fail_auto_reply(*_args, **_kwargs):
        raise AssertionError("_send_auto_reply should not be called when caller_auto_reply is disabled")

    monkeypatch.setattr(post_call, "_send_auto_reply", fail_auto_reply)

    await _run_business(
        monkeypatch,
        job_data=_job_card("business"),
        contractor=_contractor(auto_reply_sms=True),
    )


@pytest.mark.asyncio
async def test_allowed_auto_reply_sends_with_caller_auto_reply_action_and_context(monkeypatch):
    sent_sms = []

    class _Doc:
        exists = False

    class _Document:
        def get(self):
            return _Doc()

        def set(self, _data):
            return None

    class _Collection:
        def document(self, _key):
            return _Document()

    class _Firestore:
        def collection(self, name):
            assert name == "auto_reply_timestamps"
            return _Collection()

    async def fake_send_sms(*args, **kwargs):
        sent_sms.append((args, kwargs))
        return True

    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", lambda: _Firestore())
    monkeypatch.setattr(post_call, "send_sms", fake_send_sms)

    contractor = _allow(_contractor(), ActionKey.CALLER_AUTO_REPLY)

    await post_call._send_auto_reply(
        CALLER_PHONE,
        contractor,
        TWILIO_NUMBER,
        transcript_text="Caller: Please call me back",
        caller_language="en",
        call_sid="CA123",
    )

    assert len(sent_sms) == 1
    assert sent_sms[0][1]["action"] == ActionKey.CALLER_AUTO_REPLY
    assert sent_sms[0][1]["gate_context"].idempotency_key == "CA123:caller_auto_reply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contractor", "expected"),
    [
        (_contractor(jobber_access_token="token", jobber_refresh_token="ref"), False),
        (_contractor(jobber_lead_capture_enabled=True), False),
        (_contractor(jobber_access_token="token", jobber_refresh_token="ref", jobber_lead_capture_enabled=False), False),
        (_contractor(jobber_access_token="token", jobber_refresh_token="ref", jobber_lead_capture_enabled=True), True),
    ],
)
async def test_jobber_lead_capture_is_awaited_only_when_feature_flag_enabled(
    monkeypatch, contractor, expected
):
    _, _, created_jobs = await _run_business(
        monkeypatch,
        job_data=_job_card("service_request"),
        contractor=contractor,
    )

    assert bool(created_jobs) is expected


@pytest.mark.asyncio
async def test_estimate_token_create_disabled_does_not_construct_http_client(monkeypatch):
    class _Httpx:
        AsyncClient = pytest.fail

    monkeypatch.setitem(sys.modules, "httpx", _Httpx)

    msg = await post_call._format_caller_sms_with_estimate(
        _job_card("service_request", call_sid="CA123"),
        "job-1",
        _contractor(services=[{"name": "Drain cleaning"}]),
        TWILIO_NUMBER,
    )

    assert "Upload a photo" not in msg


@pytest.mark.asyncio
async def test_estimate_token_create_allowed_adds_estimate_link(monkeypatch):
    class _Httpx:
        AsyncClient = _FakeEstimateClient

    _FakeEstimateClient.constructed = 0
    _FakeEstimateClient.posts = []
    monkeypatch.setitem(sys.modules, "httpx", _Httpx)
    contractor = _approve_automation(
        _allow(
            _contractor(services=[{"name": "Drain cleaning"}]),
            ActionKey.ESTIMATE_TOKEN_CREATE,
        ),
        ActionKey.ESTIMATE_TOKEN_CREATE,
    )

    msg = await post_call._format_caller_sms_with_estimate(
        _job_card("service_request", call_sid="CA123"),
        "job-1",
        contractor,
        TWILIO_NUMBER,
    )

    assert _FakeEstimateClient.constructed == 1
    assert _FakeEstimateClient.posts[0][1]["json"] == {
        "contractor_id": "c1",
        "caller_phone": CALLER_PHONE,
        "call_sid": "CA123",
    }
    assert "https://example.com/estimate-token" in msg
