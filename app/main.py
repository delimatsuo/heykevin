"""Kevin - AI Call Screening Assistant.

FastAPI application entry point.
"""

import asyncio
from contextlib import suppress
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
from app.api.admin import router as admin_router
from app.api.app_version import router as app_version_router
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
app.include_router(admin_router)
app.include_router(app_version_router)
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


async def _orphan_call_cleanup():
    """Periodically clean up stale entries from RTDB /active_calls.

    Runs every 5 minutes. Deletes any entry where state_updated_at
    is more than 2 hours old.
    """
    import time
    from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH

    MAX_AGE = 7200  # 2 hours in seconds

    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            _init_firebase()
            from firebase_admin import db as rtdb

            loop = asyncio.get_event_loop()
            ref = rtdb.reference(ACTIVE_CALLS_PATH)
            all_calls = await loop.run_in_executor(None, ref.get)

            if not all_calls or not isinstance(all_calls, dict):
                continue

            now = time.time()
            cleaned = 0
            for call_sid, call_data in all_calls.items():
                if not isinstance(call_data, dict):
                    continue
                updated_at = call_data.get("state_updated_at", 0)
                if updated_at and now - updated_at > MAX_AGE:
                    child_ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
                    await loop.run_in_executor(None, child_ref.delete)
                    cleaned += 1

            if cleaned:
                logger.info(f"Orphan cleanup: removed {cleaned} stale active call(s)")

        except Exception as e:
            logger.warning(f"Orphan call cleanup error: {e}")


async def _call_retention_sweep():
    """Enforce the documented 90-day call-record retention. Runs every 6 hours.

    `cleanup_old_calls` has always existed but was only reachable from a manual
    admin endpoint, so nothing ever ran it — production accumulated call records
    well past the retention window that CLAUDE.md states as a privacy property.

    Each pass deletes at most MAX_BATCHES * 500 records so a large backlog drains
    over several runs rather than in one long burst of Firestore writes.
    """
    from app.db.calls import cleanup_old_calls

    MAX_BATCHES = 4

    while True:
        await asyncio.sleep(6 * 3600)
        try:
            total = 0
            for _ in range(MAX_BATCHES):
                deleted = await cleanup_old_calls()
                total += deleted
                # A short batch means the backlog is drained.
                if deleted < 500:
                    break
            if total:
                logger.info(f"Call retention sweep: deleted {total} expired record(s)")
        except Exception as e:
            logger.warning(f"Call retention sweep error: {e}")


async def _data_purge_sweep():
    """Purge accounts deactivated longer than the grace period ago.

    No-op unless PURGE_ENABLED is set (owner-gated). Runs every 6 hours.
    """
    from app.db.purge import purge_sweep

    while True:
        try:
            await purge_sweep()
        except Exception as e:
            logger.warning(f"Data purge sweep error: {type(e).__name__}")
        await asyncio.sleep(6 * 3600)


async def _lapsed_trial_sweep():
    """Transition lapsed trials from `trial` to `expired`. Runs every 6 hours.

    Without this, nothing ever writes `expired` for a trial user: the only other
    writer is the App Store notification handler, and a user who never subscribed
    has no App Store transaction to generate one. Accounts therefore sat in
    `trial` indefinitely, which left `_expired_contractor_cleanup` (which selects
    on `subscription_status == "expired"`) with nothing to act on.

    Only `trial` is swept. See `should_expire_trial` for why `active` is excluded.
    """
    import time
    from google.cloud import firestore
    from app.db.firestore_client import get_firestore_client
    from app.services.subscription import should_expire_trial

    while True:
        await asyncio.sleep(6 * 3600)
        try:
            db = get_firestore_client()
            loop = asyncio.get_event_loop()
            now = time.time()

            docs = await loop.run_in_executor(
                None,
                lambda: list(
                    db.collection("contractors")
                    .where("subscription_status", "==", "trial")
                    .stream()
                ),
            )

            def _expire_if_still_trial(doc_id: str) -> bool:
                """Re-check status inside a transaction before writing.

                StoreKit verification or an App Store notification can flip an
                account from `trial` to `active` between the query above and this
                write. An unconditional update would then clobber a freshly paid
                subscription back to `expired` and expose it to the number-release
                cleanup, so the status is re-read transactionally.
                """
                ref = db.collection("contractors").document(doc_id)

                @firestore.transactional
                def _txn(transaction):
                    snapshot = ref.get(transaction=transaction)
                    if not snapshot.exists:
                        return False
                    current = snapshot.to_dict() or {}
                    if not should_expire_trial(current, now):
                        return False
                    transaction.update(ref, {"subscription_status": "expired"})
                    return True

                return _txn(db.transaction())

            swept = 0
            for doc in docs:
                if not should_expire_trial(doc.to_dict() or {}, now):
                    continue
                try:
                    if await loop.run_in_executor(None, lambda d=doc: _expire_if_still_trial(d.id)):
                        swept += 1
                except Exception as e:
                    logger.warning(
                        "Lapsed trial sweep failed for one contractor: %s", type(e).__name__
                    )

            if swept:
                logger.info(f"Lapsed trial sweep: marked {swept} trial(s) expired")
        except Exception as e:
            logger.warning(f"Lapsed trial sweep error: {e}")


