"""Acquisition and activation measurement service (default off).

Handles Apple Ads attribution token exchange with streaming bounds and explicit
leases, attribution recording with strict attempt budgets and spacing, inbound and
forwarded call milestone observation, and StoreKit payment and trial classification.
"""

import asyncio
import json
import math
import time
from typing import Any, Optional

import httpx
from google.cloud import firestore

from app.config import settings
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

APPLE_ADS_API_URL = "https://api-adservices.apple.com/api/v1/"
MAX_ATTEMPTS = 3
MIN_SPACING_SECONDS = 5.0
LEASE_LIFETIME_SECONDS = 15.0
HTTP_DEADLINE_SECONDS = 8.0
MAX_RESPONSE_BYTES = 16 * 1024  # 16 KiB
MAX_TOKEN_BYTES = 8192
MAX_SIGNED_64_BIT_INT = (1 << 63) - 1
MIN_TIMESTAMP_MS = 978307200000  # 2001-01-01T00:00:00Z in ms
MIN_TIMESTAMP_S = 978307200.0     # 2001-01-01T00:00:00Z in s
MAX_TIMESTAMP_S = 4102444800.0    # 2100-01-01T00:00:00Z in s

VALID_CONVERSION_TYPES = frozenset({"Download", "Redownload", "PreOrder"})
VALID_CLAIM_TYPES = frozenset({"Click", "Impression"})
VALID_SIGNUP_COUNTRIES = frozenset({"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"})
KNOWN_PLACEHOLDER_IDS = frozenset({1234567890})
MAX_TRANSCRIPT_BYTES = 64 * 1024  # 64 KiB
MAX_PENDING_SCREENING_TASKS = 32
_pending_screening_tasks: set[asyncio.Task] = set()


def get_expected_apple_ads_org_id() -> int:
    """Return the configured Apple Ads expected org ID, or 0 if unconfigured/invalid."""
    org_id = settings.apple_ads_expected_org_id
    if (
        type(org_id) is int
        and not isinstance(org_id, bool)
        and 0 < org_id <= MAX_SIGNED_64_BIT_INT
        and org_id not in KNOWN_PLACEHOLDER_IDS
    ):
        return org_id
    return 0


def is_acquisition_measurement_enabled() -> bool:
    """Return True if collection is enabled AND a valid expected org ID is configured."""
    return bool(
        settings.acquisition_measurement_enabled
        and get_expected_apple_ads_org_id() > 0
    )


