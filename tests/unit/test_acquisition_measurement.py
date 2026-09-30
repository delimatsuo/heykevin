"""Unit tests for acquisition measurement safety, classification, and schema contracts."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import asyncio
import math
import threading
import time
from typing import Any, Dict, Optional
import pytest

from app.config import Settings
from app.services import acquisition
from scripts.summarize_acquisition_funnel import validate_measurement_record


@pytest.fixture(autouse=True)
def outbound_tripwire(monkeypatch):
    """Prevent real network and Firestore client construction during unit tests."""
    def _block_httpx(*args, **kwargs):
        raise RuntimeError("Real httpx network transport is forbidden in unit tests")

    def _block_requests(*args, **kwargs):
        raise RuntimeError("Real requests network call is forbidden in unit tests")

    def _block_firestore(*args, **kwargs):
        raise RuntimeError("Real google.cloud.firestore.Client construction is forbidden in unit tests")

    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", _block_httpx)
    import requests.sessions
    monkeypatch.setattr(requests.sessions.Session, "request", _block_requests)
    import google.cloud.firestore
    monkeypatch.setattr(google.cloud.firestore.Client, "__init__", _block_firestore)


class _InMemoryFirestore:
    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def collection(self, name: str):
        return _Collection(self, name)

    def transaction(self):
        return _FakeTransaction(self)


class _Collection:
    def __init__(self, fs: _InMemoryFirestore, name: str):
        self._fs = fs
        self._name = name

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}", doc_id)


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict], doc_id: str = ""):
        self.exists = exists
        self._data = data or {}
        self.id = doc_id

    def to_dict(self):
        import json
        return json.loads(json.dumps(self._data))


class _DocRef:
    def __init__(self, fs: _InMemoryFirestore, path: str, doc_id: str):
        self._fs = fs
        self._path = path
        self.id = doc_id

    def get(self, transaction=None):
        with self._fs._lock:
            data = self._fs._docs.get(self._path)
            return _Snapshot(data is not None, data, doc_id=self.id)

    def update(self, value: dict):
        with self._fs._lock:
            if self._path not in self._fs._docs:
                raise KeyError(f"Document {self._path} does not exist")
            target = self._fs._docs[self._path]
            for k, v in value.items():
                if "." in k:
                    parts = k.split(".")
                    curr = target
                    for p in parts[:-1]:
                        if p not in curr or not isinstance(curr[p], dict):
                            curr[p] = {}
                        curr = curr[p]
                    curr[parts[-1]] = v
                else:
                    target[k] = v


class _FakeTransaction:
    def __init__(self, fs: _InMemoryFirestore):
        self._fs = fs

    def update(self, doc_ref: _DocRef, value: dict):
        doc_ref.update(value)


@pytest.fixture
def fake_db(monkeypatch):
    db = _InMemoryFirestore()
    monkeypatch.setattr(acquisition, "get_firestore_client", lambda: db)
    def _fake_transactional(fn):
        def _wrapped(transaction, *args, **kwargs):
            with db._lock:
                return fn(transaction, *args, **kwargs)
        return _wrapped
    monkeypatch.setattr("google.cloud.firestore.transactional", _fake_transactional)
    return db


def test_acquisition_measurement_default_off():
    """Verify measurement is strictly default-off without explicit opt-in and valid org ID."""
    default_settings = Settings()
    assert default_settings.acquisition_measurement_enabled is False
    assert default_settings.apple_ads_expected_org_id == 0

    assert acquisition.is_acquisition_measurement_enabled() is False


def test_acquisition_measurement_enabled_requires_positive_org_id(monkeypatch):
    """Verify enabled flag alone is insufficient; positive non-placeholder org ID is required."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 0)
    assert acquisition.is_acquisition_measurement_enabled() is False

    # Placeholder ID must be rejected
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 1234567890)
    assert acquisition.is_acquisition_measurement_enabled() is False

    # Valid positive ID
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)
    assert acquisition.is_acquisition_measurement_enabled() is True


