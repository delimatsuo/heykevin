"""RTDB transactions on records that have gone, run through firebase-admin's real Reference.

firebase-admin's Reference.transaction writes the callback's result with
set_if_unchanged, which refuses None. Callbacks here return the current value to
mean "no change", which is None once the call-status webhook has deleted the
record, so every such transaction raised ValueError on an ended call.
"""

import logging
import os
import time

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest
from fastapi import HTTPException
from firebase_admin import db as rtdb

from app.db import cache
from app.services import legacy_call_commands, owner_call_actions
from app.services.state_machine import CallState
from tests.fixtures.fake_rtdb import FakeRtdbClient, use_fake_rtdb


def _live_call(**changes):
    return {"contractor_id": "owner", "state": "screening", "state_updated_at": time.time(),
            "ws_token": "ws1", **changes}


def _ref(fake, sid="CA1"):
    return rtdb.Reference(path=f"/{fake.root}/{sid}", client=fake)


def test_unchanged_result_on_a_missing_record_writes_nothing():
    fake = FakeRtdbClient({})

    assert cache.run_rtdb_transaction(_ref(fake), lambda current: current) is None
    assert fake.records == {}


def test_firebase_transaction_alone_refuses_that_result():
    """The bug: without the helper, the same callback raises on an ended call."""
    fake = FakeRtdbClient({})
    with pytest.raises(ValueError, match="none"):
        _ref(fake).transaction(lambda current: current)


def test_transaction_retries_on_a_concurrent_write_and_commits():
    fake = FakeRtdbClient({"CA1": _live_call()})
    fake.before_write = lambda records: records["CA1"].update(urgent=True)
    seen = []

    def mark(current):
        seen.append(dict(current))
        return {**current, "owner_action_status": "accepting"}

    committed = cache.run_rtdb_transaction(_ref(fake), mark)

    assert len(seen) == 2, "the 412 re-runs the callback on the fresh value"
    assert committed == fake.records["CA1"]
    assert fake.records["CA1"]["urgent"] is True
    assert fake.records["CA1"]["owner_action_status"] == "accepting"


def test_none_for_an_existing_record_is_refused_not_deleted():
    fake = FakeRtdbClient({"CA1": _live_call()})

    with pytest.raises(ValueError, match="delete_if_unchanged"):
        cache.run_rtdb_transaction(_ref(fake), lambda current: None)
    assert "CA1" in fake.records


@pytest.mark.asyncio
@pytest.mark.parametrize("hangup", ["before_read", "before_write"])
@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_owner_action_on_a_call_that_just_ended_is_unavailable_not_a_500(monkeypatch, hangup, action):
    fakes = use_fake_rtdb(monkeypatch, active_calls={"CA1": _live_call()})
    # The caller hangs up before the claim reads the record, or between its read and
    # write: the 412 retry then sees the record gone.
    setattr(fakes["active_calls"], hangup, lambda records: records.pop("CA1"))

    with pytest.raises(HTTPException) as raised:
        await owner_call_actions.handle_owner_call_action(
            call_sid="CA1", contractor_id="owner", action=action, operation_id="op1")

    assert raised.value.status_code == 404
    assert raised.value.detail == "Call unavailable"
    assert fakes["active_calls"].records == {}


@pytest.mark.asyncio
async def test_state_transition_on_an_ended_call_returns_none_without_an_error_log(monkeypatch, caplog):
    use_fake_rtdb(monkeypatch, active_calls={})

    with caplog.at_level(logging.WARNING):
        assert await cache.transition_state("CA1", CallState.SCREENING) is None

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_state_transition_is_not_reported_when_the_call_ends_mid_transaction(monkeypatch):
    fakes = use_fake_rtdb(monkeypatch, active_calls={"CA1": _live_call(state="pending", call_sid="CA1")})
    # The first attempt prepares the transition; the hangup makes its write fail (412)
    # and the retry sees the record gone.
    fakes["active_calls"].before_write = lambda records: records.pop("CA1")

    assert await cache.transition_state("CA1", CallState.SCORING) is None
    assert fakes["active_calls"].records == {}


@pytest.mark.asyncio
async def test_state_transition_still_commits_on_a_live_call(monkeypatch):
    fakes = use_fake_rtdb(monkeypatch, active_calls={"CA1": _live_call(state="pending", call_sid="CA1")})

    moved = await cache.transition_state("CA1", CallState.SCORING)

    assert moved is not None and moved.state == CallState.SCORING
    assert fakes["active_calls"].records["CA1"]["state"] == "scoring"


@pytest.mark.asyncio
async def test_legacy_command_cleanup_deletes_an_unchanged_command(monkeypatch):
    command = {"type": "take_message", "operation_id": "op1"}
    fakes = use_fake_rtdb(monkeypatch, call_commands={"CA1": command})

    assert await legacy_call_commands.delete_legacy_command_conditional("CA1", command) is None
    assert fakes["call_commands"].records == {}


@pytest.mark.asyncio
async def test_legacy_command_cleanup_keeps_a_replacement_written_mid_delete(monkeypatch):
    command = {"type": "take_message", "operation_id": "op1"}
    replacement = {"type": "take_message", "operation_id": "op2"}
    fakes = use_fake_rtdb(monkeypatch, call_commands={"CA1": command})
    fakes["call_commands"].before_write = lambda records: records.update(CA1=dict(replacement))

    assert await legacy_call_commands.delete_legacy_command_conditional("CA1", command) == replacement
    assert fakes["call_commands"].records == {"CA1": replacement}


@pytest.mark.asyncio
async def test_legacy_command_cleanup_without_an_etag_keeps_the_command_and_stops(monkeypatch):
    command = {"type": "take_message", "operation_id": "op1"}
    fakes = use_fake_rtdb(monkeypatch, call_commands={"CA1": command})
    fakes["call_commands"].send_etag = False

    assert await legacy_call_commands.delete_legacy_command_conditional("CA1", command) == command
    assert fakes["call_commands"].records == {"CA1": command}
    assert fakes["call_commands"].record_reads == ["CA1"], "no point retrying without an ETag"


@pytest.mark.asyncio
async def test_legacy_command_cleanup_on_a_missing_command_is_a_no_op(monkeypatch):
    fakes = use_fake_rtdb(monkeypatch, call_commands={})

    assert await legacy_call_commands.delete_legacy_command_conditional("CA1", {"type": "take_message"}) is None
    assert fakes["call_commands"].records == {}
