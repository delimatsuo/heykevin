"""Apple App Store Server API integration.

Handles subscription verification, promotional offer signing, and
Apple Server Notifications V2 webhook processing.
"""

import base64
import json
import time
import uuid
import asyncio
from typing import Optional

import httpx
import jwt

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# App Store Server API endpoints
APPSTORE_PRODUCTION_URL = "https://api.storekit.itunes.apple.com"
APPSTORE_SANDBOX_URL = "https://api.storekit-sandbox.itunes.apple.com"

VALID_SUBSCRIPTION_PRODUCTS = {
    "com.kevin.callscreen.personal.monthly",
    "com.kevin.callscreen.business.monthly",
    "com.kevin.callscreen.businesspro.monthly",
}

PRODUCT_TO_TIER = {
    "com.kevin.callscreen.personal.monthly": "personal",
    "com.kevin.callscreen.business.monthly": "business",
    "com.kevin.callscreen.businesspro.monthly": "businessPro",
}

PROMO_COUNTER_DOC = "subscription/promo_counter"
PROMO_MAX = 1000


def _get_appstore_url() -> str:
    if settings.appstore_environment == "production":
        return APPSTORE_PRODUCTION_URL
    return APPSTORE_SANDBOX_URL


def _get_appstore_jwt() -> str:
    """Generate a JWT for App Store Server API authentication."""
    key_content = settings.appstore_private_key
    if "|" in key_content:
        key_content = key_content.replace("|", "\n")
    elif "\\n" in key_content:
        key_content = key_content.replace("\\n", "\n")

    now = int(time.time())
    payload = {
        "iss": settings.appstore_issuer_id,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
        "bid": settings.appstore_bundle_id,
    }
    return jwt.encode(
        payload,
        key_content,
        algorithm="ES256",
        headers={"kid": settings.appstore_key_id},
    )


