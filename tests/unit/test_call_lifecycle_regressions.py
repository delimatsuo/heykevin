"""Deterministic reproductions for the call-lifetime refactor.

Strict expected failures describe desired behavior, not passing safety claims.
Remove each marker when its production fix lands. Only the final, dedicated
invariant exception is expected: fixture, timeout and cleanup failures stay red.
These tests use synthetic storage and transports; they do not qualify live calls
or Firebase consistency. No voice engine is started and no provider is called.
"""

import asyncio
from copy import deepcopy
import json
import os
from unittest.mock import AsyncMock

import pytest

for _name, _value in {
    "TWILIO_ACCOUNT_SID": "test-account-sid",
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "TELEGRAM_BOT_TOKEN": "test-telegram-token",
    "USER_PHONE": "+15550000001",
}.items():
    os.environ.setdefault(_name, _value)

from app.db import cache
from app.services.gemini_pipeline import GeminiPipeline
from app.services.relay_pipeline import RelayPipeline
from app.services.routing import Route
from app.services.voice_pipeline import VoicePipeline
from app.webhooks import media_stream, relay_stream, twilio_incoming


class KnownLifecycleGap(AssertionError):
    """Raised only after a controlled interleaving and successful teardown."""


def _gap(reason):
    return pytest.mark.xfail(strict=True, raises=KnownLifecycleGap, reason=reason)


class _AsyncioProxy:
    """Track only this module's tasks; never patch global asyncio scheduling."""

    def __init__(self, *, skip_retry_delays=False):
        self.tasks = []
        self.retry_delays = []
        self.skip_retry_delays = skip_retry_delays

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def create_task(self, coroutine, **kwargs):
        task = asyncio.create_task(coroutine, **kwargs)
        self.tasks.append(task)
        return task

    async def sleep(self, delay):
        if self.skip_retry_delays:
            assert delay == 0.5, "unexpected sleep in stream authentication"
            self.retry_delays.append(delay)
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(delay)


async def _settle(tasks):
    """Collect every owned task, including on assertion or timeout failures."""
    for task in tasks:
        if not task.done():
            task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


@pytest.mark.parametrize(
    "reverse_completion",
    [
        False,
        pytest.param(True, marks=_gap("older live transcript can overwrite newer text")),
    ],
)
async def test_latest_transcript_survives_write_completion_order(monkeypatch, reverse_completion):
    proxy = _AsyncioProxy()
    monkeypatch.setattr(media_stream, "asyncio", proxy)
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    stored = []

    async def update(_sid, data):
        if data["transcript_buffer"] == "Caller: Hello":
            first_entered.set()
            await release_first.wait()
        stored.append(data["transcript_buffer"])

    monkeypatch.setattr(media_stream, "update_active_call", update)
    lines = ["Caller: Hello"]
    pusher = media_stream.LiveTranscriptPusher("CA_lifecycle", lines, throttle=0)
    try:
        async with asyncio.timeout(2):
            pusher.push()
            await first_entered.wait()
            if not reverse_completion:
                release_first.set()
                await asyncio.gather(*proxy.tasks)
            lines.append("Kevin: How can I help?")
            pusher.push()
            # Yield one turn so an independently scheduled newer write can finish
            # while the older write is held. A serialized writer may stay blocked.
            await asyncio.sleep(0)
            release_first.set()
            await asyncio.gather(*proxy.tasks)
    finally:
        release_first.set()
        await _settle(proxy.tasks)

    assert stored and stored.count("\n".join(lines)) == 1
    if stored[-1] != "\n".join(lines):
        raise KnownLifecycleGap("older write completed last and replaced newer transcript")


class _MemoryReference:
    """Synthetic partial-update store, not a Firebase emulator."""

    def __init__(self):
        self.record = {"contractor_id": "test-owner", "ws_token": "test-token"}

    def update(self, updates):
        self.record = {**(self.record or {}), **updates}

    def delete(self):
        self.record = None


@pytest.mark.parametrize(
    "delete_while_pending",
    [
        False,
        pytest.param(
            True, marks=_gap("a pending transcript update can outlive active-call deletion")
        ),
    ],
)
async def test_transcript_write_does_not_recreate_deleted_call(monkeypatch, delete_while_pending):
    proxy, reference = _AsyncioProxy(), _MemoryReference()
    monkeypatch.setattr(media_stream, "asyncio", proxy)
    monkeypatch.setattr(cache, "_get_ref", lambda _sid: reference)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_update(sid, data):
        entered.set()
        await release.wait()
        await cache.update_active_call(sid, data)

    monkeypatch.setattr(media_stream, "update_active_call", delayed_update)
    pusher = media_stream.LiveTranscriptPusher("CA_lifecycle", ["Caller: Hello"], throttle=0)
    try:
        async with asyncio.timeout(2):
            pusher.push()
            await entered.wait()
            if delete_while_pending:
                await cache.delete_active_call("CA_lifecycle")
                assert reference.record is None
            release.set()
            await asyncio.gather(*proxy.tasks)
            if not delete_while_pending:
                assert reference.record["transcript_buffer"] == "Caller: Hello"
                await cache.delete_active_call("CA_lifecycle")
    finally:
        release.set()
        await _settle(proxy.tasks)

    if reference.record is not None:
        raise KnownLifecycleGap("pending transcript update recreated a deleted synthetic record")


async def _noop(*_args):
    pass


