"""Durable admin audit events."""

import asyncio
import hashlib
import time
from typing import Any

from app.db.firestore_client import get_firestore_client

COLLECTION = "admin_audit_events"


def _client_ip_hash(request) -> str:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    if not host:
        return ""
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


async def write_admin_audit_event(
    *,
    request,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db = get_firestore_client()
    headers = getattr(request, "headers", {}) or {}
    event = {
        "actor_type": "global_admin_token",
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "reason": reason,
        "before": before or {},
        "after": after or {},
        "metadata": metadata or {},
        "ip_hash": _client_ip_hash(request),
        "user_agent": headers.get("user-agent", ""),
        "created_at": time.time(),
    }

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document().set(event),
    )


async def list_admin_audit_events(limit: int = 100) -> list[dict]:
    db = get_firestore_client()
    loop = asyncio.get_running_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(
            db.collection(COLLECTION)
            .order_by("created_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        ),
    )
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in docs]
