"""App Store notifications that matched no Kevin account.

The webhook answers 200 to these so Apple stops retrying a notification that
can never match. Without a record, a charge for an account Kevin can't find
disappears with the 30-day logs: on 2026-10-10 a production DID_RENEW matched
no account, and nothing but one log line showed that someone was paying for
nothing. Each one is kept here, keyed by Apple's notificationUUID, so a
redelivery updates the record instead of duplicating it.
"""

from __future__ import annotations

import asyncio
import time

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from app.db.firestore_client import get_firestore_client

COLLECTION = "appstore_unmatched_notifications"


async def record_unmatched_notification(notification_id: str, fields: dict) -> None:
    """Store one unmatched notification, or count a redelivery of it."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    ref = db.collection(COLLECTION).document(notification_id)
    now = time.time()

    def _write():
        # create() fails if the document exists, so two overlapping deliveries
        # of one notification can't both write a first record.
        try:
            ref.create({**fields, "first_seen_at": now, "last_seen_at": now, "deliveries": 1})
        except AlreadyExists:
            ref.update({"last_seen_at": now, "deliveries": firestore.Increment(1)})

    await loop.run_in_executor(None, _write)