def test_parse_apple_ads_response_valid_attribution():
    """Verify parsing valid Apple Ads attribution response with all allowlisted fields."""
    raw = {
        "attribution": True,
        "orgId": 987654,
        "campaignId": 111222,
        "adGroupId": 333444,
        "keywordId": 555666,
        "conversionType": "Download",
        "claimType": "Click",
    }
    is_valid, parsed, reason = acquisition.parse_apple_ads_response(raw, expected_org_id=987654)
    assert is_valid is True
    assert reason == "ok"
    assert parsed == {
        "attribution": True,
        "org_id": 987654,
        "campaign_id": 111222,
        "ad_group_id": 333444,
        "keyword_id": 555666,
        "conversion_type": "Download",
        "claim_type": "Click",
    }


def test_parse_apple_ads_response_unattributed():
    """Verify parsing valid unattributed Apple Ads response."""
    raw = {"attribution": False}
    is_valid, parsed, reason = acquisition.parse_apple_ads_response(raw, expected_org_id=987654)
    assert is_valid is True
    assert reason == "unattributed"
    assert parsed == {"attribution": False}


def test_parse_apple_ads_response_ignores_placeholder_and_invalid_keyword_id():
    """Verify placeholder keyword ID 1234567890 and invalid keyword IDs are safely ignored."""
    raw = {
        "attribution": True,
        "orgId": 987654,
        "campaignId": 111222,
        "adGroupId": 333444,
        "keywordId": 1234567890,  # placeholder
    }
    is_valid, parsed, reason = acquisition.parse_apple_ads_response(raw, expected_org_id=987654)
    assert is_valid is True
    assert reason == "ok"
    assert "keyword_id" not in parsed


def test_parse_apple_ads_response_rejects_non_bool_attribution():
    """Verify integer 1 or 0 is rejected as attribution field."""
    raw = {
        "attribution": 1,
        "orgId": 987654,
        "campaignId": 111222,
        "adGroupId": 333444,
    }
    is_valid, parsed, reason = acquisition.parse_apple_ads_response(raw, expected_org_id=987654)
    assert is_valid is False
    assert parsed is None
    assert reason == "attribution_not_strict_bool"


def test_parse_apple_ads_response_rejects_org_id_mismatch():
    """Verify response from unexpected org ID is rejected."""
    raw = {
        "attribution": True,
        "orgId": 111111,
        "campaignId": 111222,
        "adGroupId": 333444,
    }
    is_valid, parsed, reason = acquisition.parse_apple_ads_response(raw, expected_org_id=987654)
    assert is_valid is False
    assert parsed is None
    assert reason == "org_id_mismatch"


def test_parse_apple_ads_response_rejects_placeholder_ids():
    """Verify known placeholder IDs are rejected."""
    raw = {
        "attribution": True,
        "orgId": 987654,
        "campaignId": 1234567890,
        "adGroupId": 333444,
    }
    is_valid, parsed, reason = acquisition.parse_apple_ads_response(raw, expected_org_id=987654)
    assert is_valid is False
    assert parsed is None
    assert reason == "placeholder_campaign_id"