def validate_acquisition_measurement_map(
    acq: Any,
) -> tuple[bool, int, Optional[float], Optional[int], Optional[float], float]:
    """Strictly validate stored acquisition_measurement map.

    Returns (is_valid, attempts, last_attempt_at, lease_attempt, lease_expires_at, created_at).
    """
    if not isinstance(acq, dict):
        return False, 0, None, None, None, 0.0

    schema_version = acq.get("schema_version")
    if type(schema_version) is not int or isinstance(schema_version, bool) or schema_version != 1:
        return False, 0, None, None, None, 0.0

    created_at = acq.get("created_at")
    cohort = acq.get("cohort")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
        or not (MIN_TIMESTAMP_S <= created_at <= MAX_TIMESTAMP_S)
    ):
        return False, 0, None, None, None, 0.0
    if (
        isinstance(cohort, bool)
        or not isinstance(cohort, (int, float))
        or not math.isfinite(cohort)
        or not (MIN_TIMESTAMP_S <= cohort <= MAX_TIMESTAMP_S)
        or abs(float(cohort) - float(created_at)) > 0.001
    ):
        return False, 0, None, None, None, 0.0

    created_ts = float(created_at)

    if "attempts" not in acq:
        return False, 0, None, None, None, 0.0
    attempts = acq["attempts"]
    if type(attempts) is not int or isinstance(attempts, bool) or not (0 <= attempts <= MAX_ATTEMPTS):
        return False, 0, None, None, None, 0.0

    last_attempt = acq.get("last_attempt_at")
    if attempts == 0:
        if last_attempt is not None:
            if (
                isinstance(last_attempt, bool)
                or not isinstance(last_attempt, (int, float))
                or not math.isfinite(last_attempt)
                or not (created_ts <= float(last_attempt) <= MAX_TIMESTAMP_S)
            ):
                return False, 0, None, None, None, 0.0
        last_attempt_ts = float(last_attempt) if last_attempt is not None else None
    else:
        if (
            last_attempt is None
            or isinstance(last_attempt, bool)
            or not isinstance(last_attempt, (int, float))
            or not math.isfinite(last_attempt)
            or float(last_attempt) < created_ts
            or float(last_attempt) > MAX_TIMESTAMP_S
        ):
            return False, 0, None, None, None, 0.0
        last_attempt_ts = float(last_attempt)

    lease_attempt = acq.get("lease_attempt")
    lease_expires = acq.get("lease_expires_at")
    if (lease_attempt is None) != (lease_expires is None):
        return False, 0, None, None, None, 0.0

    if lease_attempt is not None:
        if (
            type(lease_attempt) is not int
            or isinstance(lease_attempt, bool)
            or not (1 <= lease_attempt <= MAX_ATTEMPTS)
            or lease_attempt != attempts
        ):
            return False, 0, None, None, None, 0.0
        if (
            isinstance(lease_expires, bool)
            or not isinstance(lease_expires, (int, float))
            or not math.isfinite(lease_expires)
        ):
            return False, 0, None, None, None, 0.0
        if last_attempt_ts is None or not (last_attempt_ts <= float(lease_expires) <= last_attempt_ts + 15.0):
            return False, 0, None, None, None, 0.0
        lease_expires_ts = float(lease_expires)
    else:
        lease_expires_ts = None

    conv_at = acq.get("first_screening_conversation_observed_at")
    if conv_at is not None:
        if (
            isinstance(conv_at, bool)
            or not isinstance(conv_at, (int, float))
            or not math.isfinite(conv_at)
            or not (created_ts <= float(conv_at) <= MAX_TIMESTAMP_S)
        ):
            return False, 0, None, None, None, 0.0

    country = acq.get("account_country_at_signup")
    if country is not None:
        if (
            not isinstance(country, str)
            or isinstance(country, bool)
            or country not in VALID_SIGNUP_COUNTRIES
        ):
            return False, 0, None, None, None, 0.0

    return True, attempts, last_attempt_ts, lease_attempt, lease_expires_ts, created_ts


def validate_stored_attempts(acq: dict) -> tuple[bool, int, Optional[float]]:
    """Strictly validate stored attempts and last_attempt_at in acquisition_measurement.

    Returns (is_valid, attempts, last_attempt_at).
    """
    is_valid, attempts, last_attempt, _, _, _ = validate_acquisition_measurement_map(acq)
    return is_valid, attempts, last_attempt


async def check_contractor_acquisition_eligibility(contractor_id: str) -> bool:
    """Check if contractor belongs to eligible cohort and has remaining non-terminal budget.

    Returns False immediately if measurement is disabled before getting a DB client.
    """
    if not is_acquisition_measurement_enabled():
        return False

    if not contractor_id or not isinstance(contractor_id, str):
        return False

    from app.db.contractors import get_contractor

    try:
        contractor = await get_contractor(contractor_id)
    except Exception:
        return False

    if not contractor or contractor.get("active") is not True:
        return False

    acq = contractor.get("acquisition_measurement")
    is_valid, attempts, _, lease_attempt, lease_expires, _ = validate_acquisition_measurement_map(acq)
    if not is_valid:
        return False

    current_status = acq.get("attribution_status")
    if current_status in ("recorded", "already_recorded", "unattributed", "exhausted", "ineligible", "disabled"):
        return False

    if attempts >= MAX_ATTEMPTS:
        if not (lease_attempt is not None and lease_expires is not None and time.time() < lease_expires):
            return False

    return True


