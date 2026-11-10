"""Transcript ownership, bounded teardown and conditional SDK writes, without I/O."""

import asyncio
from copy import deepcopy
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

for name, value in {
    "TWILIO_ACCOUNT_SID": "test-account-sid",
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "TELEGRAM_BOT_TOKEN": "test-telegram-token",
    "USER_PHONE": "+15550000001",
}.items():
    os.environ.setdefault(name, value)

from app.db import cache
from app.webhooks import media_stream, relay_stream
from tests.fixtures.fake_rtdb import use_fake_rtdb


def _record(**changes):
    return {
        "call_sid": "CA_transcript",
        "contractor_id": "test-owner",
        "ws_token": "test-token",
        "state": "screening",
        "state_updated_at": 123.0,
        "owner_action": "take_message",
        "owner_operation_id": "test-operation",
        "transcript_buffer": "original",
        **changes,
    }


def _pusher(lines, **kwargs):
    return media_stream.LiveTranscriptPusher(
        "CA_transcript",
        lines,
        contractor_id="test-owner",
        ws_token="test-token",
        **kwargs,
    )


async def _write(text="new", **kwargs):
    return await cache.update_live_transcript(
        "CA_transcript",
        text,
        contractor_id="test-owner",
        ws_token="test-token",
        observed_at=kwargs.pop("observed_at", 200.0),
        cancelled=kwargs.pop("cancelled", threading.Event()),
        **kwargs,
    )


async def test_slow_write_coalesces_and_close_seals_the_final_snapshot(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    writes, credentials = [], []
    clock = [100.0]
    observations = []
    monkeypatch.setattr(
        media_stream, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic)
    )

    async def update(_sid, text, **kwargs):
        credentials.append((kwargs["contractor_id"], kwargs["ws_token"]))
        observations.append(kwargs["observed_at"])
        if not writes:
            entered.set()
            await release.wait()
        writes.append(text)

    monkeypatch.setattr(media_stream, "update_live_transcript", update)
    lines = ["first"]
    pusher = _pusher(lines, throttle=60)
    closing = None
    try:
        async with asyncio.timeout(2):
            pusher.push()
            await entered.wait()
            for line in ["second", "third"]:
                clock[0] += 100
                lines.append(line)
                pusher.push()
            clock[0] = 1000  # draining later must not invent newer activity
            closing = asyncio.create_task(pusher.close())
            await asyncio.sleep(0)
            assert not closing.done()
            lines.append("too late")
            pusher.push()
            release.set()
            await closing
            await pusher.close()
    finally:
        release.set()
        await pusher.close(timeout=0)
        if closing:
            await asyncio.gather(closing, return_exceptions=True)
    assert writes == ["first", "first\nsecond\nthird"]
    assert credentials == [("test-owner", "test-token")] * 2
    assert observations == [100.0, 300.0]
    assert pusher._worker.done()


async def test_close_wakes_trailing_throttle_without_waiting_for_its_window(monkeypatch):
    written = asyncio.Event()
    writes = []

    async def update(_sid, text, **_kwargs):
        writes.append(text)
        written.set()

    monkeypatch.setattr(media_stream, "update_live_transcript", update)
    lines = ["first"]
    pusher = _pusher(lines, throttle=60)
    try:
        async with asyncio.timeout(2):
            pusher.push()
            await written.wait()
            lines.append("last")
            pusher.push()
            await asyncio.sleep(0)  # the worker reaches its throttle wait
            await pusher.close(timeout=0.5)
    finally:
        await pusher.close(timeout=0)
    assert writes == ["first", "first\nlast"]
    assert pusher._worker.done() and not pusher._worker.cancelled()


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_abandoned_drain_cancels_owned_work_and_rejects_future_pushes(
    monkeypatch, cancel_close
):
    entered = asyncio.Event()
    calls = []

    async def update(_sid, text, **_kwargs):
        calls.append(text)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(media_stream, "update_live_transcript", update)
    lines = ["first"]
    pusher = _pusher(lines)
    try:
        async with asyncio.timeout(2):
            pusher.push()
            await entered.wait()
            if cancel_close:
                closing = asyncio.create_task(pusher.close(timeout=60))
                await asyncio.sleep(0)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
            else:
                await pusher.close(timeout=0)
            lines.append("too late")
            pusher.push()
            await pusher.close(timeout=0)
    finally:
        await pusher.close(timeout=0)
    assert calls == ["first"]
    assert pusher._cancelled.is_set() and pusher._worker.cancelled()


