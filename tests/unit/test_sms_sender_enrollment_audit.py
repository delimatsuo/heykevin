"""Independent behavioral probes using SDK resource shapes and sealed clients."""

import asyncio
import hashlib
import json
import logging
import os
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.rest.api.v2010.account.incoming_phone_number import IncomingPhoneNumberInstance
from twilio.rest.messaging.v1.service import ServiceInstance
from twilio.rest.messaging.v1.service.phone_number import PhoneNumberInstance
from twilio.rest.messaging.v1.service.us_app_to_person import UsAppToPersonInstance

# Dummy bootstrap happens after tests/conftest.py captures pristine environment
# names, and does not require or load any local credentials.
for _key, _value in {
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_PHONE_NUMBER": "+15005550006",
    "TELEGRAM_BOT_TOKEN": "test-token",
    "USER_PHONE": "+15555550123",
}.items():
    os.environ.setdefault(_key, _value)

from app.config import PRODUCTION_CLOUD_RUN_URL, settings
from app.services import sms_sender_enrollment as enrollment

ACCOUNT = "AC" + "1" * 32
SERVICE = "MG" + "2" * 32
CAMPAIGN = "QE" + "3" * 32
NUMBER_SID = "PN" + "4" * 32
NUMBER = "+14155552671"
TENANT = "synthetic-p01"
PROJECT = "kevin-491315"
EXPECTED_FIELDS = {
    "twilio_number", "active", "country_code", "provisioned_country_code",
    "number_provider", "number_type", "number_capabilities", "owner_sms_enabled",
    "owner_sms_opted_out", "deletion_requested_at", "deactivated_at",
    "deleted_app_detected_at", "number_released_at",
}
SCOPE = {
    "description": "Synthetic Kevin subscriber summary campaign",
    "message_flow": "Synthetic subscriber consent for call summaries",
    "us_app_to_person_usecase": "LOW_VOLUME",
}
DIGEST = hashlib.sha256(json.dumps(SCOPE, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True).encode()).hexdigest()