def test_validate_stored_attempts_strict_types():
    """Verify attempt counters validate strictly against boolean and corrupt states."""
    # Valid attempts
    valid_doc_0 = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "attempts": 0,
    }
    assert acquisition.validate_stored_attempts(valid_doc_0) == (True, 0, None)

    valid_doc_1 = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "attempts": 1,
        "last_attempt_at": 1788960010.0,
    }
    assert acquisition.validate_stored_attempts(valid_doc_1) == (True, 1, 1788960010.0)

    # Boolean is invalid
    assert acquisition.validate_stored_attempts({"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": True}) == (False, 0, None)
    assert acquisition.validate_stored_attempts({"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 1, "last_attempt_at": True}) == (False, 0, None)

    # Missing last_attempt_at when attempts > 0 is invalid
    assert acquisition.validate_stored_attempts({"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 2}) == (False, 0, None)

    # Out-of-range attempts
    assert acquisition.validate_stored_attempts({"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 4, "last_attempt_at": 1788960000.0}) == (False, 0, None)


def test_classify_apple_transaction_payment_production_paid():
    """Verify paid conversion requires Production, recognized product, price > 0, and valid purchase date."""
    valid_ms = int(time.time() * 1000)
    tx = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.personal.monthly",
        "price": 999,
        "purchaseDate": valid_ms,
    }
    res = acquisition.classify_apple_transaction_payment(tx)
    assert res["is_positive_price_purchase"] is True
    assert res["paid_tier"] == "personal"
    assert res["purchase_signed_at"] == float(valid_ms) / 1000.0
    assert res["is_storekit_trial"] is False


def test_classify_apple_transaction_payment_rejects_sandbox():
    """Verify Sandbox environment is never classified as paid conversion."""
    valid_ms = int(time.time() * 1000)
    tx = {
        "environment": "Sandbox",
        "productId": "com.kevin.callscreen.personal.monthly",
        "price": 999,
        "purchaseDate": valid_ms,
    }
    res = acquisition.classify_apple_transaction_payment(tx)
    assert res["is_positive_price_purchase"] is False
    assert res["is_storekit_trial"] is False


def test_classify_apple_transaction_payment_storekit_free_trial():
    """Verify StoreKit free trial requires explicit FREE_TRIAL offer discount and price=0."""
    valid_ms = int(time.time() * 1000)
    tx = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.business.monthly",
        "price": 0,
        "offerDiscountType": "FREE_TRIAL",
        "purchaseDate": valid_ms,
    }
    res = acquisition.classify_apple_transaction_payment(tx)
    assert res["is_positive_price_purchase"] is False
    assert res["is_storekit_trial"] is True
    assert res["trial_signed_at"] == float(valid_ms) / 1000.0


def test_classify_apple_transaction_payment_introductory_offer_with_positive_price_qualifies_as_paid():
    """Verify offerType=1 alone with price > 0 is treated as paid, not trial."""
    valid_ms = int(time.time() * 1000)
    tx = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.businesspro.monthly",
        "price": 1999,
        "offerType": 1,
        "purchaseDate": valid_ms,
    }
    res = acquisition.classify_apple_transaction_payment(tx)
    assert res["is_positive_price_purchase"] is True
    assert res["paid_tier"] == "businessPro"
    assert res["is_storekit_trial"] is False


def test_classify_apple_transaction_payment_rejects_revoked():
    """Verify revoked transaction is never classified as paid."""
    valid_ms = int(time.time() * 1000)
    tx = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.businesspro.monthly",
        "price": 7999,
        "purchaseDate": valid_ms,
        "revocationDate": valid_ms + 5000,
        "revocationReason": 1,
    }
    res = acquisition.classify_apple_transaction_payment(tx)
    assert res["is_positive_price_purchase"] is False
    assert res["is_storekit_trial"] is False


def test_classify_apple_transaction_payment_rejects_invalid_or_future_dates():
    """Verify purchaseDate before 2001 or in far future is rejected."""
    # Year 1999
    tx_old = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.personal.monthly",
        "price": 999,
        "purchaseDate": 915148800000,
    }
    assert acquisition.classify_apple_transaction_payment(tx_old)["is_positive_price_purchase"] is False

    # Far future (> now + 300s)
    future_ms = int((time.time() + 10000) * 1000)
    tx_future = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.personal.monthly",
        "price": 999,
        "purchaseDate": future_ms,
    }
    assert acquisition.classify_apple_transaction_payment(tx_future)["is_positive_price_purchase"] is False


