"""Reconciliation for App Store notifications that hit inactive accounts.

Before this change, a notification for a deactivated (deleted) account missed
the active==True lookup, logged a warning, and was acked 200 — silently
dropped. An ex-customer whose subscription kept auto-renewing paid for a dead
account and nothing ever surfaced it. Now:

- the handler falls back to an unfiltered lookup; a hit on an inactive
  account records `post_deletion_billing` on the contractor doc (count,
  last_type, last_at) without touching any subscription field,
- renewal-class notifications (money actually charged) log at ERROR,
- the phase0 account audit counts the field so reconciliation has a report,
- `post_deletion_billing` is a PROTECTED_FIELD.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import importlib.util
from pathlib import Path

import pytest

from app.db.contractors import PROTECTED_FIELDS
from app.services import subscription as sub_service


def _payload(notification_type="DID_RENEW"):
    return {
        "notificationType": notification_type,
        "data": {"signedTransactionInfo": "signed-jwt"},
    }


def _transaction_info():
    return {
        "appAccountToken": "uuid-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "transactionId": "tx-1",
        "originalTransactionId": "otx-1",
        "environment": "Production",
        "expiresDate": 4102444800000,  # 2100-01-01, ms
    }


@pytest.fixture
def wired(monkeypatch):
    """Wire the handler's seams; returns dicts capturing calls."""
    calls = {
        "updates": [],
        "lookups": [],
        "claims": [],
        "inactive_docs": {},
    }

    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda _s: _transaction_info())

    async def fake_claim(**kwargs):
        calls["claims"].append(kwargs)
        return True, kwargs["contractor_id"]

    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)

    async def fake_update(cid, fields):
        calls["updates"].append((cid, fields))
        return True

    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update)

    class StatefulFakeDoc:
        def __init__(self, data_dict):
            self._data = data_dict

        @property
        def exists(self):
            return self._data is not None

        def to_dict(self):
            return dict(self._data) if self._data else {}

    class StatefulFakeDB:
        def __init__(self, store):
            self.store = store

        def collection(self, _name):
            return self

        def document(self, doc_id):
            return StatefulDocRef(self.store, doc_id)

        def transaction(self):
            return StatefulTransaction(self.store)

    class StatefulDocRef:
        def __init__(self, store, doc_id):
            self.store = store
            self.doc_id = doc_id

        def get(self, transaction=None):
            return StatefulFakeDoc(self.store.get(self.doc_id))

    class StatefulTransaction:
        def __init__(self, store):
            self.store = store

        def update(self, doc_ref, fields):
            if doc_ref.doc_id in self.store:
                self.store[doc_ref.doc_id].update(fields)
                calls["updates"].append((doc_ref.doc_id, fields))

    monkeypatch.setattr("google.cloud.firestore.transactional", lambda fn: fn)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: StatefulFakeDB(calls["inactive_docs"]))
    return calls


def _wire_lookup(monkeypatch, calls, active_doc=None, inactive_doc=None):
    if inactive_doc and "contractor_id" in inactive_doc:
        calls["inactive_docs"][inactive_doc["contractor_id"]] = dict(inactive_doc)
        if "subscription_uuid" not in calls["inactive_docs"][inactive_doc["contractor_id"]]:
            calls["inactive_docs"][inactive_doc["contractor_id"]]["subscription_uuid"] = "uuid-1"

    async def fake_lookup(subscription_uuid, include_inactive=False):
        calls["lookups"].append((subscription_uuid, include_inactive))
        if not include_inactive:
            return active_doc
        return active_doc or inactive_doc

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup
    )


@pytest.mark.asyncio
async def test_renewal_for_inactive_account_records_reconciliation(monkeypatch, wired):
    inactive = {"contractor_id": "c1", "active": False, "deactivated_at": 1755600000}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    assert len(wired["updates"]) == 1
    cid, fields = wired["updates"][0]
    assert cid == "c1"
    rec = fields["post_deletion_billing"]
    assert rec["count"] == 1
    assert rec["last_type"] == "DID_RENEW"
    assert rec["last_at"]
    # No entitlement mutation for a dead account:
    assert "subscription_status" not in fields
    assert "subscription_tier" not in fields


@pytest.mark.asyncio
async def test_reconciliation_count_increments(monkeypatch, wired):
    inactive = {
        "contractor_id": "c1",
        "active": False,
        "post_deletion_billing": {"count": 2, "last_type": "DID_RENEW", "last_at": 1},
    }
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["count"] == 3


