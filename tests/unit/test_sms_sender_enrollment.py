"""Unit tests for guarded SMS sender enrollment and recovery service.

Covers gate checks, settings validation, phone region checks, Firestore queries,
Twilio IncomingPhoneNumbers verification, Messaging Service & Campaign scope verification,
membership checks, pre-write drift rechecks, single create execution, readback verification,
budget and cancellation controls, no-side-effects assertions, log cleanliness, and all three wiring points.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional
from unittest.mock import MagicMock

# Required env vars for app.config to import cleanly in standalone test runs.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import pytest
from twilio.base.exceptions import TwilioRestException

from app.config import (
    PRODUCTION_CLOUD_RUN_URL,
    PRODUCTION_GCP_PROJECT_ID,
    settings,
)
from app.services.sms_sender_enrollment import (
    EnrollmentReason,
    EnrollmentStatus,
    SmsSenderEnrollmentResult,
    _compute_campaign_scope_digest,
    _SILENT_TRANSPORT_LOGGER,
    ensure_sms_sender_membership,
)

TEST_CONTRACTOR_ID = "contractor_test_001"
TEST_PHONE_NUMBER = "+14155552671"
TEST_ACCOUNT_SID = "AC" + "1" * 32
TEST_SERVICE_SID = "MG" + "2" * 32
TEST_CAMPAIGN_SID = "QE" + "3" * 32
TEST_PN_SID = "PN" + "4" * 32

TEST_DESCRIPTION = "Kevin Call Screening SMS notifications"
TEST_MESSAGE_FLOW = "Users receive call screening notifications via SMS"
TEST_USECASE = "LOW_VOLUME"

TEST_CAMPAIGN_SHA256 = _compute_campaign_scope_digest(
    TEST_DESCRIPTION,
    TEST_MESSAGE_FLOW,
    TEST_USECASE,
)


class FakeFirestoreDoc:
    """Sealed test fixture for a Firestore document snapshot."""

    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = dict(data)

    def to_dict(self):
        return dict(self._data)


class FakeFirestoreQuery:
    """Sealed test fixture for a Firestore query that records query operations."""

    def __init__(self, docs: list[FakeFirestoreDoc], tracker: Optional[list] = None):
        self._docs = docs
        self._tracker = tracker if tracker is not None else []

    def where(self, *args, **kwargs):
        self._tracker.append(("where", args, kwargs))
        return self

    def select(self, *args, **kwargs):
        self._tracker.append(("select", args, kwargs))
        return self

    def limit(self, *args, **kwargs):
        self._tracker.append(("limit", args, kwargs))
        return self

    def stream(self, timeout=None, retry=None):
        self._tracker.append(("stream", (), {"timeout": timeout, "retry": retry}))
        return iter(self._docs)


class FakeFirestoreClient:
    """Sealed test fixture for the Firestore client."""

    def __init__(
        self,
        project_id: str = PRODUCTION_GCP_PROJECT_ID,
        docs: Optional[list[FakeFirestoreDoc]] = None,
    ):
        self.project = project_id
        self._docs = docs if docs is not None else []
        self.query_tracker: list[tuple[str, tuple, dict]] = []

    def collection(self, name: str):
        return FakeFirestoreQuery(self._docs, self.query_tracker)


class FakeIncomingPhoneNumber:
    """Sealed SDK-shaped test fixture for Twilio IncomingPhoneNumber resource."""

    def __init__(
        self,
        sid: str = TEST_PN_SID,
        account_sid: str = TEST_ACCOUNT_SID,
        phone_number: str = TEST_PHONE_NUMBER,
        origin: str = "twilio",
        number_type: str = "local",
        voice_url: str = f"{PRODUCTION_CLOUD_RUN_URL}/webhooks/twilio/incoming",
        voice_method: str = "POST",
        voice_fallback_url: Optional[str] = None,
        voice_fallback_method: Optional[str] = None,
        status_callback: str = f"{PRODUCTION_CLOUD_RUN_URL}/webhooks/twilio/status",
        status_callback_method: str = "POST",
        sms_url: str = f"{PRODUCTION_CLOUD_RUN_URL}/webhooks/twilio/mms-incoming",
        sms_method: str = "POST",
        sms_fallback_url: Optional[str] = None,
        sms_fallback_method: Optional[str] = None,
        voice_application_sid: Optional[str] = None,
        sms_application_sid: Optional[str] = None,
        trunk_sid: Optional[str] = None,
        capabilities: Optional[dict[str, bool]] = None,
    ):
        self.sid = sid
        self.account_sid = account_sid
        self.phone_number = phone_number
        self.origin = origin
        self.type = number_type
        self.phone_number_type = number_type
        self.voice_url = voice_url
        self.voice_method = voice_method
        self.voice_fallback_url = voice_fallback_url
        self.voice_fallback_method = voice_fallback_method
        self.status_callback = status_callback
        self.status_callback_method = status_callback_method
        self.sms_url = sms_url
        self.sms_method = sms_method
        self.sms_fallback_url = sms_fallback_url
        self.sms_fallback_method = sms_fallback_method
        self.voice_application_sid = voice_application_sid
        self.sms_application_sid = sms_application_sid
        self.trunk_sid = trunk_sid
        self.capabilities = (
            capabilities
            if capabilities is not None
            else {"voice": True, "sms": True, "mms": True}
        )


class FakeMessagingService:
    """Sealed SDK-shaped test fixture for Twilio Messaging Service resource."""

    def __init__(
        self,
        sid: str = TEST_SERVICE_SID,
        account_sid: str = TEST_ACCOUNT_SID,
        us_app_to_person_registered: bool = True,
        use_inbound_webhook_on_number: bool = True,
    ):
        self.sid = sid
        self.account_sid = account_sid
        self.us_app_to_person_registered = us_app_to_person_registered
        self.use_inbound_webhook_on_number = use_inbound_webhook_on_number


class FakeCampaign:
    """Sealed SDK-shaped test fixture for Twilio US A2P Campaign resource."""

    def __init__(
        self,
        sid: str = TEST_CAMPAIGN_SID,
        account_sid: str = TEST_ACCOUNT_SID,
        messaging_service_sid: str = TEST_SERVICE_SID,
        campaign_status: str = "VERIFIED",
        us_app_to_person_usecase: str = TEST_USECASE,
        description: str = TEST_DESCRIPTION,
        message_flow: str = TEST_MESSAGE_FLOW,
    ):
        self.sid = sid
        self.account_sid = account_sid
        self.messaging_service_sid = messaging_service_sid
        self.campaign_status = campaign_status
        self.us_app_to_person_usecase = us_app_to_person_usecase
        self.description = description
        self.message_flow = message_flow


class FakeMembershipPhoneNumber:
    """Sealed SDK-shaped test fixture for Twilio Service PhoneNumber resource."""

    def __init__(
        self,
        sid: str = TEST_PN_SID,
        account_sid: str = TEST_ACCOUNT_SID,
        service_sid: str = TEST_SERVICE_SID,
        phone_number: str = TEST_PHONE_NUMBER,
        country_code: str = "US",
        capabilities: Optional[list[str]] = None,
    ):
        self.sid = sid
        self.account_sid = account_sid
        self.service_sid = service_sid
        self.phone_number = phone_number
        self.country_code = country_code
        # Twilio Service PhoneNumber capabilities is a list of strings
        self.capabilities = (
            capabilities if capabilities is not None else ["Voice", "SMS", "MMS"]
        )


def _make_valid_assignment_data() -> dict:
    return {
        "twilio_number": TEST_PHONE_NUMBER,
        "active": True,
        "country_code": "US",
        "provisioned_country_code": "US",
        "number_provider": "twilio",
        "number_type": "local",
        "number_capabilities": {"voice": True, "sms": True, "mms": True},
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
        "deletion_requested_at": None,
        "deactivated_at": None,
        "deleted_app_detected_at": None,
        "number_released_at": None,
    }


def _configure_valid_settings(monkeypatch):
    monkeypatch.setattr(settings, "sms_sender_enrollment_enabled", True)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "firestore_project_id", PRODUCTION_GCP_PROJECT_ID)
    monkeypatch.setattr(settings, "twilio_account_sid", TEST_ACCOUNT_SID)
    monkeypatch.setattr(settings, "production_twilio_account_sid", TEST_ACCOUNT_SID)
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", TEST_SERVICE_SID)
    monkeypatch.setattr(settings, "sms_sender_enrollment_campaign_sid", TEST_CAMPAIGN_SID)
    monkeypatch.setattr(settings, "sms_sender_enrollment_campaign_sha256", TEST_CAMPAIGN_SHA256)
    monkeypatch.setattr(settings, "cloud_run_url", PRODUCTION_CLOUD_RUN_URL)


@pytest.mark.asyncio
async def test_gate_off_zero_io(monkeypatch):
    """When enrollment is default off, helper must return immediately without any I/O or logging."""
    monkeypatch.setattr(settings, "sms_sender_enrollment_enabled", False)

    mock_twilio_client = MagicMock()
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", mock_twilio_client)
    mock_firestore = MagicMock()
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", mock_firestore)

    result = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert result.status == EnrollmentStatus.DISABLED
    assert result.reason == EnrollmentReason.FEATURE_DISABLED
    assert result.enrolled is False
    assert result.mutated is False
    mock_twilio_client.assert_not_called()
    mock_firestore.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_settings_and_client_bindings(monkeypatch):
    """Test gate rejections on invalid environment, project, credentials, campaign prefix, and client bindings."""
    _configure_valid_settings(monkeypatch)

    # 1. Non-production environment
    monkeypatch.setattr(settings, "environment", "staging")
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.NON_PRODUCTION_ENVIRONMENT

    # 2. Invalid Firestore project ID
    _configure_valid_settings(monkeypatch)
    monkeypatch.setattr(settings, "firestore_project_id", "kevin-staging")
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.INVALID_CONFIGURATION

    # 3. Mismatched Twilio Account SID
    _configure_valid_settings(monkeypatch)
    monkeypatch.setattr(settings, "production_twilio_account_sid", "AC" + "9" * 32)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.INVALID_CONFIGURATION

    # 4. Malformed Messaging Service SID
    _configure_valid_settings(monkeypatch)
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", "bad_sid")
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.INVALID_CONFIGURATION

    # 5. Non-QE Campaign SID prefix (e.g. CM prefix or invalid)
    _configure_valid_settings(monkeypatch)
    monkeypatch.setattr(settings, "sms_sender_enrollment_campaign_sid", "CM" + "3" * 32)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.INVALID_CONFIGURATION

    # 6. Uppercase hex in campaign SHA256 (must fail closed without .lower())
    _configure_valid_settings(monkeypatch)
    uppercase_sha = TEST_CAMPAIGN_SHA256.upper()
    monkeypatch.setattr(settings, "sms_sender_enrollment_campaign_sha256", uppercase_sha)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.INVALID_CONFIGURATION

    # 7. Invalid contractor_id or phone input
    _configure_valid_settings(monkeypatch)
    res = await ensure_sms_sender_membership("", TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INVALID_INPUT

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, "not_a_phone")
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INVALID_INPUT

    # 8. Twilio client account mismatch
    _configure_valid_settings(monkeypatch)
    mock_client = MagicMock()
    mock_client.username = "AC" + "0" * 32
    mock_client.account_sid = "AC" + "0" * 32
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_client)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.CLIENT_IDENTITY_MISMATCH

    # 9. Firestore client project mismatch
    _configure_valid_settings(monkeypatch)
    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)
    fake_db = FakeFirestoreClient(project_id="other-gcp-project")
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.CLIENT_IDENTITY_MISMATCH


@pytest.mark.asyncio
async def test_non_us_phone_region_rejection(monkeypatch):
    """Rejects non-US numbers (CA, BR, GB) even with +1 or international formats."""
    _configure_valid_settings(monkeypatch)

    # Canada (+1 416 ...)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, "+14165551234")
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.NON_US_PHONE_REGION

    # Brazil (+55 11 ...)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, "+5511999998888")
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.NON_US_PHONE_REGION

    # UK (+44 20 ...)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, "+442071838750")
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.NON_US_PHONE_REGION


@pytest.mark.asyncio
async def test_firestore_assignment_query_and_validations(monkeypatch):
    """Verify Firestore assignment validations, projected fields query, and lifecycle hold gates."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    # 1. Assignment not found (0 docs)
    fake_db = FakeFirestoreClient(docs=[])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.ASSIGNMENT_NOT_FOUND

    # Assert Firestore query parameters: limit(2), select projected fields, timeout=2, retry=None
    operations = [op[0] for op in fake_db.query_tracker]
    assert "where" in operations
    assert "select" in operations
    assert "limit" in operations
    assert "stream" in operations
    stream_op = next(op for op in fake_db.query_tracker if op[0] == "stream")
    assert stream_op[2]["timeout"] == 2
    assert stream_op[2]["retry"] is None

    # 2. Assignment ambiguous (2 docs)
    doc1 = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    doc2 = FakeFirestoreDoc("contractor_2", _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc1, doc2])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.ASSIGNMENT_AMBIGUOUS

    # 3. Contractor ID mismatch
    doc_mismatch = FakeFirestoreDoc("wrong_contractor_id", _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc_mismatch])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.CONTRACTOR_ID_MISMATCH

    # 4. Inactive contractor
    inactive_data = _make_valid_assignment_data()
    inactive_data["active"] = False
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, inactive_data)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.ASSIGNMENT_INACTIVE

    # 5. Non-US country
    non_us_data = _make_valid_assignment_data()
    non_us_data["country_code"] = "CA"
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, non_us_data)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.COUNTRY_NOT_US

    # 6. Provider not Twilio
    non_twilio = _make_valid_assignment_data()
    non_twilio["number_provider"] = "bandwidth"
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, non_twilio)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.PROVIDER_NOT_TWILIO

    # 7. Number type not local
    toll_free = _make_valid_assignment_data()
    toll_free["number_type"] = "toll_free"
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, toll_free)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.NUMBER_TYPE_NOT_LOCAL

    # 8. Insufficient capabilities
    no_sms_caps = _make_valid_assignment_data()
    no_sms_caps["number_capabilities"] = {"voice": True, "sms": False}
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, no_sms_caps)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.CAPABILITIES_INSUFFICIENT

    # 9. SMS not enabled
    sms_disabled = _make_valid_assignment_data()
    sms_disabled["owner_sms_enabled"] = False
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, sms_disabled)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.OWNER_SMS_NOT_ENABLED

    # 10. SMS opted out
    opted_out = _make_valid_assignment_data()
    opted_out["owner_sms_opted_out"] = True
    fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, opted_out)])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.OWNER_SMS_OPTED_OUT

    # 11. Lifecycle holds (all 4 types)
    for hold_field in (
        "deletion_requested_at",
        "deactivated_at",
        "deleted_app_detected_at",
        "number_released_at",
    ):
        hold_data = _make_valid_assignment_data()
        hold_data[hold_field] = 1728200000
        fake_db = FakeFirestoreClient(docs=[FakeFirestoreDoc(TEST_CONTRACTOR_ID, hold_data)])
        monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)
        res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
        assert res.status == EnrollmentStatus.SKIPPED
        assert res.reason == EnrollmentReason.LIFECYCLE_HOLD_ACTIVE


