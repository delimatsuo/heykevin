"""Apple App Store Server API integration.

Handles subscription verification, promotional offer signing, and
Apple Server Notifications V2 webhook processing.
"""

import base64
import json
import time
import uuid
import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx
import jwt

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


# Statuses whose expiry timestamp is written by Apple (App Store Server
# Notifications). A past timestamp on these is ambiguous — see
# evaluate_subscription_access.
_APPLE_MANAGED_STATUSES = frozenset({"active"})

# Statuses whose expiry we set ourselves and can therefore trust.
_LOCALLY_MANAGED_STATUSES = frozenset({"trial"})

# Statuses that mean the paid term is ending; access lasts until the timestamp.
_TERMINAL_STATUSES = frozenset({"expired", "cancelled"})

# The product's free trial length. Historical records disagree — 93 production
# accounts carry a 3-day `subscription_expires` against 5 with 14 — so trial end
# is derived from `trial_start` rather than trusting the stored value.
TRIAL_PERIOD_DAYS = 14


def trial_expires_at(contractor: Optional[dict]) -> float:
    """Effective end of a contractor's free trial.

    Returns the later of the stored `subscription_expires` and
    `trial_start + TRIAL_PERIOD_DAYS`. Deriving from `trial_start` repairs the
    short-window records in place, with no migration; using max() means this can
    only ever extend a trial, so a promo or manual extension still wins.

    Returns 0.0 when neither field is usable, which callers treat as unknown
    (and therefore allowed) rather than expired.
    """
    if not contractor:
        return 0.0

    stored = contractor.get("subscription_expires")
    stored_ts = float(stored) if isinstance(stored, (int, float)) and stored else 0.0

    started = contractor.get("trial_start")
    if isinstance(started, (int, float)) and started:
        return max(stored_ts, float(started) + TRIAL_PERIOD_DAYS * 86400)

    return stored_ts


def evaluate_subscription_access(
    contractor: Optional[dict], now: float
) -> tuple[bool, str]:
    """Decide whether an inbound call gets paid features. Returns (allowed, reason).

    Fail-open is deliberate (CLAUDE.md design decision #1): an absent contractor,
    a missing expiry, or an unrecognized status all grant access. A false denial
    silences a paying customer's phone, which is far worse than a false grant.

    `trial` expiry IS enforced. We write that timestamp ourselves at account
    creation, nothing external can change it, so a past value is trustworthy.
    Before this function existed the gate never compared it to the clock, which
    left trials serving paid features indefinitely after lapsing.

    `active` expiry is NOT enforced, only flagged. That timestamp is advanced by
    Apple's DID_RENEW notification, so a past value means either the subscription
    lapsed or the notification never arrived — indistinguishable from Firestore
    alone. Denying on that ambiguity would cut off customers Apple is still
    billing. Callers should reconcile flagged accounts against the App Store
    Server API out of band, and only then write a terminal status.
    """
    if not contractor:
        return True, "no_contractor"

    status = str(contractor.get("subscription_status") or "").strip().lower()
    raw_expires = contractor.get("subscription_expires")
    expires = raw_expires if isinstance(raw_expires, (int, float)) else 0.0

    if not expires:
        # Unknown expiry is unknown state, not expired state.
        return True, "missing_expiry"

    if status in _LOCALLY_MANAGED_STATUSES:
        # Derived from trial_start, not the stored expiry — see trial_expires_at.
        if trial_expires_at(contractor) > now:
            return True, "trial_active"
        return False, "trial_expired"

    if status in _APPLE_MANAGED_STATUSES:
        if expires > now:
            return True, "subscription_active"
        return True, "active_stale_expiry_needs_reconciliation"

    if status in _TERMINAL_STATUSES:
        if expires > now:
            return True, "expired_grace_period"
        return False, "expired"

    return True, "unrecognized_status"


# How long a number must be quiet before we will hand it back to Twilio.
NUMBER_RELEASE_QUIET_DAYS = 14


