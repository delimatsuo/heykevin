"""Data purge for deleted accounts.

Spec: docs/superpowers/specs/2026-08-20-data-purge-pipeline.md (owner-approved
2026-08-21: 30-day grace, minimal tombstone, PURGE_ENABLED default off).

Deletion (PR #192) is a soft deactivate; this module makes the privacy
promise real: after the grace period, everything a deactivated account stored
is erased and the contractor document is reduced to a tombstone that keeps
ONLY the fields billing reconciliation depends on. `active: False` is
load-bearing on the tombstone — the App Store notification handler requires
an explicit False to attribute post-deletion charges, and the sweep query
matches on it.

Every step tolerates already-deleted data, so a crashed sweep re-runs clean.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import settings
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

_BATCH = 500

# Subcollections under contractors/{id}. customer_memory is handled
# separately: its documents carry a nested command_receipts subcollection
# that Firestore will NOT cascade-delete.
_SUBCOLLECTIONS = (
    "contacts",
    "caller_contacts",
    "service_requests",
    "inbound_messages",
    "devices",
    "settings",
    "knowledge_base",
)

# Top-level collections keyed by contractor_id.
_BY_CONTRACTOR = ("calls", "jobs", "post_call_handoffs")

# The tombstone allowlist. Everything else on the document is PII or business
# data and dies with the purge. Asserted as an allowlist in tests so a future
# field cannot leak through.
TOMBSTONE_FIELDS = (
    "active",
    "purged_at",
    "deactivated_at",
    "deletion_requested_at",
    "subscription_uuid",
    # Rebound detection (subscription.py) matches a re-signed-up customer by
    # apple_user_id — without it, a paying returnee's renewals become
    # unattributable post-purge and misclassify as post-deletion charges.
    "apple_user_id",
    "post_deletion_billing",
    "number_release_anomaly",
    "deleted_app_detected_at",
)


def _media_bucket():
    """Return the estimate-media bucket, or None in degraded mode."""
    if not settings.estimate_media_bucket:
        return None
    from google.cloud import storage

    client = storage.Client()
    return client.bucket(settings.estimate_media_bucket)


def _delete_collection_sync(db, coll, counts: dict[str, int], key: str) -> None:
    """Batched delete of every document in a collection reference."""
    while True:
        snaps = list(coll.limit(_BATCH).stream())
        if not snaps:
            return
        batch = db.batch()
        for snap in snaps:
            batch.delete(snap.reference)
        batch.commit()
        counts[key] = counts.get(key, 0) + len(snaps)


def _purge_sync(contractor_id: str) -> dict[str, Any]:
    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    snap = doc_ref.get()
    data = snap.to_dict() if snap.exists else None
    if data is None:
        return {"refused": "not_found"}
    if data.get("purged_at"):
        return {"refused": "already_purged"}
    if data.get("active") is not False:
        # Structurally impossible to purge an active (or ambiguous) account:
        # only an explicit active=False written by deactivation qualifies.
        return {"refused": "not_deactivated"}
    if not data.get("deletion_requested_at"):
        # THE critical guard: the 14-day deleted-app cleanup deactivates
        # accounts through the same deactivate_contractor — and even texts
        # 'reinstall to reactivate'. Deactivation is NOT consent to erase.
        # Only the user's own DELETE stamps deletion_requested_at.
        return {"refused": "no_deletion_request"}

    counts: dict[str, int] = {}

    # 1. Nested receipts FIRST, then their customer_memory parents.
    # list_documents(), not stream(): the forget flow deletes a memory doc
    # while writing a receipt beneath it, and stream() never yields such
    # phantom parents — their receipts would survive as permanent orphans.
    memory = doc_ref.collection("customer_memory")
    for mem_ref in memory.list_documents():
        _delete_collection_sync(
            db, mem_ref.collection("command_receipts"), counts,
            "command_receipts",
        )
    _delete_collection_sync(db, memory, counts, "customer_memory")

    # 2. Flat subcollections.
    for name in _SUBCOLLECTIONS:
        _delete_collection_sync(db, doc_ref.collection(name), counts, name)

    # 3. Estimates: GCS media is keyed by token_hash (the estimate doc id),
    #    so walk the docs to find the prefixes, delete objects, then docs.
    bucket = _media_bucket()
    from google.cloud.firestore_v1.base_query import FieldFilter

    while True:
        est_snaps = list(
            db.collection("estimates")
            .where(filter=FieldFilter("contractor_id", "==", contractor_id))
            .limit(_BATCH)
            .stream()
        )
        if not est_snaps:
            break
        batch = db.batch()
        for est in est_snaps:
            if bucket is not None:
                blobs = list(bucket.list_blobs(prefix=f"{est.id}/"))
                if blobs:
                    # on_error: a concurrent sweep may have deleted a blob
                    # already; already-gone is success for a purge.
                    bucket.delete_blobs(blobs, on_error=lambda _b: None)
                    counts["estimate_media"] = counts.get("estimate_media", 0) + len(blobs)
            else:
                # Degraded mode (bucket unset): the doc is the only mapping
                # to the GCS prefixes; record what we could not delete so
                # the operator sees the gap (lifecycle deletes it in <=90d).
                skipped = len((est.to_dict() or {}).get("media_ids") or [])
                if skipped:
                    counts["estimate_media_skipped"] = (
                        counts.get("estimate_media_skipped", 0) + skipped
                    )
            batch.delete(est.reference)
        batch.commit()
        counts["estimates"] = counts.get("estimates", 0) + len(est_snaps)

    # 4. Top-level collections keyed by contractor_id.
    for name in _BY_CONTRACTOR:
        _delete_collection_sync(
            db,
            db.collection(name).where(
                filter=FieldFilter("contractor_id", "==", contractor_id)
            ),
            counts,
            name,
        )

    # 5. The PII kill step: overwrite (no merge) with the tombstone.
    # Re-read first: the delete phase takes time, and an App Store webhook
    # write (post_deletion_billing) landing mid-purge must survive — that
    # record is the billing evidence this tombstone exists to keep.
    fresh_snap = doc_ref.get()
    fresh = (fresh_snap.to_dict() if fresh_snap.exists else None) or data
    purged_at = int(time.time())
    tombstone: dict[str, Any] = {"active": False, "purged_at": purged_at}
    for field in TOMBSTONE_FIELDS:
        if field in ("active", "purged_at"):
            continue
        if fresh.get(field) is not None:
            tombstone[field] = fresh[field]
    doc_ref.set(tombstone)

    # Aggregate counts only — never contents.
    logger.info(
        "contractor_purged",
        extra={"contractor_id": contractor_id[:8], "action": "purge"},
    )
    logger.info(f"Purge complete: contractor={contractor_id[:8]} deleted={counts}")
    return {"purged_at": purged_at, "deleted": counts}


async def purge_contractor(contractor_id: str) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _purge_sync(contractor_id))


async def purge_sweep(now: float | None = None) -> list[str]:
    """Purge every account deactivated longer than the grace period ago.

    Returns the contractor ids purged this pass. One poisoned account never
    stops the sweep.
    """
    if not settings.purge_enabled:
        return []
    now = now if now is not None else time.time()
    cutoff = now - settings.purge_grace_days * 24 * 3600

    from google.cloud.firestore_v1.base_query import FieldFilter

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    # Single-field range filter (auto-indexed; no composite index needed):
    # grace is anchored on the user's deletion REQUEST — the only thing that
    # consents to a purge. active/purged_at are re-checked client-side and
    # again inside purge_contractor.
    snaps = await loop.run_in_executor(
        None,
        lambda: list(
            db.collection("contractors")
            .where(filter=FieldFilter("deletion_requested_at", "<", cutoff))
            .stream()
        ),
    )
    purged: list[str] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        if data.get("purged_at") or data.get("active") is not False:
            continue
        cid = snap.id
        try:
            result = await purge_contractor(cid)
        except Exception as error:
            logger.error(
                "Purge failed for one contractor (sweep continues): %s: %s",
                cid[:8], type(error).__name__,
            )
            continue
        if "refused" not in result:
            purged.append(cid)
    if purged:
        logger.info(f"Purge sweep: purged {len(purged)} account(s)")
    return purged
