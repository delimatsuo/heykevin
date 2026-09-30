"""Unit tests for acquisition measurement wiring, security redaction, subscription hooks, and strict offline reducer."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import asyncio
import base64
import datetime
import json
import time
from typing import Any, Dict
import pytest
from pydantic import ValidationError

from app.api.contractors import ContractorCreate, _SENSITIVE_KEYS, _redact_contractor
from app.db.contractors import PROTECTED_FIELDS, create_contractor
from app.services import acquisition
from app.services import subscription
from app.services.subscription import (
    CrossContractorReceiptError,
    SubscriptionUpdateOutcome,
    SubscriptionUpdateResult,
    handle_appstore_notification,
    update_subscription_from_transaction,
)
from scripts.summarize_acquisition_funnel import summarize_acquisition_funnel, validate_measurement_record


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


def _unsigned_jws(payload: dict) -> str:
    header = {"alg": "ES256"}

    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


def test_contractor_create_declared_onboarding_intent_validation():
    """Verify ContractorCreate accepts personal/business and None, rejects others."""
    c1 = ContractorCreate(owner_name="Test", business_name="Test Co", declared_onboarding_intent="personal")
    assert c1.declared_onboarding_intent == "personal"

    c2 = ContractorCreate(owner_name="Test", business_name="Test Co", declared_onboarding_intent="business")
    assert c2.declared_onboarding_intent == "business"

    c3 = ContractorCreate(owner_name="Test", business_name="Test Co")
    assert c3.declared_onboarding_intent is None

    with pytest.raises(ValidationError):
        ContractorCreate(owner_name="Test", business_name="Test Co", declared_onboarding_intent="enterprise")


def test_acquisition_measurement_protected_and_redacted():
    """Verify acquisition_measurement is in PROTECTED_FIELDS and _SENSITIVE_KEYS."""
    assert "acquisition_measurement" in PROTECTED_FIELDS
    assert "acquisition_measurement" in _SENSITIVE_KEYS

    # Verify redaction removes the map
    doc = {
        "contractor_id": "cnt_1",
        "business_name": "Test Co",
        "acquisition_measurement": {"schema_version": 1},
    }
    redacted = _redact_contractor(doc)
    assert "acquisition_measurement" not in redacted


def test_validate_measurement_record_rejects_unknown_fields_without_echoing():
    """Verify reducer validator rejects any unexpected root key without echoing private values."""
    valid_rec = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "declared_onboarding_intent": "personal",
        "attempts": 1,
        "last_attempt_at": 1788960010.0,
        "attribution_status": None,
    }
    assert validate_measurement_record(valid_rec) == valid_rec

    invalid_rec = dict(valid_rec)
    sentinel = "SECRET_USER_PII_12345"
    invalid_rec["private_phone_number"] = sentinel

    with pytest.raises(ValueError) as excinfo:
        validate_measurement_record(invalid_rec)

    # Must NOT echo the key or value in the exception string
    assert sentinel not in str(excinfo.value)
    assert "private_phone_number" not in str(excinfo.value)


def test_validate_measurement_record_requires_paired_fields():
    """Verify missing paired fields (e.g. tier or signed timestamp) are rejected."""
    # Missing signed date for positive price purchase
    bad_rec = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "declared_onboarding_intent": "personal",
        "attempts": 0,
        "attribution_status": None,
        "first_positive_price_purchase_observed_at": 1788960100.0,
        "first_positive_price_purchase_tier": "personal",
        # missing first_positive_price_purchase_signed_at
    }
    with pytest.raises(ValueError, match="positive price purchase fields must be paired"):
        validate_measurement_record(bad_rec)


def test_summarize_acquisition_funnel_aggregates_synthetic_cohort():
    """Verify funnel reducer correctly aggregates counts for an immutable 2026 cohort."""
    cohort_start = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    cohort_end = datetime.datetime(2026, 9, 30, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Exact epoch generated from timezone-aware datetime(2026, 9, 10)
    ts_sept10 = datetime.datetime(2026, 9, 10, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    ts_sept11 = datetime.datetime(2026, 9, 11, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    records = [
        {
            "schema_version": 1,
            "cohort": ts_sept10,
            "created_at": ts_sept10,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": ts_sept10 + 10.0,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 111,
            "ad_group_id": 222,
            "source": "apple_ads",
            "attribution_recorded_at": ts_sept10 + 20.0,
            "first_inbound_observed_at": ts_sept10 + 100.0,
            "first_forwarded_observed_at": ts_sept10 + 200.0,
            "first_verified_entitlement_observed_at": ts_sept10 + 300.0,
            "first_verified_entitlement_tier": "personal",
            "first_positive_price_purchase_observed_at": ts_sept10 + 400.0,
            "first_positive_price_purchase_signed_at": ts_sept10 + 400.0,
            "first_positive_price_purchase_tier": "personal",
        },
        {
            "schema_version": 1,
            "cohort": ts_sept11,
            "created_at": ts_sept11,
            "declared_onboarding_intent": "business",
            "attempts": 1,
            "last_attempt_at": ts_sept11 + 10.0,
            "attribution_status": "unattributed",
            "attribution": False,
            "source": "unattributed",
            "attribution_recorded_at": ts_sept11 + 20.0,
            "first_forwarded_observed_at": ts_sept11 + 200.0,
        },
        {
            "schema_version": 1,
            "cohort": 1700000000.0,  # outside Sept 2026 cohort window
            "created_at": 1700000000.0,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "attribution_status": None,
        },
    ]

    result = summarize_acquisition_funnel(
        records=records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of=as_of,
    )

    assert result["complete"] is True
    assert result["totals"]["accounts_created"] == 2
    assert result["totals"]["attribution_recorded"] == 1
    assert result["totals"]["inbound_observed"] == 1
    assert result["totals"]["forwarding_confirmed"] == 2
    assert result["totals"]["verified_entitlement_observed"] == 1
    assert result["totals"]["positive_price_purchase_observed"] == 1
    assert result["totals"]["paid_tiers"]["personal"] == 1
    assert result["totals"]["paid_tiers"]["business"] == 0


def test_summarize_acquisition_funnel_as_of_bounds_and_restore():
    """Verify events observed after as_of are not counted and paid restore signed before creation is supported."""
    cohort_start = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    cohort_end = datetime.datetime(2026, 9, 15, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of = datetime.datetime(2026, 9, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of_ts = as_of.timestamp()

    created_ts = datetime.datetime(2026, 9, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    signed_before_creation = created_ts - 3600.0  # signed 1 hour before account creation (restore)

    records = [
        {
            "schema_version": 1,
            "cohort": created_ts,
            "created_at": created_ts,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": as_of_ts + 100,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 111,
            "ad_group_id": 222,
            "source": "apple_ads",
            "attribution_recorded_at": as_of_ts + 200,  # after as_of -> stays unknown
            "first_positive_price_purchase_observed_at": created_ts + 50,  # observed before as_of
            "first_positive_price_purchase_signed_at": signed_before_creation,  # signed before creation
            "first_positive_price_purchase_tier": "personal",
        }
    ]

    result = summarize_acquisition_funnel(
        records=records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of=as_of,
    )

    assert result["totals"]["accounts_created"] == 1
    assert result["totals"]["attribution_recorded"] == 0
    assert result["totals"]["positive_price_purchase_observed"] == 1
    # Group source must be "unknown" because attribution was recorded after as_of
    assert result["groups"][0]["source"] == "unknown"
    assert result["groups"][0]["campaign_id"] is None


@pytest.mark.asyncio
async def test_create_contractor_initializes_unknown_intent_and_roundtrips_reducer(monkeypatch):
    """Verify real create_contractor strips intent from root, initializes map, and round-trips reducer."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    captured_doc = {}
    class _FakeCollectionRef:
        def add(self, data):
            captured_doc.update(data)
            class _FakeDocRef:
                id = "cnt_created_1"
            return None, _FakeDocRef()

    class _FakeDb:
        def collection(self, name):
            return _FakeCollectionRef()

    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: _FakeDb())

    cid = await create_contractor({
        "business_name": "Test Co",
        "owner_name": "Test Owner",
        "owner_phone": "+14155550123",  # Valid North American reserved test phone
        "declared_onboarding_intent": None,  # omitted / unknown
    })

    assert cid == "cnt_created_1"
    assert "declared_onboarding_intent" not in captured_doc  # stripped from root

    acq = captured_doc.get("acquisition_measurement")
    assert acq is not None
    assert acq["declared_onboarding_intent"] == "unknown"
    assert acq["attempts"] == 0
    assert acq["attribution_status"] is None

    # Validate with reducer
    validated = validate_measurement_record(acq)
    assert validated["declared_onboarding_intent"] == "unknown"


