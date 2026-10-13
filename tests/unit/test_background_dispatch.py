"""Offline subprocess supervision, bounded protocol and durable scan fairness."""

import asyncio
import io
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app.services import background_dispatch as dispatch


def test_runner_settles_enabled_screening_measurement_before_success(monkeypatch, capsys):
    """Exercise real runner/post-call scheduling and executor shutdown offline."""
    from app import worker_runner
    from app.middleware import worker_auth
    from app.services import acquisition, post_call_handoff as handoff
    from app.services.post_call import PostCallResult

    now = time.time()
    record = {"contractor_id": "fictional-contractor", "call_status": "completed",
              "route_taken": "ai_screening", "timestamp": now - 20,
              "ended_at": now - 5, "transcript": "Caller: Hello\nKevin: How can I help?"}
    writes = []

    class Document:
        def get(self, **kwargs):
            # Still running when the recovery pass has finished; asyncio.run
            # must settle its executor before emitting the completion frame.
            time.sleep(.05)
            return SimpleNamespace(exists=True, to_dict=lambda: {
                "active": True, "acquisition_measurement": {},
            })

    db = SimpleNamespace(
        collection=lambda _: SimpleNamespace(document=lambda _: Document()),
        transaction=lambda: SimpleNamespace(update=lambda ref, updates: writes.append(updates)),
    )
    monkeypatch.setattr(acquisition, "get_firestore_client", lambda: db)
    monkeypatch.setattr(acquisition, "is_acquisition_measurement_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "validate_acquisition_measurement_map",
                        lambda _: (True, None, None, None, None, now - 100))
    monkeypatch.setattr(acquisition.firestore, "transactional", lambda fn: fn)
    monkeypatch.setattr(acquisition, "_pending_screening_tasks", set())
    monkeypatch.setattr(handoff.handoff_db, "get_handoff", AsyncMock(return_value={
        "contractor_id": "fictional-contractor", "status": "pending", "created_at": now,
    }))
    monkeypatch.setattr(handoff.call_db, "get_call", AsyncMock(return_value=record))
    monkeypatch.setattr(handoff.handoff_db, "claim_handoff", AsyncMock(return_value=True))
    monkeypatch.setattr(handoff.handoff_db, "finish_handoff", AsyncMock(return_value=True))
    monkeypatch.setattr(handoff, "_hydrate_handoff", AsyncMock(return_value={
        "transcript_lines": record["transcript"].splitlines(), "caller_phone": "",
        "contractor_phone": "", "twilio_number": "", "caller_language": "en",
        "contractor": {"contractor_id": "fictional-contractor"},
    }))
    monkeypatch.setattr(handoff, "process_post_call", AsyncMock(return_value=PostCallResult(
        status="complete", completed_effects=("call_record",), failed_effects=(),
    )))

    async def mirror(*args, **kwargs):
        await asyncio.to_thread(time.sleep, .005)
        return True

    async def ids(status, **kwargs):
        return ["CA_fictional"] if status == "pending" else []

    monkeypatch.setattr(handoff.call_db, "save_call", mirror)
    monkeypatch.setattr(handoff.handoff_db, "list_handoff_ids", ids)
    monkeypatch.setattr(worker_auth, "validate_worker_configuration", lambda: None)
    monkeypatch.setattr(sys, "argv", ["app.worker_runner", "post_calls"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))

    assert worker_runner.main() == 0
    assert len(writes) == 1
    assert writes[0]["acquisition_measurement.first_screening_conversation_observed_at"] >= now
    assert not acquisition._pending_screening_tasks
    output = json.loads(capsys.readouterr().out)
    assert output["completed"] is True


@pytest.mark.asyncio
async def test_disabled_job_performs_zero_imported_work(monkeypatch):
    monkeypatch.setattr(dispatch.settings, "purge_enabled", False)
    with pytest.raises(ValueError):
        await dispatch.run_child("data_purge", {})
    with pytest.raises(ValueError):
        await dispatch.execute_once("data_purge", {})
    assert not dispatch._CHILDREN


@pytest.mark.parametrize("job,cursor", [("orphan_calls", {"pending": "x"}),
    ("post_calls", {"pending": "x/y"}), ("post_calls", {"pending": "x" * 513}),
    ("estimates", {"processing": 1}), ("post_calls", {"pending": "x\n"})])
def test_continuation_rejects_oversized_or_unexpected_fields(job, cursor):
    with pytest.raises(ValueError):
        dispatch.validate_continuation(job, cursor)


