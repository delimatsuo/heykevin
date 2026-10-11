"""Exercise the shared pool itself; no provider, database or application I/O."""
import asyncio
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock, Thread
import time

import pytest

from app.db import jobber_disconnect_transactions as pool


@pytest.fixture(autouse=True)
def isolated_pool(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(pool, "_EXECUTOR", executor)
    monkeypatch.setattr(pool, "_OUTSTANDING", set())
    monkeypatch.setattr(pool, "_WAITING", deque())
    monkeypatch.setattr(pool, "_OUTSTANDING_LOCK", Lock())
    monkeypatch.setattr(pool, "_DRAINERS", 0)
    yield executor
    executor.shutdown(wait=True)
    assert not pool._OUTSTANDING and not pool._WAITING and pool._DRAINERS == 0


async def until(predicate, seconds=2):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, "queue condition did not become true"
        await asyncio.sleep(.002)


def counts():
    with pool._OUTSTANDING_LOCK:
        return len(pool._OUTSTANDING), len(pool._WAITING)


@pytest.mark.asyncio
async def test_four_callbacks_fifo_no_barging_and_only_two_workers(isolated_pool):
    releases = [Event() for _ in range(4)]
    started, active, peak = [], 0, 0
    lock = Lock()
    def callback(index):
        def run(deadline):
            nonlocal active, peak
            with lock:
                started.append(index)
                active += 1
                peak = max(peak, active)
            try:
                assert releases[index].wait(3)
                deadline.timeout()
                return index
            finally:
                with lock:
                    active -= 1
        return run
    tasks = []
    try:
        for i in range(4):
            tasks.append(asyncio.create_task(pool.run_bounded_async(callback(i), seconds=4)))
            await until(lambda: i in started if i < 2 else counts()[1] == i - 1)
        assert started == [0, 1] and counts() == (2, 2)
        assert isolated_pool._work_queue.qsize() == 0
        releases[0].set()
        await until(lambda: started == [0, 1, 2])
        assert counts() == (2, 1)
        releases[1].set()
        await until(lambda: started == [0, 1, 2, 3])
        releases[2].set(); releases[3].set()
        assert await asyncio.gather(*tasks) == [0, 1, 2, 3]
        assert peak == 2
    finally:
        for release in releases: release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.drain_jobber_operations()


@pytest.mark.asyncio
async def test_waiting_cancellation_expiry_and_running_cancellation_keep_capacity(isolated_pool):
    release, entered = Event(), [Event(), Event()]
    def blocked(i):
        def run(deadline):
            entered[i].set()
            assert release.wait(3)
        return run
    tasks = [asyncio.create_task(pool.run_bounded_async(blocked(i), seconds=4)) for i in range(2)]
    waiting = []
    called = []
    try:
        await until(lambda: all(e.is_set() for e in entered))
        tasks[0].cancel(); tasks[1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert counts() == (2, 0)
        expired = asyncio.create_task(pool.run_bounded_async(lambda d: called.append("expired"), seconds=.06))
        cancelled = asyncio.create_task(pool.run_bounded_async(lambda d: called.append("cancelled"), seconds=2))
        waiting += [expired, cancelled]
        await until(lambda: counts() == (2, 2))
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError): await cancelled
        with pytest.raises(TimeoutError): await expired
        assert not called and counts() == (2, 0)
        for _ in range(128):
            waiting.append(asyncio.create_task(pool.run_bounded_async(lambda d: None, seconds=4)))
        await until(lambda: counts() == (2, 128))
        with pytest.raises(pool.JobberDeadlineError):
            await pool.run_bounded_async(lambda d: called.append("overflow"), seconds=1)
        assert isolated_pool._work_queue.qsize() == 0
        assert not called and counts() == (2, 128)
    finally:
        for task in waiting: task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        release.set()
        await pool.drain_jobber_operations()


@pytest.mark.asyncio
async def test_queue_wait_consumes_original_deadline():
    release, entered = Event(), [Event(), Event()]
    def blocked(i):
        def run(d):
            entered[i].set()
            assert release.wait(3)
        return run
    tasks = [asyncio.create_task(pool.run_bounded_async(blocked(i), seconds=4)) for i in range(2)]
    seen = []
    def after_queue(d):
        seen.append(d.ends - time.monotonic())
        return "done"
    try:
        await until(lambda: all(e.is_set() for e in entered))
        task = asyncio.create_task(pool.run_bounded_async(after_queue, seconds=.8))
        await until(lambda: counts()[1] == 1)
        await asyncio.sleep(.2)
        release.set()
        assert await task == "done"
        assert 0 < seen[0] < .65
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.drain_jobber_operations()


class HeldExecutor:
    """Holds the submitted-but-not-started boundary without a hidden backlog."""
    def __init__(self): self.calls = []
    def submit(self, callback, *args):
        future = Future()
        self.calls.append((future, callback, args))
        return future
    def release(self):
        future, callback, args = self.calls.pop(0)
        assert not future.cancelled()
        future.set_running_or_notify_cancel()
        try: future.set_result(callback(*args))
        except BaseException as exc: future.set_exception(exc)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel,release_before_resume", [(False, False), (True, False), (True, True)])
async def test_submitted_wrapper_rechecks_expiry_and_cancellation(monkeypatch, cancel, release_before_resume):
    executor = HeldExecutor()
    monkeypatch.setattr(pool, "_EXECUTOR", executor)
    called = []
    task = asyncio.create_task(pool.run_bounded_async(lambda d: called.append(True), seconds=.06))
    await until(lambda: len(executor.calls) == 1)
    if cancel: task.cancel()
    if release_before_resume:
        assert counts() == (1, 0)
        executor.release()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError): await task
    if not release_before_resume:
        assert counts() == (1, 0)
        executor.release()
    await pool.drain_jobber_operations()
    assert not called and counts() == (0, 0)