@pytest.mark.asyncio
async def test_twilio_evidence_helpers_precede_throttle_and_isolate_failures(monkeypatch):
    """Verify Twilio evidence helpers execute measurement before throttle and handle measurement errors safely."""
    from app.webhooks import twilio_incoming

    measurement_called = []
    async def fake_inbound_measurement(contractor_id, seen_at):
        measurement_called.append((contractor_id, seen_at))
        raise RuntimeError("Measurement transient failure")

    monkeypatch.setattr("app.services.acquisition.record_inbound_call_measurement", fake_inbound_measurement)

    updated_doc = {}
    async def fake_update_contractor(contractor_id, updates):
        updated_doc.update(updates)
        return True

    async def fake_get_contractor(contractor_id):
        return {"contractor_id": contractor_id, "last_inbound_call_at": 1000.0}

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_contractor)

    # Call with seen_at satisfying throttle (seen_at = 5000 > 1000 + 3600)
    await twilio_incoming._record_inbound_call_evidence("cnt_1", 5000.0)

    # Measurement was executed with exact seen_at
    assert len(measurement_called) == 1
    assert measurement_called[0] == ("cnt_1", 5000.0)

    # Exception in measurement did NOT prevent last-seen update
    assert updated_doc == {"last_inbound_call_at": 5000.0}


