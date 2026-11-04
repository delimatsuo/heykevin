"""A failed renewal keeps service on, and an upgrade made in Settings reaches the server.

DID_FAIL_TO_RENEW used to mark the account expired and say the subscription
had ended, while Apple was still retrying the payment. DID_CHANGE_RENEWAL_PREF
was ignored, so an upgrade stayed unknown until the app verified or the plan
renewed.
"""

from __future__ import annotations

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest  # noqa: E402

from app.services import subscription as sub_service  # noqa: E402

TOKEN = "c2953dfe-0000-4000-8000-000000000000"
EXPIRES_MS = 4102444800000


def _payload(notification_type: str, subtype: str = "") -> dict:
    return {
        "notificationType": notification_type,
        "subtype": subtype,
        "notificationUUID": "notif-1",
        "signedDate": 1791640864000,
        "data": {"signedTransactionInfo": "signed-transaction", "environment": "Production"},
    }


@pytest.fixture
def account(monkeypatch):
    """One active account on Business; record writes and pushes."""
    state = {
        "writes": [],
        "ended": [],
        "payment_failed": [],
        "product": "com.kevin.callscreen.businesspro.monthly",
    }

    async def get_by_uuid(_token, include_inactive=False):
        return {
            "contractor_id": "c-1",
            "active": True,
            "subscription_uuid": TOKEN,
            "subscription_status": "active",
            "subscription_tier": "business",
        }

    async def claim(**_kwargs):
        return True, "c-1"

    async def update(contractor_id, fields):
        state["writes"].append((contractor_id, fields))
        return True

    async def ended(contractor_id):
        state["ended"].append(contractor_id)

    async def payment_failed(contractor_id):
        state["payment_failed"].append(contractor_id)

    monkeypatch.setattr(
        sub_service,
        "_decode_jws_payload",
        lambda _s: {
            "appAccountToken": TOKEN,
            "productId": state["product"],
            "transactionId": "tx-2",
            "originalTransactionId": "otx-1",
            "environment": "Production",
            "purchaseDate": 1791640000000,
            "expiresDate": EXPIRES_MS,
        },
    )
    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", get_by_uuid)
    monkeypatch.setattr("app.db.contractors.update_contractor", update)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", claim)
    monkeypatch.setattr("app.services.acquisition.schedule_payment_measurement", lambda *_a: None)
    monkeypatch.setattr(sub_service, "notify_subscription_ended", ended)
    monkeypatch.setattr(sub_service, "notify_renewal_payment_failed", payment_failed)
    return state


@pytest.mark.asyncio
async def test_failed_renewal_keeps_service_and_asks_for_payment(account):
    assert await sub_service.handle_appstore_notification(_payload("DID_FAIL_TO_RENEW")) is True

    assert account["writes"] == []
    assert account["ended"] == []
    assert account["payment_failed"] == ["c-1"]


@pytest.mark.asyncio
async def test_recovered_payment_stays_active(account):
    await sub_service.handle_appstore_notification(_payload("DID_FAIL_TO_RENEW"))
    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    [(_id, fields)] = account["writes"]
    assert fields["subscription_status"] == "active"
    assert account["ended"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["EXPIRED", "REFUND", "REVOKE"])
async def test_terminal_notifications_still_end_service(account, terminal):
    assert await sub_service.handle_appstore_notification(_payload(terminal)) is True

    assert account["writes"] == [("c-1", {"subscription_status": "expired"})]
    assert account["ended"] == ["c-1"]
    assert account["payment_failed"] == []


@pytest.mark.asyncio
async def test_upgrade_in_settings_updates_the_tier_right_away(account):
    await sub_service.handle_appstore_notification(_payload("DID_CHANGE_RENEWAL_PREF", "UPGRADE"))

    [(_id, fields)] = account["writes"]
    assert fields["subscription_tier"] == "businessPro"
    assert fields["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_downgrade_waits_for_the_next_renewal(account):
    account["product"] = "com.kevin.callscreen.business.monthly"

    await sub_service.handle_appstore_notification(_payload("DID_CHANGE_RENEWAL_PREF", "DOWNGRADE"))

    assert account["writes"] == []