@pytest.mark.asyncio
async def test_winddown_notification_also_recorded(monkeypatch, wired):
    """EXPIRED/REFUND on a dead account is benign wind-down but still recorded
    so the audit trail is complete."""
    inactive = {"contractor_id": "c1", "active": False}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)

    handled = await sub_service.handle_appstore_notification(_payload("EXPIRED"))

    assert handled is True
    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["last_type"] == "EXPIRED"


@pytest.mark.asyncio
async def test_unknown_token_still_returns_false(monkeypatch, wired):
    _wire_lookup(monkeypatch, wired)  # both lookups miss

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is False
    assert wired["updates"] == []


@pytest.mark.asyncio
async def test_active_contractor_renewal_path_unchanged(monkeypatch, wired):
    active = {"contractor_id": "c1", "active": True}
    _wire_lookup(monkeypatch, wired, active_doc=active)

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    _cid, fields = wired["updates"][0]
    assert fields["subscription_status"] == "active"
    assert fields["subscription_tier"] == "personal"
    assert "post_deletion_billing" not in fields


def test_post_deletion_billing_is_protected():
    assert "post_deletion_billing" in PROTECTED_FIELDS


def test_account_audit_counts_post_deletion_billing():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "phase0_account_audit_pdb", root / "scripts" / "phase0_account_audit.py"
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)

    summary = audit.summarize_contractors([
        {"post_deletion_billing": {"count": 3, "last_type": "DID_RENEW"}},
        {"post_deletion_billing": {"count": 1, "last_type": "EXPIRED"}},
        {"subscription_status": "active"},
    ])
    assert summary["post_deletion_billing_types"] == {"DID_RENEW": 1, "EXPIRED": 1}


# ---------------------------------------------------------------------------
# Review-round fixes (PR #193 findings)
# ---------------------------------------------------------------------------


def _wire_rebound(monkeypatch, calls, rebound_doc=None):
    async def fake_by_apple(apple_user_id):
        calls.setdefault("apple_lookups", []).append(apple_user_id)
        return rebound_doc

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )


@pytest.mark.asyncio
async def test_fallback_requires_explicit_inactive(monkeypatch, wired):
    """A doc without active=False (legacy doc missing the field, or a future
    reactivation) is NOT a deleted account: fall through to not-found so
    nothing is recorded and no paying customer's renewal is swallowed."""
    no_active_field = {"contractor_id": "c1"}  # active missing entirely
    _wire_lookup(monkeypatch, wired, inactive_doc=no_active_field)

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is False
    assert wired["updates"] == []


@pytest.mark.asyncio
async def test_redelivered_transaction_not_double_counted(monkeypatch, wired):
    """Apple redelivers on timeout; the same transactionId must not inflate
    the charge count an operator refunds against."""
    inactive = {
        "contractor_id": "c1",
        "active": False,
        "post_deletion_billing": {
            "count": 1, "last_type": "DID_RENEW", "last_at": 1,
            "last_transaction_id": "tx-1",
        },
    }
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    assert wired["updates"] == []


@pytest.mark.asyncio
async def test_record_carries_transaction_id(monkeypatch, wired):
    inactive = {"contractor_id": "c1", "active": False}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["last_transaction_id"] == "tx-1"


@pytest.mark.asyncio
async def test_poisoned_count_recovers_instead_of_raising(monkeypatch, wired):
    """A corrupted count (bad manual write) must not permanently dead-letter
    the account behind the webhook's 200-acking catch-all."""
    inactive = {
        "contractor_id": "c1",
        "active": False,
        "post_deletion_billing": {"count": "n/a", "last_type": "DID_RENEW", "last_at": 1},
    }
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["count"] == 1


@pytest.mark.asyncio
async def test_non_dict_record_tolerated(monkeypatch, wired):
    inactive = {"contractor_id": "c1", "active": False, "post_deletion_billing": True}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["count"] == 1


@pytest.mark.asyncio
async def test_write_failure_propagates_for_webhook_500(monkeypatch, wired):
    """Storage failures during inactive notification write must raise so the webhook
    returns HTTP 500 and Apple retries."""
    inactive = {"contractor_id": "c1", "active": False}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    async def failing_record(*args, **kwargs):
        raise RuntimeError("firestore blip")

    monkeypatch.setattr("app.db.contractors.record_inactive_notification", failing_record)

    with pytest.raises(RuntimeError, match="firestore blip"):
        await sub_service.handle_appstore_notification(_payload("DID_RENEW"))