# ---------------------------------------------------------------------------
# In-memory Firestore fake for actual _purge_sync execution
# ---------------------------------------------------------------------------


class _PurgeFakeDoc:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path.split("/")[-1]

    def collection(self, name):
        return _PurgeFakeCollection(self._store, f"{self._path}/{name}")

    def get(self):
        data = self._store.docs.get(self._path)
        return _PurgeFakeSnapshot(self._path, data)

    def set(self, value, merge=False):
        if merge and self._path in self._store.docs:
            self._store.docs[self._path].update(value)
        else:
            self._store.docs[self._path] = dict(value)

    def update(self, value):
        self._store.docs[self._path].update(value)

    def delete(self):
        self._store.docs.pop(self._path, None)

    @property
    def reference(self):
        return self


class _PurgeFakeSnapshot:
    def __init__(self, path, data):
        self._path = path
        self._data = data
        self.exists = data is not None
        self.id = path.split("/")[-1]
        self.reference = None

    def to_dict(self):
        return dict(self._data) if self._data else None


class _PurgeFakeCollection:
    def __init__(self, store, path, filters=(), limit=None):
        self._store = store
        self._path = path
        self._filters = filters
        self._limit = limit

    def document(self, doc_id):
        return _PurgeFakeDoc(self._store, f"{self._path}/{doc_id}")

    def where(self, filter=None):
        return _PurgeFakeCollection(
            self._store, self._path, self._filters + (filter,), self._limit
        )

    def limit(self, n):
        return _PurgeFakeCollection(self._store, self._path, self._filters, n)

    def list_documents(self):
        """Enumerate direct children INCLUDING phantom parents with descendants."""
        depth = self._path.count("/") + 2
        ids = set()
        for path in self._store.docs:
            if not path.startswith(self._path + "/"):
                continue
            parts = path.split("/")
            want = self._path.count("/") + 1
            ids.add(parts[want])
        return [_PurgeFakeDoc(self._store, f"{self._path}/{i}") for i in sorted(ids)]

    def stream(self, **_kwargs):
        depth = self._path.count("/") + 2
        out = []
        for path, data in sorted(self._store.docs.items()):
            if not path.startswith(self._path + "/"):
                continue
            if path.count("/") + 1 != depth:
                continue
            ok = True
            for f in self._filters:
                field, op, val = f.field_path, f.op_string, f.value
                have = data.get(field)
                if op == "==":
                    ok = have == val
                elif op == "<":
                    ok = have is not None and have < val
                else:
                    raise NotImplementedError(op)
                if not ok:
                    break
            if ok:
                snap = _PurgeFakeSnapshot(path, data)
                snap.reference = _PurgeFakeDoc(self._store, path)
                out.append(snap)
                if self._limit and len(out) >= self._limit:
                    break
        return iter(out)


class _PurgeFakeBatch:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def delete(self, ref):
        self._ops.append(ref)

    def commit(self):
        for ref in self._ops:
            ref.delete()
        self._ops = []


class _PurgeFakeDb:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _PurgeFakeCollection(self, name)

    def batch(self):
        return _PurgeFakeBatch(self)


