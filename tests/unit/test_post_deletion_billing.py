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
    calls = {"updates": [], "lookups": [], "claims": []}

    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda _s: _transaction_info())

    async def fake_claim(**kwargs):
        calls["claims"].append(kwargs)
        return True, kwargs["contractor_id"]

    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim)

    async def fake_update(cid, fields):
        calls["updates"].append((cid, fields))
        return True

    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update)
    return calls


def _wire_lookup(monkeypatch, calls, active_doc=None, inactive_doc=None):
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