@pytest.mark.asyncio
async def test_rebound_customer_recorded_without_alarm(monkeypatch, wired, caplog):
    """A customer who deleted and re-signed up keeps their StoreKit
    subscription bound to the OLD subscription_uuid forever. Their renewals
    are not post-deletion charges — record them with the rebound pointer and
    log INFO, or the operator chases refunds for an actively paying customer
    every month."""
    import logging

    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired, rebound_doc={"contractor_id": "c2", "active": True})

    with caplog.at_level(logging.INFO, logger="app.services.subscription"):
        handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    updates = dict((cid, fields) for cid, fields in wired["updates"])
    assert updates["c1"]["post_deletion_billing"]["rebound_contractor_id"] == "c2"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# Sweep-round fixes (PR #193)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_referencing_same_transaction_is_recorded(monkeypatch, wired):
    """Apple's REFUND carries the SAME transactionId as the renewal it refunds.
    Deduping on transactionId alone would swallow it and leave the audit
    claiming an outstanding charge Apple already refunded."""
    inactive = {
        "contractor_id": "c1",
        "active": False,
        "post_deletion_billing": {
            "count": 1, "last_type": "DID_RENEW", "last_at": 1,
            "last_transaction_id": "tx-1",
        },
    }
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    handled = await sub_service.handle_appstore_notification(_payload("REFUND"))

    assert handled is True
    assert len(wired["updates"]) == 1
    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["last_type"] == "REFUND"
    assert fields["post_deletion_billing"]["count"] == 2


@pytest.mark.asyncio
async def test_rebound_lookup_failure_does_not_lose_the_record(monkeypatch, wired):
    """A Firestore blip in the rebound lookup must not skip the log and write
    behind the webhook's 200-acking catch-all."""
    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)

    async def raising_by_apple(apple_user_id):
        raise RuntimeError("firestore blip")

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", raising_by_apple
    )

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    assert len(wired["updates"]) == 1
    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["count"] == 1
    assert "rebound_contractor_id" not in fields["post_deletion_billing"]


# ---------------------------------------------------------------------------
# Review-thread fixes (PR #193)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebound_entitlement_forwarded_when_new_account_not_active(monkeypatch, wired):
    """The verify path rejects the old receipt for the new account
    (OWNERSHIP_MISMATCH on the uuid check), so the webhook is the ONLY place
    a rebound customer's renewal can reach their live account. Forward the
    entitlement when their new account isn't already active."""
    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(
        monkeypatch, wired,
        rebound_doc={"contractor_id": "c2", "active": True, "subscription_status": "trial"},
    )

    handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    updates = dict((cid, fields) for cid, fields in wired["updates"])
    assert "c1" in updates and "post_deletion_billing" in updates["c1"]
    assert updates["c2"]["subscription_status"] == "active"
    assert updates["c2"]["subscription_tier"] == "personal"
    assert updates["c2"]["subscription_expires"]


@pytest.mark.asyncio
async def test_rebound_entitlement_not_stomped_when_new_account_active(monkeypatch, wired):
    """If the new account already has its own active subscription, forwarding
    the old sub's fields would stomp it — record only."""
    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(
        monkeypatch, wired,
        rebound_doc={"contractor_id": "c2", "active": True, "subscription_status": "active"},
    )

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    cids = [cid for cid, _f in wired["updates"]]
    assert cids == ["c1"]


@pytest.mark.asyncio
async def test_charge_evidence_survives_winddown(monkeypatch, wired):
    """The audit classifies by the record; a wind-down EXPIRED after a
    DID_RENEW must not erase the fact a charge happened."""
    inactive = {
        "contractor_id": "c1",
        "active": False,
        "post_deletion_billing": {
            "count": 1, "charges": 1, "last_type": "DID_RENEW", "last_at": 1,
            "last_transaction_id": "tx-0",
        },
    }
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    await sub_service.handle_appstore_notification(_payload("EXPIRED"))

    _cid, fields = wired["updates"][0]
    rec = fields["post_deletion_billing"]
    assert rec["last_type"] == "EXPIRED"
    assert rec["charges"] == 1, "charge evidence must survive wind-down"
    assert rec["count"] == 2


