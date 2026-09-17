"""Tests for owner post-call summary SMS identity and branding.

Verifies:
- Explicit 'Hey Kevin: Call summary' identity header on personal and business owner SMS.
- Neutral 'Call from' and 'CALL' header labels instead of 'Missed call' / 'MISSED CALL'.
- Translation prompt excludes the identity header; English header prepended after translation.
- Exception and whitespace-only translation fallbacks retain original body with identity header.
- Business formatting preserves all metadata without MISSED CALL fallback.
- Caller-facing confirmations retain contractor branding without owner identity headers.
"""

import os
import sys

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services import job_card, post_call

CALLER_PHONE = "+15551234567"
CONTRACTOR_PHONE = "+15550000000"
TWILIO_NUMBER = "+15559999999"
EXPECTED_HEADER = "Hey Kevin: Call summary"


class _FakeContentBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeMessagesResponse:
    def __init__(self, text: str):
        self.content = [_FakeContentBlock(text)]


class _FakeAnthropicMessages:
    def __init__(self, state: dict):
        self._state = state

    async def create(self, **kwargs):
        self._state["prompts"].append(kwargs)
        if self._state.get("exception"):
            raise self._state["exception"]
        return _FakeMessagesResponse(self._state.get("response_text", ""))


class _FakeAsyncAnthropic:
    def __init__(self, api_key: str = "", **_kwargs):
        self.api_key = api_key

    @property
    def messages(self):
        return _FakeAnthropicMessages(_anthropic_state)


_anthropic_state = {
    "prompts": [],
    "response_text": "",
    "exception": None,
}


@pytest.fixture(autouse=True)
def _patch_anthropic(monkeypatch):
    _anthropic_state["prompts"] = []
    _anthropic_state["response_text"] = ""
    _anthropic_state["exception"] = None

    fake_module = type(sys)("anthropic")
    fake_module.AsyncAnthropic = _FakeAsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def _base_contractor(**overrides):
    data = {
        "contractor_id": "c1",
        "owner_name": "Pat Owner",
        "business_name": "Pat's Plumbing",
        "user_language": "en",
        "effective_mode": "business",
        "sms_compliance_status": "approved",
        "gated_actions": {},
        "automation_approvals": {},
    }
    data.update(overrides)
    return data