class Fixture:
    """Only the intended read and membership-create surfaces exist."""

    def __init__(self):
        self.calls = []
        self.membership = False
        self.create_error = None
        self.create_effect = True
        self.membership_error = None
        self.membership_return_none = False
        self.transform = lambda _kind, _count, value: value
        self.counts = {}
        self.http_client = None
        self.assignment = {
            "twilio_number": NUMBER, "active": True, "country_code": "US",
            "provisioned_country_code": "US", "number_provider": "twilio",
            "number_type": "local", "number_capabilities": {"voice": True, "sms": True},
            "owner_sms_enabled": True, "owner_sms_opted_out": False,
        }
        self.incoming = {
            "account_sid": ACCOUNT, "sid": NUMBER_SID, "phone_number": NUMBER,
            "origin": "twilio", "type": "local",
            "capabilities": {"voice": True, "sms": True, "mms": False},
            "voice_url": PRODUCTION_CLOUD_RUN_URL + "/webhooks/twilio/incoming",
            "voice_method": "POST",
            "status_callback": PRODUCTION_CLOUD_RUN_URL + "/webhooks/twilio/status",
            "status_callback_method": "POST",
            "sms_url": PRODUCTION_CLOUD_RUN_URL + "/webhooks/twilio/mms-incoming",
            "sms_method": "POST", "sms_application_sid": None,
            "voice_application_sid": None, "trunk_sid": None,
            "voice_fallback_url": None, "voice_fallback_method": "POST",
            "sms_fallback_url": None, "sms_fallback_method": "POST",
        }
        self.service = {
            "sid": SERVICE, "account_sid": ACCOUNT,
            "us_app_to_person_registered": True, "use_inbound_webhook_on_number": True,
        }
        self.campaign = {
            "sid": CAMPAIGN, "account_sid": ACCOUNT, "messaging_service_sid": SERVICE,
            "campaign_status": "VERIFIED", **SCOPE,
        }
        self.db = SimpleNamespace(project=PROJECT, collection=self.collection)
        self.client = SimpleNamespace(
            account_sid=ACCOUNT, username=ACCOUNT,
            incoming_phone_numbers=SimpleNamespace(list=self.list_incoming),
            messaging=SimpleNamespace(v1=SimpleNamespace(services=self.service_context)),
        )

    def observe(self, kind, value):
        self.calls.append(kind)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return self.transform(kind, self.counts[kind], deepcopy(value))

    def collection(self, name):
        assert name == "contractors"
        owner = self

        class Query:
            def where(self, *, filter):
                assert filter.field_path == "twilio_number"
                assert filter.op_string == "==" and filter.value == NUMBER
                return self

            def select(self, fields):
                assert set(fields) == EXPECTED_FIELDS
                return self

            def limit(self, count):
                assert count == 2
                return self

            def stream(self, *, timeout, retry):
                assert timeout == 2 and retry is None
                records = owner.observe("assignment", [(TENANT, owner.assignment)])
                return iter(SimpleNamespace(id=doc_id, exists=True,
                                             to_dict=lambda data=data: deepcopy(data))
                            for doc_id, data in records)

        return Query()

    def list_incoming(self, *, phone_number, limit):
        assert phone_number == NUMBER and limit == 2
        records = self.observe("incoming", [self.incoming])
        return [IncomingPhoneNumberInstance(None, record, ACCOUNT) for record in records]

    def service_context(self, sid):
        assert sid == SERVICE
        return SimpleNamespace(fetch=self.fetch_service,
                               us_app_to_person=self.campaign_context,
                               phone_numbers=self.membership_context())

    def fetch_service(self):
        return ServiceInstance(None, self.observe("service", self.service))

    def campaign_context(self, sid):
        assert sid == CAMPAIGN
        return SimpleNamespace(fetch=self.fetch_campaign)

    def fetch_campaign(self):
        return UsAppToPersonInstance(None, self.observe("campaign", self.campaign), SERVICE)

    def membership_context(self):
        owner = self

        class Membership:
            def __call__(self, sid):
                assert sid == NUMBER_SID
                return self

            def fetch(self):
                owner.observe("membership", None)
                if owner.membership_return_none:
                    return None
                if owner.membership_error is not None:
                    raise owner.membership_error
                if not owner.membership:
                    raise TwilioRestException(404, "/synthetic", "absent", code=20404)
                payload = {
                    "sid": NUMBER_SID, "account_sid": ACCOUNT, "service_sid": SERVICE,
                    "phone_number": NUMBER, "country_code": "US",
                    "capabilities": ["Voice", "SMS"],
                }
                return PhoneNumberInstance(None, payload, SERVICE)

            def create(self, *, phone_number_sid):
                assert phone_number_sid == NUMBER_SID
                owner.observe("create", None)
                if owner.create_effect:
                    owner.membership = True
                if owner.create_error is not None:
                    raise owner.create_error
                # Deliberately uninformative: only independent readback is proof.
                return SimpleNamespace()

        return Membership()

    def client_factory(self, account_sid, auth_token, *, http_client):
        assert account_sid == ACCOUNT
        assert auth_token == "synthetic-transport-secret"
        assert http_client.timeout == 2
        assert http_client.session.adapters["https://"].max_retries.total == 0
        self.http_client = http_client
        return self.client


