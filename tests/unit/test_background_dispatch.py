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
    with pytest.raises(ValueError):
        await dispatch.run_child("data_purge", {})
    with pytest.raises(ValueError):
        await dispatch.execute_once("data_purge", {})
    assert not dispatch._CHILDREN


@pytest.mark.asyncio
async def test_external_call_retention_requests_strict_maintenance(monkeypatch):
    from app.services import background_maintenance

    retention = AsyncMock()
    monkeypatch.setattr(background_maintenance, "call_retention_once", retention)

    assert await dispatch.execute_once("call_retention", {}) == {}
    retention.assert_awaited_once_with(strict=True)


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
async def test_timeout_kills_term_ignoring_child_and_reaps(monkeypatch, caplog):
    processes = child_factory(monkeypatch, 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)')
    monkeypatch.setattr(dispatch, "WALL_BUDGET_SECONDS", .2)
    monkeypatch.setattr(dispatch, "TERMINATE_GRACE_SECONDS", .05)
    with pytest.raises(asyncio.TimeoutError):
        await dispatch.run_child("orphan_calls", {})
    assert processes[0].returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    assert not dispatch._CHILDREN
    assert "phase=child_timeout code=wall_budget_exceeded" in caplog.text
    assert "timeout=true" in caplog.text
    assert "phase=child_cleanup code=grace_exceeded" in caplog.text
    assert "phase=child_cleanup code=reaped exit_code=-9" in caplog.text
    assert "termination=reaped" in caplog.text


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
    def claim(db, ref, now, **_kwargs):
        claims.append(ref)
        return "failed_notify", {"caller_phone": "", "media_id": "", "media_object_path": ""}
    monkeypatch.setattr(worker, "claim_reanalysis_or_fail", claim)
    monkeypatch.setattr(worker, "_send_claimed_estimate_notifications", AsyncMock())
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

    def claim(db, ref, now, **_kwargs):
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


_DIAGNOSTIC_SECRET = "FAKE_TOKEN_phone_15555550199_provider_payload_private"


def _diagnostic_records(caplog):
    return [record for record in caplog.records if record.name == "app.background_worker"]


def test_diagnostics_reject_arbitrary_fields_and_unbounded_metadata(caplog):
    dispatch.log_worker_failure(_DIAGNOSTIC_SECRET, _DIAGNOSTIC_SECRET, _DIAGNOSTIC_SECRET,
                                returncode=10**100, timed_out=_DIAGNOSTIC_SECRET,
                                termination=_DIAGNOSTIC_SECRET)
    dispatch.log_worker_failure("estimates", "completion", "completion_write_failed",
                                returncode=True, termination="none")
    records = _diagnostic_records(caplog)
    assert len(records) == 2
    assert records[0].getMessage() == (
        "worker_failure job=unknown phase=unknown code=unknown exit_code=unknown timeout=false termination=unknown")
    assert "phase=completion code=completion_write_failed exit_code=unknown" in records[1].getMessage()
    assert _DIAGNOSTIC_SECRET not in caplog.text
    assert all(record.exc_info is None and not record.stack_info for record in records)


@pytest.mark.parametrize("phase,code", [
    ("bootstrap", "import_failed"), ("config", "config_invalid"),
    ("protocol", "input_invalid"), ("protocol", "output_invalid"),
    ("execute", "execution_failed"), ("execute", "permission_denied"),
    ("execute", "deadline_exceeded"), ("execute", "precondition_failed"),
])
@pytest.mark.asyncio
async def test_child_failure_frames_distinguish_fixed_stage_without_stderr(monkeypatch, caplog, capsys, phase, code):
    frame = {"completed": False, "failure": {"phase": phase, "code": code}}
    processes = child_factory(monkeypatch,
        f'import sys;sys.stdin.read();sys.stderr.write({_DIAGNOSTIC_SECRET!r});sys.stdout.write({json.dumps(frame)!r});sys.exit(1)')
    with pytest.raises(dispatch.WorkerChildError) as error:
        await dispatch.run_child("orphan_calls", {})
    expected_phase = {"bootstrap": "child_bootstrap", "config": "child_config",
                      "protocol": "child_protocol", "execute": "child_execute"}[phase]
    assert (error.value.phase, error.value.code) == (expected_phase, code)
    assert f"phase={expected_phase} code={code} exit_code=1" in caplog.text
    assert processes[0].returncode == 1
    assert _DIAGNOSTIC_SECRET not in caplog.text + capsys.readouterr().out
    assert all(record.exc_info is None for record in _diagnostic_records(caplog))


