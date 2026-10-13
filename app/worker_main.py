"""Private staging HTTP worker. Its only routes are health and fixed tasks."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app.db.background_jobs import BackgroundJobStore, JOB_INTERVALS
from app.middleware.worker_auth import validate_worker_configuration, verify_worker_identity
from app.services.background_dispatch import job_enabled, run_child, shutdown_children


@asynccontextmanager
async def lifespan(app):
    validate_worker_configuration()
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
        await _settle_task(asyncio.create_task(asyncio.to_thread(
            store.finish, job, nonce, completed=False)))
    except Exception:
        # Failed finalization leaves the running entry blocked too.
        pass


@app.post("/tasks/{job}", dependencies=[Depends(verify_worker_identity)])
async def task(job: str):
    if job not in JOB_INTERVALS:
        raise HTTPException(status_code=404, detail="Unknown worker job")
    if not job_enabled(job):
        return {"status": "disabled"}
    store = get_job_store()
    # Admission uses synchronous Firestore with transaction retries. Await it
    # even when the HTTP request is cancelled: an accepted claim must be fenced
    # as uncertain, never abandoned while its SDK thread may still commit.
    admission_task = asyncio.create_task(asyncio.to_thread(store.admit, job))
    try:
        admission = await asyncio.shield(admission_task)
    except asyncio.CancelledError:
        try:
            admission = await _settle_task(admission_task)
            if admission.status == "admitted":
                await _mark_attention(store, job, admission.nonce)
        except Exception:
            pass
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Worker admission unavailable") from None
    if admission.status == "needs_attention":
        raise HTTPException(status_code=409, detail="Worker job requires operator attention")
    if admission.status in {"not_due", "in_progress"}:
        return {"status": admission.status}
    if admission.status != "admitted" or not admission.nonce:
        raise HTTPException(status_code=503, detail="Invalid worker admission")
    completion = None
    try:
        continuation = await run_child(job, admission.continuation or {})
        completion = asyncio.create_task(asyncio.to_thread(
            store.finish, job, admission.nonce, completed=True, continuation=continuation))
        accepted = await asyncio.shield(completion)
        if not accepted:
            raise RuntimeError("Worker completion fenced")
    except asyncio.CancelledError:
        # If commit has already started, settle its bounded SDK operation first;
        # do not race a still-running commit thread with an attention update.
        if completion is not None:
            try:
                await _settle_task(completion)
            except Exception:
                pass
        await _mark_attention(store, job, admission.nonce)
        raise
    except Exception:
        await _mark_attention(store, job, admission.nonce)
        raise HTTPException(status_code=503, detail="Worker job requires operator attention") from None
    return {"status": "completed"}
