"""Unit tests for receipt-bound renewal-status truth (DID_CHANGE_RENEWAL_STATUS).

Comprehensive test coverage covering all verifier round-2 requirements:
- Section 1: Production defects (cross-receipt clock separation, strict cross-payload validation,
  explicit original ID on direct entitlement, finite math on charges/expiries);
- Section 2: Restored service contract (active service traversal, fallback directions, claim rejection,
  signedDate guards, same-chain preservation, generic notification isolation);
- Section 3: Truthful nonexistent document representation, real transaction.update error propagation to HTTP 500,
  inactive missing/malformed clock matrix, and deterministic conflict winner synchronization.
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import google.cloud.firestore as fs_mod
import pytest
from app.db.contractors import (
    PROTECTED_FIELDS,
    activate_subscription_entitlement,
    record_active_renewal_status,
    record_inactive_notification,
)
from app.services import subscription as sub_service
from app.services.subscription import (
    SubscriptionUpdateOutcome,
    update_subscription_from_transaction,
)


_OMITTED = object()


class FakeDocRef:
    def __init__(self, data=_OMITTED):
        if data is _OMITTED:
            self.data = {}
            self.exists = True
        elif data is None:
            self.data = None
            self.exists = False
        else:
            self.data = data
            self.exists = True
        self.updates = {}

    def get(self, transaction=None):
        class Snap:
            def __init__(self, exists, d):
                self.exists = exists
                self._d = d

            def to_dict(self):
                return dict(self._d) if self._d is not None else None

        return Snap(self.exists, self.data)

    def update(self, vals):
        if self.data is not None:
            self.data.update(vals)
        self.updates.update(vals)


class FakeTransaction:
    def __init__(self, on_update=None):
        self._read_only = False
        self.id = b"test-tx"
        self._on_update = on_update

    def update(self, doc_ref, vals):
        if self._on_update:
            self._on_update(doc_ref, vals)
        doc_ref.update(vals)


class FakeDB:
    def __init__(self, doc_ref=None, on_update=None):
        self._doc_ref = doc_ref if doc_ref is not None else FakeDocRef()
        self._on_update = on_update

    def collection(self, _name):
        return self

    def document(self, _id):
        return self._doc_ref

    def transaction(self):
        return FakeTransaction(on_update=self._on_update)


@pytest.fixture(autouse=True)
def patch_firestore_transactional(monkeypatch):
    monkeypatch.setattr(fs_mod, "transactional", lambda fn: fn)


def _renewal_payload(subtype="AUTO_RENEW_DISABLED", data=None):
    if data is None:
        data = {
            "signedTransactionInfo": "signed-tx-jwt",
            "signedRenewalInfo": "signed-rn-jwt",
        }
    p = {
        "notificationType": "DID_CHANGE_RENEWAL_STATUS",
        "subtype": subtype,
    }
    if data is not False:
        p["data"] = data
    return p


def _tx_info(
    uuid="uuid-1",
    original_id="otx-1",
    transaction_id="tx-1",
    product_id="com.kevin.callscreen.personal.monthly",
    environment="Production",
    expires_date=4102444800000,
):
    d = {
        "transactionId": transaction_id,
        "productId": product_id,
        "environment": environment,
        "expiresDate": expires_date,
    }
    if uuid is not _OMITTED:
        d["appAccountToken"] = uuid
    if original_id is not _OMITTED:
        d["originalTransactionId"] = original_id
    return d


def _rn_info(
    uuid="uuid-1",
    original_id="otx-1",
    auto_renew_status=0,
    signed_date=1700000000000,
    product_id="com.kevin.callscreen.personal.monthly",
    environment="Production",
):
    d = {
        "signedDate": signed_date,
        "productId": product_id,
        "environment": environment,
    }
    if uuid is not _OMITTED:
        d["appAccountToken"] = uuid
    if original_id is not _OMITTED:
        d["originalTransactionId"] = original_id
    if auto_renew_status is not _OMITTED:
        d["autoRenewStatus"] = auto_renew_status
    return d


# ===========================================================================
# Section 1.1 & 3.3: Inactive clock comparison only within matching chain
# ===========================================================================


@pytest.mark.asyncio
async def test_regression_round2_inactive_clock_cross_receipt_replacement(monkeypatch):
    """Top-level current receipt B, nested receipt A@9000, incoming event B@1000.
    Must bind nested receipt B at 1000, not leave A@9000."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-B",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "last_type": "DID_CHANGE_RENEWAL_STATUS",
            "last_subtype": "AUTO_RENEW_DISABLED",
            "last_transaction_id": "tx-old",
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": 9000,
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-new",
        renewal_observation={
            "original_transaction_id": "otx-B",
            "auto_renews": True,
            "signed_at_ms": 1000,
        },
    )
    assert res["outcome"] == "recorded"
    pdb = doc["post_deletion_billing"]
    assert pdb["renewal_original_transaction_id"] == "otx-B"
    assert pdb["renewal_auto_renews"] is True
    assert pdb["renewal_status_signed_at_ms"] == 1000


@pytest.mark.asyncio
async def test_regression_round3_inactive_same_receipt_missing_or_none_clock_is_first_observation(
    monkeypatch,
):
    """Missing or None same-receipt clock in prior post_deletion_billing must be accepted as first observation."""
    # Case 1: missing clock key in prior post_deletion_billing on same receipt otx-A -> normal recorded
    doc1 = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-A",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            # renewal_status_signed_at_ms key is missing
        },
    }
    doc_ref1 = FakeDocRef(doc1)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref1))

    res1 = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-new1",
        renewal_observation={
            "original_transaction_id": "otx-A",
            "auto_renews": True,
            "signed_at_ms": 1500,
        },
    )
    assert res1["outcome"] == "recorded"
    assert doc1["post_deletion_billing"]["renewal_auto_renews"] is True
    assert doc1["post_deletion_billing"]["renewal_status_signed_at_ms"] == 1500

    # Case 2: explicit None clock in prior post_deletion_billing on same receipt otx-A + duplicate tuple -> renewal_update
    doc2 = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-A",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "last_type": "DID_CHANGE_RENEWAL_STATUS",
            "last_subtype": "AUTO_RENEW_DISABLED",
            "last_transaction_id": "tx-dup",
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": None,
        },
    }
    doc_ref2 = FakeDocRef(doc2)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref2))

    res2 = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-dup",
        renewal_observation={
            "original_transaction_id": "otx-A",
            "auto_renews": False,
            "signed_at_ms": 2500,
        },
    )
    assert res2["outcome"] == "renewal_update"
    assert doc2["post_deletion_billing"]["count"] == 1
    assert doc2["post_deletion_billing"]["charges"] == 0
    assert doc2["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc2["post_deletion_billing"]["renewal_status_signed_at_ms"] == 2500


@pytest.mark.asyncio
async def test_regression_round3_direct_entitlement_strict_alias_handling(monkeypatch):
    """Direct entitlement must distinguish absent aliases from present malformed/disagreeing values."""
    claims = []
    activations = []

    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_activate(**kwargs):
        activations.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate)

    base_tx = {
        "transactionId": "tx-1",
        "appAccountToken": "uuid-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": (time.time() + 3600) * 1000,
    }

    # 1. Canonical present-null plus valid alias -> REJECT
    tx1 = dict(base_tx)
    tx1["originalTransactionId"] = None
    tx1["original_transaction_id"] = "otx-alias"
    res1 = await update_subscription_from_transaction("c1", tx1)
    assert res1.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert claims == []
    assert activations == []

    # 2. Canonical A plus alias B (disagreeing) -> REJECT
    tx2 = dict(base_tx)
    tx2["originalTransactionId"] = "otx-A"
    tx2["original_transaction_id"] = "otx-B"
    res2 = await update_subscription_from_transaction("c1", tx2)
    assert res2.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert claims == []
    assert activations == []

    # 3. Canonical truly absent, alias-only valid -> ACCEPT
    tx3 = dict(base_tx)
    tx3["original_transaction_id"] = "otx-alias-only"
    res3 = await update_subscription_from_transaction("c1", tx3)
    assert res3.outcome == SubscriptionUpdateOutcome.ACTIVE
    assert len(claims) == 1
    assert len(activations) == 1
    assert claims[0]["original_transaction_id"] == "otx-alias-only"
    assert activations[0]["original_transaction_id"] == "otx-alias-only"

    # 4. Both present and matching -> ACCEPT
    tx4 = dict(base_tx)
    tx4["originalTransactionId"] = "otx-same"
    tx4["original_transaction_id"] = "otx-same"
    res4 = await update_subscription_from_transaction("c1", tx4)
    assert res4.outcome == SubscriptionUpdateOutcome.ACTIVE
    assert len(claims) == 2
    assert len(activations) == 2
    assert claims[1]["original_transaction_id"] == "otx-same"
    assert activations[1]["original_transaction_id"] == "otx-same"


