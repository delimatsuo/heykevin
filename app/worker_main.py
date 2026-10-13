"""Private staging HTTP worker. Its only routes are health and fixed tasks."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app.db.background_jobs import BackgroundJobStore, JOB_INTERVALS
from app.middleware.worker_auth import validate_worker_configuration, verify_worker_identity
from app.services.background_dispatch import (
    WorkerChildError, classify_worker_failure, job_enabled, log_worker_failure, run_child, shutdown_children,
)


@asynccontextmanager
async def lifespan(app):
    try:
        validate_worker_configuration()
    except Exception:
        log_worker_failure("unknown", "config", "config_invalid")
        raise RuntimeError("Worker configuration invalid") from None
    try:
        yield
    finally:
        await shutdown_children()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def get_job_store():
    from app.db.firestore_client import get_firestore_client
    return BackgroundJobStore(get_firestore_client())


@app.get("/health")
async def health():
    return {"status": "ok", "worker_mode": "external"}


async def _settle_task(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _mark_attention(store, job, nonce):
    try:
        accepted = await _settle_task(asyncio.create_task(asyncio.to_thread(
            store.finish, job, nonce, completed=False)))
        if not accepted:
            log_worker_failure(job, "attention", "attention_fenced")
    except Exception as error:
        # Failed finalization leaves the running entry blocked too.
        log_worker_failure(job, "attention", classify_worker_failure(error, "attention_write_failed"))


@app.post("/tasks/{job}", dependencies=[Depends(verify_worker_identity)])
async def task(job: str):
    if job not in JOB_INTERVALS:
        raise HTTPException(status_code=404, detail="Unknown worker job")
    if not job_enabled(job):
        return {"status": "disabled"}
    try:
        store = get_job_store()
    except Exception as error:
        log_worker_failure(job, "admission", classify_worker_failure(error, "store_unavailable"))
        raise RuntimeError("Worker datastore unavailable") from None
    # Admission uses synchronous Firestore with transaction retries. Await it
    # even when the HTTP request is cancelled: an accepted claim must be fenced
    # as uncertain, never abandoned while its SDK thread may still commit.
    admission_task = asyncio.create_task(asyncio.to_thread(store.admit, job))
    try:
        admission = await asyncio.shield(admission_task)
    except asyncio.CancelledError:
        log_worker_failure(job, "admission", "cancelled")
        try:
            admission = await _settle_task(admission_task)
            if admission.status == "admitted":
                await _mark_attention(store, job, admission.nonce)
        except Exception as error:
            log_worker_failure(job, "admission", classify_worker_failure(error, "admission_settle_failed"))
        raise
    except Exception as error:
        log_worker_failure(job, "admission", classify_worker_failure(error, "admission_failed"))
        raise HTTPException(status_code=503, detail="Worker admission unavailable") from None
    if admission.status == "needs_attention":
        log_worker_failure(job, "admission", "admission_blocked")
        raise HTTPException(status_code=409, detail="Worker job requires operator attention")
    if admission.status in {"not_due", "in_progress"}:
        return {"status": admission.status}
    if admission.status != "admitted" or not admission.nonce:
        log_worker_failure(job, "admission", "admission_invalid")
        raise HTTPException(status_code=503, detail="Invalid worker admission")
    completion = None
    phase, failure_code = "child_execute", "dispatch_failed"
    try:
        continuation = await run_child(job, admission.continuation or {})
        phase, failure_code = "completion", "completion_write_failed"
        completion = asyncio.create_task(asyncio.to_thread(
            store.finish, job, admission.nonce, completed=True, continuation=continuation))
        accepted = await asyncio.shield(completion)
        if not accepted:
            failure_code = "completion_fenced"
            raise RuntimeError("Worker completion fenced")
    except asyncio.CancelledError:
        log_worker_failure(job, phase, "cancelled")
        # If commit has already started, settle its bounded SDK operation first;
        # do not race a still-running commit thread with an attention update.
        if completion is not None:
            try:
                await _settle_task(completion)
            except Exception as error:
                log_worker_failure(job, "completion", classify_worker_failure(error, "completion_write_failed"))
        await _mark_attention(store, job, admission.nonce)
        raise
    except Exception as error:
        if phase == "child_execute" and type(error) is WorkerChildError:
            log_worker_failure(job, error.phase, error.code)
        elif phase == "child_execute" and type(error) is TimeoutError:
            log_worker_failure(job, "child_timeout", "wall_budget_exceeded", timed_out=True)
        else:
            code = classify_worker_failure(error, failure_code) if phase == "completion" else failure_code
            log_worker_failure(job, phase, code)
        await _mark_attention(store, job, admission.nonce)
        raise HTTPException(status_code=503, detail="Worker job requires operator attention") from None
    return {"status": "completed"}
