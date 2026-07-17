"""Durable orchestration for at-most-once post-call side effects."""

import asyncio
import logging
import re

from app.db import calls as call_db
from app.db import post_call_handoffs as handoff_db
from app.services.post_call import process_post_call
from app.utils.logging import get_logger

logger = get_logger(__name__)

POST_CALL_TIMEOUT_SECONDS = 120.0
WORKER_INTERVAL_SECONDS = 30.0
_SAFE_LOG_METRIC_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _call_label(call_sid: str) -> str:
    return str(call_sid or "")[:8] or "unknown"


def _safe_log_metric(value: object) -> str:
    if isinstance(value, (bool, int, float)):
        return str(value)
    sanitized = _SAFE_LOG_METRIC_PATTERN.sub("_", str(value or "")[:40]).strip("_.:-")
    return sanitized or "unknown"


def _log_handoff(
    event: str,
    call_sid: str,
    *,
    level: int = logging.INFO,
    **metrics: object,
) -> None:
    metric_text = " ".join(
        f"{key}={_safe_log_metric(value)}" for key, value in metrics.items()
    )
    suffix = f" {metric_text}" if metric_text else ""
    logger.log(
        level,
        "post_call_handoff event=%s call=%s%s",
        event,
        _call_label(call_sid),
        suffix,
    )


async def _mirror_status(
    call_sid: str,
    status: str,
    *,
    failure_code: str = "",
    completed_effects: tuple[str, ...] = (),
    failed_effects: tuple[str, ...] = (),
) -> bool:
    updates = {
        "post_call_status": status,
        "post_call_failure_code": failure_code,
        "post_call_completed_effects": list(completed_effects),
        "post_call_failed_effects": list(failed_effects),
    }
    saved = await call_db.save_call(call_sid, updates)
    if not saved:
        _log_handoff(
            "mirror_failed",
            call_sid,
            level=logging.ERROR,
            status=status,
        )
    return saved


async def _hydrate_handoff(call_sid: str) -> dict:
    handoff = await handoff_db.get_handoff(call_sid) or {}
    call_record = await call_db.get_call(call_sid) or {}
    transcript = call_record.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise RuntimeError("post-call transcript unavailable")

    handoff_contractor_id = str(handoff.get("contractor_id") or "")
    call_contractor_id = str(call_record.get("contractor_id") or "")
    if (
        handoff_contractor_id
        and call_contractor_id
        and handoff_contractor_id != call_contractor_id
    ):
        raise RuntimeError("post-call contractor mismatch")
    contractor_id = handoff_contractor_id or call_contractor_id
    if not contractor_id:
        raise RuntimeError("post-call contractor unavailable")

    from app.db.contractors import get_contractor

    contractor = await get_contractor(contractor_id) or {}
    if not contractor:
        raise RuntimeError("post-call contractor unavailable")

    return {
        "transcript_lines": transcript.splitlines(),
        "caller_phone": str(call_record.get("caller_phone") or ""),
        "contractor_phone": str(contractor.get("owner_phone") or ""),
        "twilio_number": str(contractor.get("twilio_number") or ""),
        "contractor": contractor,
        "caller_language": str(handoff.get("caller_language") or "en"),
    }