@pytest.mark.asyncio
async def test_regression_round3_corrupt_non_finite_prior_counters_normalize_safely(monkeypatch):
    """Corrupt non-finite prior count and charges must not raise OverflowError or 500."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": {
            "count": float("inf"),
            "charges": float("nan"),
            "last_type": "DID_RENEW",
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_RENEW",
        transaction_id="tx-fresh",
        purchase_date_ms=1750000000000,
    )
    assert res["outcome"] == "recorded"
    assert res["count"] == 1
    assert res["charges"] == 1
    assert doc["post_deletion_billing"]["count"] == 1
    assert doc["post_deletion_billing"]["charges"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_id,expiry_builder,expected_outcome,should_activate",
    [
        (
            "future_int_ms",
            lambda: int((time.time() + 3600) * 1000),
            SubscriptionUpdateOutcome.ACTIVE,
            True,
        ),
        (
            "future_float_ms",
            lambda: float((time.time() + 3600) * 1000) + 0.5,
            SubscriptionUpdateOutcome.ACTIVE,
            True,
        ),
        (
            "future_numeric_str_ms",
            lambda: str(int((time.time() + 3600) * 1000)),
            SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
            False,
        ),
    ],
)
async def test_regression_round4_numeric_string_expiry_rejected_before_claim_or_activation(
    monkeypatch, case_id, expiry_builder, expected_outcome, should_activate
):
    """Direct update_subscription_from_transaction preserves valid int/float expiry and rejects numeric string."""
    lookups = []
    claims = []
    activations = []

    async def fake_get_contractor(cid):
        lookups.append(cid)
        return {"contractor_id": cid, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_activate(**kwargs):
        activations.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate)

    raw_expiry = expiry_builder()
    tx_info = {
        "transactionId": "tx-1",
        "originalTransactionId": "otx-1",
        "appAccountToken": "uuid-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "environment": "Production",
        "expiresDate": raw_expiry,
    }

    res = await update_subscription_from_transaction(
        contractor_id="c1",
        transaction_info=tx_info,
    )
    assert res.outcome == expected_outcome

    if should_activate:
        assert len(lookups) == 1
        assert lookups[0] == "c1"
        assert len(claims) == 1
        assert claims[0] == {
            "original_transaction_id": "otx-1",
            "contractor_id": "c1",
            "transaction_id": "tx-1",
            "product_id": "com.kevin.callscreen.personal.monthly",
            "environment": "Production",
        }
        assert len(activations) == 1
        assert activations[0] == {
            "contractor_id": "c1",
            "tier": "personal",
            "expires_ts": float(raw_expiry) / 1000.0,
            "original_transaction_id": "otx-1",
            "expected_subscription_uuid": "uuid-1",
        }
    else:
        assert res.reason == "missing_or_invalid_expiry"
        assert len(claims) == 0
        assert len(activations) == 0


@pytest.mark.asyncio
async def test_inactive_clock_same_receipt_strict_comparison(monkeypatch):
    """Event for already-nested A still compares against A clock."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-A",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": 5000,
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    # Older event @ 4000 -> does not advance
    res_older = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-older",
        renewal_observation={
            "original_transaction_id": "otx-A",
            "auto_renews": True,
            "signed_at_ms": 4000,
        },
    )
    assert res_older["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 5000

    # Newer event @ 6000 -> advances
    res_newer = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-newer",
        renewal_observation={
            "original_transaction_id": "otx-A",
            "auto_renews": True,
            "signed_at_ms": 6000,
        },
    )
    assert res_newer["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is True
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 6000


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_prior_clock", [True, False, 1000.5, "1000", 0, -500])
async def test_inactive_malformed_prior_clock_preserves_evidence_while_recording_audit(
    monkeypatch, bad_prior_clock
):
    """When prior nested clock is malformed, incoming valid same-receipt renewal observation cannot advance clock/boolean, but audit count/type record."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-A",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "last_transaction_id": "tx-seed",
            "last_type": "DID_CHANGE_RENEWAL_STATUS",
            "last_subtype": "AUTO_RENEW_DISABLED",
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": bad_prior_clock,
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-audit-distinct",
        renewal_observation={
            "original_transaction_id": "otx-A",
            "auto_renews": True,
            "signed_at_ms": 2000000000000,
        },
    )
    assert res["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 2
    assert doc["post_deletion_billing"]["charges"] == 0
    assert doc["post_deletion_billing"]["last_type"] == "DID_CHANGE_RENEWAL_STATUS"
    assert doc["post_deletion_billing"]["last_subtype"] == "AUTO_RENEW_ENABLED"
    assert doc["post_deletion_billing"]["last_transaction_id"] == "tx-audit-distinct"
    assert doc["post_deletion_billing"]["renewal_original_transaction_id"] == "otx-A"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == bad_prior_clock


@pytest.mark.asyncio
async def test_inactive_event_receipt_mismatches_both_top_and_nested(monkeypatch):
    """Event receipt matching neither top-level nor nested ignores renewal but records generic audit."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-B",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": 5000,
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-mismatch",
        renewal_observation={
            "original_transaction_id": "otx-C",
            "auto_renews": True,
            "signed_at_ms": 8000,
        },
    )
    assert res["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 2
    assert doc["post_deletion_billing"]["renewal_original_transaction_id"] == "otx-A"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 5000


@pytest.mark.asyncio
async def test_inactive_neither_top_nor_nested_exists(monkeypatch):
    """Neither top-level nor nested original ID exists -> renewal observation ignored, generic records."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-none",
        renewal_observation={
            "original_transaction_id": "otx-C",
            "auto_renews": True,
            "signed_at_ms": 8000,
        },
    )
    assert res["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 1
    assert "renewal_original_transaction_id" not in doc["post_deletion_billing"]


# ===========================================================================
# Section 1.2 & 2: Strict cross-payload presence and agreement validation
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tx_field_overrides,rn_field_overrides,should_accept",
    [
        # appAccountToken malformed on tx side with valid rn counterpart -> REJECT
        ({"appAccountToken": 123}, {"appAccountToken": "uuid-1"}, False),
        ({"appAccountToken": None}, {"appAccountToken": "uuid-1"}, False),
        ({"appAccountToken": True}, {"appAccountToken": "uuid-1"}, False),
        ({"appAccountToken": "   "}, {"appAccountToken": "uuid-1"}, False),
        ({"appAccountToken": []}, {"appAccountToken": "uuid-1"}, False),
        # appAccountToken malformed on rn side with valid tx counterpart -> REJECT
        ({"appAccountToken": "uuid-1"}, {"appAccountToken": 123}, False),
        ({"appAccountToken": "uuid-1"}, {"appAccountToken": None}, False),
        ({"appAccountToken": "uuid-1"}, {"appAccountToken": "  "}, False),
        # appAccountToken valid-string mismatch across payloads -> REJECT
        ({"appAccountToken": "uuid-1"}, {"appAccountToken": "uuid-2"}, False),
        # appAccountToken truly absent from both payloads -> REJECT
        ({"appAccountToken": _OMITTED}, {"appAccountToken": _OMITTED}, False),
        # originalTransactionId malformed on one side with valid counterpart -> REJECT
        ({"originalTransactionId": 999}, {"originalTransactionId": "otx-1"}, False),
        ({"originalTransactionId": "otx-1"}, {"originalTransactionId": {}}, False),
        ({"originalTransactionId": ""}, {"originalTransactionId": "otx-1"}, False),
        # originalTransactionId valid-string mismatch across payloads -> REJECT
        ({"originalTransactionId": "otx-1"}, {"originalTransactionId": "otx-2"}, False),
        # originalTransactionId truly absent from both payloads -> REJECT
        ({"originalTransactionId": _OMITTED}, {"originalTransactionId": _OMITTED}, False),
        # productId malformed on one side with valid counterpart -> REJECT
        ({"productId": 123}, {"productId": "com.kevin.callscreen.personal.monthly"}, False),
        ({"productId": "com.kevin.callscreen.personal.monthly"}, {"productId": None}, False),
        ({"productId": "   "}, {"productId": "com.kevin.callscreen.personal.monthly"}, False),
        # environment malformed on one side with valid counterpart -> REJECT
        ({"environment": False}, {"environment": "Production"}, False),
        ({"environment": "Production"}, {"environment": 123}, False),
    ],
)
async def test_regression_round2_reject_present_malformed_cross_payload_values(
    monkeypatch, tx_field_overrides, rn_field_overrides, should_accept
):
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    tx = _tx_info()
    for k, v in tx_field_overrides.items():
        if v is _OMITTED:
            tx.pop(k, None)
        else:
            tx[k] = v
    rn = _rn_info()
    for k, v in rn_field_overrides.items():
        if v is _OMITTED:
            rn.pop(k, None)
        else:
            rn[k] = v

    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: tx if jwt == "signed-tx-jwt" else rn,
    )

    res = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert res is should_accept
    if not should_accept:
        assert len(lookups) == 0, f"Expected 0 lookups on rejection, got {lookups}"
        assert len(claims) == 0, f"Expected 0 claims on rejection, got {claims}"
        assert len(writes) == 0, f"Expected 0 writes on rejection, got {writes}"


@pytest.mark.asyncio
async def test_cross_payload_valid_absent_fallbacks(monkeypatch):
    """Valid absent fallback in either direction succeeds with exact arguments and call counts."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    # 1. token & orig in tx only, absent from rn -> SUCCESS
    tx1 = _tx_info(
        uuid="uuid-1",
        original_id="otx-1",
        product_id="com.kevin.callscreen.personal.monthly",
        environment="Production",
    )
    rn1 = {"autoRenewStatus": 0, "signedDate": 1000}
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: tx1 if jwt == "signed-tx-jwt" else rn1)

    lookups.clear()
    claims.clear()
    writes.clear()

    res1 = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert res1 is True
    assert lookups == [("uuid-1", False)]
    assert len(claims) == 1
    assert claims[0] == {
        "original_transaction_id": "otx-1",
        "contractor_id": "c1",
        "transaction_id": "tx-1",
        "product_id": "com.kevin.callscreen.personal.monthly",
        "environment": "Production",
    }
    assert len(writes) == 1
    assert writes[0] == {
        "contractor_id": "c1",
        "expected_subscription_uuid": "uuid-1",
        "original_transaction_id": "otx-1",
        "auto_renews": False,
        "signed_at_ms": 1000,
    }

    # 2. token & orig in rn only, absent from tx -> SUCCESS
    tx2 = {"transactionId": "tx-2"}
    rn2 = _rn_info(
        uuid="uuid-1",
        original_id="otx-1",
        auto_renew_status=0,
        signed_date=2000,
        product_id="com.kevin.callscreen.personal.monthly",
        environment="Production",
    )
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: tx2 if jwt == "signed-tx-jwt" else rn2)

    lookups.clear()
    claims.clear()
    writes.clear()

    res2 = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert res2 is True
    assert lookups == [("uuid-1", False)]
    assert len(claims) == 1
    assert claims[0] == {
        "original_transaction_id": "otx-1",
        "contractor_id": "c1",
        "transaction_id": "tx-2",
        "product_id": "com.kevin.callscreen.personal.monthly",
        "environment": "Production",
    }
    assert len(writes) == 1
    assert writes[0] == {
        "contractor_id": "c1",
        "expected_subscription_uuid": "uuid-1",
        "original_transaction_id": "otx-1",
        "auto_renews": False,
        "signed_at_ms": 2000,
    }