async def lease_attribution_attempt(contractor_id: str) -> dict:
    """Transactionally lease an attribution attempt with explicit lease fields.

    Returns dict conforming to status contract:
      - 'leased': includes 'lease_attempt'
      - 'retryable': includes 'retry_after_seconds'
      - 'already_recorded', 'unattributed', 'exhausted', 'ineligible', 'disabled'
    """
    if not is_acquisition_measurement_enabled():
        return {"status": "disabled"}

    if not contractor_id or not isinstance(contractor_id, str):
        return {"status": "ineligible"}

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()
    now = time.time()

    @firestore.transactional
    def _txn(transaction) -> dict:
        if not is_acquisition_measurement_enabled():
            return {"status": "disabled"}

        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return {"status": "ineligible"}

        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return {"status": "ineligible"}

        acq = data.get("acquisition_measurement")
        is_valid, attempts, last_attempt, lease_attempt, lease_expires, _ = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return {"status": "ineligible"}

        current_status = acq.get("attribution_status")
        if current_status == "recorded":
            return {"status": "already_recorded"}
        if current_status in ("unattributed", "exhausted", "ineligible", "disabled"):
            return {"status": current_status}

        # Check existing active lease (outstanding attempt in flight blocks duplicate exhaustion)
        if lease_attempt is not None and lease_expires is not None and now < lease_expires:
            delay = max(5, min(15, int(math.ceil(lease_expires - now))))
            return {"status": "retryable", "retry_after_seconds": delay}

        # Check minimum spacing from last attempt
        if last_attempt is not None and (now - last_attempt) < MIN_SPACING_SECONDS:
            return {"status": "retryable", "retry_after_seconds": 5}

        # Check attempt budget
        if attempts >= MAX_ATTEMPTS:
            transaction.update(doc_ref, {
                "acquisition_measurement.attribution_status": "exhausted",
                "acquisition_measurement.lease_attempt": None,
                "acquisition_measurement.lease_expires_at": None,
            })
            return {"status": "exhausted"}

        new_attempts = attempts + 1
        new_expires = now + LEASE_LIFETIME_SECONDS
        transaction.update(doc_ref, {
            "acquisition_measurement.attempts": new_attempts,
            "acquisition_measurement.last_attempt_at": now,
            "acquisition_measurement.lease_attempt": new_attempts,
            "acquisition_measurement.lease_expires_at": new_expires,
        })
        return {"status": "leased", "lease_attempt": new_attempts}

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


async def record_terminal_attribution(
    contractor_id: str,
    parsed_data: dict,
    captured_lease_attempt: int,
) -> str:
    """Record terminal attribution result guarded by captured lease_attempt."""
    if not is_acquisition_measurement_enabled():
        return "disabled"

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()
    now = time.time()

    @firestore.transactional
    def _txn(transaction) -> str:
        if not is_acquisition_measurement_enabled():
            return "disabled"

        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return "ineligible"

        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return "ineligible"

        acq = data.get("acquisition_measurement")
        is_valid, attempts, _, lease_attempt, _, _ = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return "ineligible"

        # Check if attribution was already recorded or terminal
        current_status = acq.get("attribution_status")
        if current_status in ("recorded", "unattributed", "exhausted", "ineligible", "disabled"):
            return current_status

        # Verify matching lease
        if lease_attempt != captured_lease_attempt:
            return "retryable" if attempts < MAX_ATTEMPTS else "exhausted"

        is_attributed = parsed_data.get("attribution") is True
        if is_attributed:
            updates = {
                "acquisition_measurement.attribution_status": "recorded",
                "acquisition_measurement.attribution": True,
                "acquisition_measurement.source": "apple_ads",
                "acquisition_measurement.attribution_recorded_at": now,
                "acquisition_measurement.org_id": parsed_data["org_id"],
                "acquisition_measurement.campaign_id": parsed_data["campaign_id"],
                "acquisition_measurement.ad_group_id": parsed_data["ad_group_id"],
                "acquisition_measurement.lease_attempt": None,
                "acquisition_measurement.lease_expires_at": None,
            }
            if "keyword_id" in parsed_data:
                updates["acquisition_measurement.keyword_id"] = parsed_data["keyword_id"]
            if "conversion_type" in parsed_data:
                updates["acquisition_measurement.conversion_type"] = parsed_data["conversion_type"]
            if "claim_type" in parsed_data:
                updates["acquisition_measurement.claim_type"] = parsed_data["claim_type"]
            transaction.update(doc_ref, updates)
            return "recorded"
        else:
            updates = {
                "acquisition_measurement.attribution_status": "unattributed",
                "acquisition_measurement.attribution": False,
                "acquisition_measurement.source": "unattributed",
                "acquisition_measurement.attribution_recorded_at": now,
                "acquisition_measurement.lease_attempt": None,
                "acquisition_measurement.lease_expires_at": None,
            }
            transaction.update(doc_ref, updates)
            return "unattributed"

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