@pytest.mark.asyncio
async def test_drain_ownership_rejects_waiters_and_new_work_until_last_drain():
    release, entered = Event(), [Event(), Event()]
    def blocked(i):
        def run(d):
            entered[i].set()
            assert release.wait(3)
        return run
    tasks = [asyncio.create_task(pool.run_bounded_async(blocked(i), seconds=4)) for i in range(2)]
    drains = []
    try:
        await until(lambda: all(e.is_set() for e in entered))
        waiting = asyncio.create_task(pool.run_bounded_async(lambda d: pytest.fail("drained waiter ran"), seconds=3))
        tasks.append(waiting)
        await until(lambda: counts()[1] == 1)
        drains = [asyncio.create_task(pool.drain_jobber_operations()) for _ in range(2)]
        await until(lambda: pool._DRAINERS == 2)
        with pytest.raises(pool.JobberDeadlineError): await waiting
        drains[0].cancel()
        with pytest.raises(asyncio.CancelledError): await drains[0]
        assert pool._DRAINERS == 1 and counts() == (2, 0)
        with pytest.raises(pool.JobberDeadlineError):
            await pool.run_bounded_async(lambda d: pytest.fail("new work entered drain"), seconds=1)
        release.set()
        await drains[1]
        assert await pool.run_bounded_async(lambda d: "reused", seconds=1) == "reused"
    finally:
        release.set()
        await asyncio.gather(*tasks, *drains, return_exceptions=True)
        await pool.drain_jobber_operations()


@pytest.mark.asyncio
async def test_drain_timeout_retains_actual_futures_and_reopens(monkeypatch):
    executor = HeldExecutor()
    monkeypatch.setattr(pool, "_EXECUTOR", executor)
    task = asyncio.create_task(pool.run_bounded_async(lambda d: "done", seconds=3))
    await until(lambda: counts()[0] == 1)
    actual_wait = asyncio.wait_for
    async def short_drain(awaitable, timeout):
        assert timeout == 22.0
        return await actual_wait(awaitable, timeout=.02)
    with monkeypatch.context() as patch:
        patch.setattr(pool.asyncio, "wait_for", short_drain)
        with pytest.raises(TimeoutError): await pool.drain_jobber_operations()
    assert counts() == (1, 0) and pool._DRAINERS == 0
    executor.release()
    assert await task == "done"
    await pool.drain_jobber_operations()


@pytest.mark.asyncio
async def test_already_completed_future_callback_cannot_deadlock(monkeypatch):
    class Instant:
        def submit(self, callback, *args):
            future = Future()
            future.set_result(callback(*args))
            return future
    monkeypatch.setattr(pool, "_EXECUTOR", Instant())
    for i in range(20):
        assert await pool.run_bounded_async(lambda d: i, seconds=1) == i
    assert counts() == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_independent_event_loop_threads_share_fifo_and_prune_stalled_owner(cancel):
    release, stop_loop, entered = Event(), Event(), [Event(), Event()]
    outcomes, errors, ran = [], [], []
    def blocked(i):
        def run(d):
            entered[i].set()
            assert release.wait(4)
        return run
    holders = [asyncio.create_task(pool.run_bounded_async(blocked(i), seconds=5)) for i in range(2)]
    def other_loop():
        async def work():
            task = asyncio.create_task(pool.run_bounded_async(lambda d: ran.append("expired"), seconds=.08))
            await asyncio.sleep(.01)  # Enqueue before intentionally pausing this loop.
            if cancel: task.cancel()
            assert stop_loop.wait(3)
            try: await task
            except asyncio.CancelledError: outcomes.append("cancelled")
            except TimeoutError: outcomes.append("expired")
        try: asyncio.run(work())
        except BaseException as exc: errors.append(exc)
    thread = Thread(target=other_loop)
    try:
        await until(lambda: all(e.is_set() for e in entered))
        thread.start()
        await until(lambda: counts()[1] == 1)
        await asyncio.sleep(.12)
        later = asyncio.create_task(pool.run_bounded_async(lambda d: ran.append("later"), seconds=2))
        await until(lambda: counts()[1] == 1)
        release.set()
        await later
        assert ran == ["later"]
    finally:
        release.set(); stop_loop.set()
        if thread.ident is not None: thread.join(3)
        await asyncio.gather(*holders, return_exceptions=True)
        await pool.drain_jobber_operations()
    assert not thread.is_alive() and not errors and outcomes == ["cancelled" if cancel else "expired"]


@pytest.mark.parametrize("seconds", [True, False, 0, -1, float("inf"), float("nan"), "1", None, 10 ** 1000])
@pytest.mark.asyncio
async def test_invalid_deadline_rejected_before_submission(seconds):
    with pytest.raises(pool.JobberDeadlineError):
        await pool.run_bounded_async(lambda d: pytest.fail("invalid deadline submitted"), seconds=seconds)
    assert counts() == (0, 0)