# ===========================================================================
# Section 1.3: Explicit original ID required on direct verified entitlement
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_orig_id",
    [
        None,
        "",
        "   ",
        12345,
        True,
        [],
    ],
)
async def test_regression_round2_direct_entitlement_requires_explicit_original_id(
    monkeypatch, bad_orig_id
):
    claims = []
    activations = []

    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_activate(**kwargs):
        activations.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate)

    tx_info = {
        "transactionId": "tx-12345",
        "appAccountToken": "uuid-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": (time.time() + 3600) * 1000,
        "originalTransactionId": bad_orig_id,
    }

    res = await update_subscription_from_transaction(
        contractor_id="c1",
        transaction_info=tx_info,
    )
    assert res.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert len(claims) == 0
    assert len(activations) == 0


# ===========================================================================
# Section 1.4: Finite math on charges and expiries
# ===========================================================================


@pytest.mark.asyncio
async def test_regression_round2_non_finite_deactivated_at_is_conservative_charge(monkeypatch):
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "deactivated_at": float("inf"),
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_RENEW",
        transaction_id="tx-1",
        purchase_date_ms=1750000000000,
    )
    assert res["outcome"] == "recorded"
    assert res["charged_after_deletion"] is True
    assert res["charges"] == 1
    assert doc["post_deletion_billing"]["charges"] == 1


@pytest.mark.asyncio
async def test_regression_round2_direct_entitlement_rejects_non_finite_expiry(monkeypatch):
    claims = []
    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)

    tx_info = {
        "transactionId": "tx-1",
        "originalTransactionId": "otx-1",
        "appAccountToken": "uuid-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": float("inf"),
    }
    res = await update_subscription_from_transaction(
        contractor_id="c1",
        transaction_info=tx_info,
    )
    assert res.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert len(claims) == 0


# ===========================================================================
# Section 2: Restored direct executable service contracts
# ===========================================================================


@pytest.mark.asyncio
async def test_service_traversal_active_transitions_and_entitlement_preservation(monkeypatch):
    """Real service traversal calls real active helper, claims first, changes only renewal clock,
    preserves status/tier/expiry/binding/unrelated fields, and NEVER calls update_contractor."""
    initial_doc = {
        "contractor_id": "c1",
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "subscription_status": "active_sentinel",
        "subscription_tier": "business_sentinel",
        "subscription_expires": 123456789.0,
        "business_name": "Sentinel Trades LLC",
        "custom_key": 42,
    }
    doc_ref = FakeDocRef(initial_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    async def fake_lookup(uuid, include_inactive=False):
        return dict(initial_doc)

    claims = []
    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, "c1"

    async def forbidden_update_contractor(*args, **kwargs):
        raise AssertionError("update_contractor MUST NOT be called by DID_CHANGE_RENEWAL_STATUS")

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.update_contractor", forbidden_update_contractor)

    # 1. First event: AUTO_RENEW_DISABLED @ 1000
    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000),
    )
    h1 = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert h1 is True
    assert len(claims) == 1
    assert claims[0] == {
        "original_transaction_id": "otx-1",
        "contractor_id": "c1",
        "transaction_id": "tx-1",
        "product_id": "com.kevin.callscreen.personal.monthly",
        "environment": "Production",
    }
    assert doc_ref.updates == {
        "subscription_auto_renews": False,
        "subscription_renewal_status_signed_at_ms": 1000,
    }
    assert doc_ref.data["subscription_auto_renews"] is False
    assert doc_ref.data["subscription_renewal_status_signed_at_ms"] == 1000
    assert doc_ref.data["subscription_status"] == "active_sentinel"
    assert doc_ref.data["subscription_tier"] == "business_sentinel"
    assert doc_ref.data["subscription_expires"] == 123456789.0
    assert doc_ref.data["business_name"] == "Sentinel Trades LLC"
    assert doc_ref.data["custom_key"] == 42

    # 2. Second event: AUTO_RENEW_ENABLED @ 2000
    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=1, signed_date=2000),
    )
    h2 = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_ENABLED"))
    assert h2 is True
    assert len(claims) == 2
    assert claims[1] == {
        "original_transaction_id": "otx-1",
        "contractor_id": "c1",
        "transaction_id": "tx-1",
        "product_id": "com.kevin.callscreen.personal.monthly",
        "environment": "Production",
    }
    assert doc_ref.updates == {
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 2000,
    }
    assert doc_ref.data["subscription_auto_renews"] is True
    assert doc_ref.data["subscription_renewal_status_signed_at_ms"] == 2000
    assert doc_ref.data["subscription_status"] == "active_sentinel"