async def run_post_call_handoff(
    call_sid: str,
    *,
    transcript_lines: list[str] | None = None,
    caller_phone: str = "",
    contractor_phone: str = "",
    twilio_number: str = "",
    contractor: dict | None = None,
    caller_language: str = "en",
) -> str:
    """Claim and execute one handoff; uncertain work is never replayed."""
    terminal_persisted = False
    try:
        claimed = await handoff_db.claim_handoff(call_sid)
    except Exception as error:
        _log_handoff(
            "claim_error",
            call_sid,
            level=logging.ERROR,
            exception_type=type(error).__name__,
        )
        return "pending"
    if not claimed:
        _log_handoff("deduplicated", call_sid)
        return "deduplicated"

    try:
        if transcript_lines is None:
            hydrated = await _hydrate_handoff(call_sid)
            transcript_lines = hydrated["transcript_lines"]
            caller_phone = hydrated["caller_phone"]
            contractor_phone = hydrated["contractor_phone"]
            twilio_number = hydrated["twilio_number"]
            contractor = hydrated["contractor"]
            caller_language = hydrated["caller_language"]

        result = await asyncio.wait_for(
            process_post_call(
                transcript_lines=list(transcript_lines or []),
                caller_phone=caller_phone,
                call_sid=call_sid,
                contractor_phone=contractor_phone,
                twilio_number=twilio_number,
                contractor=contractor or {},
                caller_language=caller_language,
            ),
            timeout=POST_CALL_TIMEOUT_SECONDS,
        )
        persisted = await handoff_db.finish_handoff(call_sid, result)
        status, failure_code = handoff_db.terminal_outcome(result.status)
        if not persisted:
            status = "needs_attention"
            failure_code = "handoff_finish_failed"
            terminal_persisted = await handoff_db.mark_needs_attention(
                call_sid,
                failure_code,
            )
        else:
            terminal_persisted = True
        await _mirror_status(
            call_sid,
            status,
            failure_code=failure_code,
            completed_effects=result.completed_effects,
            failed_effects=result.failed_effects,
        )
        _log_handoff(
            "finished",
            call_sid,
            level=logging.ERROR if status == "needs_attention" else logging.INFO,
            status=status,
            completed_count=len(result.completed_effects),
            failed_count=len(result.failed_effects),
        )
        return status
    except asyncio.TimeoutError:
        await handoff_db.mark_needs_attention(call_sid, "processing_timeout")
        await _mirror_status(
            call_sid,
            "needs_attention",
            failure_code="processing_timeout",
        )
        _log_handoff(
            "processing_timeout",
            call_sid,
            level=logging.ERROR,
        )
        return "needs_attention"
    except asyncio.CancelledError:
        if not terminal_persisted:
            await handoff_db.mark_needs_attention(call_sid, "processing_cancelled")
            await _mirror_status(
                call_sid,
                "needs_attention",
                failure_code="processing_cancelled",
            )
        raise
    except Exception as error:
        if not terminal_persisted:
            await handoff_db.mark_needs_attention(call_sid, "processing_error")
            await _mirror_status(
                call_sid,
                "needs_attention",
                failure_code="processing_error",
            )
        _log_handoff(
            "processing_error",
            call_sid,
            level=logging.ERROR,
            exception_type=type(error).__name__,
        )
        return "needs_attention"


async def enqueue_and_run_post_call(
    *,
    transcript_lines: list[str],
    caller_phone: str,
    call_sid: str,
    contractor_phone: str = "",
    twilio_number: str = "",
    contractor: dict | None = None,
    caller_language: str = "en",
) -> str:
    """Persist one handoff before attempting its claimed side effects."""
    contractor = contractor or {}
    contractor_id = str(contractor.get("contractor_id") or "")
    enqueued = await handoff_db.enqueue_handoff(
        call_sid=call_sid,
        contractor_id=contractor_id,
        caller_language=caller_language,
    )
    if not enqueued:
        await _mirror_status(
            call_sid,
            "needs_attention",
            failure_code="enqueue_failed",
        )
        _log_handoff("enqueue_failed", call_sid, level=logging.ERROR)
        return "needs_attention"

    if not contractor_id:
        await _mirror_status(call_sid, "pending")
        _log_handoff("inline_deferred", call_sid, reason="contractor_context_unavailable")
        return "pending"

    return await run_post_call_handoff(
        call_sid,
        transcript_lines=transcript_lines,
        caller_phone=caller_phone,
        contractor_phone=contractor_phone,
        twilio_number=twilio_number,
        contractor=contractor,
        caller_language=caller_language,
    )


async def run_pending_post_calls_once(*, limit: int = 10) -> None:
    """Process pending work and quarantine stale uncertain claims."""
    pending_ids = await handoff_db.list_handoff_ids("pending", limit=limit)
    for call_sid in pending_ids:
        await run_post_call_handoff(call_sid)

    in_progress_ids = await handoff_db.list_handoff_ids(
        "in_progress",
        limit=min(max(limit * 5, 50), 100),
    )
    for call_sid in in_progress_ids:
        claimed = await handoff_db.claim_handoff(call_sid)
        if claimed:
            # In-progress records are never claimable; guard future DB changes.
            await handoff_db.mark_needs_attention(
                call_sid,
                "invalid_reclaim",
            )
        handoff = await handoff_db.get_handoff(call_sid) or {}
        if handoff.get("status") == "needs_attention":
            failure_code = str(handoff.get("failure_code") or "uncertain")
            await _mirror_status(
                call_sid,
                "needs_attention",
                failure_code=failure_code,
            )
            _log_handoff(
                "attention_required",
                call_sid,
                level=logging.ERROR,
                failure_code=failure_code,
            )


async def post_call_worker_loop() -> None:
    """Continuously drain durable pending handoffs on active instances."""
    while True:
        try:
            await run_pending_post_calls_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_handoff(
                "worker_error",
                "",
                level=logging.ERROR,
                exception_type=type(error).__name__,
            )
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