@pytest.mark.parametrize("frame", [
    {"completed": False, "failure": {"phase": "execute", "code": _DIAGNOSTIC_SECRET}},
    {"completed": False, "failure": {"phase": _DIAGNOSTIC_SECRET, "code": "execution_failed"}},
    {"completed": False, "failure": {"phase": ["execute"], "code": _DIAGNOSTIC_SECRET}},
    {"completed": False, "failure": {"phase": "execute", "code": "execution_failed", "message": _DIAGNOSTIC_SECRET}},
    {"completed": False, "failure": {"phase": "execute", "code": "execution_failed"}, "payload": _DIAGNOSTIC_SECRET},
    {"completed": False, "failure": _DIAGNOSTIC_SECRET},
])
@pytest.mark.asyncio
async def test_malicious_child_failure_frames_fail_closed_without_log_injection(monkeypatch, caplog, frame):
    child_factory(monkeypatch, f'import sys;sys.stdin.read();sys.stdout.write({json.dumps(frame)!r});sys.exit(1)')
    with pytest.raises(dispatch.WorkerChildError) as error:
        await dispatch.run_child("orphan_calls", {})
    assert (error.value.phase, error.value.code) == ("child_protocol", "invalid_frame")
    assert "phase=child_protocol code=invalid_frame" in caplog.text
    assert _DIAGNOSTIC_SECRET not in caplog.text


@pytest.mark.parametrize("raw,returncode", [
    (b'{"completed":false,"failure":{"phase":"execute","code":"execution_failed","code":"execution_failed"}}', 1),
    (b'{"completed":false,"failure":{"phase":"execute","code":"execution_failed"}}', 0),
    (b'not-json-' + _DIAGNOSTIC_SECRET.encode(), 1),
    (b'{"completed":true,"continuation":{}}', 1),
])
def test_duplicate_malformed_and_inconsistent_failure_frames_rejected(raw, returncode):
    with pytest.raises(dispatch.WorkerChildError):
        dispatch._decode_completion("orphan_calls", raw, returncode)


def test_oversized_failure_frame_rejected_before_json_parse():
    raw = json.dumps({"completed": False, "failure": {"phase": "execute", "code": _DIAGNOSTIC_SECRET * 1000}}).encode()
    with pytest.raises(dispatch.WorkerChildError) as error:
        dispatch._decode_completion("orphan_calls", raw, 1)
    assert (error.value.phase, error.value.code) == ("child_protocol", "output_overflow")


@pytest.mark.asyncio
async def test_launch_exception_diagnostic_does_not_inspect_secret_exception(monkeypatch, caplog):
    class SecretException(RuntimeError):
        def __str__(self):
            raise AssertionError("Exception string must never be read")
    failure = SecretException(_DIAGNOSTIC_SECRET)
    monkeypatch.setattr(dispatch.asyncio, "create_subprocess_exec", AsyncMock(side_effect=failure))
    with pytest.raises(SecretException):
        await dispatch.run_child("orphan_calls", {})
    assert "phase=child_launch code=launch_failed" in caplog.text
    assert _DIAGNOSTIC_SECRET not in caplog.text
    assert "SecretException" not in caplog.text
    assert all(record.exc_info is None for record in _diagnostic_records(caplog))


@pytest.mark.parametrize("trusted_type,code", list(dispatch._PROVIDER_FAILURE_TYPES))
def test_provider_exception_classification_uses_fixed_codes_only(caplog, trusted_type, code):
    failure = trusted_type(_DIAGNOSTIC_SECRET)
    fixed_code = dispatch.classify_worker_failure(failure, "execution_failed")
    assert fixed_code == code
    dispatch.log_worker_failure("estimates", "child_execute", fixed_code)
    assert f"phase=child_execute code={code}" in caplog.text
    assert _DIAGNOSTIC_SECRET not in caplog.text
    assert trusted_type.__name__ not in caplog.text