async def test_close_before_any_push_never_starts_a_writer(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(media_stream, "update_live_transcript", update)
    pusher = _pusher(["first"])
    await pusher.close()
    pusher.push()
    assert pusher._worker is None
    update.assert_not_awaited()


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize(
    "replacement",
    [
        None,
        {},
        _record(contractor_id="other"),
        _record(ws_token="rotated"),
        _record(call_sid="CA_other"),
        _record(state="ended"),
        _record(state="caller_hangup"),
        _record(state="error_forwarded"),
        _record(state="ignored"),
        _record(state="callback_initiated"),
        _record(accepted=True),
    ],
)
async def test_storage_rechecks_authority_on_read_and_transaction_retry(
    monkeypatch, replacement, race
):
    fake = use_fake_rtdb(monkeypatch, active_calls={"CA_transcript": _record()})["active_calls"]

    def replace(records):
        if replacement is None:
            records.pop("CA_transcript", None)
        else:
            records["CA_transcript"] = deepcopy(replacement)

    if race:
        fake.before_write = replace
    else:
        replace(fake.records)
    assert await _write() is False
    assert fake.records == ({} if replacement is None else {"CA_transcript": replacement})


async def test_transaction_preserves_concurrent_owner_metadata_and_timestamp(monkeypatch):
    original = _record()
    fake = use_fake_rtdb(monkeypatch, active_calls={"CA_transcript": original})["active_calls"]
    fake.before_write = lambda records: records["CA_transcript"].update(
        owner_operation_id="new-operation",
        urgent=True,
        state_updated_at=456.0,
    )
    assert await _write("") is True  # clearing text is a valid snapshot
    assert fake.records["CA_transcript"] == {
        **original,
        "transcript_buffer": "",
        "owner_operation_id": "new-operation",
        "urgent": True,
        "state_updated_at": 456.0,
    }


async def test_new_transcript_activity_keeps_long_call_owner_actions_live(monkeypatch):
    from app.services.owner_call_actions import live_record

    now = time.time()
    old = _record(state_updated_at=now - cache.STALE_THRESHOLD - 1)
    fake = use_fake_rtdb(monkeypatch, active_calls={"CA_transcript": old})["active_calls"]
    assert not live_record(old, "test-owner", now)
    assert await _write(observed_at=now)
    assert live_record(fake.records["CA_transcript"], "test-owner", now)
    assert fake.records["CA_transcript"]["state_updated_at"] == now


@pytest.mark.parametrize("observation", [None, True, 0, -1, "123", float("nan"), float("inf")])
async def test_invalid_observation_time_cannot_change_storage(monkeypatch, observation):
    fake = use_fake_rtdb(monkeypatch, active_calls={"CA_transcript": _record()})["active_calls"]
    assert not await _write(observed_at=observation)
    assert fake.records == {"CA_transcript": _record()}
    assert not fake.record_reads


async def test_cancelled_or_unbound_write_never_opens_storage(monkeypatch):
    def forbidden(_sid):
        pytest.fail("storage opened without valid local authority")

    monkeypatch.setattr(cache, "_get_ref", forbidden)
    cancelled = threading.Event()
    cancelled.set()
    assert not await _write(cancelled=cancelled)
    for owner, token in [("", "test-token"), ("test-owner", "")]:
        assert not await cache.update_live_transcript(
            "CA_transcript",
            "new",
            contractor_id=owner,
            ws_token=token,
            observed_at=200.0,
            cancelled=threading.Event(),
        )


async def test_storage_failure_reports_only_exception_type(monkeypatch, caplog):
    def failed(_sid):
        raise RuntimeError("private transcript must not be logged")

    monkeypatch.setattr(cache, "_get_ref", failed)
    assert not await _write()
    assert "RuntimeError" in caplog.text
    assert "private transcript" not in caplog.text


@pytest.mark.parametrize("change", ["unchanged", "delete", "rotate", "end", "owner-update"])
async def test_executor_completion_after_close_remains_conditionally_fenced(monkeypatch, change):
    fake = use_fake_rtdb(monkeypatch, active_calls={"CA_transcript": _record()})["active_calls"]
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = cache.run_rtdb_transaction
    observed_at = 200.0
    monkeypatch.setattr(
        media_stream, "time", SimpleNamespace(time=lambda: observed_at, monotonic=time.monotonic)
    )

    def pause_commit(_records):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2), "test did not release SDK commit"

    def observed_transaction(*args):
        try:
            return original(*args)
        finally:
            loop.call_soon_threadsafe(finished.set)

    fake.before_write = pause_commit
    monkeypatch.setattr(cache, "run_rtdb_transaction", observed_transaction)
    pusher = _pusher(["new"])
    try:
        async with asyncio.timeout(2):
            pusher.push()
            await entered.wait()
            await pusher.close(timeout=0)
            assert pusher._worker.cancelled() and not finished.is_set()
            if change == "delete":
                fake.records.clear()
            elif change == "rotate":
                fake.records["CA_transcript"]["ws_token"] = "rotated"
            elif change == "end":
                fake.records["CA_transcript"]["state"] = "ended"
            elif change == "owner-update":
                fake.records["CA_transcript"]["owner_operation_id"] = "new-operation"
            expected = deepcopy(fake.records)
            if change == "unchanged":
                # An already-sent request can still commit to unchanged authority.
                expected["CA_transcript"].update(
                    transcript_buffer="new",
                    state_updated_at=observed_at,
                )
            release.set()
            await finished.wait()
    finally:
        release.set()
        await pusher.close(timeout=0)
        await asyncio.wait_for(finished.wait(), timeout=2)
    assert fake.records == expected


