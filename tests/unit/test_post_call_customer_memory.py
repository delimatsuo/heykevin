import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services import post_call


def test_name_confirmation_requires_tokens_in_caller_speech():
    assert post_call._caller_spoke_name(
        "Kevin: What's your name?\nCaller: This is Jonathan Smith.",
        "Jonathan Smith",
    )
    assert not post_call._caller_spoke_name(
        "Kevin: Are you Jonathan Smith?\nCaller: No, wrong person.",
        "Jonathan Smith",
    )
    assert not post_call._caller_spoke_name(
        "Caller: This is Jonathan Smithson.",
        "Jonathan Smith",
    )


@pytest.mark.asyncio
async def test_post_call_writes_confirmed_product_memory(monkeypatch):
    seen = []

    class FakeRepository:
        async def lookup(self, *_args):
            return None

        async def remember(self, contractor_id, caller_phone, **kwargs):
            seen.append((contractor_id, caller_phone, kwargs))

    monkeypatch.setattr(
        "app.db.customer_memory.FirestoreCustomerMemoryRepository",
        FakeRepository,
    )

    saved = await post_call._update_customer_memory(
        {"caller_phone": "+16175550123", "caller_name": "Jonathan Smith"},
        contractor_id="contractor-1",
        call_sid="CA-name",
        transcript_text="Kevin: May I have your name?\nCaller: Jonathan Smith.",
        caller_language="en",
        capture_enabled=True,
    )

    assert saved is True
    assert seen[0][0:2] == ("contractor-1", "+16175550123")
    assert seen[0][2]["expected_revision"] == 0
    assert seen[0][2]["command_id"] == "CA-name:caller_name"


@pytest.mark.asyncio
async def test_post_call_does_not_confirm_a_model_only_name(monkeypatch):
    class FailRepository:
        def __init__(self):
            raise AssertionError("memory repository must not be opened")

    monkeypatch.setattr(
        "app.db.customer_memory.FirestoreCustomerMemoryRepository",
        FailRepository,
    )

    assert (
        await post_call._update_customer_memory(
            {"caller_phone": "+16175550123", "caller_name": "Jonathan Smith"},
            contractor_id="contractor-1",
            call_sid="CA-name",
            transcript_text="Kevin: Hello Jonathan Smith.\nCaller: I need a repair.",
            capture_enabled=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_post_call_capture_is_closed_when_flag_is_absent(monkeypatch):
    class FailRepository:
        def __init__(self):
            raise AssertionError("memory repository must not be opened")

    monkeypatch.setattr(
        "app.db.customer_memory.FirestoreCustomerMemoryRepository",
        FailRepository,
    )

    assert (
        await post_call._update_customer_memory(
            {"caller_phone": "+16175550123", "caller_name": "Jonathan Smith"},
            contractor_id="contractor-1",
            call_sid="CA-name",
            transcript_text="Caller: This is Jonathan Smith.",
        )
        is None
    )