def is_safe_to_release_number(contractor: Optional[dict], now: float) -> bool:
    """True only if releasing this contractor's Twilio number cannot strand a forward.

    Two independent conditions, both required:

    1. App deletion was detected at least NUMBER_RELEASE_QUIET_DAYS ago.
    2. No carrier-confirmed forwarded call has arrived within that same window.

    Condition 2 is the one that matters. Twilio reassigns released numbers to other
    customers after the FCC's 45-day aging period. If a user's forward is still
    live when we release, their missed calls and voicemails start landing on a
    stranger's phone system — and nothing in our stack would ever notice. Holding
    the number instead costs about a dollar a month, so this fails closed on every
    ambiguity: missing signal, unparseable timestamp, or absent record all mean no.

    Note that condition 2 can only ever *block* a release. ForwardedFrom is
    carrier-dependent, so its absence is not evidence the forward is gone — it is
    simply the absence of a reason to keep waiting.
    """
    if not contractor:
        return False

    quiet_window = NUMBER_RELEASE_QUIET_DAYS * 86400

    deleted_at = contractor.get("deleted_app_detected_at")
    if not isinstance(deleted_at, (int, float)) or not deleted_at:
        return False
    if now - deleted_at < quiet_window:
        return False

    if "forwarding_last_seen_at" in contractor:
        last_seen = contractor.get("forwarding_last_seen_at")
        if last_seen is not None:
            if not isinstance(last_seen, (int, float)) or not last_seen:
                # Present but unreadable — we cannot rule out a live forward.
                return False
            if now - last_seen < quiet_window:
                return False

    return True


def should_expire_trial(contractor: Optional[dict], now: float) -> bool:
    """True if this contractor is a lapsed trial that should be marked expired.

    Deliberately narrower than "not allowed by the gate": it only ever selects
    `trial` accounts, never `active` ones. An `active` account with a past expiry
    may simply be missing an Apple renewal notification, and writing a terminal
    status on that guess would cancel service for a paying customer. Those are
    reconciled against the App Store Server API instead.
    """
    if not contractor:
        return False
    if str(contractor.get("subscription_status") or "").strip().lower() not in _LOCALLY_MANAGED_STATUSES:
        return False
    expires = trial_expires_at(contractor)
    if not expires:
        return False
    return expires <= now


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of an Apple App Store transaction verification.

    Three terminal states:

    - ``ok=True`` and ``transaction`` populated: Apple confirmed a valid receipt.
      The subscription state described by ``transaction`` should be applied.

    - ``ok=False`` and ``unreachable=False``: Apple replied authoritatively with
      a non-success state (bundle mismatch, malformed payload, transaction not
      found in any environment, etc.). This is *not* a successful verification.
      Caller should reject the request — do not assume entitlement.

    - ``ok=False`` and ``unreachable=True``: Apple was unreachable, returned an
      unexpected shape, or our request itself failed. The verification could
      not be completed. The caller MUST fail closed (return 502 / retry); it
      MUST NOT claim the subscription is verified.
    """

    ok: bool
    transaction: Optional[dict] = None
    unreachable: bool = False
    reason: str = ""

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


def _get_transaction_lookup_urls() -> list[str]:
    """Return App Store Server API bases to try for transaction lookup.

    Apple's guidance is production first when the transaction environment is
    unknown, then sandbox only for TransactionIdNotFoundError. Staging stays
    sandbox-only to avoid accidentally touching production App Store data.
    """
    if settings.appstore_environment == "production":
        return [APPSTORE_PRODUCTION_URL, APPSTORE_SANDBOX_URL]
    return [APPSTORE_SANDBOX_URL]


def _is_transaction_not_found(response: httpx.Response) -> bool:
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except Exception:
        return False
    return str(body.get("errorCode", "")) == "4040010"


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


def _decode_jws_payload(signed_payload: str) -> Optional[dict]:
    """Decode the payload section of an Apple signed JWS."""
    parts = signed_payload.split(".")
    if len(parts) != 3:
        logger.error("Invalid App Store JWS format")
        return None

    try:
        padded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded_payload))
    except Exception as e:
        logger.error(f"Failed to decode App Store JWS payload: {e}", exc_info=True)
        return None


def _extract_transaction_info(response_body: dict) -> Optional[dict]:
    """Normalize Apple's transaction lookup response to decoded transaction info."""
    signed_transaction = response_body.get("signedTransactionInfo")
    if signed_transaction:
        transaction_info = _decode_jws_payload(signed_transaction)
        if not transaction_info:
            return None
        bundle_id = transaction_info.get("bundleId", "")
        if bundle_id and bundle_id != settings.appstore_bundle_id:
            logger.error(f"App Store transaction bundle ID mismatch: {bundle_id}")
            return None
        return transaction_info

    # Unit tests and older internal callers may already pass decoded transaction info.
    if response_body.get("productId") or response_body.get("appAccountToken"):
        return response_body

    logger.error("App Store transaction response missing signedTransactionInfo")
    return None