@pytest.fixture
def mock_post_call_io(monkeypatch):
    sent_sms = []
    sent_mms = []
    saved_calls = []
    saved_jobs = []

    async def fake_get_call(*_args, **_kwargs):
        return {}

    async def fake_save_call(call_sid, updates):
        saved_calls.append((call_sid, updates))
        return True

    async def fake_get_job_by_call_sid(_sid):
        return None

    async def fake_save_job(data):
        saved_jobs.append(data)
        return "job-123456"

    async def fake_send_sms(*args, **kwargs):
        sent_sms.append((args, kwargs))
        return True

    async def fake_send_mms(*args, **kwargs):
        sent_mms.append((args, kwargs))
        return True

    async def fake_summary_push(*_args, **_kwargs):
        return True

    async def fake_update_contact(*_args, **_kwargs):
        return True

    async def fake_update_memory(*_args, **_kwargs):
        return True

    async def fake_validate_job_address(*_args, **_kwargs):
        return None

    def fake_get_vcard_url(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(post_call.call_db, "get_call", fake_get_call)
    monkeypatch.setattr(post_call.call_db, "save_call", fake_save_call)
    monkeypatch.setattr(post_call.job_db, "get_job_by_call_sid", fake_get_job_by_call_sid)
    monkeypatch.setattr(post_call.job_db, "save_job", fake_save_job)
    monkeypatch.setattr(post_call, "send_sms", fake_send_sms)
    monkeypatch.setattr(post_call, "send_mms", fake_send_mms)
    monkeypatch.setattr(post_call, "_send_summary_push", fake_summary_push)
    monkeypatch.setattr(post_call, "_update_caller_contact", fake_update_contact)
    monkeypatch.setattr(post_call, "_update_customer_memory", fake_update_memory)
    monkeypatch.setattr(post_call, "_validate_job_address", fake_validate_job_address)
    monkeypatch.setattr(post_call, "_get_vcard_url", fake_get_vcard_url)

    return {
        "sent_sms": sent_sms,
        "sent_mms": sent_mms,
        "saved_calls": saved_calls,
        "saved_jobs": saved_jobs,
    }


@pytest.mark.asyncio
async def test_personal_process_post_call_owner_sms_identity_and_formatting(
    monkeypatch, mock_post_call_io
):
    """Personal mode: owner SMS starts with exact identity, uses 'Call from', and records owner_sms."""
    job_data = {
        "caller_name": "Pat Smith",
        "caller_phone": CALLER_PHONE,
        "issue_description": "Water heater leaking",
        "callback_number": CALLER_PHONE,
        "call_type": "unknown",
    }

    async def fake_extract(*_args, **_kwargs):
        return dict(job_data)

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract)
    monkeypatch.setattr(job_card, "extract_job_card", fake_extract)

    contractor = _base_contractor(effective_mode="personal")

    result = await post_call.process_post_call(
        transcript_lines=["Caller: My water heater is leaking"],
        caller_phone=CALLER_PHONE,
        call_sid="CA_PERSONAL_1",
        contractor_phone=CONTRACTOR_PHONE,
        twilio_number=TWILIO_NUMBER,
        contractor=contractor,
    )

    assert result.status == "complete"
    assert "owner_sms" in result.completed_effects
    assert "call_record" in result.completed_effects

    sent = mock_post_call_io["sent_sms"]
    assert len(sent) == 1
    args, kwargs = sent[0]
    to_number, body = args[0], args[1]

    assert to_number == CONTRACTOR_PHONE
    assert kwargs.get("from_number") == TWILIO_NUMBER

    # Body must start with exact header line
    assert body.startswith(f"{EXPECTED_HEADER}\n")
    # Neutral 'Call from', not 'Missed call from'
    assert "Call from Pat Smith" in body
    assert "Missed call from" not in body
    assert "Re: Water heater leaking" in body
    assert f"\U0001f4de {CALLER_PHONE}" in body

    # Split lines and check exact structure
    lines = body.split("\n")
    assert lines[0] == EXPECTED_HEADER
    assert lines[1] == "Call from Pat Smith"
    assert lines[2] == "Re: Water heater leaking"
    assert lines[3] == f"\U0001f4de {CALLER_PHONE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["personal", "business"])
async def test_translation_excludes_identity_and_preserves_english_header(
    monkeypatch, mock_post_call_io, mode
):
    """Translation prompt must NOT include the identity header, and the captured SMS starts with English identity."""
    job_data = {
        "caller_name": "Juan Perez",
        "caller_phone": CALLER_PHONE,
        "issue_description": "Fuga de agua",
        "callback_number": CALLER_PHONE,
        "call_type": "service_request" if mode == "business" else "unknown",
        "urgency": "routine",
    }

    async def fake_extract(*_args, **_kwargs):
        return dict(job_data)

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract)
    monkeypatch.setattr(job_card, "extract_job_card", fake_extract)

    translated_body = (
        "Llamada de Juan Perez\nRe: Fuga de agua\n\U0001f4de +15551234567"
        if mode == "personal"
        else "\U0001f527 NUEVO PROSPECTO\nDe: Juan Perez\n\U0001f4de +15551234567\nRe: Fuga de agua\nUrgency: ROUTINE\n\u2192 Tap to call: tel:+15551234567"
    )
    _anthropic_state["response_text"] = translated_body

    contractor = _base_contractor(
        effective_mode=mode,
        user_language="es",
    )

    result = await post_call.process_post_call(
        transcript_lines=["Caller: Tengo una fuga de agua"],
        caller_phone=CALLER_PHONE,
        call_sid="CA_TRANS_1",
        contractor_phone=CONTRACTOR_PHONE,
        twilio_number=TWILIO_NUMBER,
        contractor=contractor,
    )

    assert result.status == "complete"
    assert "owner_sms" in result.completed_effects

    # 1. Model prompt verification: MUST NOT contain OWNER_SMS_HEADER
    assert len(_anthropic_state["prompts"]) == 1
    prompt_call = _anthropic_state["prompts"][0]
    messages = prompt_call.get("messages", [])
    assert len(messages) > 0
    prompt_text = messages[0].get("content", "")
    assert EXPECTED_HEADER not in prompt_text
    assert "Hey Kevin" not in prompt_text

    if mode == "personal":
        assert "Translate this call summary notification to language code 'es'" in prompt_text
        assert "missed call" not in prompt_text.lower()
    else:
        assert "Translate this call notification SMS to language code 'es'" in prompt_text

    # 2. Captured SMS verification: starts with exact English identity followed by translated body
    sent = mock_post_call_io["sent_sms"]
    owner_sends = [s for s in sent if s[0][0] == CONTRACTOR_PHONE]
    assert len(owner_sends) == 1
    body = owner_sends[0][0][1]

    assert body.startswith(f"{EXPECTED_HEADER}\n")
    assert body == f"{EXPECTED_HEADER}\n{translated_body}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["personal", "business"])