@pytest.mark.asyncio
async def test_milestone_writers_reject_invalid_inputs_and_pre_cohort_timestamps(fake_db, monkeypatch):
    """Verify call and payment milestone writers reject NaN, inf, bool, string, and pre-cohort timestamps without max() coercion."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0  # Sept 2026
    contractor_id = "cnt_milestone_test"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now,
            "created_at": now,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    # 1. Inbound call with pre-cohort timestamp (now - 50) -> must NOT write
    await acquisition.record_inbound_call_measurement(contractor_id, now - 50.0)
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc.get("first_inbound_observed_at") is None

    # 2. Inbound call with bool/NaN -> must NOT write
    await acquisition.record_inbound_call_measurement(contractor_id, True)
    await acquisition.record_inbound_call_measurement(contractor_id, float("nan"))
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc.get("first_inbound_observed_at") is None

    # 3. Valid inbound call (now + 10) -> writes exact timestamp
    await acquisition.record_inbound_call_measurement(contractor_id, now + 10.0)
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc.get("first_inbound_observed_at") == now + 10.0

    # 4. Forwarded call with pre-cohort timestamp -> must NOT write
    await acquisition.record_forwarded_call_measurement(contractor_id, now - 100.0)
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc.get("first_forwarded_observed_at") is None

    # 5. Valid forwarded call (now + 20) -> writes exact timestamp
    await acquisition.record_forwarded_call_measurement(contractor_id, now + 20.0)
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc.get("first_forwarded_observed_at") == now + 20.0

    # 6. Payment measurement with invalid tier -> must NOT write
    await acquisition.record_payment_measurement(contractor_id, "unknown_tier", {})
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc.get("first_verified_entitlement_observed_at") is None


@pytest.mark.asyncio
async def test_record_payment_measurement_twice_preserves_original_intent_and_first_tier_date(fake_db, monkeypatch):
    """Verify calling record_payment_measurement twice (Personal then BusinessPro) preserves original intent, tier, and timestamp."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0  # Sept 2026
    contractor_id = "cnt_idempotent_payment"
    initial_schema = {
        "schema_version": 1,
        "cohort": now,
        "created_at": now,
        "declared_onboarding_intent": "personal",
        "attempts": 0,
        "last_attempt_at": None,
        "attribution_status": None,
        "lease_attempt": None,
        "lease_expires_at": None,
    }
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": dict(initial_schema),
    }

    # First call: Personal ($9.99)
    tx_personal = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.personal.monthly",
        "price": 999,
        "purchaseDate": int((now + 100.0) * 1000),
    }
    await acquisition.record_payment_measurement(contractor_id, "personal", tx_personal)

    doc1 = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc1["declared_onboarding_intent"] == "personal"
    assert doc1["first_verified_entitlement_tier"] == "personal"
    assert doc1["first_positive_price_purchase_tier"] == "personal"
    assert doc1["first_positive_price_purchase_signed_at"] == now + 100.0
    first_entitlement_obs = doc1["first_verified_entitlement_observed_at"]
    first_purchase_obs = doc1["first_positive_price_purchase_observed_at"]
    assert first_entitlement_obs is not None
    assert first_purchase_obs is not None

    # Second call: Business Pro ($79.99)
    tx_businesspro = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.businesspro.monthly",
        "price": 7999,
        "purchaseDate": int((now + 500.0) * 1000),
    }
    await acquisition.record_payment_measurement(contractor_id, "businessPro", tx_businesspro)

    doc2 = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    # Intent, first entitlement, and first positive purchase MUST remain Personal with original timestamps
    assert doc2["declared_onboarding_intent"] == "personal"
    assert doc2["first_verified_entitlement_tier"] == "personal"
    assert doc2["first_verified_entitlement_observed_at"] == first_entitlement_obs
    assert doc2["first_positive_price_purchase_tier"] == "personal"
    assert doc2["first_positive_price_purchase_signed_at"] == now + 100.0
    assert doc2["first_positive_price_purchase_observed_at"] == first_purchase_obs

    # Validate full schema roundtrips through reducer validator cleanly
    validated = validate_measurement_record(doc2)
    assert validated["first_positive_price_purchase_tier"] == "personal"