def test_purge_sync_removes_acquisition_measurement(monkeypatch):
    """Verify executing real _purge_sync on a deactivated contractor strips acquisition_measurement and sets minimal tombstone."""
    from app.db.purge import _purge_sync, TOMBSTONE_FIELDS

    db = _PurgeFakeDb()
    contractor_id = "cnt_purge_test"
    db.docs[f"contractors/{contractor_id}"] = {
        "active": False,
        "business_name": "Purged Business",
        "owner_name": "Purged Owner",
        "owner_phone": "+14155550123",
        "deactivated_at": 1000000,
        "deletion_requested_at": 1000000,
        "subscription_uuid": "uuid-purge-123",
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": 1000000.0,
            "created_at": 1000000.0,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "attribution_status": "recorded",
            "attribution": True,
            "first_positive_price_purchase_observed_at": 1000100.0,
        },
    }

    db.docs[f"contractors/{contractor_id}/contacts/c1"] = {"name": "Test Contact"}

    monkeypatch.setattr("app.db.purge.get_firestore_client", lambda: db)
    monkeypatch.setattr("app.db.purge.settings.estimate_media_bucket", "")

    res = _purge_sync(contractor_id)
    assert res["purged_at"] > 0
    assert res["deleted"].get("contacts") == 1

    # Assert document in DB is reduced to tombstone
    final_doc = db.docs.get(f"contractors/{contractor_id}")
    assert final_doc is not None
    assert final_doc["active"] is False
    assert final_doc["purged_at"] == res["purged_at"]
    assert "acquisition_measurement" not in final_doc
    assert "business_name" not in final_doc
    assert "owner_name" not in final_doc
    assert "owner_phone" not in final_doc

    # Assert only tombstone fields remain
    for key in final_doc:
        assert key in TOMBSTONE_FIELDS

    # Assert subcollection was purged
    assert f"contractors/{contractor_id}/contacts/c1" not in db.docs


# ---------------------------------------------------------------------------
# Subscription Entitlement & App Store Notification Measurement Hooks Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_subscription_from_transaction_measurement_hooks(monkeypatch):
    """Verify update_subscription_from_transaction triggers measurement once on success, zero on failure/throw, and isolates measurement exceptions."""
    contractor_id = "cnt_sub_hook_test"
    sub_uuid = "uuid-sub-hook-123"

    async def fake_get_contractor(cid):
        if cid == contractor_id:
            return {"contractor_id": cid, "subscription_uuid": sub_uuid, "active": True}
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)

    measurement_calls = []
    async def fake_record_measurement(cid, tier, tx_info):
        measurement_calls.append((cid, tier, tx_info))

    monkeypatch.setattr("app.services.acquisition.record_payment_measurement", fake_record_measurement)

    valid_tx = {
        "productId": "com.kevin.callscreen.personal.monthly",
        "appAccountToken": sub_uuid,
        "originalTransactionId": "orig_tx_123",
        "transactionId": "tx_123",
        "expiresDate": (time.time() + 86400) * 1000,
        "environment": "Production",
        "price": 999,
    }

    # 1. Accepted transaction -> record_payment_measurement called once
    async def fake_claim_ok(*args, **kwargs):
        return True, contractor_id
    async def fake_activate_ok(*args, **kwargs):
        return True

    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_ok)

    res = await update_subscription_from_transaction(contractor_id, valid_tx)
    assert res.outcome == SubscriptionUpdateOutcome.ACTIVE
    assert len(measurement_calls) == 1
    assert measurement_calls[0][0] == contractor_id
    assert measurement_calls[0][1] == "personal"

    measurement_calls.clear()

    # 2a. False update from activate_subscription_entitlement -> zero measurement calls
    async def fake_activate_fail(*args, **kwargs):
        return False
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_fail)

    res_fail = await update_subscription_from_transaction(contractor_id, valid_tx)
    assert res_fail.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert len(measurement_calls) == 0

    # 2b. Rejected payload (unknown product) -> zero measurement calls
    bad_prod_tx = dict(valid_tx, productId="unknown.product.id")
    res_unknown = await update_subscription_from_transaction(contractor_id, bad_prod_tx)
    assert res_unknown.outcome == SubscriptionUpdateOutcome.UNKNOWN_PRODUCT
    assert len(measurement_calls) == 0

    # 2c. Rejected payload (ownership mismatch) -> zero measurement calls
    bad_owner_tx = dict(valid_tx, appAccountToken="wrong-uuid")
    res_mismatch = await update_subscription_from_transaction(contractor_id, bad_owner_tx)
    assert res_mismatch.outcome == SubscriptionUpdateOutcome.OWNERSHIP_MISMATCH
    assert len(measurement_calls) == 0

    # 2d. Rejected payload (cross-contractor receipt replay) -> zero measurement calls, raises CrossContractorReceiptError
    async def fake_claim_cross(*args, **kwargs):
        return False, "other_cnt"
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_cross)

    with pytest.raises(CrossContractorReceiptError):
        await update_subscription_from_transaction(contractor_id, valid_tx)
    assert len(measurement_calls) == 0

    # 3. Thrown update -> zero measurement calls
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)
    async def fake_activate_throw(*args, **kwargs):
        raise RuntimeError("DB connection dropped")
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_throw)

    with pytest.raises(RuntimeError):
        await update_subscription_from_transaction(contractor_id, valid_tx)
    assert len(measurement_calls) == 0

    # 4. Measurement throws -> existing successful entitlement result still succeeds
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_ok)
    async def fake_record_throw(cid, tier, tx_info):
        raise RuntimeError("Measurement write failed")
    monkeypatch.setattr("app.services.acquisition.record_payment_measurement", fake_record_throw)

    res_safe = await update_subscription_from_transaction(contractor_id, valid_tx)
    assert res_safe.outcome == SubscriptionUpdateOutcome.ACTIVE