@pytest.mark.parametrize(
    "failure_type",
    ["exception", "whitespace_only", "empty_string"],
)
async def test_translation_fallback_preserves_body_and_identity(
    monkeypatch, mock_post_call_io, mode, failure_type
):
    """When translation fails or returns whitespace, retain original body beneath the identity header."""
    job_data = {
        "caller_name": "Pat Smith",
        "caller_phone": CALLER_PHONE,
        "issue_description": "Boiler broken",
        "callback_number": CALLER_PHONE,
        "call_type": "service_request" if mode == "business" else "unknown",
        "urgency": "none",
    }

    async def fake_extract(*_args, **_kwargs):
        return dict(job_data)

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract)
    monkeypatch.setattr(job_card, "extract_job_card", fake_extract)

    if failure_type == "exception":
        _anthropic_state["exception"] = RuntimeError("Anthropic translation API timeout")
    elif failure_type == "whitespace_only":
        _anthropic_state["response_text"] = "   \n\t  \n  "
    elif failure_type == "empty_string":
        _anthropic_state["response_text"] = ""

    contractor = _base_contractor(
        effective_mode=mode,
        user_language="fr",
    )

    result = await post_call.process_post_call(
        transcript_lines=["Caller: Boiler is broken"],
        caller_phone=CALLER_PHONE,
        call_sid="CA_FALLBACK_1",
        contractor_phone=CONTRACTOR_PHONE,
        twilio_number=TWILIO_NUMBER,
        contractor=contractor,
    )

    assert result.status == "complete"
    assert "owner_sms" in result.completed_effects

    sent = mock_post_call_io["sent_sms"]
    owner_sends = [s for s in sent if s[0][0] == CONTRACTOR_PHONE]
    assert len(owner_sends) == 1
    body = owner_sends[0][0][1]

    # Header is preserved
    assert body.startswith(f"{EXPECTED_HEADER}\n")

    # Original useful body is preserved beneath header
    if mode == "personal":
        assert "Call from Pat Smith" in body
        assert "Re: Boiler broken" in body
        assert f"\U0001f4de {CALLER_PHONE}" in body
    else:
        assert "\U0001f4de NEW LEAD" in body
        assert "From: Pat Smith" in body
        assert "Re: Boiler broken" in body
        assert f"\U0001f4de {CALLER_PHONE}" in body
        assert f"\u2192 Tap to call: tel:{CALLER_PHONE}" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_type", "expected_header_line", "extra_job_data"),
    [
        (
            "service_request",
            "\U0001f6a8 NEW LEAD",
            {
                "urgency": "emergency",
                "appointment_request": {"start_time": "2026-09-18T14:00:00Z"},
                "address": "742 Evergreen Terrace",
            },
        ),
        (
            "unknown",
            "\U0001f4de CALL",
            {
                "urgency": "none",
            },
        ),
        (
            "unrecognized_custom_type",
            "\U0001f4de CALL",
            {
                "urgency": "none",
            },
        ),
    ],
)
async def test_business_formatter_call_types_and_metadata(
    call_type, expected_header_line, extra_job_data, monkeypatch
):
    """Business formatter retains metadata, uses CALL (not MISSED CALL), and starts with exact identity."""
    job_data = {
        "caller_name": "Sam Taylor",
        "business_name": "Taylor Roofing",
        "caller_phone": CALLER_PHONE,
        "issue_description": "Roof leak inspection",
        "callback_number": "+15559876543",
        "call_type": call_type,
        "message": "Call before noon",
    }
    job_data.update(extra_job_data)

    contractor = _base_contractor()

    sms = await post_call._format_contractor_sms(
        job_data,
        job_id="job-9999",
        user_language="en",
        contractor=contractor,
    )

    lines = sms.split("\n")
    # Identity is first line
    assert lines[0] == EXPECTED_HEADER
    # Header line matches expected
    assert lines[1] == expected_header_line
    # Never has MISSED CALL
    assert "MISSED CALL" not in sms

    # Metadata checks
    assert "From: Sam Taylor (Taylor Roofing)" in sms
    assert f"\U0001f4de {CALLER_PHONE}" in sms
    assert "Re: Roof leak inspection" in sms
    assert "Message: Call before noon" in sms
    assert "Callback: +15559876543" in sms
    assert f"\u2192 Tap to call: tel:{CALLER_PHONE}" in sms

    if call_type == "service_request":
        assert "Urgency: EMERGENCY" in sms
        assert "\U0001f4c5 APPOINTMENT REQUEST:" in sms
        assert "(not confirmed)" in sms
        assert "\U0001f4cd 742 Evergreen Terrace" in sms