async def finalize_failed_attempt(
    contractor_id: str,
    captured_lease_attempt: int,
) -> str:
    """Clear lease fields after a failed attempt; mark exhausted if max attempts reached."""
    if not is_acquisition_measurement_enabled():
        return "disabled"

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()

    @firestore.transactional
    def _txn(transaction) -> str:
        if not is_acquisition_measurement_enabled():
            return "disabled"

        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return "ineligible"

        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return "ineligible"

        acq = data.get("acquisition_measurement")
        is_valid, attempts, _, lease_attempt, _, _ = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return "ineligible"

        current_status = acq.get("attribution_status")
        if current_status in ("recorded", "unattributed", "exhausted", "ineligible", "disabled"):
            return current_status

        # Only mutate if this attempt owns the lease
        if lease_attempt != captured_lease_attempt:
            return "retryable" if attempts < MAX_ATTEMPTS else "exhausted"

        if attempts >= MAX_ATTEMPTS:
            transaction.update(doc_ref, {
                "acquisition_measurement.attribution_status": "exhausted",
                "acquisition_measurement.lease_attempt": None,
                "acquisition_measurement.lease_expires_at": None,
            })
            return "exhausted"
        else:
            transaction.update(doc_ref, {
                "acquisition_measurement.lease_attempt": None,
                "acquisition_measurement.lease_expires_at": None,
            })
            return "retryable"

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


def _validate_positive_int64(val: Any) -> Optional[int]:
    """Return positive signed-64-bit int, or None if invalid."""
    if type(val) is not int or isinstance(val, bool):
        return None
    if 0 < val <= MAX_SIGNED_64_BIT_INT:
        return val
    return None


def parse_apple_ads_response(raw_json: Any, expected_org_id: int) -> tuple[bool, Optional[dict], str]:
    """Validate Apple Ads API response according to the strict allowlist."""
    if not isinstance(raw_json, dict):
        return False, None, "not_a_dict"

    raw_attribution = raw_json.get("attribution")
    if type(raw_attribution) is not bool:
        return False, None, "attribution_not_strict_bool"

    if not raw_attribution:
        return True, {"attribution": False}, "unattributed"

    # Attribution is True: validate required identifiers
    org_id = _validate_positive_int64(raw_json.get("orgId"))
    if org_id is None:
        return False, None, "invalid_or_missing_org_id"
    if org_id != expected_org_id:
        return False, None, "org_id_mismatch"
    if org_id in KNOWN_PLACEHOLDER_IDS:
        return False, None, "placeholder_org_id"

    campaign_id = _validate_positive_int64(raw_json.get("campaignId"))
    if campaign_id is None:
        return False, None, "invalid_or_missing_campaign_id"
    if campaign_id in KNOWN_PLACEHOLDER_IDS:
        return False, None, "placeholder_campaign_id"

    ad_group_id = _validate_positive_int64(raw_json.get("adGroupId"))
    if ad_group_id is None:
        return False, None, "invalid_or_missing_ad_group_id"
    if ad_group_id in KNOWN_PLACEHOLDER_IDS:
        return False, None, "placeholder_ad_group_id"

    parsed: dict[str, Any] = {
        "attribution": True,
        "org_id": org_id,
        "campaign_id": campaign_id,
        "ad_group_id": ad_group_id,
    }

    if "keywordId" in raw_json:
        keyword_id = _validate_positive_int64(raw_json.get("keywordId"))
        if keyword_id is not None and keyword_id not in KNOWN_PLACEHOLDER_IDS:
            parsed["keyword_id"] = keyword_id

    if "conversionType" in raw_json:
        conv = raw_json.get("conversionType")
        if isinstance(conv, str) and conv in VALID_CONVERSION_TYPES:
            parsed["conversion_type"] = conv

    if "claimType" in raw_json:
        claim = raw_json.get("claimType")
        if isinstance(claim, str) and claim in VALID_CLAIM_TYPES:
            parsed["claim_type"] = claim

    return True, parsed, "ok"


