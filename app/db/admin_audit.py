"""Durable admin audit events."""

import asyncio
import hashlib
import math
import time
from typing import Any

from app.db.firestore_client import get_firestore_client

COLLECTION = "admin_audit_events"


def _client_ip_hash(request) -> str:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    if not host:
        return ""
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


def build_lead_capture_admin_audit_event(
    *,
    contractor_id: str,
    previous_enabled: bool,
    enabled: bool,
    generation: int,
    lifecycle_epoch: int,
    timestamp: float,
    actor_type: str = "global_admin_token",
    reason: str = "admin_lead_capture_toggle",
    request_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    req_meta = dict(request_metadata) if request_metadata is not None else {}
    meta = {
        "generation": generation,
        "lifecycle_epoch": lifecycle_epoch,
        "timestamp": timestamp,
    }
    for k, v in req_meta.items():
        if k not in ("ip_hash", "user_agent"):
            meta[k] = v
    return {
        "actor_type": actor_type,
        "action": "jobber_lead_capture_update",
        "target_type": "contractor",
        "target_id": contractor_id,
        "reason": reason,
        "before": {"jobber_lead_capture_enabled": previous_enabled},
        "after": {"jobber_lead_capture_enabled": enabled},
        "metadata": meta,
        "ip_hash": req_meta.get("ip_hash", ""),
        "user_agent": req_meta.get("user_agent", ""),
        "created_at": timestamp,
        "timestamp": timestamp,
    }


async def write_admin_audit_event(
    *,
    request=None,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: float | None = None,
    db: Any = None,
) -> None:
    if created_at is not None:
        if type(created_at) is not float or not math.isfinite(created_at) or created_at <= 0.0:
            raise ValueError("created_at must be an exact finite positive float")
        audit_created_at = created_at
    else:
        audit_created_at = time.time()

    if db is None:
        db = get_firestore_client()
    headers = getattr(request, "headers", {}) or {} if request is not None else {}
    event = {
        "actor_type": "global_admin_token",
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "reason": reason,
        "before": before or {},
        "after": after or {},
        "metadata": metadata or {},
        "ip_hash": _client_ip_hash(request) if request is not None else "",
        "user_agent": headers.get("user-agent", "") if isinstance(headers, dict) else "",
        "created_at": audit_created_at,
    }

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document().set(event),
    )


async def list_admin_audit_events(limit: int = 100, db: Any = None) -> list[dict]:
    if db is None:
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