def _active_subscription_expires_ts(transaction_info: dict) -> Optional[float]:
    """Return a future expiry timestamp for an active transaction, else None."""
    if transaction_info.get("revocationDate") or transaction_info.get("revokedDate"):
        logger.warning(
            "Rejecting revoked App Store transaction: product=%s revocationDate=%s",
            transaction_info.get("productId", ""),
            transaction_info.get("revocationDate") or transaction_info.get("revokedDate"),
        )
        return None

    try:
        expires_ms = float(transaction_info.get("expiresDate") or 0)
    except (TypeError, ValueError):
        expires_ms = 0

    if expires_ms <= 0:
        logger.warning(
            "Rejecting App Store subscription transaction without expiresDate: product=%s",
            transaction_info.get("productId", ""),
        )
        return None

    expires_ts = expires_ms / 1000.0
    if expires_ts <= time.time():
        logger.warning(
            "Rejecting expired App Store transaction: product=%s expires=%s",
            transaction_info.get("productId", ""),
            expires_ts,
        )
        return None

    return expires_ts


async def verify_transaction_strict(transaction_id: str) -> VerificationResult:
    """Verify a transaction with Apple's App Store Server API (fail-closed).

    Distinguishes three outcomes:
    - Apple says the receipt is valid → ``ok=True`` with ``transaction``.
    - Apple says the receipt is invalid / not found / bundle mismatch → ``ok=False``,
      ``unreachable=False``. This is an authoritative *negative* answer.
    - We could not get an authoritative answer (network error, 5xx, unexpected
      response shape, or App Store API not configured) → ``ok=False``,
      ``unreachable=True``. Caller MUST NOT treat as success.

    Importantly, we do NOT collapse "Apple unreachable" into "verification
    failed"; the iOS-facing `/api/subscription/verify` endpoint depends on
    that distinction so it can return HTTP 502 + Retry-After rather than
    silently claiming success.
    """
    if not settings.appstore_key_id:
        logger.warning("App Store API not configured — skipping verification")
        return VerificationResult(ok=False, unreachable=True, reason="not_configured")

    try:
        token = _get_appstore_jwt()
    except Exception as e:  # noqa: BLE001
        logger.error("App Store JWT generation failed: %s", e, exc_info=True)
        return VerificationResult(ok=False, unreachable=True, reason="jwt_error")

    try:
        async with httpx.AsyncClient() as client:
            lookup_urls = _get_transaction_lookup_urls()
            last_status: Optional[int] = None
            last_url: Optional[str] = None
            for index, base_url in enumerate(lookup_urls):
                url = f"{base_url}/inApps/v1/transactions/{transaction_id}"
                last_url = url
                try:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10.0,
                    )
                except httpx.HTTPError as e:
                    logger.error(
                        "App Store transaction lookup transport error at %s: %s",
                        url, e,
                    )
                    return VerificationResult(
                        ok=False, unreachable=True, reason="transport_error"
                    )
                last_status = response.status_code

                if response.status_code == 200:
                    try:
                        body = response.json()
                    except Exception as e:  # noqa: BLE001
                        logger.error("App Store 200 response not JSON: %s", e)
                        return VerificationResult(
                            ok=False, unreachable=True, reason="bad_json"
                        )
                    transaction_info = _extract_transaction_info(body)
                    if transaction_info is None:
                        # _extract_transaction_info already logged the reason. This
                        # is an authoritative rejection (bundle mismatch, missing
                        # signedTransactionInfo) — not a transient failure.
                        return VerificationResult(
                            ok=False, unreachable=False, reason="invalid_payload"
                        )
                    return VerificationResult(ok=True, transaction=transaction_info)

                should_try_sandbox = (
                    index == 0
                    and len(lookup_urls) > 1
                    and base_url == APPSTORE_PRODUCTION_URL
                    and _is_transaction_not_found(response)
                )
                if should_try_sandbox:
                    logger.info(
                        "Production App Store transaction lookup returned 4040010; retrying sandbox for %s",
                        transaction_id,
                    )
                    continue

                # Authoritative not-found: Apple knows the transaction does not exist.
                if _is_transaction_not_found(response):
                    return VerificationResult(
                        ok=False, unreachable=False, reason="not_found"
                    )

                # 4xx that isn't "not found" is treated as authoritative invalidity
                # (e.g. bad request) — except 401/403 which suggest *our* credentials
                # are misconfigured, which is unreachable from the user's POV.
                if response.status_code in (401, 403):
                    logger.error(
                        "App Store auth failure at %s: %s %s",
                        url,
                        response.status_code,
                        response.text[:200],
                    )
                    return VerificationResult(
                        ok=False, unreachable=True, reason="auth_error"
                    )
                if 400 <= response.status_code < 500:
                    logger.error(
                        "App Store transaction lookup rejected at %s: %s %s",
                        url,
                        response.status_code,
                        response.text[:200],
                    )
                    return VerificationResult(
                        ok=False, unreachable=False, reason="rejected"
                    )

                # 5xx and anything else is unreachable — Apple's side is unhappy.
                logger.error(
                    "App Store transaction lookup server error at %s: %s %s",
                    url,
                    response.status_code,
                    response.text[:200],
                )
                return VerificationResult(
                    ok=False, unreachable=True, reason="server_error"
                )

            # Loop exhausted without an authoritative answer.
            logger.error(
                "App Store transaction lookup exhausted candidates last_status=%s last_url=%s",
                last_status, last_url,
            )
            return VerificationResult(ok=False, unreachable=True, reason="exhausted")
    except Exception as e:  # noqa: BLE001
        logger.error(f"App Store API error: {e}", exc_info=True)
        return VerificationResult(ok=False, unreachable=True, reason="exception")