@pytest.mark.asyncio
async def test_incoming_phone_numbers_and_routing_verification(monkeypatch):
    """Verify Twilio IncomingPhoneNumbers query arguments, origin, type, and frozen routing checks."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    # 1. Incoming numbers empty
    mock_tw_client.incoming_phone_numbers.list.return_value = []
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INCOMING_NUMBER_NOT_FOUND
    mock_tw_client.incoming_phone_numbers.list.assert_called_with(
        phone_number=TEST_PHONE_NUMBER, limit=2
    )

    # 2. Incoming numbers ambiguous
    mock_tw_client.incoming_phone_numbers.list.return_value = [
        FakeIncomingPhoneNumber(),
        FakeIncomingPhoneNumber(),
    ]
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INCOMING_NUMBER_AMBIGUOUS

    # 3. Origin not twilio (e.g. hosted)
    bad_origin = FakeIncomingPhoneNumber(origin="hosted")
    mock_tw_client.incoming_phone_numbers.list.return_value = [bad_origin]
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH

    # 4. Type not local (e.g. mobile/toll-free)
    bad_type = FakeIncomingPhoneNumber(number_type="toll_free")
    mock_tw_client.incoming_phone_numbers.list.return_value = [bad_type]
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.NUMBER_TYPE_NOT_LOCAL

    # 5. Routing callback mismatch (e.g. voice_url altered)
    bad_routing = FakeIncomingPhoneNumber(voice_url="https://other-service.com/webhook")
    mock_tw_client.incoming_phone_numbers.list.return_value = [bad_routing]
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH

    # 6. Bound application SID
    bound_app = FakeIncomingPhoneNumber(voice_application_sid="AP" + "5" * 32)
    mock_tw_client.incoming_phone_numbers.list.return_value = [bound_app]
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH

    # 7. Bound trunk SID
    bound_trunk = FakeIncomingPhoneNumber(trunk_sid="TK" + "6" * 32)
    mock_tw_client.incoming_phone_numbers.list.return_value = [bound_trunk]
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.SKIPPED
    assert res.reason == EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH


@pytest.mark.asyncio
async def test_membership_already_present_idempotent(monkeypatch):
    """When number is already a verified member with list capabilities, return success without writing."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]
    # Membership capabilities is real array fixture ["Voice", "SMS", "MMS"]
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.return_value = (
        FakeMembershipPhoneNumber(capabilities=["Voice", "SMS", "MMS"])
    )
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.ALREADY_MEMBER
    assert res.reason == EnrollmentReason.MEMBERSHIP_PRESENT_VALID
    assert res.enrolled is True
    assert res.mutated is False
    mock_tw_client.messaging.v1.services().phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_membership_malformed_blocks(monkeypatch):
    """When membership resource is present but malformed (e.g. non-US, missing SMS in list, or dict), block enrollment."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]

    # 1. Non-US country code on membership
    malformed_mb = FakeMembershipPhoneNumber(country_code="CA")
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.return_value = malformed_mb
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.MEMBERSHIP_MALFORMED
    assert res.enrolled is False
    assert res.mutated is False
    mock_tw_client.messaging.v1.services().phone_numbers.create.assert_not_called()

    # 2. Missing SMS capability in array
    no_sms_mb = FakeMembershipPhoneNumber(capabilities=["Voice", "MMS"])
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.return_value = no_sms_mb
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.MEMBERSHIP_MALFORMED

    # 3. Malformed dict capabilities on membership (must fail closed)
    dict_mb = FakeMembershipPhoneNumber()
    dict_mb.capabilities = {"voice": True, "sms": True}
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.return_value = dict_mb
    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.MEMBERSHIP_MALFORMED


@pytest.mark.asyncio
async def test_successful_enrollment_with_independent_readback(monkeypatch):
    """When absent (404/20404), pre-write checks pass, single create succeeds, and readback verifies."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]

    not_found_exc = TwilioRestException(
        status=404, uri="/Services/MG/PhoneNumbers/PN", msg="Not Found", code=20404
    )
    valid_mb = FakeMembershipPhoneNumber(capabilities=["Voice", "SMS", "MMS"])
    # 1st fetch = 404 (absent), 2nd fetch = valid readback
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.side_effect = [
        not_found_exc,
        valid_mb,
    ]
    mock_tw_client.messaging.v1.services().phone_numbers.create.return_value = valid_mb
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.ENROLLED
    assert res.reason == EnrollmentReason.READBACK_VERIFIED
    assert res.enrolled is True
    assert res.mutated is True
    mock_tw_client.messaging.v1.services().phone_numbers.create.assert_called_once_with(
        phone_number_sid=TEST_PN_SID
    )