async def _expired_contractor_cleanup():
    """Release numbers from accounts that no longer need them. Runs every 6 hours.

    Two triggers, see app/services/number_release.py: deleted app for 14+
    days (always on) and lapsed subscription for 30+ days
    (LAPSED_NUMBER_RELEASE_ENABLED, owner-gated). Both require a quiet number.
    """
    from app.services.number_release import run_expired_contractor_cleanup_once

    while True:
        await asyncio.sleep(6 * 3600)  # Every 6 hours
        try:
            await run_expired_contractor_cleanup_once()
        except Exception as e:
            logger.warning(f"Expired contractor cleanup error: {e}")


@app.on_event("startup")
async def startup():
    global _post_call_worker_task, _service_request_recovery_task, _estimate_worker_task

    # Validate required config
    required = ['twilio_account_sid', 'twilio_auth_token', 'anthropic_api_key',
                'deepgram_api_key', 'elevenlabs_api_key', 'api_bearer_token']
    missing = [k for k in required if not getattr(settings, k, None)]
    if missing:
        raise RuntimeError(f"Missing required config: {', '.join(missing)}")

    validate_runtime_safety()

    # Warn loudly if vapi_webhook_secret is not set in production
    if not settings.vapi_webhook_secret and settings.environment != "development":
        logger.critical("SECURITY WARNING: vapi_webhook_secret is not set — Vapi webhook is unauthenticated")

    # Start orphan call cleanup background task
    asyncio.create_task(_orphan_call_cleanup())
    asyncio.create_task(_call_retention_sweep())
    asyncio.create_task(_lapsed_trial_sweep())
    asyncio.create_task(_expired_contractor_cleanup())
    asyncio.create_task(_data_purge_sweep())
    from app.services.post_call_handoff import post_call_worker_loop

    if _post_call_worker_task is None or _post_call_worker_task.done():
        _post_call_worker_task = asyncio.create_task(post_call_worker_loop())
    from app.services.estimate_worker import estimate_worker_loop

    if _estimate_worker_task is None or _estimate_worker_task.done():
        _estimate_worker_task = asyncio.create_task(estimate_worker_loop())
    from app.services.service_request_recovery import (
        service_request_recovery_worker_loop,
    )

    if (
        settings.service_request_recovery_enabled is True
        and (
        _service_request_recovery_task is None
        or _service_request_recovery_task.done()
        )
    ):
        # Recovery processes only already-durable intents. Initiating new
        # mutations remains gated per contractor at the voice-tool boundary.
        _service_request_recovery_task = asyncio.create_task(
            service_request_recovery_worker_loop()
        )

    # F-21: drop the redacted Twilio number from startup logs entirely. Even
    # the last 4 digits are unnecessary signal in centralised logs and the
    # number is verifiable through admin tooling when actually needed.
    logger.info("Kevin starting up", extra={"environment": settings.environment})


@app.on_event("shutdown")
async def shutdown():
    global _post_call_worker_task, _service_request_recovery_task, _estimate_worker_task

    if _post_call_worker_task is not None:
        _post_call_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _post_call_worker_task
        _post_call_worker_task = None
    if _estimate_worker_task is not None:
        _estimate_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _estimate_worker_task
        _estimate_worker_task = None
    if _service_request_recovery_task is not None:
        _service_request_recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await _service_request_recovery_task
        _service_request_recovery_task = None
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
