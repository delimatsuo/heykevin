"""Unit tests for call lifecycle state machine."""

from app.services.state_machine import CallState, can_transition, is_active, ActiveCall


def test_valid_transitions():
    assert can_transition(CallState.PENDING, CallState.SCORING)
    assert can_transition(CallState.SCORING, CallState.SCREENING)
    assert can_transition(CallState.SCREENING, CallState.PICKUP_RINGING)
    assert can_transition(CallState.SCREENING, CallState.VOICEMAIL_RECORDING)
    assert can_transition(CallState.SCREENING, CallState.IGNORED)
    assert can_transition(CallState.PICKUP_RINGING, CallState.CONNECTED)
    assert can_transition(CallState.CONNECTED, CallState.ENDED)


def test_invalid_transitions():
    assert not can_transition(CallState.ENDED, CallState.SCREENING)
    assert not can_transition(CallState.PENDING, CallState.CONNECTED)
    assert not can_transition(CallState.IGNORED, CallState.PICKUP_RINGING)
    assert not can_transition(CallState.SPAM_BLOCK, CallState.SCREENING) if hasattr(CallState, "SPAM_BLOCK") else True


def test_caller_hangup_from_any_active():
    assert can_transition(CallState.SCREENING, CallState.CALLER_HANGUP)
    assert can_transition(CallState.PICKUP_RINGING, CallState.CALLER_HANGUP)
    assert can_transition(CallState.VOICEMAIL_RECORDING, CallState.CALLER_HANGUP)


def test_error_forwarded_from_any_active():
    assert can_transition(CallState.PENDING, CallState.ERROR_FORWARDED)
    assert can_transition(CallState.SCORING, CallState.ERROR_FORWARDED)
    assert can_transition(CallState.SCREENING, CallState.ERROR_FORWARDED)


def test_pickup_revert():
    """If user doesn't answer, revert from PICKUP_RINGING to SCREENING."""
    assert can_transition(CallState.PICKUP_RINGING, CallState.SCREENING)


def test_text_replied_can_continue():
    """After text reply, can continue screening or end."""
    assert can_transition(CallState.TEXT_REPLIED, CallState.SCREENING)
    assert can_transition(CallState.TEXT_REPLIED, CallState.ENDED)


def test_is_active():
    assert is_active(CallState.SCREENING)
    assert is_active(CallState.PICKUP_RINGING)
    assert is_active(CallState.CONNECTED)
    assert not is_active(CallState.ENDED)
    assert not is_active(CallState.CALLER_HANGUP)
    assert not is_active(CallState.IGNORED)


def test_active_call_serialization():
    call = ActiveCall(
        call_sid="CA123",
        caller_phone="+15551234567",
        state=CallState.SCREENING,
        conference_name="call_CA123",
    )
    data = call.to_dict()
    assert data["state"] == "screening"
    assert data["call_sid"] == "CA123"

    restored = ActiveCall.from_dict(data)
    assert restored.state == CallState.SCREENING
    assert restored.call_sid == "CA123"