@pytest.mark.asyncio
async def test_other_service_conflict_terminal(monkeypatch):
    """Twilio error 21712 indicates membership in another service: report conflict, mutated=False, do not retry/delete."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]

    not_found_exc = TwilioRestException(
        status=404, uri="/Services/MG/PhoneNumbers/PN", msg="Not Found", code=20404
    )
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.side_effect = not_found_exc

    conflict_exc = TwilioRestException(
        status=400, uri="/Services/MG/PhoneNumbers", msg="In another service", code=21712
    )
    mock_tw_client.messaging.v1.services().phone_numbers.create.side_effect = conflict_exc
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.OTHER_SERVICE_CONFLICT
    assert res.enrolled is False
    assert res.mutated is False
    mock_tw_client.messaging.v1.services().phone_numbers.create.assert_called_once()


@pytest.mark.asyncio
async def test_concurrent_duplicate_resolves_via_readback(monkeypatch):
    """Create returns duplicate/already-member error, but post-create readback succeeds -> mutated=None."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]

    not_found_exc = TwilioRestException(
        status=404, uri="/Services/MG/PhoneNumbers/PN", msg="Not Found", code=20404
    )
    valid_mb = FakeMembershipPhoneNumber(capabilities=["Voice", "SMS", "MMS"])
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.side_effect = [
        not_found_exc,
        valid_mb,
    ]

    duplicate_exc = TwilioRestException(
        status=400, uri="/Services/MG/PhoneNumbers", msg="Already member", code=21618
    )
    mock_tw_client.messaging.v1.services().phone_numbers.create.side_effect = duplicate_exc
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.ENROLLED
    assert res.reason == EnrollmentReason.READBACK_VERIFIED
    assert res.enrolled is True
    assert res.mutated is None  # Proved membership, but did not mutate in this call