def child_factory(monkeypatch, code):
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        assert args == (sys.executable, "-m", "app.worker_runner", "orphan_calls")
        process = await original(sys.executable, "-c", code, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(dispatch.asyncio, "create_subprocess_exec", spawn)
    return processes


@pytest.mark.asyncio
async def test_success_waits_for_child_exit_and_reaps(monkeypatch):
    processes = child_factory(monkeypatch, 'import sys,time;sys.stdout.write(\'{"completed":true,"continuation":{}}\');sys.stdout.flush();time.sleep(.08)')
    start = time.monotonic()
    assert await dispatch.run_child("orphan_calls", {}) == {}
    assert time.monotonic() - start >= .08
    assert processes[0].returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    assert not dispatch._CHILDREN


@pytest.mark.asyncio
async def test_timeout_kills_term_ignoring_child_and_reaps(monkeypatch):
    processes = child_factory(monkeypatch, 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)')
    monkeypatch.setattr(dispatch, "WALL_BUDGET_SECONDS", .2)
    monkeypatch.setattr(dispatch, "TERMINATE_GRACE_SECONDS", .05)
    with pytest.raises(asyncio.TimeoutError):
        await dispatch.run_child("orphan_calls", {})
    assert processes[0].returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    assert not dispatch._CHILDREN


@pytest.mark.asyncio
async def test_cancel_settles_child_even_with_repeated_cancellation(monkeypatch):
    processes = child_factory(monkeypatch, 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)')
    monkeypatch.setattr(dispatch, "TERMINATE_GRACE_SECONDS", .05)
    task = asyncio.create_task(dispatch.run_child("orphan_calls", {}))
    while not processes:
        await asyncio.sleep(.001)
    await asyncio.sleep(.05)
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode in {-15, -9}
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)


@pytest.mark.asyncio
async def test_oversized_child_output_is_rejected_without_echo(monkeypatch):
    processes = child_factory(monkeypatch, 'import sys,time;sys.stdout.write("x"*1000000);sys.stdout.flush();time.sleep(20)')
    with pytest.raises(RuntimeError, match="protocol overflow"):
        await dispatch.run_child("orphan_calls", {})
    assert processes[0].returncode is not None
    assert not dispatch._CHILDREN


@pytest.mark.asyncio
async def test_timeout_settles_descendant_after_process_leader_exits(monkeypatch, tmp_path):
    """A surviving descendant must not hold the response pipe open forever."""
    marker = tmp_path / "descendant.pid"
    code = f'''
import os, signal, sys, time
from pathlib import Path
marker = Path({str(marker)!r})
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    marker.write_text(str(os.getpid()))
    time.sleep(20)
    os._exit(0)
while not marker.exists():
    time.sleep(.001)
sys.stdout.write('{{"completed":true,"continuation":{{}}}}')
sys.stdout.flush()
os._exit(0)
'''
    processes = child_factory(monkeypatch, code)
    monkeypatch.setattr(dispatch, "WALL_BUDGET_SECONDS", .3)
    monkeypatch.setattr(dispatch, "TERMINATE_GRACE_SECONDS", .05)
    start = time.monotonic()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(dispatch.run_child("orphan_calls", {}), 3)
        assert marker.exists()
        assert processes[0].returncode == 0
        assert time.monotonic() - start < 2
        assert not dispatch._CHILDREN
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_success_kills_term_ignoring_descendant_with_closed_stdout(monkeypatch, tmp_path):
    """Leader success and pipe EOF must not leave provider work executing."""
    marker = tmp_path / "descendant.pid"
    heartbeat = tmp_path / "heartbeat"
    code = f'''
import os, signal, sys, time
from pathlib import Path
marker, heartbeat = Path({str(marker)!r}), Path({str(heartbeat)!r})
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1)
    marker.write_text(str(os.getpid()))
    for tick in range(2000):
        heartbeat.write_text(str(tick))
        time.sleep(.01)
    os._exit(0)
while not heartbeat.exists():
    time.sleep(.001)
sys.stdout.write('{{"completed":true,"continuation":{{}}}}')
sys.stdout.flush()
os._exit(0)
'''
    processes = child_factory(monkeypatch, code)
    monkeypatch.setattr(dispatch, "TERMINATE_GRACE_SECONDS", .05)
    try:
        assert await asyncio.wait_for(dispatch.run_child("orphan_calls", {}), 3) == {}
        assert marker.exists() and processes[0].returncode == 0
        # Allow SIGKILL delivery, then prove the descendant cannot keep working.
        await asyncio.sleep(.03)
        stopped_at = heartbeat.read_text()
        await asyncio.sleep(.08)
        assert heartbeat.read_text() == stopped_at
        assert not dispatch._CHILDREN
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_postcall_continuations_wrap_and_survive_restarts_without_poison_starvation(monkeypatch):
    from app.services import post_call_handoff as handoff
    pending = ["a", "b"]
    observed = []
    reads = []

    async def list_ids(status, *, limit, start_after=None):
        reads.append((status, limit, start_after))
        ids = pending if status == "pending" else ["i1", "i2"]
        return [sid for sid in ids if not start_after or sid > start_after][:limit]

    async def run(sid):
        observed.append(sid)
        return "needs_attention" if sid == "a" else "completed"

    monkeypatch.setattr(handoff.handoff_db, "list_handoff_ids", list_ids)
    monkeypatch.setattr(handoff, "run_post_call_handoff", run)
    monkeypatch.setattr(handoff.handoff_db, "claim_handoff", AsyncMock(return_value=False))
    monkeypatch.setattr(handoff.handoff_db, "get_handoff", AsyncMock(return_value={"status": "in_progress"}))
    first = await handoff.run_bounded_post_calls_once({})
    # Restart is a new invocation with only persisted cursor, no process globals.
    second = await handoff.run_bounded_post_calls_once(first)
    third = await handoff.run_bounded_post_calls_once(second)
    assert observed == ["a", "b", "a"]
    assert first == {"pending": "a", "in_progress": "i2"}
    assert ("in_progress", 25, "i2") in reads