def test_account_audit_counts_charged_accounts():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "phase0_account_audit_charged", root / "scripts" / "phase0_account_audit.py"
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)

    summary = audit.summarize_contractors([
        {"post_deletion_billing": {"count": 3, "charges": 2, "last_type": "EXPIRED"}},
        {"post_deletion_billing": {"count": 1, "charges": 0, "last_type": "EXPIRED"}},
        {"subscription_status": "active"},
    ])
    assert summary["post_deletion_charged_accounts"] == 1


@pytest.mark.asyncio
async def test_pre_deactivation_renewal_is_not_a_post_deletion_charge(
    monkeypatch, wired, caplog
):
    """A renewal PURCHASED while the account was still active (delivered late
    or redelivered after deactivation) is a valid charge — flagging it would
    send the operator refund-chasing a legitimate payment."""
    import logging

    inactive = {"contractor_id": "c1", "active": False, "deactivated_at": 1755600000}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)
    monkeypatch.setattr(
        sub_service, "_decode_jws_payload",
        lambda _s: {**_transaction_info(), "purchaseDate": 1755599000 * 1000},
    )

    with caplog.at_level(logging.INFO, logger="app.services.subscription"):
        handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is True
    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["charges"] == 0
    assert fields["post_deletion_billing"]["count"] == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_post_deactivation_renewal_is_a_charge(monkeypatch, wired):
    inactive = {"contractor_id": "c1", "active": False, "deactivated_at": 1755600000}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)
    monkeypatch.setattr(
        sub_service, "_decode_jws_payload",
        lambda _s: {**_transaction_info(), "purchaseDate": 1755601000 * 1000},
    )

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["charges"] == 1


@pytest.mark.asyncio
async def test_missing_purchase_date_stays_conservative(monkeypatch, wired):
    """Without a purchaseDate we cannot prove the charge predates deactivation
    — count it, so a real post-deletion charge is never silently excused."""
    inactive = {"contractor_id": "c1", "active": False, "deactivated_at": 1755600000}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(monkeypatch, wired)

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    _cid, fields = wired["updates"][0]
    assert fields["post_deletion_billing"]["charges"] == 1


@pytest.mark.asyncio
async def test_terminal_event_expires_forwarded_entitlement(monkeypatch, wired):
    """If the rebound account's entitlement CAME from forwarding (provenance
    marker), a terminal event for the old subscription must end it — or the
    rebound account stays 'active' after Apple ended the sub."""
    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(
        monkeypatch, wired,
        rebound_doc={
            "contractor_id": "c2", "active": True,
            "subscription_status": "active",
            "subscription_forwarded_from": "c1",
        },
    )

    await sub_service.handle_appstore_notification(_payload("EXPIRED"))

    updates = dict((cid, fields) for cid, fields in wired["updates"])
    assert updates["c2"]["subscription_status"] == "expired"


@pytest.mark.asyncio
async def test_terminal_event_never_touches_independent_subscription(monkeypatch, wired):
    """A rebound account whose subscription was NOT forwarded (they bought
    their own) must never be expired by the old sub's wind-down."""
    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(
        monkeypatch, wired,
        rebound_doc={"contractor_id": "c2", "active": True, "subscription_status": "active"},
    )

    await sub_service.handle_appstore_notification(_payload("EXPIRED"))

    cids = [cid for cid, _f in wired["updates"]]
    assert cids == ["c1"]


@pytest.mark.asyncio
async def test_forwarding_stamps_provenance(monkeypatch, wired):
    inactive = {"contractor_id": "c1", "active": False, "apple_user_id": "apple-1"}
    _wire_lookup(monkeypatch, wired, inactive_doc=inactive)
    _wire_rebound(
        monkeypatch, wired,
        rebound_doc={"contractor_id": "c2", "active": True, "subscription_status": "trial"},
    )

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    updates = dict((cid, fields) for cid, fields in wired["updates"])
    assert updates["c2"]["subscription_forwarded_from"] == "c1"


def test_account_audit_reports_rebound_separately():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "phase0_account_audit_rebound", root / "scripts" / "phase0_account_audit.py"
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)

    summary = audit.summarize_contractors([
        {"post_deletion_billing": {"charges": 2, "last_type": "DID_RENEW"}},
        {"post_deletion_billing": {"charges": 3, "last_type": "DID_RENEW",
                                   "rebound_contractor_id": "c9"}},
    ])
    # rebound renewals are legitimate charges for an active customer — they
    # must not send operators refund-chasing.
    assert summary["post_deletion_charged_accounts"] == 1
    assert summary["post_deletion_rebound_accounts"] == 1