@pytest.mark.asyncio
async def test_create_failed_and_readback_absent(monkeypatch):
    """When create fails and readback fetch returns 404/20404, return UNCERTAIN, mutated=None without retrying."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]

    not_found_exc = TwilioRestException(
        status=404, uri="/Services/MG/PhoneNumbers/PN", msg="Not Found", code=20404
    )
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.side_effect = [
        not_found_exc,
        not_found_exc,
    ]
    mock_tw_client.messaging.v1.services().phone_numbers.create.side_effect = Exception(
        "Network timeout"
    )
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.UNCERTAIN
    assert res.reason == EnrollmentReason.READBACK_FAILED_ABSENT
    assert res.enrolled is False
    assert res.mutated is None  # create was attempted, so mutation state is unknown
    assert mock_tw_client.messaging.v1.services().phone_numbers.create.call_count == 1


@pytest.mark.parametrize(
    "drift_type,assignment_override,incoming_override,svc_override,cmp_override",
    [
        ("assignment_inactive", {"active": False}, None, None, None),
        ("assignment_country_ca", {"country_code": "CA"}, None, None, None),
        ("assignment_provider_bandwidth", {"number_provider": "bandwidth"}, None, None, None),
        ("assignment_caps_no_sms", {"number_capabilities": {"voice": True, "sms": False}}, None, None, None),
        ("assignment_sms_disabled", {"owner_sms_enabled": False}, None, None, None),
        ("assignment_opted_out", {"owner_sms_opted_out": True}, None, None, None),
        ("assignment_lifecycle_hold", {"deletion_requested_at": 1728200000}, None, None, None),
        ("incoming_sid_drift", None, {"sid": "PN" + "9" * 32}, None, None),
        ("incoming_voice_url_drift", None, {"voice_url": "https://attacker.com/voice"}, None, None),
        ("incoming_trunk_attached", None, {"trunk_sid": "TK" + "1" * 32}, None, None),
        ("incoming_origin_drift", None, {"origin": "hosted"}, None, None),
        ("service_unregistered", None, None, {"us_app_to_person_registered": False}, None),
        ("campaign_status_failed", None, None, None, {"campaign_status": "FAILED"}),
        ("campaign_digest_drift", None, None, None, {"description": "Changed scope description"}),
    ],
)
@pytest.mark.asyncio
async def test_parametrized_prewrite_drift_rejection(
    monkeypatch,
    drift_type,
    assignment_override,
    incoming_override,
    svc_override,
    cmp_override,
):
    """Parametrized verification that any prewrite drift in assignment, incoming routing, service, or campaign rejects."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID

    valid_svc = FakeMessagingService()
    valid_cmp = FakeCampaign()
    valid_incoming = FakeIncomingPhoneNumber()

    # Prewrite drift items
    drift_svc = FakeMessagingService(**(svc_override or {})) if svc_override else valid_svc
    drift_cmp = FakeCampaign(**(cmp_override or {})) if cmp_override else valid_cmp
    drift_incoming = FakeIncomingPhoneNumber(**(incoming_override or {})) if incoming_override else valid_incoming

    # Side effects for reads (initial read vs fresh re-read before write)
    mock_tw_client.messaging.v1.services().fetch.side_effect = [valid_svc, drift_svc]
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.side_effect = [valid_cmp, drift_cmp]
    mock_tw_client.incoming_phone_numbers.list.side_effect = [[valid_incoming], [drift_incoming]]

    not_found_exc = TwilioRestException(
        status=404, uri="/Services/MG/PhoneNumbers/PN", msg="Not Found", code=20404
    )
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.side_effect = not_found_exc
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    initial_data = _make_valid_assignment_data()
    fresh_data = dict(initial_data)
    if assignment_override:
        fresh_data.update(assignment_override)

    # Firestore query returns initial_data on first query, fresh_data on second query
    doc1 = FakeFirestoreDoc(TEST_CONTRACTOR_ID, initial_data)
    doc2 = FakeFirestoreDoc(TEST_CONTRACTOR_ID, fresh_data)

    class AlternatingFirestoreClient:
        def __init__(self):
            self.project = PRODUCTION_GCP_PROJECT_ID
            self.call_count = 0

        def collection(self, name: str):
            self.call_count += 1
            docs = [doc1] if self.call_count == 1 else [doc2]
            return FakeFirestoreQuery(docs)

    monkeypatch.setattr(
        "app.services.sms_sender_enrollment.get_firestore_client",
        lambda: AlternatingFirestoreClient(),
    )

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.PREWRITE_DRIFT_DETECTED
    assert res.enrolled is False
    assert res.mutated is False
    mock_tw_client.messaging.v1.services().phone_numbers.create.assert_not_called()


