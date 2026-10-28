"""Kevin - AI Call Screening Assistant.

FastAPI application entry point.
"""

import asyncio
import os
import signal

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import (
    settings,
    staging_native_live_safety_controls_enabled,
    validate_runtime_safety,
)
from app.middleware.auth import verify_api_token
from app.utils.logging import setup_logging, get_logger
from app.webhooks.twilio_incoming import router as twilio_router
from app.webhooks.media_stream import router as media_stream_router
from app.webhooks.relay_stream import router as relay_stream_router
from app.api.contractors import public_router as contractors_public_router
from app.api.contacts import router as contacts_router
from app.api.calls import router as calls_router
from app.api.knowledge import router as knowledge_router
from app.api.settings import router as settings_router
from app.api.voip import router as voip_router
from app.api.contractors import router as contractors_router
from app.api.vcard import router as vcard_router
from app.api.estimates import router as estimates_router
from app.api.integrations import router as integrations_router
from app.api.forwarding import router as forwarding_router
from app.api.subscription import router as subscription_router
from app.webhooks.appstore import router as appstore_router
from app.webhooks.jobber import router as jobber_webhook_router
from app.api.admin import router as admin_router
from app.api.app_version import router as app_version_router
from app.api.acquisition import router as acquisition_router
from app.api.markets import router as markets_router
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Initialize logging
setup_logging(settings.log_level)
logger = get_logger(__name__)

# Graceful shutdown flag
_shutting_down = False
_post_call_worker_task: asyncio.Task | None = None
_service_request_recovery_task: asyncio.Task | None = None
_estimate_worker_task: asyncio.Task | None = None
_jobber_disconnect_recovery_task: asyncio.Task | None = None
_housekeeping_tasks: dict[str, asyncio.Task] = {}
_cutover_task: asyncio.Task | None = None
_effective_background_worker_mode = "embedded"

app = FastAPI(
    title="Kevin",
    description="AI-powered call screening assistant",
    version="0.1.0",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
)