@pytest.mark.parametrize("engine", ["media", "relay"])
@pytest.mark.parametrize(
    "ending",
    ["disconnect", "constructor-error", "start-error", "cancel", "stop-error", "stop-cancel"],
)
async def test_handler_always_closes_writer_after_final_pipeline_callbacks(
    monkeypatch, engine, ending
):
    module = media_stream if engine == "media" else relay_stream
    observed, writes, persisted = [], [], []
    authenticated = _record()
    monkeypatch.setattr(module, "_init_firebase", lambda: None)
    monkeypatch.setattr(
        "firebase_admin.db.reference", lambda _path: SimpleNamespace(get=lambda: authenticated)
    )
    monkeypatch.setattr(module, "_resolve_active_call", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "get_active_call", AsyncMock(return_value=None))

    class Pusher(media_stream.LiveTranscriptPusher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._throttle = 60
            observed.append(self)

    async def update(sid, text, **kwargs):
        writes.append((sid, text, kwargs["contractor_id"], kwargs["ws_token"]))
        return True

    async def save(_sid, record):
        persisted.append(record["transcript"])
        return True

    monkeypatch.setattr(module, "LiveTranscriptPusher", Pusher)
    monkeypatch.setattr(media_stream, "update_live_transcript", update)
    monkeypatch.setattr("app.db.calls.save_call", save)

    class Pipeline:
        def __init__(self, **kwargs):
            if ending == "constructor-error":
                raise RuntimeError("synthetic constructor failure")
            self.on_transcript = kwargs["on_transcript"]

        def start_background_tasks(self):
            if ending == "start-error":
                raise RuntimeError("synthetic start failure")

        async def handle_message(self, _message):
            await self.on_transcript("Caller", "first")
            if ending == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("synthetic disconnect")

        async def stop(self):
            await self.on_transcript("Kevin", "final callback")
            if ending == "stop-cancel":
                raise asyncio.CancelledError()
            if ending == "stop-error":
                raise RuntimeError("synthetic stop failure")

    async def serve(pipeline, _ingress, **_kwargs):
        pipeline.start_background_tasks()
        await pipeline.handle_message({})

    class Socket:
        async def accept(self):
            pass

        async def receive_text(self):
            return json.dumps(
                {
                    "event": "start",
                    "streamSid": "MZ_test",
                    "start": {"customParameters": {"ws_token": "test-token"}},
                    "type": "setup",
                    "customParameters": {"ws_token": "test-token"},
                }
            )

        async def iter_text(self):
            await asyncio.Event().wait()
            yield ""

        async def close(self, **_kwargs):
            pass

    monkeypatch.setattr(module, "VoicePipeline" if engine == "media" else "RelayPipeline", Pipeline)
    if engine == "media":
        monkeypatch.setattr(module, "_serve_pipeline_ingress", serve)
    entry = module.media_stream_ws if engine == "media" else module.relay_stream_ws
    async with asyncio.timeout(2):
        if ending in {"cancel", "stop-cancel"}:
            with pytest.raises(asyncio.CancelledError):
                await entry(Socket(), "CA_transcript")
        else:
            await entry(Socket(), "CA_transcript")
    assert len(observed) == 1 and observed[0]._closed
    if ending == "constructor-error":
        assert observed[0]._worker is None and not writes and not persisted
        return
    assert observed[0]._worker.done()
    assert writes[-1][0] == "CA_transcript" and writes[-1][1].endswith("Kevin: final callback")
    assert writes[-1][2:] == ("test-owner", "test-token")
    if ending != "stop-cancel":
        assert persisted[-1].endswith("Kevin: final callback")