@pytest.mark.asyncio
async def test_handle_appstore_notification_measurement_hooks(monkeypatch):
    """Verify handle_appstore_notification DID_RENEW/SUBSCRIBED triggers measurement only when contractor updated, and isolates exceptions."""
    contractor_id = "cnt_notif_hook_test"
    sub_uuid = "uuid-notif-hook-123"

    async def fake_get_by_uuid(uuid_val, include_inactive=False):
        if uuid_val == sub_uuid:
            return {"contractor_id": contractor_id, "subscription_uuid": sub_uuid, "active": True}
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_get_by_uuid)

    async def fake_claim_ok(*args, **kwargs):
        return True, contractor_id
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)

    # Patch push notification device token lookup and push sending to prevent real Firestore credential refresh
    async def fake_get_device_token(contractor_id=None):
        return None

    async def fake_send_regular_push(*args, **kwargs):
        raise AssertionError("send_regular_push should not be called when device_token is None")

    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)

    measurement_calls = []
    async def fake_record_measurement(cid, tier, tx_info):
        measurement_calls.append((cid, tier, tx_info))

    monkeypatch.setattr("app.services.acquisition.record_payment_measurement", fake_record_measurement)

    valid_tx = {
        "productId": "com.kevin.callscreen.business.monthly",
        "appAccountToken": sub_uuid,
        "originalTransactionId": "orig_notif_123",
        "transactionId": "tx_notif_123",
        "expiresDate": (time.time() + 86400) * 1000,
        "environment": "Production",
        "price": 4999,
        "purchaseDate": time.time() * 1000,
    }

    notif_payload = {
        "notificationType": "DID_RENEW",
        "subtype": "BILLING_RECOVERY",
        "data": {
            "signedTransactionInfo": _unsigned_jws(valid_tx)
        }
    }

    # 1. Accepted notification with update_contractor returning True -> measurement called once
    async def fake_update_ok(cid, updates):
        return True
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)

    handled = await handle_appstore_notification(notif_payload)
    assert handled is True
    assert len(measurement_calls) == 1
    assert measurement_calls[0][0] == contractor_id
    assert measurement_calls[0][1] == "business"

    measurement_calls.clear()

    # 2a. False update from update_contractor -> zero measurement calls
    async def fake_update_false(cid, updates):
        return False
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_false)

    handled_false = await handle_appstore_notification(notif_payload)
    assert handled_false is True  # Notification processing completes
    assert len(measurement_calls) == 0

    # 2b. Rejected payload (unknown product) -> zero measurement calls
    bad_prod_tx = dict(valid_tx, productId="unknown.product")
    bad_prod_payload = {
        "notificationType": "DID_RENEW",
        "subtype": "",
        "data": {"signedTransactionInfo": _unsigned_jws(bad_prod_tx)}
    }
    handled_bad = await handle_appstore_notification(bad_prod_payload)
    assert handled_bad is False
    assert len(measurement_calls) == 0

    # 2c. Non-renewal/subscription notification type (EXPIRED) -> zero measurement calls and pushes isolated
    expired_payload = {
        "notificationType": "EXPIRED",
        "subtype": "VOLUNTARY",
        "data": {"signedTransactionInfo": _unsigned_jws(valid_tx)}
    }
    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "active": True}
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)

    handled_exp = await handle_appstore_notification(expired_payload)
    assert handled_exp is True
    assert len(measurement_calls) == 0

    # 3. Thrown update from update_contractor -> zero measurement calls
    async def fake_update_throw(cid, updates):
        raise RuntimeError("Database error during update")
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_throw)

    with pytest.raises(RuntimeError):
        await handle_appstore_notification(notif_payload)
    assert len(measurement_calls) == 0

    # 4. Measurement throws -> notification handling still succeeds (returns True)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)
    async def fake_record_throw(cid, tier, tx_info):
        raise RuntimeError("Measurement write error")
    monkeypatch.setattr("app.services.acquisition.record_payment_measurement", fake_record_throw)

    handled_safe = await handle_appstore_notification(notif_payload)
    assert handled_safe is True
