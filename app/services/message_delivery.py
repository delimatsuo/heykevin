"""Operational handling and reconciliation for outbound message receipts."""

import asyncio
import logging
import re

from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient

from app.config import settings
from app.db import message_delivery_receipts as receipt_db
from app.utils.logging import get_logger


logger = get_logger(__name__)


_SAFE_LABEL_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")
TWILIO_HTTP_TIMEOUT_SECONDS = 8.0
RECONCILIATION_FETCH_TIMEOUT_SECONDS = 10.0


def _label(value: object) -> str:
    label = _SAFE_LABEL_PATTERN.sub("_", str(value or "")[:128]).strip("_.:-")
    return label[:8] or "unknown"


async def _project_receipt(receipt_id: str) -> bool:
    """Project the latest receipt state, never a stale callback summary."""
    try:
        projected = await receipt_db.project_receipt_to_call(receipt_id)
    except Exception as error:
        logger.error(
            "message_delivery event=call_projection_error receipt=%s exception_type=%s",
            _label(receipt_id),
            type(error).__name__,
        )
        return False
    if not projected:
        logger.error(
            "message_delivery event=call_projection_failed receipt=%s",
            _label(receipt_id),
        )
    return projected


async def record_submission_failure(receipt_id: str) -> bool:
    """Persist and mirror a provider create failure without exposing its payload."""
    try:
        recorded = await receipt_db.mark_submission_failed(receipt_id)
    except Exception as error:
        logger.error(
            "message_delivery event=submission_failure_store_error receipt=%s exception_type=%s",
            _label(receipt_id),
            type(error).__name__,
        )
        return False
    if recorded:
        await _project_receipt(receipt_id)
    return recorded


async def handle_provider_status(
    *,
    receipt_id: str,
    provider_message_sid: str,
    provider_status: str,
    provider_error_code: object = "",
) -> str:
    """Apply one authenticated provider status without logging provider payloads."""
    try:
        update = await receipt_db.record_provider_update(
            receipt_id,
            provider_status=provider_status,
            provider_message_sid=provider_message_sid,
            provider_error_code=provider_error_code,
        )
    except Exception as error:
        logger.error(
            "message_delivery event=receipt_storage_error receipt=%s exception_type=%s",
            _label(receipt_id),
            type(error).__name__,
        )
        return "error"
    summary = update.summary
    if summary and update.outcome in {"updated", "conflict", "ignored"}:
        if not await _project_receipt(receipt_id):
            return "error"
    if summary and update.outcome in {"updated", "conflict"}:
        status = summary.get("status", "unknown")
        level = logging.ERROR if status == "failed" else logging.INFO
        event = "terminal_failure" if status == "failed" else "status_updated"
        logger.log(
            level,
            "message_delivery event=%s receipt=%s call=%s effect=%s status=%s provider_status=%s outcome=%s",
            event,
            _label(summary.get("receipt_id")),
            _label(summary.get("call_sid")),
            summary.get("effect", "unknown"),
            status,
            summary.get("provider_status", "unknown"),
            update.outcome,
        )
    elif update.outcome in {"invalid", "not_found"}:
        logger.warning(
            "message_delivery event=callback_rejected receipt=%s outcome=%s",
            _label(receipt_id),
            update.outcome,
        )
    return update.outcome


async def repair_pending_call_projections_once(*, limit: int = 20) -> int:
    """Repair durable receipt-to-call projections without provider side effects."""
    try:
        receipt_ids = await receipt_db.list_pending_projection_ids(limit=limit)
    except Exception as error:
        logger.error(
            "message_delivery event=projection_list_failed exception_type=%s",
            type(error).__name__,
        )
        return 0
    repaired = 0
    for receipt_id in receipt_ids:
        if await _project_receipt(receipt_id):
            repaired += 1
        else:
            await receipt_db.defer_projection(receipt_id)
    return repaired


async def reconcile_pending_receipts_once(*, limit: int = 20) -> int:
    """Fetch old pending Message resources; never create or resend a message."""
    try:
        candidates = await receipt_db.list_reconciliation_candidates(limit=limit)
    except Exception as error:
        logger.error(
            "message_delivery event=reconciliation_list_failed exception_type=%s",
            type(error).__name__,
        )
        return 0

    reconciled = 0
    client = None
    for due_candidate in candidates:
        candidate = await receipt_db.claim_reconciliation(due_candidate.receipt_id)
        if candidate is None:
            continue
        if not candidate.provider_message_sid:
            if await receipt_db.mark_missing_provider_id(
                candidate.receipt_id,
                lease_token=candidate.lease_token,
            ):
                await _project_receipt(candidate.receipt_id)
                reconciled += 1
                logger.error(
                    "message_delivery event=missing_provider_id receipt=%s",
                    _label(candidate.receipt_id),
                )
            continue

        try:
            if client is None:
                client = Client(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                    http_client=TwilioHttpClient(
                        timeout=TWILIO_HTTP_TIMEOUT_SECONDS,
                    ),
                )
            message = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda sid=candidate.provider_message_sid: client.messages(
                        sid
                    ).fetch(),
                ),
                timeout=RECONCILIATION_FETCH_TIMEOUT_SECONDS,
            )
            provider_status = str(getattr(message, "status", "") or "").lower()
            outcome = await handle_provider_status(
                receipt_id=candidate.receipt_id,
                provider_message_sid=candidate.provider_message_sid,
                provider_status=provider_status,
                provider_error_code=getattr(message, "error_code", None),
            )
            await receipt_db.mark_reconciled(
                candidate.receipt_id,
                lease_token=candidate.lease_token,
            )
            if outcome != "error":
                reconciled += 1
                if provider_status in receipt_db.PROVIDER_STATUS_RANK:
                    logger.warning(
                        "message_delivery event=reconciliation_pending receipt=%s provider_status=%s",
                        _label(candidate.receipt_id),
                        provider_status,
                    )
        except Exception as error:
            await receipt_db.mark_reconciled(
                candidate.receipt_id,
                lease_token=candidate.lease_token,
            )
            logger.error(
                "message_delivery event=reconciliation_fetch_failed receipt=%s exception_type=%s",
                _label(candidate.receipt_id),
                type(error).__name__,
            )
    return reconciled
