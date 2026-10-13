"""Fixed dispatch table and bounded, awaited child-process supervision."""

import asyncio
import json
import os
import signal
import sys

from app.config import settings
from app.db.background_jobs import JOB_INTERVALS

WALL_BUDGET_SECONDS = 240.0
TERMINATE_GRACE_SECONDS = 2.0
MAX_PROTOCOL_BYTES = 4096
_CHILDREN: set = set()
_CLEANUPS: dict = {}


def job_enabled(job: str) -> bool:
    if job not in JOB_INTERVALS:
        return False
    flags = {"data_purge": "purge_enabled", "service_requests": "service_request_recovery_enabled",
             "jobber_disconnect": "jobber_disconnect_recovery_enabled"}
    return job not in flags or getattr(settings, flags[job]) is True


def validate_continuation(job: str, continuation: object) -> dict:
    if not isinstance(continuation, dict):
        raise ValueError("Invalid continuation")
    allowed = {"post_calls": {"pending", "in_progress"}, "estimates": {"processing"}}.get(job, set())
    if not set(continuation).issubset(allowed):
        raise ValueError("Unexpected continuation")
    for value in continuation.values():
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 512
                                  or "/" in value or any(ord(c) < 32 for c in value)):
            raise ValueError("Invalid continuation cursor")
    if len(json.dumps(continuation).encode()) > MAX_PROTOCOL_BYTES:
        raise ValueError("Oversized continuation")
    return continuation


async def execute_once(job: str, continuation: dict) -> dict:
    """Child-only one-pass dispatcher; never imports the public API."""
    if not job_enabled(job):
        raise ValueError("Unavailable background job")
    validate_continuation(job, continuation)
    from app.services import background_maintenance as maintenance

    simple = {"orphan_calls": maintenance.orphan_calls_once,
              "call_retention": maintenance.call_retention_once,
              "expired_contractors": maintenance.expired_contractors_once,
              "data_purge": maintenance.data_purge_once}
    if job in simple:
        await simple[job]()
    elif job == "lapsed_trials":
        await maintenance.lapsed_trials_once(strict=True)
    elif job == "post_calls":
        from app.services.post_call_handoff import run_bounded_post_calls_once
        return await run_bounded_post_calls_once(continuation)
    elif job == "estimates":
        from app.services.estimate_worker import run_pending_estimates_once
        return await run_pending_estimates_once(page_size=25, max_analyses=1, continuation=continuation)
    elif job == "service_requests":
        from app.services.service_request_recovery import ServiceRequestRecoveryWorker, FirestoreServiceRequestRepository
        await ServiceRequestRecoveryWorker(FirestoreServiceRequestRepository(), batch_size=1).run_once()
    elif job == "jobber_disconnect":
        from app.services.jobber_disconnect_recovery import run_jobber_disconnect_recovery_once
        await run_jobber_disconnect_recovery_once()
    return {}


async def _drain_and_wait(process):
    if process.stdout is not None:
        while await process.stdout.read(4096):
            pass
    await process.wait()


def _signal_child(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


async def _settle_child(process):
    _signal_child(process, signal.SIGTERM)

    async def settle_group():
        await _drain_and_wait(process)
        # EOF and leader exit do not prove descendants have stopped: a child
        # can close stdout and ignore TERM. The new session owns this group.
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(.01)

    try:
        await asyncio.wait_for(settle_group(), timeout=TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _signal_child(process, signal.SIGKILL)
        await _drain_and_wait(process)


async def _finish_cleanup(process):
    """Repeated request cancellation cannot interrupt terminate/kill/reap."""
    cleanup = _CLEANUPS.get(process)
    if cleanup is None:
        cleanup = asyncio.create_task(_settle_child(process))
        _CLEANUPS[process] = cleanup
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    await cleanup


async def shutdown_children():
    await asyncio.gather(*(_finish_cleanup(child) for child in tuple(_CHILDREN)))


async def run_child(job: str, continuation: dict) -> dict:
    if not job_enabled(job):
        raise ValueError("Unavailable background job")
    payload = json.dumps(validate_continuation(job, continuation)).encode()
    process = None

    async def launch_and_exchange():
        nonlocal process
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.worker_runner", job,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            limit=MAX_PROTOCOL_BYTES + 1,
        ))
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Spawning is atomic at the OS boundary; retrieve its handle before
            # propagating cancellation so a newly-created child cannot escape.
            while not launch.done():
                try:
                    await asyncio.shield(launch)
                except asyncio.CancelledError:
                    continue
            process = launch.result()
            _CHILDREN.add(process)
            raise
        _CHILDREN.add(process)
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        raw = await process.stdout.read(MAX_PROTOCOL_BYTES + 1)
        # read(n) may return a partial frame; continue without exceeding the cap.
        while len(raw) <= MAX_PROTOCOL_BYTES:
            part = await process.stdout.read(MAX_PROTOCOL_BYTES + 1 - len(raw))
            if not part:
                break
            raw += part
        if len(raw) > MAX_PROTOCOL_BYTES:
            raise RuntimeError("Worker protocol overflow")
        await process.wait()
        if process.returncode != 0:
            raise RuntimeError("Worker child failed")
        output = json.loads(raw)
        if not isinstance(output, dict) or set(output) != {"completed", "continuation"} or output["completed"] is not True:
            raise RuntimeError("Invalid worker completion")
        return validate_continuation(job, output["continuation"])

    try:
        return await asyncio.wait_for(launch_and_exchange(), WALL_BUDGET_SECONDS)
    finally:
        if process is not None:
            try:
                await _finish_cleanup(process)
            finally:
                _CHILDREN.discard(process)
                _CLEANUPS.pop(process, None)