@pytest.mark.asyncio
async def test_claim_rejection_prevents_active_helper_mutation(monkeypatch):
    """Claim rejection stops before active helper write; doc is unmutated."""
    initial_doc = {
        "contractor_id": "c1",
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 500,
    }
    doc_ref = FakeDocRef(initial_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    async def fake_lookup(uuid, include_inactive=False):
        return dict(initial_doc)

    async def rejecting_claim(**kwargs):
        return False, "other-contractor"

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", rejecting_claim)

    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000),
    )
    res = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert res is False
    assert doc_ref.data["subscription_auto_renews"] is True
    assert doc_ref.data["subscription_renewal_status_signed_at_ms"] == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_signed_date",
    [
        0,
        -100,
        True,
        False,
        1000.5,
        "1000",
        None,
        [],
        {},
    ],
)
async def test_service_parsing_rejects_malformed_signed_date(monkeypatch, bad_signed_date):
    """Service parsing rejects invalid signedDate before lookup/claim/write."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append(uuid)
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(*args, **kwargs):
        writes.append(args)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    rn = _rn_info(auto_renew_status=0, signed_date=bad_signed_date)
    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else rn,
    )

    res = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert res is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("generic_notif_type", ["DID_RENEW", "SUBSCRIBED"])
async def test_generic_notification_never_selects_or_replaces_original_transaction_id(
    monkeypatch, generic_notif_type
):
    """Generic notification handler (DID_RENEW, SUBSCRIBED) on active user never updates subscription_original_transaction_id."""
    contractor = {
        "contractor_id": "c1",
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_tier": "personal",
        "subscription_original_transaction_id": "otx-pinned",
    }
    captured_updates = []

    async def fake_lookup(uuid, include_inactive=False):
        return contractor

    async def fake_update(cid, fields):
        captured_updates.append(fields)
        return True

    async def fake_claim(**kwargs):
        return True, "c1"

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)

    generic_tx = _tx_info(uuid="uuid-1", original_id="otx-new", transaction_id="tx-renew")
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda _jwt: generic_tx)

    payload = {
        "notificationType": generic_notif_type,
        "data": {"signedTransactionInfo": "signed-jwt"},
    }
    handled = await sub_service.handle_appstore_notification(payload)
    assert handled is True
    assert len(captured_updates) > 0
    for upd in captured_updates:
        assert "subscription_original_transaction_id" not in upd


# ===========================================================================
# Section 3.1: Truthful nonexistent documents representation
# ===========================================================================


@pytest.mark.asyncio
async def test_truthful_nonexistent_documents(monkeypatch):
    """FakeDocRef(None) represents a truly nonexistent document (exists=False).
    All helpers must return the appropriate no-op / not_found outcome with zero staged updates."""
    doc_ref_none = FakeDocRef(None)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_none))

    # 1. activate_subscription_entitlement
    ok_act = await activate_subscription_entitlement(
        contractor_id="c1",
        tier="personal",
        expires_ts=2000000000.0,
        original_transaction_id="otx-1",
        expected_subscription_uuid="uuid-1",
    )
    assert ok_act is False
    assert doc_ref_none.updates == {}

    # 2. record_active_renewal_status
    ok_rec = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-1",
        auto_renews=False,
        signed_at_ms=1000,
    )
    assert ok_rec is False
    assert doc_ref_none.updates == {}

    # 3. record_inactive_notification
    res_inact = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_RENEW",
        transaction_id="tx-1",
    )
    assert res_inact["outcome"] == "not_found"
    assert doc_ref_none.updates == {}


# ===========================================================================
# Section 3.2: Real transaction.update failure reaches webhook 500
# ===========================================================================


@pytest.mark.asyncio
async def test_appstore_webhook_http_500_on_real_transaction_update_failure(monkeypatch):
    """Real record_inactive_notification helper and real service handler propagate a
    transaction.update failure out to the webhook route, which returns HTTP 500."""
    from app.webhooks.appstore import handle_appstore_notification as webhook_endpoint

    inactive_doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(inactive_doc)

    def failing_update(ref, fields):
        raise RuntimeError("firestore write error")

    db_instance = FakeDB(doc_ref=doc_ref, on_update=failing_update)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: db_instance)

    async def fake_lookup(uuid, include_inactive=False):
        if not include_inactive:
            return None
        return {"contractor_id": "c1", "active": False, "subscription_uuid": "uuid-1"}

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", lambda *a: None)

    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(),
    )
    monkeypatch.setattr(
        "app.webhooks.appstore._decode_notification_payload",
        lambda sp: _renewal_payload("AUTO_RENEW_DISABLED"),
    )

    class MockRequest:
        def __init__(self, json_data):
            self._json_data = json_data
        async def json(self):
            return self._json_data

    req = MockRequest({"signedPayload": "signed-root-jwt"})
    resp = await webhook_endpoint(req)
    assert resp.status_code == 500
    body = json.loads(resp.body)
    assert body["error"] == "internal processing error"


# ===========================================================================
# Section 3.4: Deterministically force each conflict winner
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["generic", "renewal"])
async def test_deterministic_optimistic_conflict_retry_forced_winner(winner):
    """Deterministically forces the specified task to commit first while both read v1.
    The loser observes a version conflict, retries against v2, and successfully commits v3."""
    from google.api_core.exceptions import Aborted

    class VersionedStore:
        def __init__(self):
            self.data = {
                "active": False,
                "subscription_uuid": "uuid-1",
                "subscription_original_transaction_id": "otx-1",
                "post_deletion_billing": None,
            }
            self.version = 1
            self.retries = 0
            self.read_barrier = threading.Barrier(2)
            self.first_commit_event = threading.Event()
            self.lock = threading.Lock()
            self.first_winner = None

    store = VersionedStore()

    class VersionedTransaction:
        def __init__(self, store, task_name):
            self.store = store
            self.task_name = task_name
            self.read_version = 0
            self.attempt = 0
            self.staged_updates = {}

        def update(self, doc_ref, fields):
            self.staged_updates.update(fields)

    class VersionedDocRef:
        def __init__(self, store):
            self.store = store

        def get(self, transaction=None):
            with self.store.lock:
                if transaction:
                    transaction.read_version = self.store.version
                class Snap:
                    def __init__(self, d):
                        self.exists = True
                        self._d = dict(d)

                    def to_dict(self):
                        return dict(self._d)

                snap = Snap(self.store.data)

            if transaction and transaction.attempt == 0:
                self.store.read_barrier.wait(timeout=3.0)
            return snap

    class VersionedFakeDB:
        def __init__(self, store):
            self.store = store
            self._current_task_name = "unknown"

        def collection(self, _name):
            return self

        def document(self, _id):
            return VersionedDocRef(self.store)

        def transaction(self):
            return VersionedTransaction(self.store, self._current_task_name)

    db_instance = VersionedFakeDB(store)

    def transactional_with_retry(fn):
        def wrapper(transaction, *args, **kwargs):
            for attempt in range(5):
                transaction.attempt = attempt
                transaction.staged_updates = {}
                try:
                    result = fn(transaction, *args, **kwargs)
                    # Gating for attempt 0: enforce winner
                    if attempt == 0:
                        if transaction.task_name != winner:
                            # Loser waits until winner has committed
                            store.first_commit_event.wait(timeout=3.0)

                    with store.lock:
                        if transaction.read_version != store.version:
                            store.retries += 1
                            raise Aborted("Version conflict")
                        store.data.update(transaction.staged_updates)
                        store.version += 1
                        if store.first_winner is None:
                            store.first_winner = transaction.task_name
                            store.first_commit_event.set()
                        return result
                except Aborted:
                    if attempt == 4:
                        raise
            return None
        return wrapper

    import app.db.contractors as db_contractors

    orig_get_db = db_contractors.get_firestore_client
    orig_txn = fs_mod.transactional
    try:
        db_contractors.get_firestore_client = lambda: db_instance
        fs_mod.transactional = transactional_with_retry

        async def run_generic():
            db_instance._current_task_name = "generic"
            return await record_inactive_notification(
                contractor_id="c1",
                expected_subscription_uuid="uuid-1",
                notification_type="DID_RENEW",
                transaction_id="tx-generic",
                purchase_date_ms=1750000000000,
            )

        async def run_renewal():
            db_instance._current_task_name = "renewal"
            return await record_inactive_notification(
                contractor_id="c1",
                expected_subscription_uuid="uuid-1",
                notification_type="DID_CHANGE_RENEWAL_STATUS",
                subtype="AUTO_RENEW_DISABLED",
                transaction_id="tx-renewal",
                renewal_observation={
                    "original_transaction_id": "otx-1",
                    "auto_renews": False,
                    "signed_at_ms": 1000,
                },
            )

        res1, res2 = await asyncio.gather(run_generic(), run_renewal())
        assert res1["outcome"] == "recorded"
        assert res2["outcome"] == "recorded"
        assert store.first_winner == winner
        assert store.retries >= 1

        pdb = store.data["post_deletion_billing"]
        assert pdb["count"] == 2
        assert pdb["charges"] == 1
        assert pdb["renewal_original_transaction_id"] == "otx-1"
        assert pdb["renewal_auto_renews"] is False
        assert pdb["renewal_status_signed_at_ms"] == 1000
    finally:
        db_contractors.get_firestore_client = orig_get_db
        fs_mod.transactional = orig_txn


# ===========================================================================
# Section B3, C1, C2: Boundary protections, catalog proofs
# ===========================================================================


@pytest.mark.asyncio
async def test_contractor_patch_boundary_drops_protected_fields(monkeypatch):
    """Submitting the 4 protected fields through contractor PATCH drops them before update_contractor."""
    from app.api import contractors as contractors_api

    captured_updates = []

    async def fake_get_contractor(cid):
        return {"contractor_id": "c1", "business_name": "Old Business", "active": True}

    async def fake_update_contractor(cid, fields):
        captured_updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    # 1. Pydantic model parses standard editable fields and drops unknown/protected fields
    body_dict = {
        "business_name": "New Business",
        "subscription_original_transaction_id": "hack_otx",
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 999999,
        "subscription_forwarded_from": "c0",
    }
    update_model = contractors_api.ContractorUpdate(**body_dict)
    assert not hasattr(update_model, "subscription_original_transaction_id")
    assert not hasattr(update_model, "subscription_auto_renews")
    assert not hasattr(update_model, "subscription_renewal_status_signed_at_ms")
    assert not hasattr(update_model, "subscription_forwarded_from")

    # 2. Endpoint handler filters out any PROTECTED_FIELDS from update payload
    req = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id="c1"))
    res = await contractors_api.api_update_contractor("c1", update_model, req)
    assert res == {"status": "ok"}
    assert len(captured_updates) == 1
    cid, fields = captured_updates[0]
    assert cid == "c1"
    assert fields == {"business_name": "New Business"}
    assert "subscription_original_transaction_id" not in fields
    assert "subscription_auto_renews" not in fields
    assert "subscription_renewal_status_signed_at_ms" not in fields
    assert "subscription_forwarded_from" not in fields


def test_localization_catalog_exact_reviewed_translations_and_no_obsolete_keys():
    """Verify ios/Kevin/Localizable.xcstrings contains exact approved copy and no obsolete keys."""
    catalog_path = Path(__file__).resolve().parents[2] / "ios" / "Kevin" / "Localizable.xcstrings"
    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    strings = catalog["strings"]

    # 1. Exact English Keys
    title_key = "Check Apple Subscription"
    body_key = (
        "Deleting your Hey Kevin account does not cancel your Apple subscription. "
        "Check Manage Subscription first to make sure automatic renewal is off."
    )
    assert title_key in strings
    assert body_key in strings

    # 2. Exact Spanish Translations
    assert strings[title_key]["localizations"]["es"]["stringUnit"]["value"] == "Revisa la suscripción de Apple"
    assert strings[body_key]["localizations"]["es"]["stringUnit"]["value"] == (
        "Eliminar tu cuenta de Hey Kevin no cancela tu suscripción de Apple. "
        "Primero, abre Gestionar suscripción y comprueba que la renovación automática esté desactivada."
    )

    # 3. Exact Brazilian Portuguese Translations
    assert strings[title_key]["localizations"]["pt-BR"]["stringUnit"]["value"] == "Verifique a assinatura da Apple"
    assert strings[body_key]["localizations"]["pt-BR"]["stringUnit"]["value"] == (
        "Excluir sua conta do Hey Kevin não cancela sua assinatura da Apple. "
        "Primeiro, acesse Gerenciar assinatura e verifique se a renovação automática está desativada."
    )

    # 4. Obsolete keys absent
    assert "Subscription Still Active" not in strings
    assert (
        "Deleting your account does not cancel your Apple subscription, and you would keep being charged. "
        "Cancel it under Manage Subscription first."
    ) not in strings


def test_ios_sources_contain_no_auto_renews_consumption():
    """Verify iOS Swift sources do not consume subscription_auto_renews."""
    ios_dir = Path(__file__).resolve().parents[2] / "ios" / "Kevin"
    for swift_file in ios_dir.rglob("*.swift"):
        with open(swift_file, "r", encoding="utf-8") as f:
            content = f.read()
            assert "subscription_auto_renews" not in content, f"Unexpected consumption in {swift_file}"


def test_four_internal_fields_in_protected_fields():
    expected = {
        "subscription_original_transaction_id",
        "subscription_auto_renews",
        "subscription_renewal_status_signed_at_ms",
        "subscription_forwarded_from",
    }
    assert expected <= set(PROTECTED_FIELDS)


def test_four_internal_fields_redacted_from_profile_responses():
    from app.api.contractors import _redact_contractor

    doc = {
        "contractor_id": "c1",
        "business_name": "Test Plumbing",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_original_transaction_id": "otx-1",
        "subscription_auto_renews": False,
        "subscription_renewal_status_signed_at_ms": 1000,
        "subscription_forwarded_from": "c0",
    }

    redacted = _redact_contractor(doc)
    assert "subscription_original_transaction_id" not in redacted
    assert "subscription_auto_renews" not in redacted
    assert "subscription_renewal_status_signed_at_ms" not in redacted
    assert "subscription_forwarded_from" not in redacted
    assert redacted["business_name"] == "Test Plumbing"


# ===========================================================================
# 11 Targeted Mutation Sensitivity Probes (Tests)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subtype,status_val",
    [
        ("AUTO_RENEW_DISABLED", 1),
        ("AUTO_RENEW_ENABLED", 0),
    ],
)
async def test_mutation_probe_1_subtype_status_guard(monkeypatch, subtype, status_val):
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append(uuid)
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(*args, **kwargs):
        writes.append(args)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    rn = _rn_info(auto_renew_status=status_val, signed_date=1000)
    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else rn,
    )

    res = await sub_service.handle_appstore_notification(_renewal_payload(subtype))
    assert res is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
async def test_mutation_probe_2_active_current_original_binding(monkeypatch):
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-other",
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    ok = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-1",
        auto_renews=False,
        signed_at_ms=1000,
    )
    assert ok is False
    assert doc_ref.updates == {}


@pytest.mark.asyncio
async def test_mutation_probe_3_strict_newer_clock(monkeypatch):
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "subscription_auto_renews": False,
        "subscription_renewal_status_signed_at_ms": 2000,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    ok = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-1",
        auto_renews=True,
        signed_at_ms=2000,
    )
    assert ok is False
    assert doc_ref.updates == {}


@pytest.mark.asyncio
async def test_mutation_probe_4_inactive_active_state_guard(monkeypatch):
    doc = {"active": True, "subscription_uuid": "uuid-1"}
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_RENEW",
    )
    assert res.get("outcome") == "not_inactive"
    assert doc_ref.updates == {}


@pytest.mark.asyncio
async def test_mutation_probe_5_inactive_subtype_in_dedupe(monkeypatch):
    doc = {
        "contractor_id": "c1",
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    async def fake_lookup(uuid, include_inactive=False):
        return doc if include_inactive else None

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", lambda *a: None)

    # 1. AUTO_RENEW_DISABLED
    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000),
    )
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert doc["post_deletion_billing"]["count"] == 1

    # 2. AUTO_RENEW_ENABLED on same tx-1 (must NOT dedupe)
    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=1, signed_date=2000),
    )
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_ENABLED")) is True
    assert doc["post_deletion_billing"]["count"] == 2


@pytest.mark.asyncio
async def test_mutation_probe_6_preservation_merge(monkeypatch):
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-1",
        renewal_observation={
            "original_transaction_id": "otx-1",
            "auto_renews": False,
            "signed_at_ms": 1000,
        },
    )
    await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_RENEW",
        transaction_id="tx-2",
        purchase_date_ms=1750000000000,
        rebound_contractor_id="c2",
    )
    pdb = doc["post_deletion_billing"]
    assert pdb["count"] == 2
    assert pdb["rebound_contractor_id"] == "c2"
    assert pdb["renewal_original_transaction_id"] == "otx-1"
    assert pdb["renewal_auto_renews"] is False
    assert pdb["renewal_status_signed_at_ms"] == 1000


@pytest.mark.asyncio
async def test_mutation_probe_7_no_rebound_update_from_did_change_renewal(monkeypatch):
    inactive = {
        "contractor_id": "c1",
        "active": False,
        "subscription_uuid": "uuid-1",
        "apple_user_id": "apple-1",
        "subscription_original_transaction_id": "otx-1",
    }
    rebound = {
        "contractor_id": "c2",
        "active": True,
        "subscription_uuid": "uuid-2",
        "subscription_status": "trial",
        "subscription_auto_renews": None,
    }

    async def fake_lookup(uuid, include_inactive=False):
        return inactive if include_inactive else None

    async def fake_by_apple(apple_uid):
        return rebound if apple_uid == "apple-1" else None

    rebound_updates = []
    async def fake_update(cid, fields):
        if cid == "c2":
            rebound_updates.append(fields)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update)

    doc_ref = FakeDocRef(inactive)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000),
    )

    handled = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert handled is True
    assert len(rebound_updates) == 0


@pytest.mark.asyncio
async def test_mutation_probe_8_cross_receipt_clock_separation(monkeypatch):
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-B",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "renewal_original_transaction_id": "otx-A",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": 9000,
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-new",
        renewal_observation={
            "original_transaction_id": "otx-B",
            "auto_renews": True,
            "signed_at_ms": 1000,
        },
    )
    assert res["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["renewal_original_transaction_id"] == "otx-B"
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 1000


@pytest.mark.asyncio
async def test_mutation_probe_9_present_malformed_identity_rejected(monkeypatch):
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    tx = _tx_info(uuid="uuid-1")
    tx["productId"] = 12345  # Malformed number
    rn = _rn_info(uuid="uuid-1", product_id="com.kevin.callscreen.personal.monthly")

    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda jwt: tx if jwt == "signed-tx-jwt" else rn,
    )
    res = await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED"))
    assert res is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
async def test_mutation_probe_10_explicit_original_id_required_on_direct_binding(monkeypatch):
    claims = []
    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)

    tx_info = {
        "transactionId": "tx-12345",
        "appAccountToken": "uuid-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": (time.time() + 3600) * 1000,
    }
    res = await update_subscription_from_transaction(
        contractor_id="c1",
        transaction_info=tx_info,
    )
    assert res.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert len(claims) == 0


@pytest.mark.asyncio
async def test_mutation_probe_11_production_transaction_update_error_propagation(monkeypatch):
    inactive_doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
    }
    doc_ref = FakeDocRef(inactive_doc)

    def failing_update(ref, fields):
        raise RuntimeError("firestore write error")

    db_instance = FakeDB(doc_ref=doc_ref, on_update=failing_update)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: db_instance)

    with pytest.raises(RuntimeError, match="firestore write error"):
        await record_inactive_notification(
            contractor_id="c1",
            expected_subscription_uuid="uuid-1",
            notification_type="DID_RENEW",
            transaction_id="tx-1",
        )


# ===========================================================================
# Section 2 & 3: Restored Acceptance Test Families
# ===========================================================================


@pytest.mark.asyncio
async def test_did_change_renewal_missing_signed_transaction_info_rejects(monkeypatch):
    """Missing signedTransactionInfo rejects before lookup/claim/write."""
    lookups = []
    claims = []
    writes = []
    decode_calls = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    def fake_decode(jwt_val):
        decode_calls.append(jwt_val)
        if len(decode_calls) % 2 == 1:
            return _tx_info(uuid="uuid-1", original_id="otx-1", product_id="com.kevin.callscreen.personal.monthly")
        return _rn_info(uuid="uuid-1", original_id="otx-1", product_id="com.kevin.callscreen.personal.monthly", auto_renew_status=0, signed_date=1000)

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", fake_decode)

    payload = _renewal_payload("AUTO_RENEW_DISABLED", data={"signedRenewalInfo": "signed-rn-jwt"})
    assert await sub_service.handle_appstore_notification(payload) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0
    assert len(decode_calls) == 0


@pytest.mark.asyncio
async def test_did_change_renewal_missing_signed_renewal_info_rejects(monkeypatch):
    """Missing signedRenewalInfo rejects before lookup/claim/write."""
    lookups = []
    claims = []
    writes = []
    decode_calls = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    def fake_decode(jwt_val):
        decode_calls.append(jwt_val)
        if len(decode_calls) % 2 == 1:
            return _tx_info(uuid="uuid-1", original_id="otx-1", product_id="com.kevin.callscreen.personal.monthly")
        return _rn_info(uuid="uuid-1", original_id="otx-1", product_id="com.kevin.callscreen.personal.monthly", auto_renew_status=0, signed_date=1000)

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", fake_decode)

    payload = _renewal_payload("AUTO_RENEW_DISABLED", data={"signedTransactionInfo": "signed-tx-jwt"})
    assert await sub_service.handle_appstore_notification(payload) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0
    assert len(decode_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_val", [123, True, [], {}, "", "   "])
async def test_did_change_renewal_non_string_signed_payloads_reject(monkeypatch, bad_val):
    """Non-string, whitespace, or empty signed payloads reject before lookup/decode/claim/write."""
    lookups = []
    claims = []
    writes = []
    decode_calls = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    def fake_decode(jwt_val):
        decode_calls.append(jwt_val)
        if len(decode_calls) % 2 == 1:
            return _tx_info(uuid="uuid-1", original_id="otx-1", product_id="com.kevin.callscreen.personal.monthly")
        return _rn_info(uuid="uuid-1", original_id="otx-1", product_id="com.kevin.callscreen.personal.monthly", auto_renew_status=0, signed_date=1000)

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", fake_decode)

    # 1. Malformed signedTransactionInfo
    payload1 = _renewal_payload("AUTO_RENEW_DISABLED", data={"signedTransactionInfo": bad_val, "signedRenewalInfo": "signed-rn-jwt"})
    assert await sub_service.handle_appstore_notification(payload1) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0
    assert len(decode_calls) == 0

    # 2. Malformed signedRenewalInfo
    payload2 = _renewal_payload("AUTO_RENEW_DISABLED", data={"signedTransactionInfo": "signed-tx-jwt", "signedRenewalInfo": bad_val})
    assert await sub_service.handle_appstore_notification(payload2) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0
    assert len(decode_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_decoded", [["array"], "scalar", None, 123, True])
async def test_did_change_renewal_decoded_array_or_scalar_rejects(monkeypatch, bad_decoded):
    """Decoded transaction or renewal payload returning non-dict rejects before lookup/claim/write."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    # 1. Non-dict transaction payload
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: bad_decoded if jwt == "signed-tx-jwt" else _rn_info())
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is False
    assert len(lookups) == 0

    # 2. Non-dict renewal payload after valid transaction payload
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else bad_decoded)
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_data", [None, "string", [1, 2], 123, True, _OMITTED])
async def test_did_change_renewal_non_dict_data_rejects(monkeypatch, bad_data):
    """Missing or non-dict data field rejects before lookup."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    if bad_data is _OMITTED:
        payload = {"notificationType": "DID_CHANGE_RENEWAL_STATUS", "subtype": "AUTO_RENEW_DISABLED"}
    else:
        payload = {"notificationType": "DID_CHANGE_RENEWAL_STATUS", "subtype": "AUTO_RENEW_DISABLED", "data": bad_data}
    assert await sub_service.handle_appstore_notification(payload) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
async def test_product_id_mismatch_between_payloads_rejects(monkeypatch):
    """Disagreeing productId across payloads rejects before lookup/claim."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    tx = _tx_info(product_id="com.kevin.callscreen.personal.monthly")
    rn = _rn_info(product_id="com.kevin.callscreen.business.monthly")
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: tx if jwt == "signed-tx-jwt" else rn)

    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