@pytest.mark.asyncio
async def test_estimate_bounded_cursor_skips_not_due_and_wraps(monkeypatch):
    from app.services import estimate_worker as worker
    records = [SimpleNamespace(id=f"e{i:02}", reference=None,
        to_dict=lambda: {"lease_expires_at": 1000}) for i in range(30)]
    fetch_limits = []

    class Query:
        def __init__(self):
            self.cursor = ""
            self.count = None
        def where(self, *args): return self
        def order_by(self, *args): return self
        def document(self, cursor): return cursor
        def start_after(self, value):
            self.cursor = value["__name__"]
            return self
        def limit(self, count):
            self.count = count
            fetch_limits.append(count)
            return self
        def stream(self):
            return [doc for doc in records if doc.id > self.cursor][:self.count]

    monkeypatch.setattr(worker, "get_firestore_client", lambda: SimpleNamespace(collection=lambda _: Query()))
    claim = AsyncMock()
    monkeypatch.setattr(worker, "claim_reanalysis_or_fail", claim)
    first = await worker.run_pending_estimates_once(now=100, page_size=25, max_analyses=1, continuation={})
    second = await worker.run_pending_estimates_once(now=100, page_size=25, max_analyses=1, continuation=first)
    third = await worker.run_pending_estimates_once(now=100, page_size=25, max_analyses=1, continuation=second)
    assert first == {"processing": "e24"}
    assert second == {"processing": "e29"}
    assert third == first
    assert all(limit == 25 for limit in fetch_limits)
    claim.assert_not_called()


@pytest.mark.asyncio
async def test_estimate_only_one_item_claim_per_external_pass(monkeypatch):
    from app.services import estimate_worker as worker
    records = [SimpleNamespace(id=f"e{i}", reference=f"e{i}",
        to_dict=lambda: {"lease_expires_at": 0}) for i in range(3)]
    class Query:
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, count): return self
        def stream(self): return records
    monkeypatch.setattr(worker, "get_firestore_client", lambda: SimpleNamespace(collection=lambda _: Query()))
    claims = []
    def claim(db, ref, now):
        claims.append(ref)
        return "failed_notify", {"caller_phone": "", "media_id": "", "media_object_path": ""}
    monkeypatch.setattr(worker, "claim_reanalysis_or_fail", claim)
    monkeypatch.setattr(worker, "send_estimate_notifications", AsyncMock())
    result = await worker.run_pending_estimates_once(now=100, page_size=25, max_analyses=1)
    assert claims == ["e0"]
    assert result == {"processing": "e0"}


@pytest.mark.asyncio
async def test_temporary_estimate_failure_preserves_other_work_and_later_retry(monkeypatch):
    from app.services import estimate_worker as worker

    records = [SimpleNamespace(id=f"e{i}", reference=f"e{i}",
        to_dict=lambda: {"lease_expires_at": 0}) for i in range(2)]

    class Query:
        def __init__(self): self.cursor = ""
        def where(self, *args): return self
        def order_by(self, *args): return self
        def document(self, cursor): return cursor
        def limit(self, count): return self
        def start_after(self, value):
            self.cursor = value["__name__"]
            return self
        def stream(self): return [doc for doc in records if doc.id > self.cursor]

    monkeypatch.setattr(worker, "get_firestore_client", lambda: SimpleNamespace(collection=lambda _: Query()))
    claims = []

    def claim(db, ref, now):
        claims.append(ref)
        return "reclaimed", {"media_id": ref, "media_object_path": "fictional/video",
            "attempts": claims.count(ref), "media_content_type": "video/mp4"}

    monkeypatch.setattr(worker, "claim_reanalysis_or_fail", claim)
    monkeypatch.setattr(worker, "read_media", lambda _: b"fictional-media")
    monkeypatch.setattr(worker, "analyze_media", AsyncMock(side_effect=[
        TimeoutError("temporary fictional provider error"), {"estimate": 1}, {"estimate": 2}]))
    monkeypatch.setattr(worker, "claim_notification_and_complete", lambda *args: (True, False, {}))
    notifications = AsyncMock()
    monkeypatch.setattr(worker, "send_estimate_notifications", notifications)
    cursor = {}
    for _ in range(3):
        cursor = await worker.run_pending_estimates_once(
            now=100, page_size=25, max_analyses=1, continuation=cursor)
    assert claims == ["e0", "e1", "e0"]
    notifications.assert_not_called()