@pytest.mark.parametrize("kind", ["subclass", "same_name"])
def test_provider_exception_classification_rejects_untrusted_types(kind):
    trusted_type = dispatch.google_errors.PermissionDenied
    base = trusted_type if kind == "subclass" else RuntimeError
    untrusted_type = type(trusted_type.__name__, (base,), {})
    failure = untrusted_type(_DIAGNOSTIC_SECRET)
    assert dispatch.classify_worker_failure(failure, "execution_failed") == "execution_failed"


@pytest.mark.asyncio
async def test_runner_bootstrap_failure_in_fresh_interpreter_is_bounded(monkeypatch, caplog):
    processes = child_factory(monkeypatch, f'''
import builtins, runpy, sys
assert not any(name.startswith("app.") for name in sys.modules)
original_import = builtins.__import__
def failed_import(name, *args, **kwargs):
    if name == "app.middleware.worker_auth":
        raise ImportError({_DIAGNOSTIC_SECRET!r})
    return original_import(name, *args, **kwargs)
builtins.__import__ = failed_import
sys.argv = ["app.worker_runner", "orphan_calls"]
runpy.run_module("app.worker_runner", run_name="__main__")
''')
    with pytest.raises(dispatch.WorkerChildError) as error:
        await dispatch.run_child("orphan_calls", {})
    assert (error.value.phase, error.value.code) == ("child_bootstrap", "import_failed")
    assert processes[0].returncode == 1
    assert _DIAGNOSTIC_SECRET not in caplog.text
    assert all(record.exc_info is None for record in _diagnostic_records(caplog))
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)


@pytest.mark.parametrize("stage,expected_code", [
    ("bootstrap", "import_failed"), ("config", "config_invalid"),
    ("input", "input_invalid"), ("execute", "execution_failed"),
    ("output", "output_invalid"), ("permission", "permission_denied"),
])
def test_runner_emits_bounded_failure_without_exception_details(monkeypatch, capsys, stage, expected_code):
    import builtins
    from app import worker_runner
    from app.middleware import worker_auth
    from google.api_core import exceptions as google_errors

    class SecretException(RuntimeError):
        def __str__(self):
            raise AssertionError("Exception string must never be read")

    monkeypatch.setattr(sys, "argv", ["app.worker_runner", "orphan_calls"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(
        b"invalid-" + _DIAGNOSTIC_SECRET.encode() if stage == "input" else b"{}")))
    monkeypatch.setattr(worker_auth, "validate_worker_configuration", lambda: None)
    async def fail_execute(job, continuation):
        raise SecretException(_DIAGNOSTIC_SECRET)
    async def invalid_output(job, continuation):
        return {"untrusted": _DIAGNOSTIC_SECRET}
    async def permission_failure(job, continuation):
        raise google_errors.PermissionDenied(_DIAGNOSTIC_SECRET)
    if stage == "bootstrap":
        original = builtins.__import__
        def failed_import(name, *args, **kwargs):
            if name == "app.middleware.worker_auth":
                raise SecretException(_DIAGNOSTIC_SECRET)
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", failed_import)
    elif stage == "config":
        def fail_config(): raise SecretException(_DIAGNOSTIC_SECRET)
        monkeypatch.setattr(worker_auth, "validate_worker_configuration", fail_config)
    elif stage == "output":
        monkeypatch.setattr(dispatch, "execute_once", invalid_output)
    elif stage == "permission":
        monkeypatch.setattr(dispatch, "execute_once", permission_failure)
    else:
        monkeypatch.setattr(dispatch, "execute_once", fail_execute)
    assert worker_runner.main() == 1
    captured = capsys.readouterr()
    assert len(captured.out.encode()) <= dispatch.MAX_PROTOCOL_BYTES
    assert _DIAGNOSTIC_SECRET not in captured.out + captured.err
    assert "SecretException" not in captured.out + captured.err
    phase = {"input": "protocol", "output": "protocol", "permission": "execute"}.get(stage, stage)
    assert json.loads(captured.out) == {"completed": False, "failure": {"phase": phase, "code": expected_code}}