@pytest.mark.asyncio
async def test_business_post_call_integration_and_caller_confirmation_branding_separation(
    monkeypatch, mock_post_call_io
):
    """Integration of business formatting into process_post_call:
    Owner SMS has Hey Kevin header; caller confirmation keeps contractor branding without owner header.
    """
    job_data = {
        "caller_name": "Jordan Lee",
        "caller_phone": CALLER_PHONE,
        "issue_description": "Burst pipe in basement",
        "callback_number": CALLER_PHONE,
        "call_type": "service_request",
        "urgency": "emergency",
    }

    async def fake_extract(*_args, **_kwargs):
        return dict(job_data)

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract)
    monkeypatch.setattr(job_card, "extract_job_card", fake_extract)

    contractor = _base_contractor(
        contractor_id="c_brand_1",
        owner_name="Dave Miller",
        business_name="Miller Emergency Plumbing",
        gated_actions={"caller_confirmation_sms": True},
    )

    result = await post_call.process_post_call(
        transcript_lines=["Caller: A pipe burst in my basement"],
        caller_phone=CALLER_PHONE,
        call_sid="CA_INTEG_1",
        contractor_phone=CONTRACTOR_PHONE,
        twilio_number=TWILIO_NUMBER,
        contractor=contractor,
    )

    assert result.status == "complete"
    assert "owner_sms" in result.completed_effects
    assert "caller_confirmation" in result.completed_effects

    sent = mock_post_call_io["sent_sms"]
    assert len(sent) == 2

    # 1. Owner SMS
    owner_sms = next(s for s in sent if s[0][0] == CONTRACTOR_PHONE)
    owner_args, owner_kwargs = owner_sms
    owner_body = owner_args[1]
    assert owner_kwargs.get("from_number") == TWILIO_NUMBER
    assert owner_body.startswith(f"{EXPECTED_HEADER}\n")
    assert "\U0001f6a8 NEW LEAD" in owner_body
    assert "From: Jordan Lee" in owner_body
    assert "Re: Burst pipe in basement" in owner_body
    assert "Urgency: EMERGENCY" in owner_body
    assert f"\u2192 Tap to call: tel:{CALLER_PHONE}" in owner_body

    # 2. Caller Confirmation SMS
    caller_sms = next(s for s in sent if s[0][0] == CALLER_PHONE)
    caller_args, caller_kwargs = caller_sms
    caller_body = caller_args[1]
    assert caller_kwargs.get("from_number") == TWILIO_NUMBER
    # Contractor branding is preserved
    assert "Thanks for calling Miller Emergency Plumbing!" in caller_body
    assert "Dave Miller will get back to you shortly." in caller_body
    assert "Issue: Burst pipe in basement" in caller_body
    assert "Ref: KV-JOB-12" in caller_body
    # MUST NOT contain Hey Kevin owner header
    assert EXPECTED_HEADER not in caller_body
    assert not caller_body.startswith("Hey Kevin")
