"""Apple App Store Server API integration.

Handles subscription verification, promotional offer signing, and
Apple Server Notifications V2 webhook processing.
"""

import asyncio
import base64
import json
import math
import time
import uuid
from dataclasses import dataclass
from enum import Enum
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
        # An explicitly terminal status is known state, not unknown state, so the
        # fail-open default must not apply to it. Returning early here would let
        # an `expired` or `cancelled` account regain full access purely because
        # its timestamp was missing.
        if status in _TERMINAL_STATUSES:
            return False, "expired"
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


class SubscriptionUpdateOutcome(str, Enum):
    """Typed result of applying an Apple-verified transaction."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    UNKNOWN_PRODUCT = "unknown_product"
    MALFORMED_TRANSACTION = "malformed_transaction"


@dataclass(frozen=True)
class SubscriptionUpdateResult:
    """Outcome of applying a transaction that Apple already verified."""

    outcome: SubscriptionUpdateOutcome
    reason: str = ""

    @property
    def entitlement_active(self) -> bool:
        return self.outcome is SubscriptionUpdateOutcome.ACTIVE


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


def parse_revocation_date(transaction_info: dict) -> tuple[bool, Optional[float], Optional[str]]:
    """Parse optional revocationDate / revokedDate from transaction payload.

    Returns (is_revoked, revocation_ts_seconds, error_reason).
    - If neither key present: (False, None, None) -> not revoked
    - If valid and revoked: (True, float_seconds, None)
    - If malformed: (False, None, error_description)
    """
    if not isinstance(transaction_info, dict):
        return False, None, "malformed_revocation_date"

    has_canon = "revocationDate" in transaction_info
    has_alias = "revokedDate" in transaction_info
    if not has_canon and not has_alias:
        return False, None, None

    val_canon = None
    if has_canon:
        val = transaction_info["revocationDate"]
        if (
            val is None
            or isinstance(val, bool)
            or not isinstance(val, (int, float))
            or not math.isfinite(val)
            or val <= 0
        ):
            return False, None, "malformed_revocation_date"
        val_canon = val

    val_alias = None
    if has_alias:
        val = transaction_info["revokedDate"]
        if (
            val is None
            or isinstance(val, bool)
            or not isinstance(val, (int, float))
            or not math.isfinite(val)
            or val <= 0
        ):
            return False, None, "malformed_revocation_date"
        val_alias = val

    if has_canon and has_alias:
        if val_canon != val_alias:
            return False, None, "conflicting_revocation_date"
        return True, float(val_canon) / 1000.0, None

    if has_canon:
        return True, float(val_canon) / 1000.0, None

    return True, float(val_alias) / 1000.0, None


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
    is_revoked, rev_ts, rev_err = parse_revocation_date(transaction_info)
    if is_revoked or rev_err is not None:
        logger.warning(
            "Rejecting revoked or malformed App Store transaction: product=%s",
            transaction_info.get("productId", ""),
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
    return await get_processed_transaction(contractor_id, transaction_id) is not None


async def get_processed_transaction(
    contractor_id: str, transaction_id: str
) -> Optional[dict]:
    """Return a processed transaction record, preserving its entitlement outcome.

    Records written before outcomes were introduced only exist after a successful
    activation, so they are safely interpreted as ``active``.
    """
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_path = f"contractors/{contractor_id}/transactions/{transaction_id}"
    doc = await loop.run_in_executor(None, lambda: db.document(doc_path).get())
    if not doc.exists:
        return None
    record = doc.to_dict() or {}
    record.setdefault("outcome", SubscriptionUpdateOutcome.ACTIVE.value)
    return record


async def mark_transaction_seen(
    contractor_id: str,
    transaction_id: str,
    outcome: SubscriptionUpdateOutcome = SubscriptionUpdateOutcome.ACTIVE,
):
    """Record a terminal transaction outcome to prevent replay."""
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_path = f"contractors/{contractor_id}/transactions/{transaction_id}"
    await loop.run_in_executor(
        None,
        lambda: db.document(doc_path).set(
            {
                "processed_at": time.time(),
                "outcome": outcome.value,
            }
        ),
    )


async def update_processed_transaction_outcome(
    contractor_id: str,
    transaction_id: str,
    outcome: SubscriptionUpdateOutcome,
):
    """Update only outcome on an existing processed transaction record."""
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_path = f"contractors/{contractor_id}/transactions/{transaction_id}"
    await loop.run_in_executor(
        None,
        lambda: db.document(doc_path).update(
            {
                "outcome": outcome.value,
            }
        ),
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


async def update_subscription_from_transaction(
    contractor_id: str, transaction_info: dict
) -> SubscriptionUpdateResult:
    """Update contractor subscription status from a verified Apple transaction.

    Returns a typed result so callers can distinguish a safely acknowledged
    inactive transaction from ownership and payload failures. Raises
    CrossContractorReceiptError if the underlying Apple receipt has already
    been claimed by a different contractor (F-06: global receipt-replay
    defense).
    """
    from app.db.apple_transactions import claim_transaction

    product_id = transaction_info.get("productId", "")
    if not isinstance(product_id, str) or not product_id.strip():
        logger.error(f"Unknown product ID shape: {product_id}")
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.UNKNOWN_PRODUCT,
            reason="unknown_product",
        )
    product_id = product_id.strip()
    tier = PRODUCT_TO_TIER.get(product_id)
    if not tier:
        logger.error(f"Unknown product ID: {product_id}")
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.UNKNOWN_PRODUCT,
            reason="unknown_product",
        )

    # Validate ownership: appAccountToken must match contractor's subscription_uuid
    from app.db.contractors import get_contractor
    app_account_token = transaction_info.get("appAccountToken", "")
    if not app_account_token or not isinstance(app_account_token, str):
        logger.error(f"appAccountToken missing in transaction for contractor {contractor_id}")
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.OWNERSHIP_MISMATCH,
            reason="missing_app_account_token",
        )
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
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.OWNERSHIP_MISMATCH,
            reason="app_account_token_mismatch",
        )

    # Reject malformed subscription payloads before claiming their receipt.
    # Apple auto-renewable subscription transactions always carry an expiry.
    raw_exp = transaction_info.get("expiresDate")
    if (
        raw_exp is None
        or isinstance(raw_exp, bool)
        or not isinstance(raw_exp, (int, float))
        or not math.isfinite(raw_exp)
        or raw_exp <= 0
    ):
        logger.warning(
            "Rejecting App Store subscription transaction without finite positive expiresDate: product=%s",
            product_id,
        )
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
            reason="missing_or_invalid_expiry",
        )
    expires_ms = float(raw_exp)

    # Direct verified entitlement requires an explicit originalTransactionId (F-06).
    has_canonical = "originalTransactionId" in transaction_info
    has_alias = "original_transaction_id" in transaction_info
    if not has_canonical and not has_alias:
        logger.error("Verified App Store transaction missing explicit originalTransactionId")
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
            reason="missing_transaction_id",
        )

    s_canon = None
    if has_canonical:
        v = transaction_info["originalTransactionId"]
        if not isinstance(v, str) or not v.strip():
            logger.error("Verified App Store transaction has invalid canonical originalTransactionId")
            return SubscriptionUpdateResult(
                SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
                reason="missing_transaction_id",
            )
        s_canon = v.strip()

    s_alias = None
    if has_alias:
        v = transaction_info["original_transaction_id"]
        if not isinstance(v, str) or not v.strip():
            logger.error("Verified App Store transaction has invalid alias original_transaction_id")
            return SubscriptionUpdateResult(
                SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
                reason="missing_transaction_id",
            )
        s_alias = v.strip()

    if has_canonical and has_alias:
        if s_canon != s_alias:
            logger.error("Verified App Store transaction has conflicting originalTransactionId aliases")
            return SubscriptionUpdateResult(
                SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
                reason="missing_transaction_id",
            )
        original_id = s_canon
    elif has_canonical:
        original_id = s_canon
    else:
        original_id = s_alias

    # Validate revocationDate / revokedDate shape before claim
    is_revoked, rev_ts, rev_err = parse_revocation_date(transaction_info)
    if rev_err is not None:
        logger.error("Verified App Store transaction has malformed revocation date: %s", rev_err)
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
            reason=rev_err,
        )

    ok, owner = await claim_transaction(
        original_transaction_id=original_id,
        contractor_id=contractor_id,
        transaction_id=str(transaction_info.get("transactionId", "")),
        product_id=product_id,
        environment=str(transaction_info.get("environment", "")),
    )
    if not ok:
        raise CrossContractorReceiptError(original_id, owner or "unknown")

    if is_revoked:
        logger.warning(
            "Acknowledging revoked App Store transaction without entitlement: product=%s",
            product_id,
        )
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.INACTIVE,
            reason="revoked",
        )

    expires_ts = expires_ms / 1000.0
    if expires_ts <= time.time():
        logger.info(
            "Acknowledging expired App Store transaction without entitlement: product=%s",
            product_id,
        )
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.INACTIVE,
            reason="expired",
        )

    from app.db.contractors import activate_subscription_entitlement

    ok = await activate_subscription_entitlement(
        contractor_id=contractor_id,
        tier=tier,
        expires_ts=expires_ts,
        original_transaction_id=original_id,
        expected_subscription_uuid=app_account_token,
    )
    if not ok:
        logger.error(f"Failed to activate subscription entitlement for contractor {contractor_id}")
        return SubscriptionUpdateResult(
            SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
            reason="activation_failed",
        )
    logger.info(f"Subscription updated: contractor={contractor_id} tier={tier}")
    return SubscriptionUpdateResult(SubscriptionUpdateOutcome.ACTIVE)


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


def _validate_cross_payload_field(
    d1: dict, d2: dict, field: str, required: bool = False
) -> tuple[bool, Optional[str]]:
    """Validate and extract a string field across transaction and renewal payloads.

    Rules (verifier round 2 defect 1.2):
    - If a key is present in either payload, it MUST be a non-empty string.
      Present null, bool, number, list, dict, or whitespace-only is malformed -> (False, None).
    - If present in both, stripped values must agree exactly.
    - If required=True, at least one payload must contain a valid value.
    - If required=False and absent in both, returns (True, None).
    """
    has1 = field in d1
    has2 = field in d2
    v1 = d1.get(field)
    v2 = d2.get(field)

    s1 = None
    if has1:
        if not isinstance(v1, str) or not v1.strip():
            return False, None
        s1 = v1.strip()

    s2 = None
    if has2:
        if not isinstance(v2, str) or not v2.strip():
            return False, None
        s2 = v2.strip()

    if has1 and has2:
        if s1 != s2:
            return False, None
        return True, s1

    if has1:
        return True, s1
    if has2:
        return True, s2

    if required:
        return False, None
    return True, None


async def handle_appstore_notification(payload: dict) -> bool:
    """Process an App Store Server Notification V2 payload.

    payload is the decoded signed payload (already JWT-verified by the webhook).
    Returns True if handled successfully.
    """
    from app.db.apple_transactions import claim_transaction
    from app.db.contractors import (
        conditionally_backfill_subscription_binding,
        get_contractor_by_apple_user_id,
        get_contractor_by_subscription_uuid,
        is_missing_or_malformed_binding,
        record_active_renewal_status,
        record_inactive_notification,
        update_contractor,
    )

    notification_type = payload.get("notificationType", "")
    subtype = payload.get("subtype", "")
    renewal_info = payload.get("data", {})

    if notification_type == "DID_CHANGE_RENEWAL_STATUS":
        if subtype not in ("AUTO_RENEW_DISABLED", "AUTO_RENEW_ENABLED"):
            logger.warning(f"Invalid subtype for DID_CHANGE_RENEWAL_STATUS: {subtype}")
            return False

        if not isinstance(renewal_info, dict):
            logger.warning("Missing or non-dict data in DID_CHANGE_RENEWAL_STATUS")
            return False

        signed_transaction = renewal_info.get("signedTransactionInfo")
        signed_renewal = renewal_info.get("signedRenewalInfo")
        if (
            not isinstance(signed_transaction, str)
            or not signed_transaction.strip()
            or not isinstance(signed_renewal, str)
            or not signed_renewal.strip()
        ):
            logger.warning("Missing or non-string signedTransactionInfo/signedRenewalInfo in DID_CHANGE_RENEWAL_STATUS")
            return False

        transaction_info = _decode_jws_payload(signed_transaction)
        if not isinstance(transaction_info, dict):
            logger.warning("Decoded signedTransactionInfo is not a dict")
            return False

        rn_info = _decode_jws_payload(signed_renewal)
        if not isinstance(rn_info, dict):
            logger.warning("Decoded signedRenewalInfo is not a dict")
            return False

        auto_renew_status = rn_info.get("autoRenewStatus")
        if type(auto_renew_status) is not int or auto_renew_status not in (0, 1):
            logger.warning("Invalid autoRenewStatus (must be int 0 or 1): %s", type(auto_renew_status))
            return False
        if subtype == "AUTO_RENEW_DISABLED" and auto_renew_status != 0:
            logger.warning("autoRenewStatus %s disagrees with subtype %s", auto_renew_status, subtype)
            return False
        if subtype == "AUTO_RENEW_ENABLED" and auto_renew_status != 1:
            logger.warning("autoRenewStatus %s disagrees with subtype %s", auto_renew_status, subtype)
            return False

        signed_date = rn_info.get("signedDate")
        if type(signed_date) is not int or isinstance(signed_date, bool) or signed_date <= 0:
            logger.warning("Invalid signedDate (must be positive int): %s", signed_date)
            return False

        ok, app_account_token = _validate_cross_payload_field(transaction_info, rn_info, "appAccountToken", required=True)
        if not ok or not app_account_token:
            logger.warning("Invalid or missing appAccountToken across payloads")
            return False

        ok, original_id = _validate_cross_payload_field(transaction_info, rn_info, "originalTransactionId", required=True)
        if not ok or not original_id:
            logger.warning("Invalid or missing originalTransactionId across payloads")
            return False

        ok, product_id = _validate_cross_payload_field(transaction_info, rn_info, "productId", required=False)
        if not ok:
            logger.warning("Invalid or mismatched productId across payloads")
            return False
        product_id = product_id or ""

        ok, env = _validate_cross_payload_field(transaction_info, rn_info, "environment", required=False)
        if not ok:
            logger.warning("Invalid or mismatched environment across payloads")
            return False
        env = env or ""

        # Validate revocation date shape before contractor lookup or writes (A2)
        is_revoked, rev_ts, rev_err = parse_revocation_date(transaction_info)
        if rev_err is not None:
            logger.error("App Store notification has malformed revocation date: %s", rev_err)
            return False

        contractor = await get_contractor_by_subscription_uuid(app_account_token)
        if contractor and contractor.get("active") is True:
            contractor_id = contractor["contractor_id"]

            tx_id = str(transaction_info.get("transactionId", "") or "")
            ok, owner = await claim_transaction(
                original_transaction_id=original_id,
                contractor_id=contractor_id,
                transaction_id=tx_id,
                product_id=product_id,
                environment=env,
            )
            if not ok:
                logger.error(
                    "App Store notification cross-contractor reject: original_tx=%s contractor=%s already_bound_to=%s type=%s",
                    original_id, contractor_id, owner, notification_type,
                )
                return False

            # Conditional binding backfill for legacy active subscribers missing binding
            live_binding = contractor.get("subscription_original_transaction_id")
            if is_missing_or_malformed_binding(live_binding) and not is_revoked:
                tx_raw_exp = transaction_info.get("expiresDate")
                if (
                    tx_raw_exp is not None
                    and not isinstance(tx_raw_exp, bool)
                    and isinstance(tx_raw_exp, (int, float))
                    and math.isfinite(tx_raw_exp)
                    and tx_raw_exp > 0
                ):
                    tx_expires_ts = float(tx_raw_exp) / 1000.0
                    tx_tier = PRODUCT_TO_TIER.get(product_id, "")
                    if (
                        tx_expires_ts > time.time()
                        and tx_tier
                        and contractor.get("subscription_status") == "active"
                        and contractor.get("subscription_tier") == tx_tier
                    ):
                        stored_exp = contractor.get("subscription_expires")
                        if (
                            stored_exp is not None
                            and isinstance(stored_exp, (int, float))
                            and not isinstance(stored_exp, bool)
                            and abs(float(stored_exp) - tx_expires_ts) < 1.0
                        ):
                            backfill_res = await conditionally_backfill_subscription_binding(
                                contractor_id=contractor_id,
                                expected_subscription_uuid=app_account_token,
                                original_transaction_id=original_id,
                                expected_tier=tx_tier,
                                expected_expires_ts=tx_expires_ts,
                            )
                            logger.info(
                                "Renewal notification conditional binding backfill outcome=%s",
                                backfill_res.value,
                            )

            auto_renews = (auto_renew_status == 1)
            ok_active = await record_active_renewal_status(
                contractor_id=contractor_id,
                expected_subscription_uuid=app_account_token,
                original_transaction_id=original_id,
                auto_renews=auto_renews,
                signed_at_ms=signed_date,
            )
            if ok_active:
                logger.info(f"Active renewal status recorded: contractor={contractor_id} auto_renews={auto_renews}")
            else:
                logger.info(f"Active renewal status no-op (stale/mismatched): contractor={contractor_id}")
            return True

        inactive = await get_contractor_by_subscription_uuid(
            app_account_token, include_inactive=True
        )
        if inactive and inactive.get("active") is False:
            cid = inactive.get("contractor_id")
            if not cid or not isinstance(cid, str):
                return False
            tx_id = str(transaction_info.get("transactionId", "") or "")
            rebound = None
            apple_uid = str(inactive.get("apple_user_id") or "")
            if apple_uid:
                try:
                    rebound = await get_contractor_by_apple_user_id(apple_uid)
                except Exception as rebound_err:
                    logger.warning(
                        "Rebound lookup failed (recording without it): %s", rebound_err
                    )
            rebound_cid = rebound.get("contractor_id") if (isinstance(rebound, dict) and rebound.get("contractor_id")) else None
            renewal_obs = {
                "original_transaction_id": original_id,
                "auto_renews": (auto_renew_status == 1),
                "signed_at_ms": signed_date,
            }
            res = await record_inactive_notification(
                contractor_id=cid,
                expected_subscription_uuid=app_account_token,
                notification_type=notification_type,
                subtype=subtype,
                transaction_id=tx_id,
                purchase_date_ms=None,
                rebound_contractor_id=rebound_cid,
                renewal_observation=renewal_obs,
            )
            outcome = res.get("outcome")
            if outcome == "duplicate":
                logger.info(
                    "Post-deletion notification redelivery ignored: contractor=%s type=%s",
                    cid, notification_type,
                )
            elif outcome == "renewal_update":
                logger.info(
                    "Post-deletion renewal clock advanced for duplicate notification: contractor=%s type=%s signed_at=%s",
                    cid, notification_type, signed_date,
                )
            elif outcome == "recorded":
                if rebound:
                    logger.info(
                        "Post-deletion notification from rebound customer: contractor=%s now=%s type=%s",
                        cid, rebound["contractor_id"], notification_type,
                    )
                else:
                    logger.info(
                        "Post-deletion App Store notification recorded: contractor=%s type=%s",
                        cid, notification_type,
                    )
            else:
                logger.warning(
                    "Post-deletion notification write skipped due to state mismatch (%s): contractor=%s type=%s",
                    outcome, cid, notification_type,
                )
                return False
            return True

        logger.warning(
            "Contractor not found for appAccountToken (subscription_uuid prefix=%s)",
            (app_account_token[:8] if app_account_token else "(empty)"),
        )
        return False

    # Other notification types (DID_RENEW, SUBSCRIBED, EXPIRED, etc.)
    signed_transaction = renewal_info.get("signedTransactionInfo", "")
    transaction_info = _decode_jws_payload(signed_transaction) if signed_transaction else {}

    if not isinstance(transaction_info, dict):
        logger.warning(f"Invalid transaction payload in notification: type={notification_type}")
        return False

    # Strict optional revocation validation before contractor lookup, inactive recording, global claim, forwarding, or update
    is_revoked, rev_ts, rev_err = parse_revocation_date(transaction_info)
    if rev_err is not None:
        logger.error("App Store notification has malformed revocation date: %s", rev_err)
        return False

    app_account_token = transaction_info.get("appAccountToken", "")
    if not app_account_token or not isinstance(app_account_token, str):
        logger.warning(f"No appAccountToken in notification: type={notification_type}")
        return False

    contractor = await get_contractor_by_subscription_uuid(app_account_token)
    if not contractor:
        inactive = await get_contractor_by_subscription_uuid(
            app_account_token, include_inactive=True
        )
        if inactive and inactive.get("active") is False:
            cid = inactive["contractor_id"]
            tx_id = str(transaction_info.get("transactionId") or "")
            purchase_ms = transaction_info.get("purchaseDate")
            rebound = None
            apple_uid = str(inactive.get("apple_user_id") or "")
            if apple_uid:
                try:
                    rebound = await get_contractor_by_apple_user_id(apple_uid)
                except Exception as rebound_err:
                    logger.warning(
                        "Rebound lookup failed (recording without it): %s", rebound_err
                    )
            rebound_cid = rebound["contractor_id"] if rebound else None
            res = await record_inactive_notification(
                contractor_id=cid,
                expected_subscription_uuid=app_account_token,
                notification_type=notification_type,
                subtype=subtype,
                transaction_id=tx_id,
                purchase_date_ms=purchase_ms,
                rebound_contractor_id=rebound_cid,
            )
            outcome = res.get("outcome")
            if outcome == "duplicate":
                logger.info(
                    "Post-deletion notification redelivery ignored: contractor=%s type=%s",
                    cid, notification_type,
                )
                return True
            elif outcome != "recorded":
                logger.warning(
                    "Post-deletion notification write skipped due to state mismatch (%s): contractor=%s type=%s",
                    outcome, cid, notification_type,
                )
                return False

            if rebound:
                is_charge = notification_type in ("DID_RENEW", "SUBSCRIBED")
                if is_charge and rebound.get("subscription_status") != "active":
                    product_id = transaction_info.get("productId", "")
                    fwd_tier = PRODUCT_TO_TIER.get(product_id, "")
                    fwd_expires = _active_subscription_expires_ts(transaction_info)
                    if fwd_tier and fwd_expires:
                        try:
                            await update_contractor(rebound["contractor_id"], {
                                "subscription_status": "active",
                                "subscription_tier": fwd_tier,
                                "subscription_expires": fwd_expires,
                                "subscription_forwarded_from": cid,
                            })
                            logger.info(
                                "Rebound entitlement forwarded: %s -> %s tier=%s",
                                cid, rebound["contractor_id"], fwd_tier,
                            )
                        except Exception as fwd_err:
                            logger.error(
                                "Rebound entitlement forward failed: %s -> %s err=%s",
                                cid, rebound["contractor_id"], fwd_err,
                            )
                elif (
                    notification_type in ("EXPIRED", "DID_FAIL_TO_RENEW", "REFUND", "REVOKE")
                    and rebound.get("subscription_forwarded_from") == cid
                ):
                    try:
                        await update_contractor(rebound["contractor_id"], {
                            "subscription_status": "expired",
                        })
                        logger.info(
                            "Forwarded entitlement expired: %s -> %s type=%s",
                            cid, rebound["contractor_id"], notification_type,
                        )
                    except Exception as fwd_err:
                        logger.error(
                            "Forwarded entitlement expiry failed: %s -> %s err=%s",
                            cid, rebound["contractor_id"], fwd_err,
                        )

                logger.info(
                    "Post-deletion notification from rebound customer: contractor=%s now=%s type=%s",
                    cid, rebound["contractor_id"], notification_type,
                )
            elif res.get("charged_after_deletion"):
                logger.error(
                    "Post-deletion App Store charge recorded: contractor=%s type=%s count=%s",
                    cid, notification_type, res.get("count"),
                )
            else:
                logger.info(
                    "Post-deletion App Store notification recorded: contractor=%s type=%s",
                    cid, notification_type,
                )
            return True

        logger.warning(
            "Contractor not found for appAccountToken (subscription_uuid prefix=%s)",
            (app_account_token[:8] if app_account_token else "(empty)"),
        )
        return False
    contractor_id = contractor["contractor_id"]

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
                    contractor_id=contractor_id,
                )
        except Exception as push_err:
            logger.warning(f"Expiry push failed (non-critical): {push_err}")

    return True
