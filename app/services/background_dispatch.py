"""Fixed dispatch table and bounded, awaited child-process supervision."""

import asyncio
import json
import logging
import os
import signal
import sys

from google.api_core import exceptions as google_errors

from app.db.background_jobs import JOB_INTERVALS, validate_continuation as _validate_continuation

WALL_BUDGET_SECONDS = 240.0
TERMINATE_GRACE_SECONDS = 2.0
MAX_PROTOCOL_BYTES = 4096
_CHILDREN: set = set()
_CLEANUPS: dict = {}
_CHILD_JOBS: dict = {}
_CHILD_FAILURES: dict = {}

logger = logging.getLogger("app.background_worker")

# External mode deliberately carries only jobs with a bounded, persisted scan
# cursor or a fixed per-pass action cap. Configuration rejects enabled purge
# and provider-recovery loops until each has its own reviewed contract.
EXTERNAL_JOB_NAMES = frozenset({
    "orphan_calls", "call_retention", "lapsed_trials", "expired_contractors",
    "post_calls", "estimates",
})

# Exact trusted class identities select fixed codes. Exception names, messages,
# attributes and tracebacks are never inspected or serialized.
_PROVIDER_FAILURE_TYPES = (
    (google_errors.PermissionDenied, "permission_denied"),
    (google_errors.Unauthenticated, "authentication_failed"),
    (google_errors.FailedPrecondition, "precondition_failed"),
    (google_errors.Aborted, "contention"),
    (google_errors.ServiceUnavailable, "transport_failed"),
    (google_errors.DeadlineExceeded, "deadline_exceeded"),
    (google_errors.ResourceExhausted, "resource_exhausted"),
    (TimeoutError, "deadline_exceeded"),
)
_PROVIDER_FAILURE_CODES = frozenset(code for _, code in _PROVIDER_FAILURE_TYPES)


def classify_worker_failure(error: BaseException, default: str) -> str:
    for trusted_type, code in _PROVIDER_FAILURE_TYPES:
        if type(error) is trusted_type:
            return code
    return default

# Diagnostic values are fixed vocabulary, never exception text, class names,
# caller identifiers, provider data or arbitrary child-process output.
DIAGNOSTIC_CODES = {
    "bootstrap": frozenset({"import_failed"}),
    "config": frozenset({"config_invalid"}),
    "admission": frozenset({"store_unavailable", "admission_failed", "admission_invalid",
                            "admission_blocked", "admission_settle_failed", "cutover_blocked", "cancelled"}),
    "child_launch": frozenset({"launch_failed", "cancelled"}),
    "child_bootstrap": frozenset({"import_failed"}),
    "child_config": frozenset({"config_invalid"}),
    "child_protocol": frozenset({"input_invalid", "output_invalid", "output_overflow",
                                  "invalid_frame", "invalid_continuation", "exchange_failed", "cancelled"}),
    "child_execute": frozenset({"execution_failed", "dispatch_failed", "cancelled"}),
    "child_exit": frozenset({"nonzero_exit"}),
    "child_timeout": frozenset({"wall_budget_exceeded"}),
    "child_cleanup": frozenset({"grace_exceeded", "reaped", "cleanup_failed"}),
    "completion": frozenset({"completion_write_failed", "completion_fenced", "cancelled"}),
    "attention": frozenset({"attention_write_failed", "attention_fenced"}),
}
for _phase in ("admission", "child_launch", "child_execute", "completion", "attention"):
    DIAGNOSTIC_CODES[_phase] |= _PROVIDER_FAILURE_CODES

CHILD_FAILURE_CODES = {
    "bootstrap": frozenset({"import_failed"}),
    "config": frozenset({"config_invalid"}),
    "protocol": frozenset({"input_invalid", "output_invalid"}),
    "execute": frozenset({"execution_failed"}) | _PROVIDER_FAILURE_CODES,
}


def log_worker_failure(job, phase, code, *, returncode=None, timed_out=False, termination="none"):
    """Only fixed vocabulary and bounded numeric metadata cross the log boundary."""
    safe_job = job if type(job) is str and job in JOB_INTERVALS else "unknown"
    safe_phase = phase if type(phase) is str and phase in DIAGNOSTIC_CODES else "unknown"
    safe_code = (code if type(code) is str and code in DIAGNOSTIC_CODES.get(safe_phase, ())
                 else "unknown")
    safe_exit = returncode if type(returncode) is int and -128 <= returncode <= 255 else "unknown"
    safe_termination = (termination if type(termination) is str
                        and termination in {"none", "terminated", "killed", "reaped", "failed"}
                        else "unknown")
    logger.warning(
        "worker_failure job=%s phase=%s code=%s exit_code=%s timeout=%s termination=%s",
        safe_job, safe_phase, safe_code, safe_exit,
        "true" if timed_out is True else "false", safe_termination,
    )


class WorkerChildError(RuntimeError):
    def __init__(self, phase: str, code: str):
        # Safe exception text also preserves the existing protocol-overflow test.
        super().__init__("Worker protocol overflow" if code == "output_overflow" else "Worker child failed")
        self.phase = phase
        self.code = code