async def verify_transaction(transaction_id: str) -> Optional[dict]:
    """Backward-compatible wrapper. Returns transaction dict on success, else None.

    Note: this collapses "Apple unreachable" and "Apple says invalid" into a
    single None result and is therefore *not safe* to use for fail-closed
    decisions. Use ``verify_transaction_strict`` from new code.
    """
    result = await verify_transaction_strict(transaction_id)
    return result.transaction if result.ok else None


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


def _resolve_original_transaction_id(transaction_info: dict) -> str:
    """Extract the stable original_transaction_id, falling back to transaction_id."""
    original = transaction_info.get("originalTransactionId") or transaction_info.get("original_transaction_id")
    if original:
        return str(original)
    # Fall back to transactionId — in StoreKit 2 these are equal for the very
    # first transaction in a subscription chain. Better to bind something than
    # nothing.
    txid = transaction_info.get("transactionId") or transaction_info.get("transaction_id")
    return str(txid) if txid else ""


class CrossContractorReceiptError(Exception):
    """Raised when an Apple receipt has already been bound to a different contractor."""

    def __init__(self, original_transaction_id: str, owner_contractor_id: str):
        super().__init__(
            f"original_transaction_id={original_transaction_id} already bound to {owner_contractor_id}"
        )
        self.original_transaction_id = original_transaction_id
        self.owner_contractor_id = owner_contractor_id