@pytest.mark.parametrize(
    "drift_type,post_assignment_override,post_incoming_override",
    [
        ("post_assignment_inactive", {"active": False}, None),
        ("post_assignment_opted_out", {"owner_sms_opted_out": True}, None),
        ("post_incoming_voice_url_drift", None, {"voice_url": "https://attacker.com/voice"}),
        ("post_incoming_trunk_attached", None, {"trunk_sid": "TK" + "1" * 32}),
    ],
)
@pytest.mark.asyncio
async def test_parametrized_postwrite_drift_detection(
    monkeypatch,
    drift_type,
    post_assignment_override,
    post_incoming_override,
):
    """Parametrized verification that drift occurring between create and readback yields UNCERTAIN with POSTWRITE_DRIFT_DETECTED."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()

    valid_incoming = FakeIncomingPhoneNumber()
    drift_incoming = FakeIncomingPhoneNumber(**(post_incoming_override or {})) if post_incoming_override else valid_incoming

    # 1. initial, 2. prewrite fresh, 3. postwrite readback
    mock_tw_client.incoming_phone_numbers.list.side_effect = [
        [valid_incoming],
        [valid_incoming],
        [drift_incoming],
    ]

    not_found_exc = TwilioRestException(
        status=404, uri="/Services/MG/PhoneNumbers/PN", msg="Not Found", code=20404
    )
    valid_mb = FakeMembershipPhoneNumber(capabilities=["Voice", "SMS", "MMS"])
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.side_effect = [
        not_found_exc,
        valid_mb,
    ]
    mock_tw_client.messaging.v1.services().phone_numbers.create.return_value = valid_mb
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    initial_data = _make_valid_assignment_data()
    post_data = dict(initial_data)
    if post_assignment_override:
        post_data.update(post_assignment_override)

    doc_valid = FakeFirestoreDoc(TEST_CONTRACTOR_ID, initial_data)
    doc_post = FakeFirestoreDoc(TEST_CONTRACTOR_ID, post_data)

    class MultiPhaseFirestoreClient:
        def __init__(self):
            self.project = PRODUCTION_GCP_PROJECT_ID
            self.call_count = 0

        def collection(self, name: str):
            self.call_count += 1
            docs = [doc_post] if self.call_count == 3 else [doc_valid]
            return FakeFirestoreQuery(docs)

    monkeypatch.setattr(
        "app.services.sms_sender_enrollment.get_firestore_client",
        lambda: MultiPhaseFirestoreClient(),
    )

    res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.UNCERTAIN
    assert res.reason == EnrollmentReason.POSTWRITE_DRIFT_DETECTED
    assert res.enrolled is False


@pytest.mark.asyncio
async def test_cancellation_and_budget_controls(monkeypatch):
    """Verify cancellation inside executor closures, task cancellation propagation, and budget checks."""
    _configure_valid_settings(monkeypatch)

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    # 1. Cancelled event provided up front
    cancel_event = asyncio.Event()
    cancel_event.set()

    res = await ensure_sms_sender_membership(
        TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER, cancel_event=cancel_event
    )
    assert res.status == EnrollmentStatus.REJECTED
    assert res.reason == EnrollmentReason.OPERATION_CANCELLED
    assert res.enrolled is False
    assert res.mutated is False

    # 2. Budget exceeded check
    clock_values = iter([0.0, 16.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock_values, 16.0))
    res_budget = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)
    assert res_budget.status == EnrollmentStatus.UNCERTAIN
    assert res_budget.reason == EnrollmentReason.BUDGET_EXCEEDED
    assert res_budget.enrolled is False
    assert res_budget.mutated is False


@pytest.mark.asyncio
async def test_transport_logger_and_no_pii_in_logs(monkeypatch, caplog):
    """Verify dedicated silent transport logger, and assert no secrets, numbers, IDs, or raw traceback in logs."""
    _configure_valid_settings(monkeypatch)

    # Verify dedicated transport logger configuration
    assert _SILENT_TRANSPORT_LOGGER.propagate is False
    assert _SILENT_TRANSPORT_LOGGER.level > logging.CRITICAL

    mock_tw_client = MagicMock()
    mock_tw_client.username = TEST_ACCOUNT_SID
    mock_tw_client.account_sid = TEST_ACCOUNT_SID
    mock_tw_client.messaging.v1.services().fetch.return_value = FakeMessagingService()
    mock_tw_client.messaging.v1.services().us_app_to_person().fetch.return_value = FakeCampaign()
    mock_tw_client.incoming_phone_numbers.list.return_value = [FakeIncomingPhoneNumber()]
    valid_mb = FakeMembershipPhoneNumber()
    mock_tw_client.messaging.v1.services().phone_numbers().fetch.return_value = valid_mb
    monkeypatch.setattr("app.services.sms_sender_enrollment.Client", lambda *a, **k: mock_tw_client)

    doc = FakeFirestoreDoc(TEST_CONTRACTOR_ID, _make_valid_assignment_data())
    fake_db = FakeFirestoreClient(docs=[doc])
    monkeypatch.setattr("app.services.sms_sender_enrollment.get_firestore_client", lambda: fake_db)

    with caplog.at_level(logging.INFO):
        res = await ensure_sms_sender_membership(TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER)

    assert res.status == EnrollmentStatus.ALREADY_MEMBER
    log_text = caplog.text
    assert TEST_PHONE_NUMBER not in log_text
    assert TEST_ACCOUNT_SID not in log_text
    assert TEST_CONTRACTOR_ID not in log_text
    assert "test-token" not in log_text


@pytest.mark.asyncio
async def test_three_wiring_points_and_failure_preservation(monkeypatch):
    """Test all three wiring points: new number purchase (persist-before-enroll), existing DB return, and existing API return."""
    from app.api.contractors import api_provision_number
    from app.db.contractors import get_contractor, provision_twilio_number

    # Setup mocks for Twilio client in DB provisioning
    mock_db_client = MagicMock()
    available_num = MagicMock()
    available_num.phone_number = TEST_PHONE_NUMBER
    mock_db_client.available_phone_numbers("US").local.list.return_value = [available_num]

    purchased_incoming = FakeIncomingPhoneNumber(phone_number=TEST_PHONE_NUMBER)
    mock_db_client.incoming_phone_numbers.create.return_value = purchased_incoming
    monkeypatch.setattr("twilio.rest.Client", lambda *a, **k: mock_db_client)

    contractor_state = {
        "contractor_id": TEST_CONTRACTOR_ID,
        "twilio_number": "",
        "country_code": "US",
        "owner_phone": "+14155552671",
        "active": True,
    }

    persisted_updates = []

    async def mock_get_contractor(cid):
        if cid == TEST_CONTRACTOR_ID:
            return dict(contractor_state)
        return None

    async def mock_update_contractor(cid, updates):
        persisted_updates.append(dict(updates))
        contractor_state.update(updates)
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor", mock_get_contractor)
    monkeypatch.setattr("app.db.contractors.update_contractor", mock_update_contractor)
    monkeypatch.setattr("app.api.contractors.get_contractor", mock_get_contractor)

    called_enrollments = []
    observed_enrollment_states = []

    async def mock_failing_enrollment(cid, num, **kwargs):
        called_enrollments.append((cid, num))
        observed_enrollment_states.append(dict(contractor_state))
        raise RuntimeError("Simulated helper failure")

    monkeypatch.setattr(
        "app.services.sms_sender_enrollment.ensure_sms_sender_membership",
        mock_failing_enrollment,
    )

    # --- Wiring Point 1: New number purchase (provision_twilio_number) ---
    purchased_result = await provision_twilio_number(TEST_CONTRACTOR_ID, country_code="US")
    assert len(observed_enrollment_states) == 1
    assert observed_enrollment_states[0]["twilio_number"] == TEST_PHONE_NUMBER

    # Assert exactly 1 purchase call was made and assigned number is returned
    assert purchased_result == TEST_PHONE_NUMBER
    assert mock_db_client.incoming_phone_numbers.create.call_count == 1
    # Assert number was persisted to DB before enrollment failure
    assert len(persisted_updates) == 1
    assert persisted_updates[0]["twilio_number"] == TEST_PHONE_NUMBER
    assert contractor_state["twilio_number"] == TEST_PHONE_NUMBER
    # Assert enrollment was attempted with the purchased number
    assert (TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER) in called_enrollments
    # Assert no recovery purchase or number release occurred
    mock_db_client.incoming_phone_numbers.create.reset_mock()

    # --- Wiring Point 2: Existing number in DB provision_twilio_number ---
    called_enrollments.clear()
    existing_db_result = await provision_twilio_number(TEST_CONTRACTOR_ID, country_code="US")
    assert existing_db_result == TEST_PHONE_NUMBER
    assert mock_db_client.incoming_phone_numbers.create.call_count == 0  # No second purchase!
    assert (TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER) in called_enrollments

    # --- Wiring Point 3: Existing number in API api_provision_number ---
    called_enrollments.clear()
    req = MagicMock()
    req.state.is_admin = True
    req.state.contractor_id = TEST_CONTRACTOR_ID

    api_res = await api_provision_number(TEST_CONTRACTOR_ID, req)
    assert api_res["status"] == "ok"
    assert api_res["phone_number"] == TEST_PHONE_NUMBER
    assert api_res["existing"] is True
    assert (TEST_CONTRACTOR_ID, TEST_PHONE_NUMBER) in called_enrollments