@pytest.mark.parametrize("non_paid_tx", [
    {"environment": "Sandbox", "productId": "com.kevin.callscreen.personal.monthly", "price": 999, "purchaseDate": 1788960100000},  # Sandbox
    {"environment": "Production", "productId": "com.kevin.callscreen.personal.monthly", "price": 0, "offerDiscountType": "FREE_TRIAL", "purchaseDate": 1788960100000},  # Free trial
    {"environment": "Production", "productId": "com.kevin.callscreen.personal.monthly", "purchaseDate": 1788960100000},  # Missing price
    {"environment": "Production", "productId": "com.kevin.callscreen.personal.monthly", "price": "999", "purchaseDate": 1788960100000},  # String price
])
@pytest.mark.asyncio
async def test_non_paid_transactions_cannot_increment_positive_price_purchase(fake_db, monkeypatch, non_paid_tx):
    """Verify Sandbox, free trial, missing price, or invalid price types cannot write positive price purchase fields."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = "cnt_non_paid_test"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now,
            "created_at": now,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    await acquisition.record_payment_measurement(contractor_id, "personal", non_paid_tx)

    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    # Entitlement may be recorded if tier was personal, but positive price purchase must NOT be recorded
    assert doc.get("first_positive_price_purchase_observed_at") is None
    assert doc.get("first_positive_price_purchase_tier") is None
    assert doc.get("first_positive_price_purchase_signed_at") is None

    # If it was a valid StoreKit free trial, verify trial fields are written
    if non_paid_tx.get("offerDiscountType") == "FREE_TRIAL" and non_paid_tx.get("price") == 0:
        assert doc.get("first_storekit_trial_observed_at") is not None
        assert doc.get("first_storekit_trial_signed_at") == 1788960100.0


@pytest.mark.parametrize("bad_record", [
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "declared_onboarding_intent": ["personal"], "attempts": 0, "attribution_status": None},  # intent list
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "declared_onboarding_intent": "personal", "attempts": 0, "attribution_status": {"recorded": 1}},  # status dict
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "declared_onboarding_intent": "personal", "attempts": 0, "attribution_status": None, "first_verified_entitlement_observed_at": 1788960010.0, "first_verified_entitlement_tier": ["personal"]},  # tier list
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "declared_onboarding_intent": "personal", "attempts": 0, "attribution_status": None, "org_id": 1234567890},  # placeholder ID
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "declared_onboarding_intent": "personal", "attempts": 0, "attribution_status": None, "org_id": -100},  # negative ID
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "declared_onboarding_intent": "personal", "attempts": 0, "attribution_status": None, "org_id": True},  # bool ID
])
def test_reducer_parameterized_malformed_records(bad_record):
    """Verify reducer validator raises ValueError on malformed IDs and scalar types without echoing values."""
    with pytest.raises(ValueError):
        validate_measurement_record(bad_record)


@pytest.mark.asyncio
async def test_schedule_payment_measurement_default_off(monkeypatch):
    """Verify scheduler drops task immediately when measurement is disabled or org ID is unconfigured."""
    assert len(acquisition._pending_payment_tasks) == 0

    # 1. Default settings (disabled)
    acquisition.schedule_payment_measurement("cnt_1", "personal", {"price": 999})
    assert len(acquisition._pending_payment_tasks) == 0

    # 2. Enabled flag True but expected org ID is 0
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 0)
    acquisition.schedule_payment_measurement("cnt_1", "personal", {"price": 999})
    assert len(acquisition._pending_payment_tasks) == 0


@pytest.mark.parametrize("bad_contractor_id, bad_tier, bad_tx", [
    ("", "personal", {"price": 999}),
    (None, "personal", {"price": 999}),
    (123, "personal", {"price": 999}),
    ("cnt_1", "invalid_tier", {"price": 999}),
    ("cnt_1", "", {"price": 999}),
    ("cnt_1", None, {"price": 999}),
    ("cnt_1", "personal", None),
    ("cnt_1", "personal", "not_a_dict"),
    ("cnt_1", "personal", [1, 2, 3]),
])
@pytest.mark.asyncio
async def test_schedule_payment_measurement_validation_filters(monkeypatch, bad_contractor_id, bad_tier, bad_tx):
    """Verify scheduler drops tasks with invalid contractor ID, tier, or non-dict transaction info."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    acquisition.schedule_payment_measurement(bad_contractor_id, bad_tier, bad_tx)
    assert len(acquisition._pending_payment_tasks) == 0