async def update_subscription_from_transaction(contractor_id: str, transaction_info: dict) -> bool:
    """Update contractor subscription status from a verified Apple transaction.

    Returns True if updated successfully. Raises CrossContractorReceiptError if
    the underlying Apple receipt has already been claimed by a different
    contractor (F-06: global receipt-replay defense).
    """
    from app.db.contractors import update_contractor
    from app.db.apple_transactions import claim_transaction

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
        # F-16: log only the first 8 chars of each UUID. Sufficient to
        # correlate across logs without disclosing the full user-identifying
        # values to anyone with log read access.
        logger.error(
            "appAccountToken mismatch: expected_prefix=%s got_prefix=%s",
            (expected_uuid[:8] if expected_uuid else "(empty)"),
            (app_account_token[:8] if app_account_token else "(empty)"),
        )
        return False

    expires_ts = _active_subscription_expires_ts(transaction_info)
    if not expires_ts:
        return False

    # Global receipt-replay defense (F-06): bind original_transaction_id to this
    # contractor atomically. If a different contractor already claimed it, fail.
    original_id = _resolve_original_transaction_id(transaction_info)
    if original_id:
        ok, owner = await claim_transaction(
            original_transaction_id=original_id,
            contractor_id=contractor_id,
            transaction_id=str(transaction_info.get("transactionId", "")),
            product_id=product_id,
            environment=str(transaction_info.get("environment", "")),
        )
        if not ok:
            raise CrossContractorReceiptError(original_id, owner or "unknown")

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
    transaction_info = _decode_jws_payload(signed_transaction) if signed_transaction else {}

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
        # F-16: redact the appAccountToken UUID to prefix-only.
        logger.warning(
            "Contractor not found for appAccountToken (subscription_uuid prefix=%s)",
            (app_account_token[:8] if app_account_token else "(empty)"),
        )
        return False
    contractor_id = docs[0].id

    # F-06: global receipt-replay defense. The original_transaction_id is the
    # stable Apple receipt identity; reject if it has already been bound to a
    # different contractor than the one that owns this appAccountToken.
    from app.db.apple_transactions import claim_transaction
    original_id = _resolve_original_transaction_id(transaction_info)
    if original_id:
        ok, owner = await claim_transaction(
            original_transaction_id=original_id,
            contractor_id=contractor_id,
            transaction_id=str(transaction_info.get("transactionId", "")),
            product_id=str(transaction_info.get("productId", "")),
            environment=str(transaction_info.get("environment", "")),
        )
        if not ok:
            logger.error(
                "App Store notification cross-contractor reject: original_tx=%s contractor=%s already_bound_to=%s type=%s",
                original_id, contractor_id, owner, notification_type,
            )
            return False

    if notification_type in ("DID_RENEW", "SUBSCRIBED"):
        product_id = transaction_info.get("productId", "")
        tier = PRODUCT_TO_TIER.get(product_id, "")
        expires_ts = _active_subscription_expires_ts(transaction_info)
        if tier and expires_ts:
            await update_contractor(contractor_id, {
                "subscription_tier": tier,
                "subscription_status": "active",
                "subscription_expires": expires_ts,
            })
            logger.info(f"Subscription renewed: {contractor_id} → {tier}")
        else:
            logger.warning(f"Rejected active subscription notification: {contractor_id} product={product_id}")
            return False

    elif notification_type in ("EXPIRED", "DID_FAIL_TO_RENEW", "REFUND", "REVOKE"):
        await update_contractor(contractor_id, {
            "subscription_status": "expired",
        })
        logger.info(f"Subscription expired/cancelled: {contractor_id} type={notification_type}")

        # Send push so the user knows their trial/subscription ended
        try:
            from app.services.push_notification import send_regular_push, get_device_token
            device_token = await get_device_token(contractor_id=contractor_id)
            if device_token:
                await send_regular_push(
                    device_token=device_token,
                    title="Your Kevin subscription has ended",
                    body="Subscribe to keep Kevin screening your calls.",
                )
        except Exception as push_err:
            logger.warning(f"Expiry push failed (non-critical): {push_err}")

    return True