@pytest.fixture
def fx(monkeypatch):
    fixture = Fixture()
    for name, value in {
        "sms_sender_enrollment_enabled": True, "environment": "production",
        "firestore_project_id": PROJECT, "twilio_account_sid": ACCOUNT,
        "production_twilio_account_sid": ACCOUNT, "twilio_auth_token": "synthetic-transport-secret",
        "twilio_messaging_service_sid": SERVICE,
        "sms_sender_enrollment_campaign_sid": CAMPAIGN,
        "sms_sender_enrollment_campaign_sha256": DIGEST,
        "cloud_run_url": PRODUCTION_CLOUD_RUN_URL,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(enrollment, "Client", fixture.client_factory)
    monkeypatch.setattr(enrollment, "get_firestore_client", lambda: fixture.db)
    return fixture


@pytest.mark.asyncio
async def test_real_sdk_membership_create_then_idempotent(fx):
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.status == enrollment.EnrollmentStatus.ENROLLED
    assert result.enrolled and result.mutated is True
    assert fx.counts["create"] == 1
    assert fx.counts["membership"] == 2
    assert fx.counts["assignment"] == 3 and fx.counts["incoming"] == 3
    again = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert again.status == enrollment.EnrollmentStatus.ALREADY_MEMBER
    assert again.enrolled and again.mutated is False
    assert fx.counts["create"] == 1


@pytest.mark.parametrize("field,value", [
    ("active", False), ("active", 1), ("country_code", "CA"),
    ("provisioned_country_code", "BR"), ("number_provider", "other"),
    ("number_type", "mobile"), ("number_capabilities", {"voice": True, "sms": "true"}),
    ("owner_sms_enabled", None), ("owner_sms_enabled", False),
    ("owner_sms_opted_out", True), ("owner_sms_opted_out", None),
    ("deletion_requested_at", 1), ("deactivated_at", 0),
    ("deleted_app_detected_at", False), ("number_released_at", 1),
])
@pytest.mark.asyncio
async def test_assignment_guards_never_create(fx, field, value):
    fx.assignment[field] = value
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert not result.enrolled and result.mutated is False
    assert "create" not in fx.calls


@pytest.mark.parametrize("kind,count,field,value", [
    ("assignment", 2, "owner_sms_opted_out", True),
    ("assignment", 2, "twilio_number", "+14155552672"),
    ("incoming", 2, "account_sid", "AC" + "9" * 32),
    ("incoming", 2, "sid", "PN" + "9" * 32),
    ("incoming", 2, "capabilities", {"voice": True, "sms": False}),
    ("incoming", 2, "voice_fallback_url", "https://synthetic.invalid/drift"),
    ("service", 2, "account_sid", "AC" + "9" * 32),
    ("service", 2, "use_inbound_webhook_on_number", False),
    ("campaign", 2, "campaign_status", "FAILED"),
    ("campaign", 2, "description", "Changed campaign scope"),
])
@pytest.mark.asyncio
async def test_fresh_prewrite_drift_blocks_create(fx, kind, count, field, value):
    def transform(read_kind, read_count, payload):
        if read_kind == kind and read_count == count:
            if kind == "assignment":
                payload[0][1][field] = value
            elif kind == "incoming":
                payload[0][field] = value
            else:
                payload[field] = value
        return payload
    fx.transform = transform
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.status == enrollment.EnrollmentStatus.REJECTED
    assert result.mutated is False and "create" not in fx.calls


@pytest.mark.parametrize("kind,field,value", [
    ("assignment", "owner_sms_opted_out", True),
    ("assignment", "active", False),
    ("incoming", "account_sid", "AC" + "9" * 32),
    ("incoming", "sms_fallback_url", "https://synthetic.invalid/drift"),
])
@pytest.mark.asyncio
async def test_postwrite_drift_is_uncertain(fx, kind, field, value):
    def transform(read_kind, read_count, payload):
        if read_kind == kind and read_count == 3:
            if kind == "assignment":
                payload[0][1][field] = value
            else:
                payload[0][field] = value
        return payload
    fx.transform = transform
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.status == enrollment.EnrollmentStatus.UNCERTAIN
    assert not result.enrolled and result.mutated is True
    assert fx.counts["create"] == 1


@pytest.mark.parametrize("effective", [False, True])
@pytest.mark.asyncio
async def test_ambiguous_create_records_unknown_mutation(fx, effective):
    fx.create_error = TimeoutError("private synthetic payload must never be logged")
    fx.create_effect = effective
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.enrolled is effective and result.mutated is None
    assert fx.counts["create"] == 1


@pytest.mark.parametrize("field,value", [
    ("sms_sender_enrollment_campaign_sid", "CM" + "3" * 32),
    ("sms_sender_enrollment_campaign_sha256", DIGEST.upper()),
])
@pytest.mark.asyncio
async def test_campaign_pin_syntax_does_not_get_normalized(fx, monkeypatch, field, value):
    monkeypatch.setattr(settings, field, value)
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.status == enrollment.EnrollmentStatus.REJECTED
    assert fx.calls == [] and fx.http_client is None


@pytest.mark.parametrize("field,value", [("origin", "hosted"), ("type", "mobile")])
@pytest.mark.asyncio
async def test_provider_resource_type_and_origin_guards(fx, field, value):
    fx.incoming[field] = value
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert not result.enrolled and "create" not in fx.calls


@pytest.mark.asyncio
async def test_transport_and_error_logs_are_private(fx, caplog):
    fx.create_error = TimeoutError("private synthetic payload must never be logged")
    fx.create_effect = False
    with caplog.at_level(logging.DEBUG):
        await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
        fx.http_client.log_request({"method": "GET", "url": "https://synthetic.invalid/" + NUMBER_SID,
                                    "params": {"PhoneNumber": NUMBER}, "headers": {}, "data": {}})
    for sensitive in (NUMBER, NUMBER_SID, TENANT, "synthetic-transport-secret",
                      "private synthetic payload", "https://synthetic.invalid/"):
        assert sensitive not in caplog.text


@pytest.mark.parametrize("expire_service_read", [1, 2])
@pytest.mark.asyncio
async def test_budget_expired_between_provider_reads_prevents_next_read(fx, monkeypatch, expire_service_read):
    now = [0.0]
    monkeypatch.setattr(enrollment.time, "monotonic", lambda: now[0])
    def transform(kind, count, value):
        if kind == "service" and count == expire_service_read:
            now[0] = 16.0
        return value
    fx.transform = transform
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.reason == enrollment.EnrollmentReason.BUDGET_EXCEEDED
    assert fx.counts["service"] == expire_service_read
    assert fx.counts.get("campaign", 0) == expire_service_read - 1
    assert "create" not in fx.calls


@pytest.mark.asyncio
async def test_membership_without_resource_or_absence_proof_never_creates(fx):
    fx.membership_return_none = True
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert "create" not in fx.calls
    assert result.status == enrollment.EnrollmentStatus.REJECTED
    assert result.reason == enrollment.EnrollmentReason.MEMBERSHIP_MALFORMED
    assert result.mutated is False and "create" not in fx.calls


@pytest.mark.parametrize("fault", [
    TwilioRestException(404, "/synthetic", "authorization fault", code=20003),
    TwilioRestException(403, "/synthetic", "denied", code=20404),
    TimeoutError("synthetic lookup timed out"),
])
@pytest.mark.asyncio
async def test_membership_fault_never_proves_absence(fx, fault):
    fx.membership_error = fault
    result = await enrollment.ensure_sms_sender_membership(TENANT, NUMBER)
    assert result.status == enrollment.EnrollmentStatus.UNCERTAIN
    assert result.mutated is False and "create" not in fx.calls


@pytest.mark.asyncio
async def test_cancel_queued_executor_operation_blocks_provider_io(fx, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original = asyncio.get_running_loop().run_in_executor
    # Queue a single executor operation, cancel while it is parked, then let it
    # run. This reproduces cancellation after scheduling and before execution.
    def parked(executor, function, *args):
        def worker():
            try:
                entered.set()
                release.wait(2)
                return function(*args)
            finally:
                completed.set()
        return original(executor, worker)
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", parked)
    task = asyncio.create_task(enrollment.ensure_sms_sender_membership(TENANT, NUMBER))
    while not entered.is_set():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    # A finite completion wait keeps the assertion after queued work exits.
    assert await original(None, completed.wait, 2)
    assert fx.calls == []


@pytest.mark.asyncio
async def test_cancel_after_fresh_campaign_prevents_create(fx):
    loop = asyncio.get_running_loop()
    def transform(kind, count, payload):
        if kind == "campaign" and count == 2:
            loop.call_soon_threadsafe(task.cancel)
        return payload
    fx.transform = transform
    task = asyncio.create_task(enrollment.ensure_sms_sender_membership(TENANT, NUMBER))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "create" not in fx.calls
