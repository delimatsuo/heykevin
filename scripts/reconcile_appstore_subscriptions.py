#!/usr/bin/env python3
"""Ask Apple the current billing state of every paying contractor.

`subscription_expires` is only advanced by App Store Server Notifications, so a
missed notification is indistinguishable from a genuine lapse by looking at
Firestore alone. This closes that gap by asking Apple directly, keyed on the
original_transaction_id already stored as the document ID in the
`apple_transactions` collection.

Read-only. Prints no phone numbers or names.

Requires the App Store credentials, which live in Cloud Run env vars rather than
the local .env:

    APPSTORE_KEY_ID=...  APPSTORE_ISSUER_ID=...  APPSTORE_PRIVATE_KEY=...  \
    APPSTORE_ENVIRONMENT=production FIRESTORE_PROJECT_ID=kevin-491315 \
    uv run python scripts/reconcile_appstore_subscriptions.py

Without them the JWT cannot be signed and every row reports ERROR/ValueError.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from app.services.subscription import (  # noqa: E402
    APPSTORE_PRODUCTION_URL,
    APPSTORE_SANDBOX_URL,
    _decode_jws_payload,
    _get_appstore_jwt,
)
from app.db.firestore_client import get_firestore_client  # noqa: E402

# Apple's documented status codes for auto-renewable subscriptions.
STATUS = {
    1: "ACTIVE",
    2: "EXPIRED",
    3: "IN_BILLING_RETRY",
    4: "IN_GRACE_PERIOD",
    5: "REVOKED",
}


async def statuses_for(original_id: str, environment: str) -> tuple[str, str]:
    """Return (verdict, detail) for one original transaction id."""
    base = APPSTORE_PRODUCTION_URL if environment.lower() == "production" else APPSTORE_SANDBOX_URL
    token = _get_appstore_jwt()
    url = f"{base}/inApps/v1/subscriptions/{original_id}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return "QUERY_FAILED", f"HTTP {r.status_code} {r.text[:90]}"

    body = r.json()
    for group in body.get("data", []):
        for item in group.get("lastTransactions", []):
            code = item.get("status")
            verdict = STATUS.get(code, f"UNKNOWN({code})")
            renewal = _decode_jws_payload(item.get("signedRenewalInfo", "")) or {}
            txn = _decode_jws_payload(item.get("signedTransactionInfo", "")) or {}
            expires_ms = txn.get("expiresDate")
            expires = ""
            if expires_ms:
                days = (expires_ms / 1000 - time.time()) / 86400
                expires = f"expires {abs(days):.0f}d {'from now' if days > 0 else 'AGO'}"
            auto = renewal.get("autoRenewStatus")
            auto_s = {1: "auto-renew ON", 0: "auto-renew OFF"}.get(auto, "")
            return verdict, "  ".join(x for x in (expires, auto_s) if x)
    return "NO_DATA", "Apple returned no subscription groups"


async def main() -> int:
    db = get_firestore_client()
    bindings = [(d.id, d.to_dict() or {}) for d in db.collection("apple_transactions").stream()]
    contractors = {d.id: (d.to_dict() or {}) for d in db.collection("contractors").stream()}

    print(f"{'contractor':<12}{'our status':<11}{'tier':<13}{'APPLE SAYS':<17}detail")
    print("-" * 92)
    for original_id, binding in sorted(bindings, key=lambda b: str(b[1].get("contractor_id"))):
        cid = str(binding.get("contractor_id") or "")
        env = str(binding.get("environment") or "Production")
        c = contractors.get(cid, {})
        ours = str(c.get("subscription_status") or "-")
        tier = str(c.get("subscription_tier") or "-")
        try:
            verdict, detail = await statuses_for(original_id, env)
        except Exception as e:
            verdict, detail = "ERROR", type(e).__name__
        flag = ""
        if verdict == "ACTIVE" and ours != "active":
            flag = "  <-- PAYING, we think otherwise"
        if verdict in {"EXPIRED", "REVOKED"} and ours == "active":
            flag = "  <-- NOT paying, we still serve them"
        if verdict == "ACTIVE" and c.get("active") is False:
            flag = "  <-- BILLED FOR A DEACTIVATED ACCOUNT"
        print(f"{cid[:10]:<12}{ours:<11}{tier:<13}{verdict:<17}{detail}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
