"""Owner Confirm: tap in Recents writes Google Calendar and marks the request confirmed.

Auto-book stays off. This path is an explicit owner tap, so it must succeed
without `gated_actions.google_create_event`.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest
from fastapi import HTTPException

from app.api import calls as calls_api
from app.services.appointment_confirm import AppointmentConfirmError, confirm_appointment
from app.services.gated_actions import ActionKey, GATE_POLICIES


CALL_SID = "CAconfirm1"
START_TIME = "2026-08-11T10:00:00-04:00"
END_TIME = "2026-08-11T11:00:00-04:00"

PENDING_REQUEST = {
    "title": "Faucet repair - John Smith",
    "start_time": START_TIME,
    "end_time": END_TIME,
    "description": "Kitchen sink leaking",
    "status": "pending_owner_confirmation",
}


def _contractor(**overrides):
    data = {
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
    }
    data.update(overrides)
    return data


def _call(**overrides):
    data = {
        "call_sid": CALL_SID,
        "contractor_id": "c1",
        "appointment_request": dict(PENDING_REQUEST),
    }
    data.update(overrides)
    return data


class _State:
    def __init__(self, contractor_id="c1", is_admin=False):
        self.contractor_id = contractor_id
        self.is_admin = is_admin


class _Request:
    def __init__(self, contractor_id="c1", is_admin=False):
        self.state = _State(contractor_id=contractor_id, is_admin=is_admin)


@pytest.fixture
def booked_events(monkeypatch):
    created = []

    async def fake_book_appointment(*args, **kwargs):
        created.append((args, kwargs))
        return "event-1"

    monkeypatch.setattr(
        "app.services.appointment_confirm.book_appointment", fake_book_appointment
    )
    return created


@pytest.fixture
def saved_calls(monkeypatch):
    saved = []

    async def fake_save_call(call_sid, data):
        saved.append((call_sid, data))
        return True

    monkeypatch.setattr("app.services.appointment_confirm.save_call", fake_save_call)
    return saved


def test_owner_confirm_action_does_not_reuse_auto_book_gate():
    policy = GATE_POLICIES[ActionKey.OWNER_CONFIRM_CALENDAR_EVENT]
    auto_book = GATE_POLICIES[ActionKey.GOOGLE_CREATE_EVENT]

    assert ActionKey.OWNER_CONFIRM_CALENDAR_EVENT.value == "owner_confirm_calendar_event"
    assert policy.requires_flag is False
    assert policy.requires_integration_approval is False
    assert policy.requires_owner_confirmation is True
    assert policy.requires_idempotency is True
    assert auto_book.requires_flag is True
    assert auto_book.requires_integration_approval is True


@pytest.mark.asyncio
async def test_pending_request_owner_tap_saves_confirmed(booked_events, saved_calls):
    result = await confirm_appointment(
        contractor=_contractor(),
        call=_call(),
        call_sid=CALL_SID,
    )

    assert result["status"] == "confirmed"
    assert result["booked"] is True
    assert result["event_id"] == "event-1"
    assert len(booked_events) == 1
    _args, kwargs = booked_events[0]
    assert kwargs["call_sid"] == CALL_SID
    assert saved_calls[0][0] == CALL_SID
    saved_request = saved_calls[0][1]["appointment_request"]
    assert saved_request["status"] == "confirmed"
    assert saved_request["event_id"] == "event-1"
    assert saved_request["start_time"] == START_TIME
    assert isinstance(saved_request["confirmed_at"], int)


@pytest.mark.asyncio
async def test_auto_book_flag_off_still_succeeds(booked_events, saved_calls):
    contractor = _contractor()
    assert "gated_actions" not in contractor

    result = await confirm_appointment(
        contractor=contractor,
        call=_call(),
        call_sid=CALL_SID,
    )

    assert result["status"] == "confirmed"
    assert result["booked"] is True
    assert len(booked_events) == 1


@pytest.mark.asyncio
async def test_missing_appointment_request_is_not_found(booked_events, saved_calls):
    with pytest.raises(AppointmentConfirmError) as exc:
        await confirm_appointment(
            contractor=_contractor(),
            call=_call(appointment_request=None),
            call_sid=CALL_SID,
        )

    assert exc.value.status_code == 404
    assert booked_events == []
    assert saved_calls == []


@pytest.mark.asyncio
async def test_calendar_failure_leaves_status_pending(monkeypatch, saved_calls):
    async def fake_book_appointment(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.services.appointment_confirm.book_appointment", fake_book_appointment
    )

    with pytest.raises(AppointmentConfirmError) as exc:
        await confirm_appointment(
            contractor=_contractor(),
            call=_call(),
            call_sid=CALL_SID,
        )

    assert exc.value.status_code == 502
    assert saved_calls == []


@pytest.mark.asyncio
async def test_second_confirm_is_idempotent_when_event_id_stored(booked_events, saved_calls):
    call = _call(
        appointment_request={
            **PENDING_REQUEST,
            "status": "confirmed",
            "event_id": "event-1",
            "confirmed_at": 1_700_000_000,
        }
    )

    result = await confirm_appointment(
        contractor=_contractor(),
        call=call,
        call_sid=CALL_SID,
    )

    assert result["status"] == "already_confirmed"
    assert result["booked"] is True
    assert result["event_id"] == "event-1"
    assert booked_events == []
    assert saved_calls == []


@pytest.mark.asyncio
async def test_wrong_contractor_is_denied(monkeypatch):
    async def fake_get_call(call_sid):
        return _call()

    monkeypatch.setattr(calls_api, "get_call", fake_get_call)

    with pytest.raises(HTTPException) as exc:
        await calls_api.api_confirm_appointment(CALL_SID, _Request(contractor_id="other"))

    assert exc.value.status_code == 403
