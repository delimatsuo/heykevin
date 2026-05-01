"""Global Apple App Store transaction registry.

Each Apple subscription has a stable `originalTransactionId` (the receipt's
top-level identity). Once that receipt is bound to a Hey Kevin contractor, no
other contractor may claim it. This module persists that binding and supports
atomic claim semantics so that two concurrent verify/notification flows cannot
both succeed.

Firestore data model:

    apple_transactions/{original_transaction_id}
        contractor_id: str            # the bound owner
        first_seen_at: float          # unix timestamp of initial binding
        last_seen_at: float           # unix timestamp of most recent observation
        last_transaction_id: str      # most recent transaction_id observed
        product_id: str               # last observed product id
        environment: str              # "Sandbox" or "Production" if Apple reported it

Usage:

    binding = await get_transaction_binding(original_id)
    if binding and binding["contractor_id"] != contractor_id:
        # cross-contractor reuse — reject

    # Atomic claim:
    ok, existing_owner = await claim_transaction(
        original_transaction_id=original_id,
        contractor_id=contractor_id,
        transaction_id=tx_id,
        product_id=product_id,
        environment=env,
    )
    if not ok:
        # existing_owner != contractor_id → reject
        ...
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple

from app.utils.logging import get_logger

logger = get_logger(__name__)


COLLECTION = "apple_transactions"


def _doc_ref(original_transaction_id: str):
    """Return the Firestore DocumentReference for a given original_transaction_id."""
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    return db.collection(COLLECTION).document(original_transaction_id)


async def get_transaction_binding(original_transaction_id: str) -> Optional[dict]:
    """Return the existing binding dict, or None if the transaction is unbound."""
    if not original_transaction_id:
        return None
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(None, lambda: _doc_ref(original_transaction_id).get())
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data.setdefault("original_transaction_id", original_transaction_id)
    return data


async def claim_transaction(
    *,
    original_transaction_id: str,
    contractor_id: str,
    transaction_id: str = "",
    product_id: str = "",
    environment: str = "",
) -> Tuple[bool, Optional[str]]:
    """Atomically bind an Apple original_transaction_id to a contractor.

    Returns (ok, existing_owner):
      - (True, contractor_id)  if newly bound, or already bound to this contractor
      - (False, other_id)      if already bound to a different contractor

    Uses a Firestore transaction so concurrent claims cannot both win.
    """
    if not original_transaction_id or not contractor_id:
        return False, None

    from app.db.firestore_client import get_firestore_client
    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = _doc_ref(original_transaction_id)

    @fs.transactional
    def _txn(transaction) -> Tuple[bool, Optional[str]]:
        snapshot = doc_ref.get(transaction=transaction)
        now = time.time()
        if snapshot.exists:
            data = snapshot.to_dict() or {}
            existing = data.get("contractor_id", "")
            if existing and existing != contractor_id:
                return False, existing
            # Same owner — refresh metadata.
            update: dict = {"last_seen_at": now}
            if transaction_id:
                update["last_transaction_id"] = transaction_id
            if product_id:
                update["product_id"] = product_id
            if environment:
                update["environment"] = environment
            transaction.set(doc_ref, update, merge=True)
            return True, contractor_id

        payload = {
            "contractor_id": contractor_id,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        if transaction_id:
            payload["last_transaction_id"] = transaction_id
        if product_id:
            payload["product_id"] = product_id
        if environment:
            payload["environment"] = environment
        transaction.set(doc_ref, payload)
        return True, contractor_id

    loop = asyncio.get_event_loop()
    transaction = db.transaction()
    ok, owner = await loop.run_in_executor(None, lambda: _txn(transaction))
    if not ok:
        logger.warning(
            "apple_transactions: cross-contractor claim rejected original_tx=%s requested_by=%s owned_by=%s",
            original_transaction_id,
            contractor_id,
            owner,
        )
    return ok, owner