@pytest.mark.asyncio
async def test_schedule_payment_measurement_cap_32_and_drop(monkeypatch):
    """Verify scheduler bounds pending tasks at 32 and discards additional tasks at capacity."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    release_event = asyncio.Event()

    async def fake_held_writer(cid, tier, tx):
        await release_event.wait()

    monkeypatch.setattr(acquisition, "record_payment_measurement", fake_held_writer)

    try:
        # Schedule 32 tasks
        for i in range(32):
            acquisition.schedule_payment_measurement(f"cnt_{i}", "personal", {"price": 999})
        assert len(acquisition._pending_payment_tasks) == 32

        # 33rd task must be dropped immediately without waiting
        acquisition.schedule_payment_measurement("cnt_overflow", "personal", {"price": 999})
        assert len(acquisition._pending_payment_tasks) == 32
    finally:
        release_event.set()
        if acquisition._pending_payment_tasks:
            await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)
        assert len(acquisition._pending_payment_tasks) == 0


@pytest.mark.asyncio
async def test_schedule_payment_measurement_payload_allowlist_and_mutation_isolation(monkeypatch):
    """Verify scheduler copies only classification fields and isolates against subsequent caller dict mutations."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    captured_payloads = []
    release_event = asyncio.Event()

    async def fake_held_writer(cid, tier, tx):
        captured_payloads.append(dict(tx))
        await release_event.wait()

    monkeypatch.setattr(acquisition, "record_payment_measurement", fake_held_writer)

    valid_ms = 1788960100000
    tx_orig = {
        "environment": "Production",
        "productId": "com.kevin.callscreen.personal.monthly",
        "revocationDate": valid_ms + 1000,
        "purchaseDate": valid_ms,
        "price": 999,
        "offerDiscountType": "FREE_TRIAL",
        # Sensitive and arbitrary fields that MUST NOT be retained
        "appAccountToken": "SECRET_UUID_12345",
        "transactionId": "TX_SENSITIVE_999",
        "originalTransactionId": "ORIG_TX_123",
        "callerMutableDict": {"nested": "data"},
    }

    try:
        acquisition.schedule_payment_measurement("cnt_1", "personal", tx_orig)

        # Mutate caller dict in place before task is released
        tx_orig["price"] = 0
        tx_orig["productId"] = "mutated.product"
        tx_orig["environment"] = "Sandbox"
        tx_orig.clear()

        # Let task proceed
        release_event.set()
        await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)

        assert len(captured_payloads) == 1
        captured = captured_payloads[0]
        # Assert only classification fields present with original values
        assert captured == {
            "environment": "Production",
            "productId": "com.kevin.callscreen.personal.monthly",
            "revocationDate": valid_ms + 1000,
            "purchaseDate": valid_ms,
            "price": 999,
            "offerDiscountType": "FREE_TRIAL",
        }
        assert "appAccountToken" not in captured
        assert "transactionId" not in captured
        assert "originalTransactionId" not in captured
        assert "callerMutableDict" not in captured
    finally:
        release_event.set()
        if acquisition._pending_payment_tasks:
            await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_schedule_payment_measurement_cleanup_on_completion_error_and_cancellation(monkeypatch):
    """Verify done callback safely cleans up task set on success, exceptions, and task cancellation."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    # 1. Successful completion
    async def fake_success(cid, tier, tx):
        pass

    monkeypatch.setattr(acquisition, "record_payment_measurement", fake_success)
    acquisition.schedule_payment_measurement("cnt_1", "personal", {"price": 999})
    await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)
    assert len(acquisition._pending_payment_tasks) == 0

    # 2. Exception in task
    async def fake_throw(cid, tier, tx):
        raise RuntimeError("DB pool connection error")

    monkeypatch.setattr(acquisition, "record_payment_measurement", fake_throw)
    acquisition.schedule_payment_measurement("cnt_2", "personal", {"price": 999})
    await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)
    assert len(acquisition._pending_payment_tasks) == 0

    # 3. Cancelled task
    release_event = asyncio.Event()

    async def fake_held(cid, tier, tx):
        await release_event.wait()

    monkeypatch.setattr(acquisition, "record_payment_measurement", fake_held)
    acquisition.schedule_payment_measurement("cnt_3", "personal", {"price": 999})
    assert len(acquisition._pending_payment_tasks) == 1
    task = next(iter(acquisition._pending_payment_tasks))
    task.cancel()
    release_event.set()
    await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)
    assert len(acquisition._pending_payment_tasks) == 0
