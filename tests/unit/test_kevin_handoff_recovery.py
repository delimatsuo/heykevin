"""A failed hand-off to Kevin retries only while the caller is provably still waiting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# Imported first because it sets the fictional settings the app modules need.
from tests.unit.test_owner_call_actions import backend, fresh  # noqa: F401, I001

from app.services import owner_call_actions as a  # noqa: E402
from app.webhooks import twilio_incoming as incoming  # noqa: E402

CONFERENCE = "direct"


@pytest.fixture
def client(backend, monkeypatch):  # noqa: F811
    import twilio.rest

    provider = MagicMock()
    monkeypatch.setattr(twilio.rest, "Client", lambda *args, **kwargs: provider)
    monkeypatch.setattr(incoming, "REDIRECT_RECOVERY_DELAY_SECONDS", 0)
    backend.record = fresh(conference_name=CONFERENCE, accepted=False)
    return provider


def _update(client):
    return client.calls.return_value.update


def _token(call) -> str:
    twiml = call.kwargs["twiml"]
    return twiml.split('name="ws_token" value="')[1].split('"')[0]


async def _hand_off():
    return await incoming._hand_off_to_kevin("CA1", "owner", CONFERENCE)


@pytest.mark.asyncio
async def test_first_success_never_checks_the_conference(backend, client):  # noqa: F811
    assert await _hand_off() is True

    _update(client).assert_called_once()
    a._conference_contains_call.assert_not_awaited()
    assert backend.record["kevin_redirect_status"] == "succeeded"


@pytest.mark.asyncio
async def test_failed_redirect_retries_when_caller_is_still_waiting(backend, client):  # noqa: F811
    _update(client).side_effect = [RuntimeError("provider down"), MagicMock()]
    a._conference_contains_call.return_value = True

    assert await _hand_off() is True

    assert _update(client).call_count == 2
    first, second = _update(client).call_args_list
    assert _token(first) != _token(second)
    assert backend.record["ws_token"] == _token(second)
    assert backend.record["kevin_redirect_status"] == "succeeded"


@pytest.mark.asyncio
async def test_no_retry_when_caller_already_left_the_ring(backend, client):  # noqa: F811
    _update(client).side_effect = RuntimeError("response lost")
    a._conference_contains_call.return_value = False

    assert await _hand_off() is False

    _update(client).assert_called_once()
    assert backend.record["kevin_redirect_status"] == "pending"


@pytest.mark.asyncio
async def test_no_retry_when_twilio_cannot_say_where_the_caller_is(backend, client):  # noqa: F811
    _update(client).side_effect = RuntimeError("response lost")
    a._conference_contains_call.side_effect = TimeoutError()

    assert await _hand_off() is False

    _update(client).assert_called_once()
    assert a._conference_contains_call.await_count == incoming.REDIRECT_RECOVERY_ATTEMPTS
    assert backend.record["kevin_redirect_status"] == "pending"


@pytest.mark.asyncio
async def test_caller_who_reaches_the_ring_late_still_gets_kevin(backend, client):  # noqa: F811
    _update(client).side_effect = [RuntimeError("provider down"), MagicMock()]
    a._conference_contains_call.side_effect = [False, True]

    assert await _hand_off() is True

    assert _update(client).call_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"accepted": True},
        {"owner_action": "accept"},
        {"state": "ended"},
        {"contractor_id": "other"},
        {"conference_name": "replaced"},
        {"kevin_redirect_status": "succeeded"},
        {"kevin_redirect_status": "uncertain"},
    ],
)
@pytest.mark.asyncio
async def test_no_retry_once_the_call_has_moved_on(backend, client, change, monkeypatch):  # noqa: F811
    redirect = AsyncMock(return_value=False)
    monkeypatch.setattr(incoming, "_async_redirect_to_kevin", redirect)
    a._conference_contains_call.return_value = True
    backend.record.update(change)

    assert await _hand_off() is False

    redirect.assert_awaited_once()
    a._conference_contains_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_cannot_override_an_owner_pickup_that_wins_the_release(backend, client):  # noqa: F811
    _update(client).side_effect = RuntimeError("provider down")
    a._conference_contains_call.return_value = True
    assert await incoming._async_redirect_to_kevin("CA1", "owner", expected_conference=CONFERENCE) is False
    pending = dict(backend.record)
    backend.retry_with = {**pending, "accepted": True}

    assert await a.release_stranded_fallback_redirect(
        "CA1", contractor_id="owner", conference_name=CONFERENCE
    ) == "stop"

    assert backend.record["accepted"] is True
    assert backend.record["kevin_redirect_nonce"] == pending["kevin_redirect_nonce"]


@pytest.mark.asyncio
async def test_ring_with_no_device_recovers_through_the_retry(backend, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        "app.services.push_notification.get_device_token", AsyncMock(return_value=None)
    )
    _update(client).side_effect = [RuntimeError("provider down"), MagicMock()]
    a._conference_contains_call.return_value = True

    await incoming._ring_contractor("CA1", "+15550100", "", CONFERENCE, "owner", "en")

    assert _update(client).call_count == 2
    assert backend.record["kevin_redirect_status"] == "succeeded"