async def test_environment_mismatch_between_payloads_rejects(monkeypatch):
    """Disagreeing environment across payloads rejects before lookup/claim."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    tx = _tx_info(environment="Production")
    rn = _rn_info(environment="Sandbox")
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: tx if jwt == "signed-tx-jwt" else rn)

    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cid,uuid,orig,auto,signed",
    [
        ("", "uuid-1", "otx-1", True, 1000),
        ("c1", "", "otx-1", True, 1000),
        ("c1", "uuid-1", "", True, 1000),
        ("c1", "uuid-1", "otx-1", "not-bool", 1000),
        ("c1", "uuid-1", "otx-1", True, "not-int"),
        ("c1", "uuid-1", "otx-1", True, 0),
        ("c1", "uuid-1", "otx-1", True, -100),
        ("c1", "uuid-1", "otx-1", True, True),
    ],
)
async def test_record_active_renewal_status_input_validation(monkeypatch, cid, uuid, orig, auto, signed):
    """Invalid helper arguments return False before accessing DB."""
    doc_ref = FakeDocRef({"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": "otx-1"})
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    ok = await record_active_renewal_status(
        contractor_id=cid,
        expected_subscription_uuid=uuid,
        original_transaction_id=orig,
        auto_renews=auto,
        signed_at_ms=signed,
    )
    assert ok is False
    assert doc_ref.updates == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_clock", [True, False, 1000.5, "1000", 0, -100])
async def test_record_active_renewal_status_stored_clock_validation(monkeypatch, stored_clock):
    """Malformed stored clock in active doc is safe no-op returning False."""
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "subscription_renewal_status_signed_at_ms": stored_clock,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    ok = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-1",
        auto_renews=True,
        signed_at_ms=2000,
    )
    assert ok is False
    assert doc_ref.updates == {}


@pytest.mark.asyncio
async def test_record_active_renewal_status_preserves_entitlement_and_unrelated_fields(monkeypatch):
    """Active helper update writes ONLY auto_renews and clock, preserving all other fields."""
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "subscription_status": "active",
        "subscription_tier": "business",
        "subscription_expires": 1770000000.0,
        "subscription_forwarded_from": "c0",
        "business_name": "Acme Trades",
        "custom_metadata": {"key": "value"},
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    ok = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-1",
        auto_renews=False,
        signed_at_ms=1000,
    )
    assert ok is True
    assert doc_ref.updates == {
        "subscription_auto_renews": False,
        "subscription_renewal_status_signed_at_ms": 1000,
    }
    assert doc["subscription_original_transaction_id"] == "otx-1"
    assert doc["subscription_status"] == "active"
    assert doc["subscription_tier"] == "business"
    assert doc["subscription_expires"] == 1770000000.0
    assert doc["subscription_forwarded_from"] == "c0"
    assert doc["business_name"] == "Acme Trades"
    assert doc["custom_metadata"] == {"key": "value"}


@pytest.mark.asyncio
async def test_activate_subscription_entitlement_guards(monkeypatch):
    """Entitlement helper guards active state, UUID match, finite positive expiry, and preserves same-chain renewal state."""
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-same",
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 5000,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    # 1. Inactive doc returns False with 0 updates
    doc["active"] = False
    assert await activate_subscription_entitlement("c1", "personal", 2000000000.0, "otx-same", "uuid-1") is False
    assert doc_ref.updates == {}

    # Missing active returns False
    del doc["active"]
    assert await activate_subscription_entitlement("c1", "personal", 2000000000.0, "otx-same", "uuid-1") is False
    assert doc_ref.updates == {}
    doc["active"] = True

    # 2. UUID mismatch returns False with 0 updates
    assert await activate_subscription_entitlement("c1", "personal", 2000000000.0, "otx-same", "uuid-wrong") is False
    assert doc_ref.updates == {}

    # Missing UUID returns False
    del doc["subscription_uuid"]
    assert await activate_subscription_entitlement("c1", "personal", 2000000000.0, "otx-same", "uuid-1") is False
    assert doc_ref.updates == {}
    doc["subscription_uuid"] = "uuid-1"

    # 3. Non-finite / non-positive expiry returns False with 0 updates
    assert await activate_subscription_entitlement("c1", "personal", float("inf"), "otx-same", "uuid-1") is False
    assert await activate_subscription_entitlement("c1", "personal", float("nan"), "otx-same", "uuid-1") is False
    assert await activate_subscription_entitlement("c1", "personal", 0, "otx-same", "uuid-1") is False
    assert await activate_subscription_entitlement("c1", "personal", -500, "otx-same", "uuid-1") is False
    assert await activate_subscription_entitlement("c1", "personal", "2000000000", "otx-same", "uuid-1") is False
    assert await activate_subscription_entitlement("c1", "personal", True, "otx-same", "uuid-1") is False
    assert doc_ref.updates == {}

    # 4. Same-chain activation preserves existing renewal boolean/clock
    ok_same = await activate_subscription_entitlement(
        contractor_id="c1",
        tier="personal",
        expires_ts=2000000000.0,
        original_transaction_id="otx-same",
        expected_subscription_uuid="uuid-1",
    )
    assert ok_same is True
    assert doc_ref.updates == {
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 2000000000.0,
        "subscription_original_transaction_id": "otx-same",
        "subscription_forwarded_from": None,
    }
    assert doc["subscription_auto_renews"] is True
    assert doc["subscription_renewal_status_signed_at_ms"] == 5000


@pytest.mark.asyncio
async def test_receipt_switch_prevents_stale_chain_renewal_update(monkeypatch):
    """Switching receipt from A to B resets renewal state and prevents late A events from mutating B."""
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-A",
        "subscription_auto_renews": False,
        "subscription_renewal_status_signed_at_ms": 1000,
        "subscription_forwarded_from": "c0",
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    # Activate receipt B
    ok_b = await activate_subscription_entitlement(
        contractor_id="c1",
        tier="business",
        expires_ts=2000000000.0,
        original_transaction_id="otx-B",
        expected_subscription_uuid="uuid-1",
    )
    assert ok_b is True
    assert doc_ref.updates == {
        "subscription_status": "active",
        "subscription_tier": "business",
        "subscription_expires": 2000000000.0,
        "subscription_original_transaction_id": "otx-B",
        "subscription_forwarded_from": None,
        "subscription_auto_renews": None,
        "subscription_renewal_status_signed_at_ms": None,
    }
    assert doc["subscription_original_transaction_id"] == "otx-B"
    assert doc["subscription_auto_renews"] is None
    assert doc["subscription_renewal_status_signed_at_ms"] is None
    assert doc["subscription_forwarded_from"] is None

    # Late renewal event for receipt A arrives -> REJECT
    ok_a_late = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-A",
        auto_renews=True,
        signed_at_ms=5000,
    )
    assert ok_a_late is False
    assert doc["subscription_auto_renews"] is None

    # Renewal event for receipt B arrives -> ACCEPT
    ok_b_event = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-B",
        auto_renews=True,
        signed_at_ms=6000,
    )
    assert ok_b_event is True
    assert doc["subscription_auto_renews"] is True
    assert doc["subscription_renewal_status_signed_at_ms"] == 6000


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_purchase_date", [None, "invalid", True, float("nan"), float("inf"), 0, -100])
async def test_record_inactive_malformed_purchase_date_preserves_conservative_charge(
    monkeypatch, bad_purchase_date
):
    """Malformed or non-finite purchase dates on charge events stay conservative."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "deactivated_at": 1700000000,
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_RENEW",
        transaction_id="tx-1",
        purchase_date_ms=bad_purchase_date,
    )
    assert res["outcome"] == "recorded"
    assert res["charged_after_deletion"] is True
    assert res["charges"] == 1