# CORS — restrictive by default
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://heykevin.one"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Webhook routes
app.include_router(twilio_router)
app.include_router(media_stream_router)
app.include_router(relay_stream_router)
app.include_router(contractors_public_router)
app.include_router(contacts_router)
app.include_router(calls_router)
app.include_router(knowledge_router)
app.include_router(settings_router)
app.include_router(voip_router)
app.include_router(contractors_router)
app.include_router(vcard_router)
app.include_router(estimates_router)
app.include_router(integrations_router)
app.include_router(forwarding_router)
app.include_router(subscription_router)
app.include_router(appstore_router)
app.include_router(jobber_webhook_router)
app.include_router(admin_router)
app.include_router(app_version_router)
app.include_router(acquisition_router)
app.include_router(markets_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/admin")
async def admin_page():
    return FileResponse("app/static/admin.html")


@app.get("/health")
async def health():
    """Health check with non-secret deploy identity."""
    staging_live_safety = staging_native_live_safety_controls_enabled()
    return {
        "status": "ok",
        "worker_mode": _effective_background_worker_mode,
        "environment": settings.environment,
        "service": os.getenv("K_SERVICE", ""),
        "revision": os.getenv("K_REVISION", ""),
        "deploy_sha": os.getenv("DEPLOY_SHA", ""),
        "gemini_live_staging_safety_controls_enabled": staging_live_safety,
        "gemini_live_model_tools_enabled": not staging_live_safety,
        "gemini_live_automatic_terminal_actions_enabled": not staging_live_safety,
    }


if settings.environment == "development":

    @app.delete("/debug/twilio-number/{phone}", dependencies=[Depends(verify_api_token)])
    async def debug_release_number(phone: str):
        """Debug: release an orphaned Twilio number."""
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        numbers = client.incoming_phone_numbers.list(phone_number=f"+{phone}", limit=1)
        if numbers:
            numbers[0].delete()
            return {"status": "released", "number": phone}
        return {"status": "not_found"}

    @app.get("/debug/twilio-calls", dependencies=[Depends(verify_api_token)])
    async def debug_twilio_calls():
        """Debug: check recent Twilio call history."""
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        calls = client.calls.list(limit=10)
        result = []
        for c in calls:
            result.append({
                "sid": c.sid,
                "from": c.from_formatted,
                "to": c.to_formatted,
                "status": c.status,
                "direction": c.direction,
                "start_time": str(c.start_time),
                "duration": c.duration,
            })
        return {"calls": result}

    @app.get("/debug/twilio-numbers", dependencies=[Depends(verify_api_token)])
    async def debug_twilio_numbers():
        """Debug: check Twilio number webhook configuration."""
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        numbers = client.incoming_phone_numbers.list(limit=20)
        result = []
        for n in numbers:
            result.append({
                "number": n.phone_number,
                "voice_url": n.voice_url,
                "voice_method": n.voice_method,
                "voice_application_sid": n.voice_application_sid,
                "voice_fallback_url": n.voice_fallback_url,
                "sms_url": n.sms_url,
                "status_callback": n.status_callback,
            })
        return {"numbers": result}


async def _periodic_maintenance(name: str, interval: int, *, immediate: bool = False):
    from app.services import background_maintenance

    while True:
        if not immediate:
            await asyncio.sleep(interval)
        try:
            await getattr(background_maintenance, name)()
        except Exception as error:
            logger.warning("Background maintenance failed: %s", type(error).__name__)
        if immediate:
            await asyncio.sleep(interval)


async def _orphan_call_cleanup():
    await _periodic_maintenance("orphan_calls_once", 300)


async def _call_retention_sweep():
    await _periodic_maintenance("call_retention_once", 6 * 3600)


async def _lapsed_trial_sweep():
    await _periodic_maintenance("lapsed_trials_once", 6 * 3600)


async def _expired_contractor_cleanup():
    await _periodic_maintenance("expired_contractors_once", 6 * 3600)


async def _data_purge_sweep():
    await _periodic_maintenance("data_purge_once", 6 * 3600, immediate=True)


def _start_embedded_background_loops():
    """Start legacy loops only after the shared cutover record permits them."""
    global _post_call_worker_task, _service_request_recovery_task, _estimate_worker_task, _jobber_disconnect_recovery_task

    for factory in (
        _orphan_call_cleanup, _call_retention_sweep, _lapsed_trial_sweep,
        _expired_contractor_cleanup, _data_purge_sweep,
    ):
        task = _housekeeping_tasks.get(factory.__name__)
        if task is None or task.done():
            _housekeeping_tasks[factory.__name__] = asyncio.create_task(factory())
    from app.services.post_call_handoff import post_call_worker_loop

    if _post_call_worker_task is None or _post_call_worker_task.done():
        _post_call_worker_task = asyncio.create_task(post_call_worker_loop())
    from app.services.estimate_worker import estimate_worker_loop

    if _estimate_worker_task is None or _estimate_worker_task.done():
        _estimate_worker_task = asyncio.create_task(estimate_worker_loop())
    from app.services.service_request_recovery import service_request_recovery_worker_loop

    if (
        settings.service_request_recovery_enabled is True
        and (_service_request_recovery_task is None or _service_request_recovery_task.done())
    ):
        # Recovery processes only already-durable intents. Initiating new
        # mutations remains gated per contractor at the voice-tool boundary.
        _service_request_recovery_task = asyncio.create_task(service_request_recovery_worker_loop())

    from app.services.jobber_disconnect_recovery import jobber_disconnect_recovery_worker_loop

    if (
        getattr(settings, "jobber_disconnect_recovery_enabled", False) is True
        and (_jobber_disconnect_recovery_task is None or _jobber_disconnect_recovery_task.done())
    ):
        _jobber_disconnect_recovery_task = asyncio.create_task(jobber_disconnect_recovery_worker_loop())


def _get_background_cutover_store():
    from app.db.background_jobs import BackgroundWorkerCutoverStore
    from app.db.firestore_client import get_firestore_client

    return BackgroundWorkerCutoverStore(get_firestore_client())


async def _wait_for_embedded_cutover(store, *, fallback: bool) -> None:
    """Drain any admitted external child before restarting embedded loops."""
    global _effective_background_worker_mode

    while True:
        try:
            ready = await asyncio.to_thread(store.begin_embedded)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Background-worker cutover read failed: %s", type(error).__name__)
            await asyncio.sleep(5)
            continue
        if ready:
            _start_embedded_background_loops()
            _effective_background_worker_mode = "embedded_fallback" if fallback else "embedded"
            return
        await asyncio.sleep(5)


async def _watch_external_cutover(store, generation: str) -> None:
    """Fall back to embedded processing after an external lease expires."""
    while True:
        await asyncio.sleep(15)
        try:
            active = await asyncio.to_thread(store.external_active, generation)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Background-worker lease read failed: %s", type(error).__name__)
            continue
        if not active:
            await _wait_for_embedded_cutover(store, fallback=True)
            return


async def _configure_background_workers() -> None:
    """Select an effective mode without permitting an unchecked handoff."""
    global _cutover_task, _effective_background_worker_mode

    # External mode is prohibited outside staging. Preserve normal embedded
    # operation in production, development, and tests without an extra control
    # datastore dependency.
    if settings.environment != "staging":
        _start_embedded_background_loops()
        _effective_background_worker_mode = "embedded"
        return

    try:
        store = _get_background_cutover_store()
    except Exception as error:
        logger.warning("Background-worker cutover store unavailable: %s", type(error).__name__)
        _effective_background_worker_mode = "cutover_unavailable"
        return

    if settings.background_worker_mode == "external":
        try:
            if await asyncio.to_thread(
                store.activate_external, settings.background_worker_cutover_generation,
            ):
                _effective_background_worker_mode = "external"
                _cutover_task = asyncio.create_task(_watch_external_cutover(
                    store, settings.background_worker_cutover_generation,
                ))
                return
        except Exception as error:
            logger.warning("Background-worker external activation rejected: %s", type(error).__name__)

    # A missing, expired, or incomplete external record is never permission to
    # stop recovery. This transaction fences new external claims first; if a
    # previously admitted child is still running, wait for it instead of racing
    # legacy loops against the same work.
    try:
        ready = await asyncio.to_thread(store.begin_embedded)
    except Exception as error:
        logger.warning("Background-worker embedded cutover unavailable: %s", type(error).__name__)
        _effective_background_worker_mode = "cutover_unavailable"
        return
    fallback = settings.background_worker_mode == "external"
    if ready:
        _start_embedded_background_loops()
        _effective_background_worker_mode = "embedded_fallback" if fallback else "embedded"
        return
    _effective_background_worker_mode = "draining"
    _cutover_task = asyncio.create_task(_wait_for_embedded_cutover(store, fallback=fallback))


@app.on_event("startup")
async def startup():
    # Validate required config
    required = ['twilio_account_sid', 'twilio_auth_token', 'anthropic_api_key',
                'deepgram_api_key', 'elevenlabs_api_key', 'api_bearer_token']
    missing = [k for k in required if not getattr(settings, k, None)]
    if missing:
        raise RuntimeError(f"Missing required config: {', '.join(missing)}")

    validate_runtime_safety()

    await _configure_background_workers()

    # F-21: drop the redacted Twilio number from startup logs entirely. Even
    # the last 4 digits are unnecessary signal in centralised logs and the
    # number is verifiable through admin tooling when actually needed.
    logger.info("Kevin starting up", extra={"environment": settings.environment})


@app.on_event("shutdown")
async def shutdown():
    global _post_call_worker_task, _service_request_recovery_task, _estimate_worker_task, _jobber_disconnect_recovery_task, _cutover_task

    tasks = list(_housekeeping_tasks.values()) + [
        _post_call_worker_task, _estimate_worker_task,
        _service_request_recovery_task, _jobber_disconnect_recovery_task, _cutover_task,
    ]
    tasks = [task for task in tasks if task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _housekeeping_tasks.clear()
    _post_call_worker_task = _estimate_worker_task = None
    _service_request_recovery_task = _jobber_disconnect_recovery_task = None
    _cutover_task = None

    from app.db.jobber_disconnect_transactions import drain_jobber_operations
    await drain_jobber_operations()
    logger.info("Kevin shutting down — finishing in-flight requests")


def _handle_sigterm(*args):
    """Handle SIGTERM gracefully — stop accepting new requests."""
    global _shutting_down
    _shutting_down = True
    logger.info("SIGTERM received — initiating graceful shutdown")


signal.signal(signal.SIGTERM, _handle_sigterm)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