def _decode_completion(job: str, raw: bytes, returncode: int) -> dict:
    if len(raw) > MAX_PROTOCOL_BYTES:
        raise WorkerChildError("child_protocol", "output_overflow")
    duplicate_fields = False

    def object_pairs(pairs):
        nonlocal duplicate_fields
        value = {}
        for key, item in pairs:
            if key in value:
                duplicate_fields = True
            value[key] = item
        return value

    if not raw and returncode != 0:
        raise WorkerChildError("child_exit", "nonzero_exit")
    try:
        output = json.loads(raw, object_pairs_hook=object_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerChildError("child_protocol", "invalid_frame") from None
    if isinstance(output, dict) and output.get("completed") is False:
        failure = output.get("failure")
        if (duplicate_fields or set(output) != {"completed", "failure"} or not isinstance(failure, dict)
                or set(failure) != {"phase", "code"}
                or type(failure.get("phase")) is not str
                or type(failure.get("code")) is not str
                or failure["code"] not in CHILD_FAILURE_CODES.get(failure["phase"], ())
                or returncode == 0):
            raise WorkerChildError("child_protocol", "invalid_frame")
        phase = {"bootstrap": "child_bootstrap", "config": "child_config",
                 "protocol": "child_protocol", "execute": "child_execute"}[failure["phase"]]
        raise WorkerChildError(phase, failure["code"])
    if returncode != 0:
        raise WorkerChildError("child_exit", "nonzero_exit")
    if not isinstance(output, dict) or set(output) != {"completed", "continuation"} or output["completed"] is not True:
        raise WorkerChildError("child_protocol", "invalid_frame")
    try:
        return validate_continuation(job, output["continuation"])
    except (ValueError, TypeError, RecursionError):
        raise WorkerChildError("child_protocol", "invalid_continuation") from None



def job_enabled(job: str) -> bool:
    return job in EXTERNAL_JOB_NAMES and job in JOB_INTERVALS


def validate_continuation(job: str, continuation: object) -> dict:
    return _validate_continuation(job, continuation)


async def execute_once(job: str, continuation: dict) -> dict:
    """Child-only one-pass dispatcher; never imports the public API."""
    if not job_enabled(job):
        raise ValueError("Unavailable background job")
    validate_continuation(job, continuation)
    from app.services import background_maintenance as maintenance

    if job == "orphan_calls":
        return await maintenance.orphan_calls_once(continuation=continuation, page_size=25)
    if job == "call_retention":
        await maintenance.call_retention_once(strict=True)
    elif job == "lapsed_trials":
        return await maintenance.lapsed_trials_once(
            strict=True, continuation=continuation, page_size=25,
        )
    elif job == "expired_contractors":
        return await maintenance.expired_contractors_once(
            continuation=continuation, page_size=5,
        )
    elif job == "post_calls":
        from app.services.post_call_handoff import run_bounded_post_calls_once
        return await run_bounded_post_calls_once(continuation)
    elif job == "estimates":
        from app.services.estimate_worker import run_pending_estimates_once
        return await run_pending_estimates_once(page_size=25, max_analyses=1, continuation=continuation)
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
        if process in _CHILD_FAILURES:
            log_worker_failure(_CHILD_JOBS.get(process), "child_cleanup", "reaped",
                               returncode=process.returncode, timed_out=_CHILD_FAILURES[process],
                               termination="terminated")
    except asyncio.TimeoutError:
        log_worker_failure(_CHILD_JOBS.get(process), "child_cleanup", "grace_exceeded",
                           returncode=process.returncode, timed_out=_CHILD_FAILURES.get(process, False),
                           termination="killed")
        _signal_child(process, signal.SIGKILL)
        await _drain_and_wait(process)
        log_worker_failure(_CHILD_JOBS.get(process), "child_cleanup", "reaped",
                           returncode=process.returncode, timed_out=_CHILD_FAILURES.get(process, False),
                           termination="reaped")


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
    try:
        payload = json.dumps(validate_continuation(job, continuation)).encode()
    except Exception:
        log_worker_failure(job, "child_protocol", "input_invalid")
        raise
    process = None
    phase = "child_launch"

    async def launch_and_exchange():
        nonlocal process, phase
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
            _CHILD_JOBS[process] = job
            raise
        _CHILDREN.add(process)
        _CHILD_JOBS[process] = job
        phase = "child_protocol"
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
            raise WorkerChildError("child_protocol", "output_overflow")
        await process.wait()
        return _decode_completion(job, raw, process.returncode)

    try:
        return await asyncio.wait_for(launch_and_exchange(), WALL_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        if process is not None:
            _CHILD_FAILURES[process] = True
        log_worker_failure(job, "child_timeout", "wall_budget_exceeded",
                           returncode=process.returncode if process is not None else None, timed_out=True)
        raise
    except asyncio.CancelledError:
        if process is not None:
            _CHILD_FAILURES[process] = False
        log_worker_failure(job, phase, "cancelled",
                           returncode=process.returncode if process is not None else None)
        raise
    except WorkerChildError as error:
        if process is not None:
            _CHILD_FAILURES[process] = False
        log_worker_failure(job, error.phase, error.code,
                           returncode=process.returncode if process is not None else None)
        raise
    except Exception as error:
        if process is not None:
            _CHILD_FAILURES[process] = False
        default = "launch_failed" if phase == "child_launch" else "exchange_failed"
        code = classify_worker_failure(error, default) if phase == "child_launch" else default
        log_worker_failure(job, phase, code,
                           returncode=process.returncode if process is not None else None)
        raise
    finally:
        if process is not None:
            try:
                await _finish_cleanup(process)
            except Exception:
                log_worker_failure(job, "child_cleanup", "cleanup_failed",
                                   returncode=process.returncode, termination="failed")
                raise
            finally:
                _CHILDREN.discard(process)
                _CLEANUPS.pop(process, None)
                _CHILD_JOBS.pop(process, None)
                _CHILD_FAILURES.pop(process, None)