async def verify_transaction(transaction_id: str) -> Optional[dict]:
    """Verify a transaction with Apple's App Store Server API.

    Returns transaction info dict or None on failure.
    """
    if not settings.appstore_key_id:
        logger.warning("App Store API not configured — skipping verification")
        return None

    url = f"{_get_appstore_url()}/inApps/v1/transactions/{transaction_id}"
    try:
        token = _get_appstore_jwt()
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        if response.status_code == 200:
            return response.json()
        logger.error(f"App Store transaction lookup failed: {response.status_code} {response.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"App Store API error: {e}", exc_info=True)
        return None


async def is_transaction_seen(contractor_id: str, transaction_id: str) -> bool:
    """Check if we've already processed this transaction ID (deduplication)."""
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_path = f"contractors/{contractor_id}/transactions/{transaction_id}"
    doc = await loop.run_in_executor(None, lambda: db.document(doc_path).get())
    return doc.exists


async def mark_transaction_seen(contractor_id: str, transaction_id: str):
    """Record a processed transaction ID to prevent replay."""
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_path = f"contractors/{contractor_id}/transactions/{transaction_id}"
    await loop.run_in_executor(
        None,
        lambda: db.document(doc_path).set({"processed_at": time.time()}),
    )


async def update_subscription_from_transaction(contractor_id: str, transaction_info: dict) -> bool:
    """Update contractor subscription status from a verified Apple transaction.

    Returns True if updated successfully.
    """
    from app.db.contractors import update_contractor

    product_id = transaction_info.get("productId", "")
    tier = PRODUCT_TO_TIER.get(product_id)
    if not tier:
        logger.error(f"Unknown product ID: {product_id}")
        return False

    # Validate ownership: appAccountToken must match contractor's subscription_uuid
    from app.db.contractors import get_contractor
    app_account_token = transaction_info.get("appAccountToken", "")
    if not app_account_token:
        logger.error(f"appAccountToken missing in transaction for contractor {contractor_id}")
        return False
    contractor_profile = await get_contractor(contractor_id)
    expected_uuid = (contractor_profile or {}).get("subscription_uuid", "")
    if not expected_uuid or app_account_token != expected_uuid:
        logger.error(f"appAccountToken mismatch: expected subscription_uuid={expected_uuid!r}, got {app_account_token!r}")
        return False

    expires_ms = transaction_info.get("expiresDate", 0)
    expires_ts = expires_ms / 1000.0 if expires_ms else time.time() + 30 * 86400

    await update_contractor(contractor_id, {
        "subscription_tier": tier,
        "subscription_status": "active",
        "subscription_expires": expires_ts,
    })
    logger.info(f"Subscription updated: contractor={contractor_id} tier={tier}")
    return True


async def check_promo_eligible() -> bool:
    """Check if promo counter is under 1,000. Does NOT increment."""
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(None, lambda: db.document(PROMO_COUNTER_DOC).get())
    if not doc.exists:
        return True
    count = doc.to_dict().get("count", 0)
    return count < PROMO_MAX


async def claim_promo_slot() -> bool:
    """Atomically check counter < 1000 and increment. Returns True if slot claimed."""
    from app.db.firestore_client import get_firestore_client
    from google.cloud import firestore as fs
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    @fs.transactional
    def _txn(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        count = snapshot.to_dict().get("count", 0) if snapshot.exists else 0
        if count >= PROMO_MAX:
            return False
        transaction.set(doc_ref, {"count": count + 1}, merge=True)
        return True

    doc_ref = db.document(PROMO_COUNTER_DOC)
    transaction = db.transaction()
    result = await loop.run_in_executor(None, lambda: _txn(transaction, doc_ref))
    return bool(result)


def sign_promotional_offer(
    product_id: str,
    offer_id: str,
    application_username: str,
) -> Optional[dict]:
    """Sign a StoreKit promotional offer using ECDSA P-256.

    Returns dict with nonce, timestamp, keyIdentifier, signature or None on error.
    """
    if not settings.appstore_private_key:
        logger.warning("App Store key not configured — cannot sign offer")
        return None

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.backends import default_backend

        key_content = settings.appstore_private_key
        if "|" in key_content:
            key_content = key_content.replace("|", "\n")
        elif "\\n" in key_content:
            key_content = key_content.replace("\\n", "\n")

        private_key = serialization.load_pem_private_key(
            key_content.encode(),
            password=None,
            backend=default_backend(),
        )

        nonce = str(uuid.uuid4()).lower()
        timestamp = int(time.time() * 1000)

        # Message format per Apple docs:
        # appBundleId + \u2063 + keyIdentifier + \u2063 + productIdentifier + \u2063 + offerIdentifier + \u2063 + applicationUsername + \u2063 + nonce + \u2063 + timestamp
        message = "\u2063".join([
            settings.appstore_bundle_id,
            settings.appstore_key_id,
            product_id,
            offer_id,
            application_username,
            nonce,
            str(timestamp),
        ]).encode("utf-8")

        signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        signature_b64 = base64.b64encode(signature).decode()

        return {
            "keyIdentifier": settings.appstore_key_id,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": signature_b64,
        }
    except Exception as e:
        logger.error(f"Offer signing failed: {e}", exc_info=True)
        return None


async def handle_appstore_notification(payload: dict) -> bool:
    """Process an App Store Server Notification V2 payload.

    payload is the decoded signed payload (already JWT-verified by the webhook).
    Returns True if handled successfully.
    """
    from app.db.contractors import update_contractor
    from app.db.firestore_client import get_firestore_client

    notification_type = payload.get("notificationType", "")

    # Extract transaction info from signed renewal info
    renewal_info = payload.get("data", {})
    signed_transaction = renewal_info.get("signedTransactionInfo", "")

    # Decode the signed JWTs from Apple (trust after signature verified upstream)
    transaction_info = {}
    if signed_transaction:
        try:
            # Decode without verification — Apple signature already verified at webhook layer
            parts = signed_transaction.split(".")
            if len(parts) == 3:
                padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
                transaction_info = json.loads(base64.urlsafe_b64decode(padded))
        except Exception as e:
            logger.error(f"Failed to decode transaction JWT: {e}")

    # Look up contractor by app account token (= subscription_uuid stored at purchase)
    app_account_token = transaction_info.get("appAccountToken", "")
    if not app_account_token:
        logger.warning(f"No appAccountToken in notification: type={notification_type}")
        return False

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    # subscription_uuid is stored in contractor document; look up by that field
    docs = await loop.run_in_executor(
        None,
        lambda: list(
            db.collection("contractors")
            .where("subscription_uuid", "==", app_account_token)
            .where("active", "==", True)
            .limit(1)
            .stream()
        ),
    )
    if not docs:
        logger.warning(f"Contractor not found for appAccountToken (subscription_uuid): {app_account_token}")
        return False
    contractor_id = docs[0].id

    if notification_type in ("DID_RENEW", "SUBSCRIBED"):
        product_id = transaction_info.get("productId", "")
        tier = PRODUCT_TO_TIER.get(product_id, "")
        expires_ms = transaction_info.get("expiresDate", 0)
        expires_ts = expires_ms / 1000.0 if expires_ms else time.time() + 30 * 86400
        if tier:
            await update_contractor(contractor_id, {
                "subscription_tier": tier,
                "subscription_status": "active",
                "subscription_expires": expires_ts,
            })
            logger.info(f"Subscription renewed: {contractor_id} → {tier}")

    elif notification_type in ("EXPIRED", "DID_FAIL_TO_RENEW", "REFUND", "REVOKE"):
        await update_contractor(contractor_id, {
            "subscription_status": "expired",
        })
        logger.info(f"Subscription expired/cancelled: {contractor_id} type={notification_type}")

    return True
