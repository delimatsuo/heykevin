"""Owned-task shutdown against controlled workers and synthetic transports."""

import asyncio
import inspect
import os
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

from app.services.call_tasks import CallTasks, pipeline_tasks
from app.services.gemini_pipeline import GeminiPipeline
from app.services.relay_pipeline import RelayPipeline
from app.services.voice_pipeline import VoicePipeline
from app.services.urgent_handoff import start_urgency_task


async def _noop(*_args):
    pass


def _pipeline(monkeypatch, engine, events=None):
    events = [] if events is None else events

    async def closed():
        events.append("resource-closed")

    client = SimpleNamespace(aclose=AsyncMock(side_effect=closed))
    monkeypatch.setattr("app.services.voice_pipeline.httpx.AsyncClient", lambda **_: client)
    config = {"contractor_id": "test-owner", "effective_mode": "personal"}
    if engine == "relay":
        pipeline = RelayPipeline(contractor_config=config, send_to_twilio=_noop)
    else:
        cls = GeminiPipeline if engine == "gemini" else VoicePipeline
        pipeline = cls(on_audio_out=_noop, on_transcript=_noop, contractor_config=config)
        pipeline._connected = True
        websocket = SimpleNamespace(close=AsyncMock(side_effect=closed), send=AsyncMock())
        if engine == "gemini":
            pipeline._ws = websocket
        else:
            pipeline._deepgram_ws = websocket
    return pipeline


async def _dispose(pipeline, *tasks):
    for task in tasks:
        if task and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in tasks if t], return_exceptions=True)
    await pipeline.stop()


@pytest.mark.parametrize("engine", ["gemini", "voice", "relay"])
async def test_concurrent_stop_waiters_share_cleanup_and_cancellation_is_shielded(
    monkeypatch, engine
):
    events = []
    pipeline = _pipeline(monkeypatch, engine, events)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            events.append("worker-finished")

    task = pipeline_tasks(pipeline).create(worker())
    first = second = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            first = asyncio.create_task(pipeline.stop())
            await cleaning.wait()
            second = asyncio.create_task(pipeline.stop())
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not second.done() and task.cancelling() == 1
            assert not events
            release.set()
            await second
            before = list(events)
            await pipeline.stop()
            assert events == before
    finally:
        release.set()
        await _dispose(pipeline, task, first, second)
    assert events[0] == "worker-finished"
    assert events.count("resource-closed") == {"gemini": 1, "voice": 2, "relay": 0}[engine]
    assert task.cancelled()


@pytest.mark.parametrize("engine", ["gemini", "voice", "relay"])
async def test_two_owned_workers_can_request_stop_without_self_await(monkeypatch, engine):
    pipeline = _pipeline(monkeypatch, engine)
    entered = [asyncio.Event(), asyncio.Event()]
    release, requested = asyncio.Event(), asyncio.Event()
    returned = []

    async def worker(index):
        entered[index].set()
        await release.wait()
        await pipeline.stop()
        returned.append(index)
        if len(returned) == 2:
            requested.set()

    tasks = [pipeline_tasks(pipeline).create(worker(i)) for i in range(2)]
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(*(e.wait() for e in entered))
            release.set()
            await requested.wait()
            await pipeline.stop()  # external caller gets the completion barrier
            await asyncio.gather(*tasks)
    finally:
        release.set()
        await _dispose(pipeline, *tasks)
    assert sorted(returned) == [0, 1]
    assert not getattr(pipeline, "_active" if engine == "relay" else "_connected")


@pytest.mark.parametrize("engine", ["gemini", "voice", "relay"])
async def test_superseded_cancelled_task_is_retained_until_cleanup_finishes(monkeypatch, engine):
    pipeline = _pipeline(monkeypatch, engine)
    scope = pipeline_tasks(pipeline)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def old_worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    old = scope.create(old_worker())
    pipeline._summary_task = old
    stopping = new = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            old.cancel()
            await cleaning.wait()
            new = scope.create(_noop())
            pipeline._summary_task = new
            stopping = asyncio.create_task(pipeline.stop())
            await asyncio.sleep(0)
            assert not stopping.done() and old.cancelling() == 1
            release.set()
            await stopping
    finally:
        release.set()
        await _dispose(pipeline, old, new, stopping)
    assert old.cancelled() and new.done()


async def test_timeout_retains_straggler_and_rejects_new_coroutine(caplog):
    scope = CallTasks(drain_timeout=0)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = scope.create(stubborn())
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            await scope.close()
            await cancelled.wait()
            assert not task.done() and task in scope._tasks
            coroutine = _noop()
            assert scope.create(coroutine).cancelled()
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
            assert "drain timed out" in caplog.text
    finally:
        release.set()
        await task
    await asyncio.sleep(0)
    assert task not in scope._tasks


