"""Payload-free admin operations for outbound message delivery receipts."""

import os
from types import SimpleNamespace
from typing import get_args

from fastapi import HTTPException
import pytest


os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.api import admin as admin_api
from app.db import message_delivery_receipts as receipt_db


def _admin_request(is_admin: bool = True):
    return SimpleNamespace(
        state=SimpleNamespace(is_admin=is_admin),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="test-client"),
    )


def test_admin_and_receipt_status_enums_do_not_drift():
    assert set(get_args(admin_api.MessageDeliveryReceiptStatus)) == set(
        receipt_db.VALID_RECEIPT_STATUSES
    )
    assert set(get_args(admin_api.MessageDeliveryResolution)) == set(
        receipt_db.ACKNOWLEDGEMENT_RESOLUTIONS
    )


@pytest.mark.asyncio
async def test_admin_lists_bounded_payload_free_receipts(monkeypatch):
    async def list_receipts(status, *, limit):
        assert status == "failed"
        assert limit == 3
        return [
            {
                "receipt_id": "receipt-test",
                "call_sid": "CA_test",
                "effect": "owner_sms",
                "channel": "sms",
                "status": "failed",
                "provider_status": "undelivered",
                "provider_error_code": 30001,
                "failure_code": "provider_delivery_failed",
            }
        ]

    monkeypatch.setattr(receipt_db, "list_receipts", list_receipts)

    response = await admin_api.admin_list_message_delivery_receipts(
        _admin_request(),
        status="failed",
        limit=2,
    )

    assert response["status"] == "failed"
    assert response["count"] == 1
    assert response["has_more"] is False
    assert response["receipts"][0]["effect"] == "owner_sms"
    rendered = repr(response)
    assert "provider_message_sid" not in rendered
    assert "private" not in rendered


@pytest.mark.asyncio
async def test_receipt_queue_requires_global_admin():
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.admin_list_message_delivery_receipts(
            _admin_request(False),
            status="failed",
            limit=50,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_acknowledges_failed_receipt_without_retry(monkeypatch):
    audits = []
    projections = []
    acknowledgements = []

    async def get_receipt(receipt_id):
        assert receipt_id == "receipt-test"
        return {
            "call_sid": "CA_test",
            "effect": "owner_sms",
            "channel": "sms",
            "status": "failed",
            "provider_status": "undelivered",
            "provider_error_code": 30001,
            "failure_code": "provider_delivery_failed",
            "provider_message_sid": "private-provider-identifier",
            "body": "private message body",
        }

    async def acknowledge_receipt(receipt_id, resolution):
        acknowledgements.append((receipt_id, resolution))
        return True

    async def project_receipt_to_call(receipt_id):
        projections.append(receipt_id)
        return True

    async def write_audit(**kwargs):
        audits.append(kwargs)
        return True

    monkeypatch.setattr(receipt_db, "get_receipt", get_receipt)
    monkeypatch.setattr(receipt_db, "acknowledge_receipt", acknowledge_receipt)
    monkeypatch.setattr(
        receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )
    monkeypatch.setattr(admin_api, "write_admin_audit_event", write_audit)

    response = await admin_api.admin_acknowledge_message_delivery_receipt(
        "receipt-test",
        admin_api.AcknowledgeMessageDeliveryReceiptRequest(
            resolution="customer_contacted_manually"
        ),
        _admin_request(),
    )

    assert response == {
        "status": "acknowledged",
        "receipt_id": "receipt-test",
        "resolution": "customer_contacted_manually",
        "call_status_mirrored": True,
    }
    assert acknowledgements == [
        ("receipt-test", "customer_contacted_manually")
    ]
    assert projections == ["receipt-test"]
    assert audits[0]["action"] == "acknowledge_message_delivery_receipt"
    assert audits[0]["target_type"] == "message_delivery_receipt"
    assert "private" not in repr(audits)


@pytest.mark.asyncio
async def test_admin_receipt_queue_uses_overfetch_for_has_more(monkeypatch):
    async def list_receipts(_status, *, limit):
        assert limit == 3
        return [
            {"receipt_id": "receipt-1"},
            {"receipt_id": "receipt-2"},
            {"receipt_id": "receipt-3"},
        ]

    monkeypatch.setattr(receipt_db, "list_receipts", list_receipts)

    response = await admin_api.admin_list_message_delivery_receipts(
        _admin_request(),
        status="failed",
        limit=2,
    )

    assert response["count"] == 2
    assert response["has_more"] is True
    assert [item["receipt_id"] for item in response["receipts"]] == [
        "receipt-1",
        "receipt-2",
    ]


@pytest.mark.asyncio
async def test_admin_acknowledgement_skips_malformed_call_projection(monkeypatch):
    async def get_receipt(_receipt_id):
        return {
            "call_sid": "",
            "effect": "unknown",
            "status": "failed",
            "failure_code": "provider_delivery_failed",
        }

    async def acknowledge_receipt(_receipt_id, _resolution):
        return True

    async def project_receipt_to_call(receipt_id):
        assert receipt_id == "receipt-test"
        return False

    async def write_audit(**_kwargs):
        return True

    monkeypatch.setattr(receipt_db, "get_receipt", get_receipt)
    monkeypatch.setattr(receipt_db, "acknowledge_receipt", acknowledge_receipt)
    monkeypatch.setattr(
        receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )
    monkeypatch.setattr(admin_api, "write_admin_audit_event", write_audit)

    response = await admin_api.admin_acknowledge_message_delivery_receipt(
        "receipt-test",
        admin_api.AcknowledgeMessageDeliveryReceiptRequest(
            resolution="no_action_required"
        ),
        _admin_request(),
    )

    assert response["status"] == "acknowledged"
    assert response["call_status_mirrored"] is False


@pytest.mark.asyncio
async def test_admin_rejects_acknowledgement_for_nonfailed_receipt(monkeypatch):
    async def get_receipt(_receipt_id):
        return {"status": "pending"}

    monkeypatch.setattr(receipt_db, "get_receipt", get_receipt)

    with pytest.raises(HTTPException) as exc_info:
        await admin_api.admin_acknowledge_message_delivery_receipt(
            "receipt-test",
            admin_api.AcknowledgeMessageDeliveryReceiptRequest(
                resolution="no_action_required"
            ),
            _admin_request(),
        )

    assert exc_info.value.status_code == 409