@pytest.mark.parametrize("engine", ["gemini", "voice", "relay"])
@pytest.mark.parametrize(
    "cleanup_already_finished",
    [
        True,
        pytest.param(
            False, marks=_gap("pipeline stop cancels but does not join summary-task cleanup")
        ),
    ],
)
async def test_stop_returns_after_owned_task_cleanup(monkeypatch, engine, cleanup_already_finished):
    # Replace only the unused HTTP transport. Run actual constructors and stop().
    monkeypatch.setattr("app.services.voice_pipeline.httpx.AsyncClient", lambda **_: AsyncMock())
    config = {"contractor_id": "test-owner", "effective_mode": "personal"}
    if engine == "relay":
        pipeline = RelayPipeline(contractor_config=config, send_to_twilio=_noop)
        pipeline._active = True
    else:
        cls = GeminiPipeline if engine == "gemini" else VoicePipeline
        pipeline = cls(on_audio_out=_noop, on_transcript=_noop, contractor_config=config)
        pipeline._connected = True
    entered, cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events, owned = [], []

    async def worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()
            events.append("cleanup-finished")

    async def stop():
        await pipeline.stop()
        events.append("stop-returned")

    task = asyncio.create_task(worker())
    owned.append(task)
    pipeline._summary_task = task
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            if cleanup_already_finished:
                task.cancel()
                release_cleanup.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            stopping = asyncio.create_task(stop())
            owned.append(stopping)
            await cleanup_entered.wait()
            await asyncio.sleep(0)  # permit stop to return if it does not join
            release_cleanup.set()
            await stopping
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled(), "stop did not cancel the owned task"
            assert not getattr(pipeline, "_active" if engine == "relay" else "_connected")
    finally:
        release_cleanup.set()
        await _settle(owned)

    assert sorted(events) == ["cleanup-finished", "stop-returned"]
    if events != ["cleanup-finished", "stop-returned"]:
        raise KnownLifecycleGap(f"{engine} stop returned before owned cleanup finished")


class _SetupProbeComplete(Exception):
    """End the synthetic stream just after successful authentication."""


class _SetupSocket:
    def __init__(self, engine, token):
        self.first = (
            {
                "event": "start",
                "streamSid": "MZ_lifecycle",
                "start": {"customParameters": {"ws_token": token}},
            }
            if engine == "media"
            else {"type": "setup", "customParameters": {"ws_token": token}}
        )
        self.close_codes = []

    async def accept(self):
        pass

    async def receive_text(self):
        return json.dumps(self.first)

    async def iter_text(self):
        await asyncio.Event().wait()
        yield ""  # async generator, never yields media

    async def close(self, code=1000):
        self.close_codes.append(code)


@pytest.mark.parametrize("engine", ["media", "relay"])
@pytest.mark.parametrize(
    "scenario",
    [
        "ready",
        "wrong-token",
        pytest.param(
            "history-blocked", marks=_gap("history persistence delays live stream authority")
        ),
    ],
)
async def test_stream_authorization_does_not_wait_for_call_history(monkeypatch, engine, scenario):
    module = media_stream if engine == "media" else relay_stream
    proxy = _AsyncioProxy(skip_retry_delays=True)
    monkeypatch.setattr(module, "asyncio", proxy)
    monkeypatch.setattr(module, "_init_firebase", lambda: None)
    entered, release_history = asyncio.Event(), asyncio.Event()
    records, reads, authorized = {}, [], []

    async def save_history(_sid, _record):
        entered.set()
        await release_history.wait()
        return True

    async def save_active(active):
        records[active.call_sid] = active.to_dict()
        return True

    class Reference:
        def get(self):
            record = deepcopy(records.get("CA_lifecycle"))
            reads.append(record)
            return record

    async def resolve(sid, record):
        authorized.append((sid, record))
        raise _SetupProbeComplete()

    monkeypatch.setattr("app.db.calls.save_call", save_history)
    monkeypatch.setattr(cache, "save_active_call", save_active)
    monkeypatch.setattr(
        "app.services.push_notification.get_device_token", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("firebase_admin.db.reference", lambda _path: Reference())
    monkeypatch.setattr(module, "_resolve_active_call", resolve)
    token = "wrong-token" if scenario == "wrong-token" else "test-token"
    websocket = _SetupSocket(engine, token)
    routing = asyncio.create_task(
        twilio_incoming._post_routing_tasks(
            call_sid="CA_lifecycle",
            caller_phone="+15550000002",
            caller_name="Test Caller",
            trust_score=0,
            score_breakdown={},
            route=Route.AI_SCREENING,
            lookups={},
            conference_name="test-conference",
            contractor_id="test-owner",
            ws_token="test-token",
        )
    )
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            if scenario != "history-blocked":
                release_history.set()
                await routing
            entry = module.media_stream_ws if engine == "media" else module.relay_stream_ws
            await entry(websocket, "CA_lifecycle")
            release_history.set()
            await routing
    finally:
        release_history.set()
        await _settle([routing, *proxy.tasks])

    assert records["CA_lifecycle"]["contractor_id"] == "test-owner"
    assert records["CA_lifecycle"]["ws_token"] == "test-token"
    assert 1 <= len(reads) <= 3
    assert websocket.close_codes == ([1011] if authorized else [1008])
    if scenario == "wrong-token":
        assert not authorized
        assert len(reads) == 1 and not proxy.retry_delays
    elif not authorized:
        assert scenario == "history-blocked"
        assert reads == [None, None, None] and proxy.retry_delays == [0.5] * 3
        raise KnownLifecycleGap(f"{engine} rejected a valid stream while history save was blocked")
    else:
        assert len(authorized) == 1
        assert authorized[0][1]["contractor_id"] == "test-owner"
        assert authorized[0][1]["ws_token"] == token