async def process_apple_ads_attribution(contractor_id: str, token: str) -> dict:
    """Perform bounded, streaming Apple Ads attribution exchange under an 8-second deadline."""
    if not is_acquisition_measurement_enabled():
        return {"status": "disabled"}

    # Validate token UTF-8 bytes length
    try:
        token_bytes = token.encode("utf-8")
    except Exception:
        return {"status": "ineligible"}

    if not (1 <= len(token_bytes) <= MAX_TOKEN_BYTES):
        return {"status": "ineligible"}

    lease = await lease_attribution_attempt(contractor_id)
    lease_status = lease.get("status")
    if lease_status != "leased":
        if lease_status == "retryable":
            return {"status": "retryable", "retry_after_seconds": lease.get("retry_after_seconds", 5)}
        return {"status": lease_status}

    captured_lease_attempt = lease["lease_attempt"]
    expected_org_id = get_expected_apple_ads_org_id()

    # Stream POST with httpx under overall asyncio timeout of 8 seconds
    try:
        async with asyncio.timeout(HTTP_DEADLINE_SECONDS):
            timeout = httpx.Timeout(5.0, connect=3.0, read=5.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    APPLE_ADS_API_URL,
                    content=token_bytes,
                    headers={"Content-Type": "text/plain"},
                ) as response:
                    status_code = response.status_code
                    body_chunks = []
                    total_bytes = 0
                    exceeded = False
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > MAX_RESPONSE_BYTES:
                            exceeded = True
                            break
                        body_chunks.append(chunk)

            if exceeded:
                logger.warning("Apple Ads API response exceeded max body size")
                res_status = await finalize_failed_attempt(contractor_id, captured_lease_attempt)
                if res_status == "retryable":
                    return {"status": "retryable", "retry_after_seconds": 5}
                return {"status": res_status}

            if status_code == 200:
                try:
                    raw_json = json.loads(b"".join(body_chunks).decode("utf-8"))
                except Exception:
                    raw_json = None

                is_valid, parsed_data, reason = parse_apple_ads_response(raw_json, expected_org_id)
                if not is_valid or parsed_data is None:
                    logger.warning("Apple Ads API response failed validation")
                    res_status = await finalize_failed_attempt(contractor_id, captured_lease_attempt)
                    if res_status == "retryable":
                        return {"status": "retryable", "retry_after_seconds": 5}
                    return {"status": res_status}

                terminal_status = await record_terminal_attribution(contractor_id, parsed_data, captured_lease_attempt)
                return {"status": terminal_status}

            else:
                logger.warning("Apple Ads API returned non-200 status")
                res_status = await finalize_failed_attempt(contractor_id, captured_lease_attempt)
                if res_status == "retryable":
                    return {"status": "retryable", "retry_after_seconds": 5}
                return {"status": res_status}

    except (httpx.TransportError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
        logger.warning(f"Apple Ads network failure: {type(exc).__name__}")
        res_status = await finalize_failed_attempt(contractor_id, captured_lease_attempt)
        if res_status == "retryable":
            return {"status": "retryable", "retry_after_seconds": 5}
        return {"status": res_status}
    except Exception as exc:
        logger.warning(f"Apple Ads unexpected failure: {type(exc).__name__}")
        res_status = await finalize_failed_attempt(contractor_id, captured_lease_attempt)
        if res_status == "retryable":
            return {"status": "retryable", "retry_after_seconds": 5}
        return {"status": res_status}


async def record_inbound_call_measurement(contractor_id: str, seen_at: float) -> None:
    """Record first_inbound_observed_at once in acquisition_measurement if present."""
    if not is_acquisition_measurement_enabled():
        return
    if not contractor_id or not isinstance(contractor_id, str):
        return
    if (
        isinstance(seen_at, bool)
        or not isinstance(seen_at, (int, float))
        or not math.isfinite(seen_at)
        or not (MIN_TIMESTAMP_S <= seen_at <= MAX_TIMESTAMP_S)
    ):
        return

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()

    @firestore.transactional
    def _txn(transaction):
        if not is_acquisition_measurement_enabled():
            return
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return
        acq = data.get("acquisition_measurement")
        is_valid, _, _, _, _, created_ts = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return
        if acq.get("first_inbound_observed_at") is not None:
            return
        if float(seen_at) < created_ts:
            return
        transaction.update(doc_ref, {
            "acquisition_measurement.first_inbound_observed_at": float(seen_at)
        })

    transaction = db.transaction()
    try:
        await loop.run_in_executor(None, lambda: _txn(transaction))
    except Exception as e:
        logger.warning(f"Inbound call measurement failed: {type(e).__name__}")


async def record_forwarded_call_measurement(contractor_id: str, seen_at: float) -> None:
    """Record first_forwarded_observed_at once in acquisition_measurement if present."""
    if not is_acquisition_measurement_enabled():
        return
    if not contractor_id or not isinstance(contractor_id, str):
        return
    if (
        isinstance(seen_at, bool)
        or not isinstance(seen_at, (int, float))
        or not math.isfinite(seen_at)
        or not (MIN_TIMESTAMP_S <= seen_at <= MAX_TIMESTAMP_S)
    ):
        return

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()

    @firestore.transactional
    def _txn(transaction):
        if not is_acquisition_measurement_enabled():
            return
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return
        acq = data.get("acquisition_measurement")
        is_valid, _, _, _, _, created_ts = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return
        if acq.get("first_forwarded_observed_at") is not None:
            return
        if float(seen_at) < created_ts:
            return
        transaction.update(doc_ref, {
            "acquisition_measurement.first_forwarded_observed_at": float(seen_at)
        })

    transaction = db.transaction()
    try:
        await loop.run_in_executor(None, lambda: _txn(transaction))
    except Exception as e:
        logger.warning(f"Forwarded call measurement failed: {type(e).__name__}")


def classify_apple_transaction_payment(transaction_info: dict) -> dict:
    """Classify verified Apple transaction payload for acquisition measurement."""
    from app.services.subscription import PRODUCT_TO_TIER, parse_revocation_date

    res = {
        "is_positive_price_purchase": False,
        "purchase_signed_at": None,
        "paid_tier": None,
        "is_storekit_trial": False,
        "trial_signed_at": None,
    }

    if not isinstance(transaction_info, dict):
        return res

    # Environment must be Production
    if transaction_info.get("environment") != "Production":
        return res

    # Recognized product
    product_id = transaction_info.get("productId")
    if not isinstance(product_id, str) or not product_id.strip():
        return res
    tier = PRODUCT_TO_TIER.get(product_id.strip())
    if not tier:
        return res

    # Must not be revoked
    is_revoked, _, rev_err = parse_revocation_date(transaction_info)
    if is_revoked or rev_err is not None:
        return res

    # Valid purchaseDate: strict positive integer in ms between 2001-01-01 and (now + 300s)
    raw_pd = transaction_info.get("purchaseDate")
    if type(raw_pd) is not int or isinstance(raw_pd, bool):
        return res
    max_valid_pd_ms = int((time.time() + 300.0) * 1000.0)
    if not (MIN_TIMESTAMP_MS <= raw_pd <= max_valid_pd_ms):
        return res
    purchase_signed_ts = float(raw_pd) / 1000.0

    raw_price = transaction_info.get("price")
    offer_discount = transaction_info.get("offerDiscountType")
    is_free_trial_discount = (offer_discount == "FREE_TRIAL")

    # StoreKit free trial: explicit offerDiscountType=FREE_TRIAL with strict integer price=0
    if (
        is_free_trial_discount
        and type(raw_price) is int
        and not isinstance(raw_price, bool)
        and raw_price == 0
    ):
        res["is_storekit_trial"] = True
        res["trial_signed_at"] = purchase_signed_ts
        return res

    # Positive-price purchase: price > 0, not a free trial discount
    if (
        not is_free_trial_discount
        and type(raw_price) is int
        and not isinstance(raw_price, bool)
        and raw_price > 0
    ):
        res["is_positive_price_purchase"] = True
        res["purchase_signed_at"] = purchase_signed_ts
        res["paid_tier"] = tier
        return res

    return res


VALID_PAYMENT_TIERS = frozenset({"personal", "business", "businessPro"})
MAX_PENDING_PAYMENT_TASKS = 32
_pending_payment_tasks: set[asyncio.Task] = set()
PAYMENT_CLASSIFICATION_FIELDS = frozenset({
    "environment",
    "productId",
    "revocationDate",
    "purchaseDate",
    "price",
    "offerDiscountType",
})


def schedule_payment_measurement(
    contractor_id: str,
    tier: str,
    transaction_info: dict,
) -> None:
    """Synchronously enqueue payment measurement task without awaiting or blocking."""
    if not is_acquisition_measurement_enabled():
        return
    if not contractor_id or not isinstance(contractor_id, str):
        return
    if not isinstance(tier, str) or tier not in VALID_PAYMENT_TIERS:
        return
    if not isinstance(transaction_info, dict):
        return
    if len(_pending_payment_tasks) >= MAX_PENDING_PAYMENT_TASKS:
        return

    clean_tx = {
        k: transaction_info[k]
        for k in PAYMENT_CLASSIFICATION_FIELDS
        if k in transaction_info
    }

    coro = record_payment_measurement(contractor_id, tier, clean_tx)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return

    try:
        task = loop.create_task(coro)
    except Exception:
        coro.close()
        return

    _pending_payment_tasks.add(task)

    def _done_cb(t: asyncio.Task) -> None:
        _pending_payment_tasks.discard(t)
        if t.cancelled():
            return
        try:
            exc = t.exception()
            if exc:
                logger.warning(f"Payment measurement task failed: {type(exc).__name__}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Payment measurement task error: {type(e).__name__}")

    task.add_done_callback(_done_cb)


async def record_payment_measurement(
    contractor_id: str,
    tier: str,
    transaction_info: dict,
) -> None:
    """Record verified entitlement, positive price purchase, or trial into acquisition_measurement."""
    if not is_acquisition_measurement_enabled():
        return
    if not contractor_id or not isinstance(contractor_id, str):
        return
    if not isinstance(tier, str) or tier not in VALID_PAYMENT_TIERS:
        return

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()
    now = time.time()

    @firestore.transactional
    def _txn(transaction):
        if not is_acquisition_measurement_enabled():
            return
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return
        acq = data.get("acquisition_measurement")
        is_valid, _, _, _, _, created_ts = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return
        if now < created_ts:
            return

        updates = {}

        # 1. First verified entitlement
        if acq.get("first_verified_entitlement_observed_at") is None:
            updates["acquisition_measurement.first_verified_entitlement_observed_at"] = now
            updates["acquisition_measurement.first_verified_entitlement_tier"] = tier

        # 2. Payment classification
        payment_class = classify_apple_transaction_payment(transaction_info)
        if payment_class["is_positive_price_purchase"]:
            if acq.get("first_positive_price_purchase_observed_at") is None:
                updates["acquisition_measurement.first_positive_price_purchase_observed_at"] = now
                updates["acquisition_measurement.first_positive_price_purchase_signed_at"] = payment_class["purchase_signed_at"]
                updates["acquisition_measurement.first_positive_price_purchase_tier"] = payment_class["paid_tier"]

        if payment_class["is_storekit_trial"]:
            if acq.get("first_storekit_trial_observed_at") is None:
                updates["acquisition_measurement.first_storekit_trial_observed_at"] = now
                updates["acquisition_measurement.first_storekit_trial_signed_at"] = payment_class["trial_signed_at"]

        if updates:
            transaction.update(doc_ref, updates)

    transaction = db.transaction()
    try:
        await loop.run_in_executor(None, lambda: _txn(transaction))
    except Exception as e:
        logger.warning(f"Payment measurement failed: {type(e).__name__}")


def parse_qualifying_screening_conversation(transcript: Any) -> bool:
    """Parse durable decrypted transcript to check for Caller line followed by Kevin line.

    Parse exact case-sensitive line prefixes 'Caller:' and 'Kevin:'; each must
    have non-whitespace text, with a Kevin line after a Caller line.
    """
    if not isinstance(transcript, str):
        return False
    if not transcript.strip():
        return False
    try:
        transcript_bytes = transcript.encode("utf-8")
    except Exception:
        return False
    if len(transcript_bytes) > MAX_TRANSCRIPT_BYTES:
        return False

    caller_seen = False
    for line in transcript.splitlines():
        if line.startswith("Caller:"):
            caller_text = line[7:].strip()
            if caller_text:
                caller_seen = True
        elif line.startswith("Kevin:"):
            kevin_text = line[6:].strip()
            if kevin_text and caller_seen:
                return True
    return False


def qualify_screening_conversation(
    call_record: Any,
    observed_at: Optional[float] = None,
) -> tuple[bool, Optional[float], Optional[float]]:
    """Strictly qualify durable call record for screening conversation observation.

    Returns (is_qualified, call_start_ts, observed_at_ts).
    """
    if not is_acquisition_measurement_enabled():
        return False, None, None

    if not isinstance(call_record, dict):
        return False, None, None

    # Exact call_status == "completed" and route_taken == "ai_screening"
    call_status = call_record.get("call_status")
    if call_status != "completed":
        return False, None, None

    route_taken = call_record.get("route_taken")
    if route_taken != "ai_screening":
        return False, None, None

    # Durable decrypted transcript parsing
    transcript = call_record.get("transcript")
    if not parse_qualifying_screening_conversation(transcript):
        return False, None, None

    now = time.time()
    obs = now if observed_at is None else observed_at
    if (
        isinstance(obs, bool)
        or not isinstance(obs, (int, float))
        or not math.isfinite(obs)
        or not (MIN_TIMESTAMP_S <= obs <= MAX_TIMESTAMP_S)
        or obs > now
    ):
        return False, None, None

    call_start = call_record.get("timestamp")
    if (
        isinstance(call_start, bool)
        or not isinstance(call_start, (int, float))
        or not math.isfinite(call_start)
        or not (MIN_TIMESTAMP_S <= call_start <= MAX_TIMESTAMP_S)
    ):
        return False, None, None

    ended_at = call_record.get("ended_at")
    if (
        isinstance(ended_at, bool)
        or not isinstance(ended_at, (int, float))
        or not math.isfinite(ended_at)
        or not (MIN_TIMESTAMP_S <= ended_at <= MAX_TIMESTAMP_S)
    ):
        return False, None, None

    start_ts = float(call_start)
    ended_ts = float(ended_at)
    obs_ts = float(obs)

    if not (start_ts <= ended_ts <= obs_ts):
        return False, None, None

    return True, start_ts, obs_ts


def schedule_screening_conversation_measurement(
    contractor_id: str,
    call_record: dict,
    observed_at: Optional[float] = None,
) -> None:
    """Synchronously enqueue screening conversation measurement task without awaiting or blocking."""
    if not is_acquisition_measurement_enabled():
        return
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or contractor_id.strip() != contractor_id
    ):
        return
    if not isinstance(call_record, dict):
        return
    call_cid = call_record.get("contractor_id")
    if (
        not isinstance(call_cid, str)
        or not call_cid
        or call_cid != contractor_id
    ):
        return
    if len(_pending_screening_tasks) >= MAX_PENDING_SCREENING_TASKS:
        return

    is_qualified, start_ts, obs_ts = qualify_screening_conversation(
        call_record, observed_at=observed_at
    )
    if not is_qualified or start_ts is None or obs_ts is None:
        return

    coro = record_screening_conversation_measurement(
        contractor_id=contractor_id,
        call_start=start_ts,
        observed_at=obs_ts,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return

    try:
        task = loop.create_task(coro)
    except Exception:
        coro.close()
        return

    _pending_screening_tasks.add(task)

    def _done_cb(t: asyncio.Task) -> None:
        _pending_screening_tasks.discard(t)
        if t.cancelled():
            return
        try:
            exc = t.exception()
            if exc:
                logger.warning(f"Screening conversation measurement task failed: {type(exc).__name__}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Screening conversation measurement task error: {type(e).__name__}")

    task.add_done_callback(_done_cb)


async def record_screening_conversation_measurement(
    contractor_id: str,
    call_start: float,
    observed_at: float,
) -> None:
    """Record first_screening_conversation_observed_at once in acquisition_measurement if eligible."""
    if not is_acquisition_measurement_enabled():
        return
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or contractor_id.strip() != contractor_id
    ):
        return
    if (
        isinstance(call_start, bool)
        or not isinstance(call_start, (int, float))
        or not math.isfinite(call_start)
        or not (MIN_TIMESTAMP_S <= call_start <= MAX_TIMESTAMP_S)
    ):
        return
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(observed_at)
        or not (MIN_TIMESTAMP_S <= observed_at <= MAX_TIMESTAMP_S)
    ):
        return
    now = time.time()
    if float(observed_at) > now:
        return
    if float(call_start) > float(observed_at):
        return

    db = get_firestore_client()
    doc_ref = db.collection("contractors").document(contractor_id)
    loop = asyncio.get_event_loop()

    @firestore.transactional
    def _txn(transaction):
        if not is_acquisition_measurement_enabled():
            return
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return
        acq = data.get("acquisition_measurement")
        is_valid, _, _, _, _, created_ts = validate_acquisition_measurement_map(acq)
        if not is_valid:
            return
        if acq.get("first_screening_conversation_observed_at") is not None:
            return
        if float(call_start) < created_ts:
            return
        if float(observed_at) < created_ts:
            return
        current_now = time.time()
        if float(observed_at) > current_now:
            return
        transaction.update(doc_ref, {
            "acquisition_measurement.first_screening_conversation_observed_at": float(observed_at)
        })

    transaction = db.transaction()
    try:
        await loop.run_in_executor(None, lambda: _txn(transaction))
    except Exception as e:
        logger.warning(f"Screening conversation measurement failed: {type(e).__name__}")