async def test_resource_failure_does_not_skip_remaining_close_and_logs_no_payload(caplog):
    scope = CallTasks()
    closed = []

    async def broken():
        raise RuntimeError("private provider payload")

    async def following():
        closed.append(True)

    await scope.close((broken, following))
    assert closed == [True]
    assert "RuntimeError" in caplog.text and "private provider payload" not in caplog.text


async def test_resource_timeout_is_bounded_and_later_resources_still_close(caplog):
    scope = CallTasks(resource_timeout=0)
    closed = []

    async def blocked():
        await asyncio.Event().wait()

    async def following():
        closed.append(True)

    async with asyncio.timeout(2):
        await scope.close((blocked, following))
    assert closed == [True] and "resource close timed out" in caplog.text
    assert not [task for task in scope._tasks if not task.done()]
    assert not [task for task in scope._resources if not task.done()]


@pytest.mark.parametrize("engine", ["gemini", "voice", "relay"])
async def test_urgency_work_is_owned_even_after_its_handle_is_replaced(monkeypatch, engine):
    pipeline = _pipeline(monkeypatch, engine)
    entered = [asyncio.Event(), asyncio.Event()]
    cleaned = []

    async def callback(index):
        entered[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(index)

    tasks = [start_urgency_task(pipeline, callback, i) for i in range(2)]
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(*(e.wait() for e in entered))
            await pipeline.stop()
    finally:
        await _dispose(pipeline, *tasks)
    assert sorted(cleaned) == [0, 1] and all(t.cancelled() for t in tasks)


async def test_voice_detached_utterance_is_joined_before_http_close(monkeypatch):
    events = []
    pipeline = _pipeline(monkeypatch, "voice", events)
    entered = asyncio.Event()

    async def utterance(_text):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("utterance-finished")

    monkeypatch.setattr(pipeline, "_process_utterance", utterance)
    pipeline._utterance_buffer = ["synthetic caller speech"]
    try:
        async with asyncio.timeout(2):
            await pipeline._flush_utterance()
            await entered.wait()
            await pipeline.stop()
    finally:
        await pipeline.stop()
    assert events == ["utterance-finished", "resource-closed", "resource-closed"]


@pytest.mark.parametrize("engine", ["gemini", "voice", "relay"])
async def test_start_after_stop_cannot_restart_provider_or_background_work(monkeypatch, engine):
    pipeline = _pipeline(monkeypatch, engine)
    connect = AsyncMock(side_effect=AssertionError("provider connection attempted"))
    monkeypatch.setattr("websockets.connect", connect)
    await pipeline.stop()
    if engine == "relay":
        pipeline.start_background_tasks()
        pipeline._start_generation()
        assert pipeline._generate_task is None
    else:
        assert not await pipeline.start()
    connect.assert_not_awaited()
    assert not [task for task in pipeline_tasks(pipeline)._tasks if not task.done()]


@pytest.mark.parametrize("engine", ["gemini", "voice"])
async def test_connection_finishing_after_stop_is_closed_without_restarting(monkeypatch, engine):
    pipeline = _pipeline(monkeypatch, engine)
    entered, release = asyncio.Event(), asyncio.Event()
    late = SimpleNamespace(close=AsyncMock(), send=AsyncMock())

    async def connect(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return late

    monkeypatch.setattr("websockets.connect", connect)
    starting = asyncio.create_task(pipeline.start())
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            await pipeline.stop()
            release.set()
            assert await starting is False
    finally:
        release.set()
        await _dispose(pipeline, starting)
    late.close.assert_awaited_once()
    late.send.assert_not_awaited()
    assert not pipeline._connected


async def test_gemini_setup_ack_after_stop_cannot_reactivate_pipeline(monkeypatch):
    pipeline = _pipeline(monkeypatch, "gemini")
    entered, release = asyncio.Event(), asyncio.Event()

    async def receive():
        entered.set()
        await release.wait()
        return '{"setupComplete": {}}'

    websocket = SimpleNamespace(close=AsyncMock(), send=AsyncMock(), recv=receive)
    monkeypatch.setattr("websockets.connect", AsyncMock(return_value=websocket))
    starting = asyncio.create_task(pipeline.start())
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            await pipeline.stop()
            release.set()
            assert await starting is False
    finally:
        release.set()
        await _dispose(pipeline, starting)
    assert not pipeline._connected and pipeline._receive_task is None
    websocket.close.assert_awaited_once()


async def test_deepgram_receive_task_can_replace_itself_without_self_cancellation(monkeypatch):
    pipeline = _pipeline(monkeypatch, "voice")
    replacement = SimpleNamespace(close=AsyncMock(), send=AsyncMock())
    monkeypatch.setattr("websockets.connect", AsyncMock(return_value=replacement))

    async def receive():
        await asyncio.Event().wait()

    monkeypatch.setattr(pipeline, "_deepgram_receive_loop", receive)

    async def reconnect_from_receive():
        pipeline._deepgram_task = asyncio.current_task()
        return await pipeline._connect_deepgram()

    caller = pipeline_tasks(pipeline).create(reconnect_from_receive())
    try:
        async with asyncio.timeout(2):
            assert await caller is True
            assert pipeline._deepgram_task is not caller
            await pipeline.stop()
    finally:
        await _dispose(pipeline, caller)
    assert not caller.cancelled() and pipeline._deepgram_task.cancelled()


@pytest.mark.parametrize("engine", ["gemini", "voice"])
async def test_connection_completion_during_shutdown_cannot_cancel_its_own_cleanup(
    monkeypatch, engine
):
    pipeline = _pipeline(monkeypatch, engine)
    entered, release = asyncio.Event(), asyncio.Event()
    late = SimpleNamespace(close=AsyncMock(), send=AsyncMock())

    async def connect(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return late

    monkeypatch.setattr("websockets.connect", connect)
    starting = asyncio.create_task(pipeline.start())
    stopping = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            # stop sets its latch, then the connection resumes before the
            # separately scheduled drain snapshots its workers.
            stopping = asyncio.create_task(pipeline.stop())
            release.set()
            assert await starting is False
            await stopping
            late.close.assert_awaited_once()
    finally:
        release.set()
        await _dispose(pipeline, starting, stopping)


@pytest.mark.parametrize("already_cancelling", [False, True])
async def test_relay_parent_cancellation_does_not_interrupt_child_cleanup(
    monkeypatch, already_cancelling
):
    pipeline = _pipeline(monkeypatch, "relay")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    parent_cancelled = asyncio.Event()
    events = []

    async def generation(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await release.wait()
                events.append("cleanup-finished")
            except asyncio.CancelledError:
                events.append("cleanup-interrupted")
                raise

    monkeypatch.setattr(pipeline, "_generate_reply", generation)
    parent = pipeline_tasks(pipeline).create(pipeline._deliver_message_instruction())
    cancel = parent.cancel

    def observed_cancel(*args, **kwargs):
        parent_cancelled.set()
        return cancel(*args, **kwargs)

    monkeypatch.setattr(parent, "cancel", observed_cancel)
    stopping = child = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            child = pipeline._generate_task
            if already_cancelling:
                child.cancel()
                await cleaning.wait()
            stopping = asyncio.create_task(pipeline.stop())
            await parent_cancelled.wait()
            await cleaning.wait()
            # Let cancellation propagate through wait_for's own unwind.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            await stopping
    finally:
        release.set()
        await _dispose(pipeline, parent, child, stopping)
    assert events == ["cleanup-finished"]


async def test_resource_cleanup_survives_its_owned_waiter_being_cancelled():
    scope = CallTasks()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        finished.set()

    worker = scope.create(scope.close_resource(close))
    stopping = None
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            stopping = asyncio.create_task(scope.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not stopping.done()
            release.set()
            await stopping
            assert finished.is_set() and worker.cancelled()
    finally:
        release.set()
        await asyncio.gather(worker, *([stopping] if stopping else []), return_exceptions=True)
        await scope.close()


@pytest.mark.parametrize("engine", ["gemini", "voice"])
async def test_startup_callback_stopping_pipeline_prevents_followup_output(monkeypatch, engine):
    pipeline = _pipeline(monkeypatch, engine)
    followup = AsyncMock()
    if engine == "gemini":
        websocket = SimpleNamespace(
            close=AsyncMock(), send=AsyncMock(), recv=AsyncMock(return_value='{"setupComplete":{}}')
        )
        monkeypatch.setattr("websockets.connect", AsyncMock(return_value=websocket))

        async def receive():
            await asyncio.Event().wait()

        monkeypatch.setattr(pipeline, "_receive_loop", receive)
        monkeypatch.setattr(pipeline, "_send_greeting", pipeline.stop)
        monkeypatch.setattr(pipeline, "_send_opening_intake_instructions", followup)
    else:
        monkeypatch.setattr(pipeline, "_connect_deepgram_with_retry", AsyncMock(return_value=True))

        async def transcript(*_args):
            await pipeline.stop()

        pipeline.on_transcript = transcript
        monkeypatch.setattr(pipeline, "_speak", followup)
    try:
        async with asyncio.timeout(2):
            assert await pipeline.start() is False
    finally:
        await pipeline.stop()
    followup.assert_not_awaited()
    assert not pipeline._connected


async def test_receive_failure_after_shutdown_cannot_end_call_again(monkeypatch):
    pipeline = _pipeline(monkeypatch, "gemini")
    pipeline.on_call_complete = AsyncMock()
    await pipeline.stop()
    await pipeline._complete_after_receive_failure()
    pipeline.on_call_complete.assert_not_awaited()