@pytest.mark.asyncio
async def test_inactive_duplicate_monotonic_clock_advance_regression_sequence(monkeypatch):
    """Duplicate tuple sequence: disabled false@1000, duplicate disabled false@3000, then distinct enabled true@2000 without clock rollback."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    # Event 1: First delivery @ 1000: AUTO_RENEW_DISABLED, False
    r1 = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-1",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": False, "signed_at_ms": 1000},
    )
    assert r1["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 1
    assert doc["post_deletion_billing"]["charges"] == 0
    assert doc["post_deletion_billing"]["last_type"] == "DID_CHANGE_RENEWAL_STATUS"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 1000

    # Event 2: Exact same tuple/subtype and False, signed 3000 -> renewal_update
    r2 = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-1",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": False, "signed_at_ms": 3000},
    )
    assert r2["outcome"] == "renewal_update"
    assert doc["post_deletion_billing"]["count"] == 1
    assert doc["post_deletion_billing"]["charges"] == 0
    assert doc["post_deletion_billing"]["last_type"] == "DID_CHANGE_RENEWAL_STATUS"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 3000

    # Event 3: Same transaction ID but distinct AUTO_RENEW_ENABLED, True, signed 2000 -> distinct generic audit record increments count to 2, but clock not rolled backward from 3000
    r3 = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-1",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": 2000},
    )
    assert r3["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 2
    assert doc["post_deletion_billing"]["charges"] == 0
    assert doc["post_deletion_billing"]["last_type"] == "DID_CHANGE_RENEWAL_STATUS"
    assert doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 3000


@pytest.mark.asyncio
async def test_inactive_renewal_observation_receipt_and_clock_guards(monkeypatch):
    """Receipt mismatch and malformed prior clock do not update renewal evidence while recording audit."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "renewal_original_transaction_id": "otx-1",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": 5000,
        },
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_ENABLED",
        transaction_id="tx-other-receipt",
        renewal_observation={"original_transaction_id": "otx-unknown", "auto_renews": True, "signed_at_ms": 9999},
    )
    assert res["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 2
    assert doc["post_deletion_billing"]["renewal_original_transaction_id"] == "otx-1"
    assert doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 5000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_obs",
    [
        {"original_transaction_id": 123, "auto_renews": True, "signed_at_ms": 1000},
        {"original_transaction_id": "", "auto_renews": True, "signed_at_ms": 1000},
        {"original_transaction_id": "otx-1", "auto_renews": "yes", "signed_at_ms": 1000},
        {"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": "1000"},
        {"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": -100},
        {"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": True},
    ],
)
async def test_record_inactive_strictly_validates_renewal_observation_at_db_seam(monkeypatch, bad_obs):
    """Malformed renewal_observation dicts at DB seam are safely ignored while recording audit."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    res = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-1",
        renewal_observation=bad_obs,
    )
    assert res["outcome"] == "recorded"
    assert doc["post_deletion_billing"]["count"] == 1
    assert "renewal_original_transaction_id" not in doc["post_deletion_billing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_subtype,bad_status",
    [
        (None, 0),
        ("", 0),
        ("INVALID_SUBTYPE", 0),
        (123, 0),
        ("AUTO_RENEW_DISABLED", None),
        ("AUTO_RENEW_DISABLED", True),
        ("AUTO_RENEW_DISABLED", False),
        ("AUTO_RENEW_DISABLED", "0"),
        ("AUTO_RENEW_DISABLED", "1"),
        ("AUTO_RENEW_DISABLED", 0.0),
        ("AUTO_RENEW_DISABLED", 1.0),
        ("AUTO_RENEW_DISABLED", []),
        ("AUTO_RENEW_DISABLED", {}),
        ("AUTO_RENEW_DISABLED", -1),
        ("AUTO_RENEW_DISABLED", 2),
        ("AUTO_RENEW_DISABLED", 99),
        ("AUTO_RENEW_DISABLED", 1),  # disagreement
        ("AUTO_RENEW_ENABLED", 0),   # disagreement
        ("AUTO_RENEW_DISABLED", _OMITTED),  # autoRenewStatus key truly absent
    ],
)
async def test_subtype_status_and_type_guards_reject_without_mutation(monkeypatch, bad_subtype, bad_status):
    """Invalid subtype, autoRenewStatus, absent autoRenewStatus, or subtype/status disagreement rejects before lookup/claim/write."""
    lookups = []
    claims = []
    writes = []

    async def fake_lookup(uuid, include_inactive=False):
        lookups.append((uuid, include_inactive))
        return {"contractor_id": "c1", "active": True, "subscription_uuid": "uuid-1"}

    async def fake_claim(**kwargs):
        claims.append(kwargs)
        return True, None

    async def fake_write(**kwargs):
        writes.append(kwargs)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr("app.db.contractors.record_active_renewal_status", fake_write)

    tx = _tx_info()
    rn = _rn_info()
    if bad_status is _OMITTED:
        rn.pop("autoRenewStatus", None)
        assert "autoRenewStatus" not in rn
    else:
        rn["autoRenewStatus"] = bad_status
        assert "autoRenewStatus" in rn
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: tx if jwt == "signed-tx-jwt" else rn)

    payload = {"notificationType": "DID_CHANGE_RENEWAL_STATUS", "subtype": bad_subtype, "data": {"signedTransactionInfo": "signed-tx-jwt", "signedRenewalInfo": "signed-rn-jwt"}}
    assert await sub_service.handle_appstore_notification(payload) is False
    assert len(lookups) == 0
    assert len(claims) == 0
    assert len(writes) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc_state",
    [
        # 1. document active key absent
        {"subscription_uuid": "uuid-1", "subscription_original_transaction_id": "otx-1"},
        # 2. document active false
        {"active": False, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": "otx-1"},
        # 3. stored subscription_uuid key absent
        {"active": True, "subscription_original_transaction_id": "otx-1"},
        # 4. stored UUID mismatched
        {"active": True, "subscription_uuid": "uuid-wrong", "subscription_original_transaction_id": "otx-1"},
        # 5. current original key absent
        {"active": True, "subscription_uuid": "uuid-1"},
        # 6. current original exactly None
        {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": None},
        # 7. current original malformed non-string (int)
        {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": 12345},
        # 7b. current original malformed non-string (container)
        {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": ["otx-1"]},
        # 8. current original empty/whitespace string
        {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": ""},
        {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": "   "},
        # 9. current original valid-string mismatch
        {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": "otx-mismatched"},
    ],
)
async def test_current_receipt_binding_missing_or_mismatch_remains_unknown(monkeypatch, doc_state):
    """Active doc missing/mismatched/malformed attributes rejects renewal update as safe no-op."""
    doc = dict(doc_state)
    original_snapshot = dict(doc_state)
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    ok = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        original_transaction_id="otx-1",
        auto_renews=False,
        signed_at_ms=1000,
    )
    assert ok is False
    assert doc_ref.updates == {}
    assert doc == original_snapshot


@pytest.mark.asyncio
async def test_equal_or_older_events_are_no_ops_and_newer_wins(monkeypatch):
    """Equal/older clock returns False, strictly newer returns True."""
    doc = {
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "subscription_renewal_status_signed_at_ms": 5000,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    assert await record_active_renewal_status("c1", "uuid-1", "otx-1", True, 5000) is False
    assert await record_active_renewal_status("c1", "uuid-1", "otx-1", True, 4000) is False
    assert await record_active_renewal_status("c1", "uuid-1", "otx-1", True, 6000) is True


@pytest.mark.asyncio
async def test_active_and_inactive_helper_guards(monkeypatch):
    """Active and inactive helpers strictly guard input parameters, state, and monotonicity."""
    # Active helper input guards
    assert await record_active_renewal_status("", "uuid-1", "otx-1", True, 1000) is False
    assert await record_active_renewal_status("c1", "", "otx-1", True, 1000) is False
    assert await record_active_renewal_status("c1", "uuid-1", "", True, 1000) is False
    assert await record_active_renewal_status("c1", "uuid-1", "otx-1", "not-bool", 1000) is False
    assert await record_active_renewal_status("c1", "uuid-1", "otx-1", True, "not-int") is False

    # Inactive helper input guards
    assert (await record_inactive_notification("", "uuid-1", "DID_RENEW"))["outcome"] == "invalid_input"
    assert (await record_inactive_notification("c1", "", "DID_RENEW"))["outcome"] == "invalid_input"
    assert (await record_inactive_notification("c1", "uuid-1", ""))["outcome"] == "invalid_input"

    # Inactive helper state guards: active doc -> not_inactive
    active_doc = {"active": True, "subscription_uuid": "uuid-1", "subscription_original_transaction_id": "otx-1"}
    doc_ref_act = FakeDocRef(active_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_act))
    res_act = await record_inactive_notification("c1", "uuid-1", "DID_RENEW")
    assert res_act["outcome"] == "not_inactive"
    assert doc_ref_act.updates == {}

    # Inactive helper state guards: missing active -> not_inactive
    no_act_doc = {"subscription_uuid": "uuid-1"}
    doc_ref_no_act = FakeDocRef(no_act_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_no_act))
    res_no_act = await record_inactive_notification("c1", "uuid-1", "DID_RENEW")
    assert res_no_act["outcome"] == "not_inactive"

    # Inactive helper state guards: uuid mismatch -> uuid_mismatch
    inact_doc = {"active": False, "subscription_uuid": "uuid-1"}
    doc_ref_inact = FakeDocRef(inact_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_inact))
    res_mismatch = await record_inactive_notification("c1", "uuid-wrong", "DID_RENEW")
    assert res_mismatch["outcome"] == "uuid_mismatch"
    assert doc_ref_inact.updates == {}

    # Inactive helper state guards: missing stored uuid -> uuid_mismatch
    no_uuid_doc = {"active": False}
    doc_ref_no_uuid = FakeDocRef(no_uuid_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_no_uuid))
    res_no_uuid = await record_inactive_notification("c1", "uuid-1", "DID_RENEW")
    assert res_no_uuid["outcome"] == "uuid_mismatch"
    assert doc_ref_no_uuid.updates == {}

    # Inactive same-receipt equal and older clock are no-op for renewal truth; strictly newer accepted
    seeded_doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": {
            "count": 1,
            "charges": 0,
            "last_transaction_id": "tx-dup",
            "last_type": "DID_CHANGE_RENEWAL_STATUS",
            "last_subtype": "AUTO_RENEW_DISABLED",
            "renewal_original_transaction_id": "otx-1",
            "renewal_auto_renews": False,
            "renewal_status_signed_at_ms": 5000,
        },
    }
    doc_ref_seed = FakeDocRef(seeded_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_seed))

    # Equal clock @ 5000 on duplicate tuple -> duplicate, no change
    res_eq = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-dup",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": 5000},
    )
    assert res_eq["outcome"] == "duplicate"
    assert seeded_doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert seeded_doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 5000

    # Older clock @ 4000 on duplicate tuple -> duplicate, no change
    res_old = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-dup",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": 4000},
    )
    assert res_old["outcome"] == "duplicate"
    assert seeded_doc["post_deletion_billing"]["renewal_auto_renews"] is False
    assert seeded_doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 5000

    # Strictly newer clock @ 6000 on duplicate tuple -> renewal_update
    res_new = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-dup",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": True, "signed_at_ms": 6000},
    )
    assert res_new["outcome"] == "renewal_update"
    assert seeded_doc["post_deletion_billing"]["renewal_auto_renews"] is True
    assert seeded_doc["post_deletion_billing"]["renewal_status_signed_at_ms"] == 6000


@pytest.mark.asyncio
async def test_inactive_subtype_sensitive_dedupe(monkeypatch):
    """Same transaction ID with differing subtype is not deduped."""
    doc = {
        "contractor_id": "c1",
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    async def fake_lookup(uuid, include_inactive=False):
        return doc if include_inactive else None

    async def fake_apple_lookup(apple_uid):
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_apple_lookup)

    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000))
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert doc["post_deletion_billing"]["count"] == 1

    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=1, signed_date=2000))
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_ENABLED")) is True
    assert doc["post_deletion_billing"]["count"] == 2


@pytest.mark.asyncio
async def test_inactive_sequential_events_preserve_mixed_evidence(monkeypatch):
    """Sequential mixed events preserve all nested post_deletion_billing fields in both directions."""
    doc = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref = FakeDocRef(doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))

    # Direction 1: Renewal -> Generic -> Expired
    await record_inactive_notification("c1", "uuid-1", "DID_CHANGE_RENEWAL_STATUS", "AUTO_RENEW_DISABLED", "tx-1", renewal_observation={"original_transaction_id": "otx-1", "auto_renews": False, "signed_at_ms": 1000})
    await record_inactive_notification("c1", "uuid-1", "DID_RENEW", None, "tx-2", purchase_date_ms=1750000000000, rebound_contractor_id="c2")
    await record_inactive_notification("c1", "uuid-1", "EXPIRED", None, "tx-3")

    pdb = doc["post_deletion_billing"]
    assert pdb["count"] == 3
    assert pdb["charges"] == 1
    assert pdb["last_type"] == "EXPIRED"
    assert pdb["rebound_contractor_id"] == "c2"
    assert pdb["renewal_original_transaction_id"] == "otx-1"
    assert pdb["renewal_auto_renews"] is False
    assert pdb["renewal_status_signed_at_ms"] == 1000

    # Direction 2: Generic -> Renewal on fresh doc
    doc2 = {
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": {
            "count": 2,
            "charges": 1,
            "last_type": "DID_RENEW",
            "rebound_contractor_id": "c2",
            "unrelated_sentinel": "keep_me",
        },
    }
    doc_ref2 = FakeDocRef(doc2)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref2))

    res_r = await record_inactive_notification(
        contractor_id="c1",
        expected_subscription_uuid="uuid-1",
        notification_type="DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        transaction_id="tx-4",
        renewal_observation={"original_transaction_id": "otx-1", "auto_renews": False, "signed_at_ms": 2500},
    )
    assert res_r["outcome"] == "recorded"
    pdb2 = doc2["post_deletion_billing"]
    assert pdb2["count"] == 3
    assert pdb2["charges"] == 1
    assert pdb2["last_type"] == "DID_CHANGE_RENEWAL_STATUS"
    assert pdb2["rebound_contractor_id"] == "c2"
    assert pdb2["unrelated_sentinel"] == "keep_me"
    assert pdb2["renewal_original_transaction_id"] == "otx-1"
    assert pdb2["renewal_auto_renews"] is False
    assert pdb2["renewal_status_signed_at_ms"] == 2500


@pytest.mark.asyncio
async def test_inactive_source_never_projects_renewal_status_to_rebound(monkeypatch):
    """DID_CHANGE_RENEWAL_STATUS on inactive user never mutates rebound contractor."""
    inactive = {"contractor_id": "c1", "active": False, "subscription_uuid": "uuid-1", "apple_user_id": "apple-1"}
    rebound = {"contractor_id": "c2", "active": True, "subscription_uuid": "uuid-2"}
    rebound_updates = []

    async def fake_lookup(uuid, include_inactive=False):
        return inactive if include_inactive else None

    async def fake_apple_lookup(apple_uid):
        return rebound if apple_uid == "apple-1" else None

    async def fake_update(cid, fields):
        rebound_updates.append((cid, fields))
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_apple_lookup)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update)
    doc_ref = FakeDocRef(inactive)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref))
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info())

    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert len(rebound_updates) == 0


@pytest.mark.asyncio
async def test_service_outcomes_and_failure_boundaries(monkeypatch, caplog):
    """Exercise real handler distinguishing active/inactive outcomes, logging classifications, and boundaries."""
    import logging
    caplog.set_level(logging.INFO)

    # 1. Active recorded
    active_doc = {
        "contractor_id": "c1",
        "active": True,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
    }
    doc_ref_act = FakeDocRef(active_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_act))

    async def fake_lookup_act(uuid, include_inactive=False):
        return active_doc

    async def fake_claim(**kwargs):
        return True, "c1"

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup_act)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000))

    caplog.clear()
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert "Active renewal status recorded" in caplog.text

    # 2. Active stale/no-op (older clock)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info() if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=1, signed_date=500))
    caplog.clear()
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_ENABLED")) is True
    assert "Active renewal status no-op (stale/mismatched)" in caplog.text

    # 3. Inactive recorded
    inactive_doc = {
        "contractor_id": "c1",
        "active": False,
        "subscription_uuid": "uuid-1",
        "subscription_original_transaction_id": "otx-1",
        "post_deletion_billing": None,
    }
    doc_ref_inact = FakeDocRef(inactive_doc)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: FakeDB(doc_ref_inact))

    async def fake_lookup_inact(uuid, include_inactive=False):
        return inactive_doc if include_inactive else None

    async def fake_apple_lookup(apple_uid):
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup_inact)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_apple_lookup)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info(transaction_id="tx-1") if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000))

    caplog.clear()
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert "Post-deletion App Store notification recorded" in caplog.text

    # 4. Inactive renewal_update (newer clock on same duplicate tuple)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info(transaction_id="tx-1") if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=2000))
    caplog.clear()
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert "Post-deletion renewal clock advanced" in caplog.text

    # 5. Inactive duplicate (older clock on same duplicate tuple)
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info(transaction_id="tx-1") if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1500))
    caplog.clear()
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is True
    assert "Post-deletion notification redelivery ignored" in caplog.text

    # 6. Service outcome mapping: inactive helper results for not_inactive, uuid_mismatch, invalid_input
    for mocked_outcome in ("not_inactive", "uuid_mismatch", "invalid_input"):
        inactive_helper_calls = []

        async def fake_record_inactive(**kwargs):
            inactive_helper_calls.append(kwargs)
            return {"outcome": mocked_outcome}

        monkeypatch.setattr("app.db.contractors.record_inactive_notification", fake_record_inactive)
        monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup_inact)
        monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda jwt: _tx_info(transaction_id="tx-1") if jwt == "signed-tx-jwt" else _rn_info(auto_renew_status=0, signed_date=1000))

        caplog.clear()
        assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is False
        assert len(inactive_helper_calls) == 1
        assert f"Post-deletion notification write skipped due to state mismatch ({mocked_outcome})" in caplog.text
        assert "Post-deletion App Store notification recorded" not in caplog.text
        assert "Post-deletion renewal clock advanced" not in caplog.text
        assert "Post-deletion notification redelivery ignored" not in caplog.text

    # 7. Contractor not found -> returns False
    async def fake_lookup_none(uuid, include_inactive=False):
        return None
    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup_none)
    caplog.clear()
    assert await sub_service.handle_appstore_notification(_renewal_payload("AUTO_RENEW_DISABLED")) is False
