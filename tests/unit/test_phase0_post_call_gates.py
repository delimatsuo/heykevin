import asyncio
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services import post_call
from app.services.gated_actions import ActionKey


@pytest.mark.asyncio
async def test_business_post_call_does_not_contact_caller_or_jobber_when_gates_disabled(monkeypatch):
    sent_sms = []
    sent_mms = []
    created_jobs = []

    async def fake_extract_job_card(*_args, **_kwargs):
        return {
            "caller_phone": "+15551234567",
            "caller_name": "Pat",
            "call_type": "service_request",
            "issue_description": "leaky faucet",
        }

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

    async def fake_create_jobber_job(*args, **kwargs):
        created_jobs.append((args, kwargs))

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract_job_card)
    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr("app.db.jobs.get_job_by_call_sid", fake_get_job_by_call_sid)
    monkeypatch.setattr(post_call, "save_job", fake_save_job)
    monkeypatch.setattr(post_call, "send_sms", fake_send_sms)
    monkeypatch.setattr(post_call, "send_mms", fake_send_mms)
    monkeypatch.setattr(post_call, "_create_jobber_job", fake_create_jobber_job)
    monkeypatch.setattr(post_call, "_send_summary_push", fake_save_call)

    await post_call._process_business(
        transcript_text="Caller: I need a leaky faucet fixed",
        caller_phone="+15551234567",
        call_sid="CA123",
        contractor_phone="+15550000000",
        twilio_number="+15559999999",
        contractor={
            "contractor_id": "c1",
            "jobber_access_token": "token",
            "owner_name": "Owner",
            "business_name": "Owner Plumbing",
        },
    )
    await asyncio.sleep(0)

    caller_side_effects = [
        item for item in sent_sms + sent_mms
        if item[1].get("action") in {
            ActionKey.CALLER_CONFIRMATION_SMS,
            ActionKey.CALLER_CONFIRMATION_MMS,
            ActionKey.CALLER_VCARD_MMS,
            ActionKey.CALLER_AUTO_REPLY,
        }
    ]
    caller_contacts = [
        item for item in sent_sms + sent_mms
        if item[0] and item[0][0] == "+15551234567"
    ]
    assert caller_side_effects == []
    assert caller_contacts == []
    assert created_jobs == []
