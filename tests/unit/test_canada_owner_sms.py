"""Synthetic CA consent/enrollment probes; any unstubbed provider access fails."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from twilio.base.exceptions import TwilioRestException

for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TELEGRAM_BOT_TOKEN"):
    os.environ.setdefault(name, "synthetic")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("USER_PHONE", "+14165550100")

from app.config import settings, PRODUCTION_GCP_PROJECT_ID, PRODUCTION_CLOUD_RUN_URL
from app.services import canada_sms_enrollment as enrollment
from app.services.canada_sms_consent import (
    valid_canada_consent, canada_consent_required, canada_delivery_ready, valid_canada_delivery_binding,
)
from app.services import owner_sms
from app.db import owner_sms as consent_db
from app.api.contractors import OwnerSmsConsentRequest, _redact_contractor, api_owner_sms_consent, ContractorUpdate
from app.db.contractors import PROTECTED_FIELDS

CID = "synthetic-ca-tenant"
ACCOUNT = "AC" + "1" * 32
SERVICE = "MG" + "2" * 32
OTHER = "MG" + "3" * 32
PN = "PN" + "4" * 32
SENDER = "+14165550100"
OWNER = "+16045550100"


def profile():
    return {
        "contractor_id": CID, "owner_phone": OWNER, "owner_phone_e164": OWNER,
        "twilio_number": SENDER, "country_code": "CA", "provisioned_country_code": "CA",
        "active": True, "number_provider": "twilio", "number_type": "local",
        "number_capabilities": {"voice": True, "sms": True}, "owner_sms_enabled": True,
        "owner_sms_opted_out": False, "owner_sms_opt_out_revision": 0,
        "owner_sms_consent": {"contractor_id": CID, "owner_phone_e164": OWNER, "twilio_number": SENDER,
            "disclosure_version": "ca-owner-sms-v1", "source": "ios_settings",
            "opt_out_revision": 0, "collected_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            "delivery_state": "verified", "account_sid": ACCOUNT, "messaging_service_sid": SERVICE,
            "verified_at": datetime.now(timezone.utc) - timedelta(milliseconds=500)},
    }


class Doc:
    def __init__(self, data, id=CID):
        self.id, self.data, self.exists = id, data, True
    def to_dict(self):
        return deepcopy(self.data)


class Query:
    def __init__(self, docs): self.docs = docs
    def where(self, **kwargs): return self
    def limit(self, limit): assert limit == 2; return self
    def stream(self, **kwargs): return iter(self.docs)


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def fail(*args, **kwargs): raise AssertionError("Unstubbed provider access")
    monkeypatch.setattr("google.cloud.firestore.Client", fail)
    monkeypatch.setattr(enrollment, "get_firestore_client", fail)
    monkeypatch.setattr(enrollment, "Client", fail)
    monkeypatch.setattr(consent_db, "get_firestore_client", fail)
    monkeypatch.setattr(owner_sms, "Client", fail)
    monkeypatch.setattr("app.db.contractors.get_contractor", fail)


@pytest.fixture
def world(monkeypatch):
    for key, value in {
        "canada_sms_enabled": True, "environment": "production",
        "firestore_project_id": PRODUCTION_GCP_PROJECT_ID, "cloud_run_url": PRODUCTION_CLOUD_RUN_URL,
        "twilio_account_sid": ACCOUNT, "production_twilio_account_sid": ACCOUNT,
        "canada_messaging_service_sid": SERVICE, "twilio_messaging_service_sid": OTHER,
    }.items(): monkeypatch.setattr(settings, key, value)
    data = profile()
    doc = Doc(data)
    db = SimpleNamespace(project=PRODUCTION_GCP_PROJECT_ID, collection=lambda _: Query([doc]))
    service_value = SimpleNamespace(sid=SERVICE, account_sid=ACCOUNT, use_inbound_webhook_on_number=True, usecase="notifications")
    incoming = SimpleNamespace(sid=PN, phone_number=SENDER, account_sid=ACCOUNT, origin="twilio", type="local",
        voice_url=PRODUCTION_CLOUD_RUN_URL + "/webhooks/twilio/incoming", voice_method="POST",
        status_callback=PRODUCTION_CLOUD_RUN_URL + "/webhooks/twilio/status", status_callback_method="POST",
        sms_url=PRODUCTION_CLOUD_RUN_URL + "/webhooks/twilio/mms-incoming", sms_method="POST",
        capabilities={"voice": True, "sms": True})
    member = SimpleNamespace(sid=PN, phone_number=SENDER, account_sid=ACCOUNT, service_sid=SERVICE, country_code="CA", capabilities=["Voice", "SMS"])
    client = MagicMock(username=ACCOUNT, account_sid=ACCOUNT)
    service = client.messaging.v1.services.return_value
    service.fetch.return_value = service_value
    client.messaging.v1.services.list.return_value = [service_value]
    client.incoming_phone_numbers.list.return_value = [incoming]
    service.phone_numbers.return_value.fetch.return_value = member
    monkeypatch.setattr(enrollment, "get_firestore_client", lambda: db)
    monkeypatch.setattr(enrollment, "Client", lambda *a, **kw: client)
    return SimpleNamespace(data=data, doc=doc, db=db, client=client, service=service, service_value=service_value, incoming=incoming, member=member)


def missing(): return TwilioRestException(status=404, uri="synthetic", code=20404)


@pytest.mark.asyncio
async def test_default_off_zero_io(monkeypatch):
    monkeypatch.setattr(settings, "canada_sms_enabled", False)
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.status == "disabled" and result.mutated is False


@pytest.mark.parametrize("key,value", [
    ("environment", "staging"), ("firestore_project_id", "other"),
    ("cloud_run_url", "https://other.example"), ("twilio_account_sid", OTHER),
    ("production_twilio_account_sid", "AC" + "9" * 32),
    ("canada_messaging_service_sid", ""), ("canada_messaging_service_sid", OTHER),
    ("canada_messaging_service_sid", SERVICE + "\n"),
])
@pytest.mark.asyncio
async def test_configuration_no_io(world, monkeypatch, key, value):
    monkeypatch.setattr(settings, key, value)
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert not result.enrolled
    world.client.incoming_phone_numbers.list.assert_not_called()


@pytest.mark.parametrize("number", ["+14155552671", "+12425550100", "+442079460000", "4165550100"])
@pytest.mark.asyncio
async def test_actual_canada_region(world, number):
    result = await enrollment.ensure_canada_sms_sender_membership(CID, number)
    assert result.reason == "invalid_ca_identity"
    world.client.incoming_phone_numbers.list.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("active", False), ("country_code", "US"), ("provisioned_country_code", "US"),
    ("number_provider", "other"), ("number_type", "mobile"), ("number_capabilities", {"voice": True}),
    ("owner_phone_e164", "+14155552671"), ("owner_phone_e164", "bad"),
    ("owner_sms_enabled", False), ("owner_sms_enabled", None),
    ("owner_sms_opted_out", True), ("owner_sms_opted_out", None),
    ("owner_sms_opt_out_revision", True), ("owner_sms_opt_out_revision", 1),
    ("deletion_requested_at", 1), ("deactivated_at", 1), ("deleted_app_detected_at", 1),
    ("number_released_at", 1), ("owner_sms_consent", None), ("owner_sms_consent", {}),
])
@pytest.mark.asyncio
async def test_assignment_policy(world, field, value):
    world.data[field] = value
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert not result.enrolled
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("contractor_id", "copied-tenant"), ("owner_phone_e164", SENDER), ("twilio_number", OWNER),
    ("disclosure_version", "old"), ("source", "patch"), ("opt_out_revision", True),
    ("collected_at", "2026-10-13"), ("collected_at", datetime.now()),
    ("collected_at", datetime.now(timezone.utc) + timedelta(days=1)),
])
def test_consent_binding(field, value):
    data = profile(); data["owner_sms_consent"][field] = value
    assert not valid_canada_consent(data)
    assert canada_consent_required(data)


@pytest.mark.asyncio
async def test_duplicate_and_cross_tenant(world):
    world.doc.id = "another-tenant"
    assert not (await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)).enrolled
    world.doc.id = CID
    world.db.collection = lambda _: Query([world.doc, world.doc])
    assert not (await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)).enrolled
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.parametrize("target", ["db", "client", "service", "membership"])
@pytest.mark.asyncio
async def test_client_and_resource_account_binding(world, target):
    if target == "db": world.db.project = "other"
    if target == "client": world.client.account_sid = "AC" + "9" * 32
    if target == "service": world.service_value.account_sid = "AC" + "9" * 32
    if target == "membership": world.member.account_sid = "AC" + "9" * 32
    assert not (await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)).enrolled
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_already_member_double_readback_and_no_mutation(world):
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.enrolled and result.status == "already_member" and result.mutated is False
    assert world.service.fetch.call_count == 2
    assert world.client.incoming_phone_numbers.list.call_count == 2
    world.service.phone_numbers.create.assert_not_called()
    world.client.incoming_phone_numbers.update.assert_not_called()
    world.service.update.assert_not_called()


@pytest.mark.asyncio
async def test_one_create_and_post_readback(world):
    world.service.phone_numbers.return_value.fetch.side_effect = [missing(), missing(), world.member]
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.enrolled and result.status == "enrolled" and result.mutated is True
    world.service.phone_numbers.create.assert_called_once_with(phone_number_sid=PN)
    assert world.client.incoming_phone_numbers.list.call_count == 3


@pytest.mark.asyncio
async def test_send_readback_never_creates(world):
    world.service.phone_numbers.return_value.fetch.side_effect = missing()
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER, allow_create=False)
    assert not result.enrolled
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_other_service_conflict_refuses_transfer(world):
    other_service = SimpleNamespace(sid=OTHER, account_sid=ACCOUNT)
    world.client.messaging.v1.services.list.return_value = [world.service_value, other_service]
    world.service.phone_numbers.return_value.fetch.side_effect = [missing(), SimpleNamespace(**{**vars(world.member), "service_sid": OTHER})]
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.reason == "other_service_conflict"
    world.service.phone_numbers.create.assert_not_called()
    world.service.phone_numbers.delete.assert_not_called()


@pytest.mark.parametrize("kind", ["voice", "sms", "service", "suppression", "assignment", "membership"])
@pytest.mark.asyncio
async def test_frozen_prewrite_drift(world, kind):
    def mutate():
        if kind == "voice": world.incoming.voice_url = "https://other.example"
        if kind == "sms": world.incoming.sms_url = "https://other.example"
        if kind == "service": world.service_value.usecase = "marketing"
        if kind == "suppression": world.service_value.advanced_opt_out = False
        if kind == "assignment": world.data["owner_sms_enabled"] = False
        if kind == "membership": world.member.country_code = "US"
        return None
    world.service.phone_numbers.return_value.fetch.side_effect = lambda: (mutate(), world.member)[1]
    assert not (await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)).enrolled
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_create_timeout_uncertain_no_retry(world):
    world.service.phone_numbers.return_value.fetch.side_effect = missing()
    world.service.phone_numbers.create.side_effect = TimeoutError()
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.status == "uncertain" and not result.enrolled and result.mutated is None
    world.service.phone_numbers.create.assert_called_once()


@pytest.mark.asyncio
async def test_post_create_readback_timeout_uncertain(world):
    world.service.phone_numbers.return_value.fetch.side_effect = [missing(), missing(), TimeoutError()]
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.status == "uncertain" and not result.enrolled
    world.service.phone_numbers.create.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_before_mutation(world):
    event = asyncio.Event()
    event.set()
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER, cancel_event=event)
    assert result.reason == "operation_cancelled"
    world.service.phone_numbers.create.assert_not_called()


def test_projection_redacts_record_and_patch_cannot_manufacture(world):
    data = profile(); data.pop("owner_sms_consent")
    assert _redact_contractor(data)["owner_sms_consent_required"] is True
    data = profile()
    assert "owner_sms_consent" not in _redact_contractor(data)
    assert _redact_contractor(data)["owner_sms_consent_required"] is False
    assert "owner_sms_consent" in PROTECTED_FIELDS
    assert "owner_sms_consent" not in ContractorUpdate(owner_sms_consent=data["owner_sms_consent"]).model_dump()
    assert not canada_consent_required({"country_code": "US"})


def consent_body(**changes):
    return {"disclosure_version": "ca-owner-sms-v1", "owner_phone_e164": OWNER,
        "twilio_number": SENDER, "expected_revision": 0, **changes}


@pytest.mark.parametrize("change", [{"source": "fake"}, {"expected_revision": True}, {"expected_revision": -1}, {"expected_revision": "0"}, {"disclosure_version": "old"}])
def test_endpoint_strict_body(change):
    with pytest.raises(ValidationError): OwnerSmsConsentRequest(**consent_body(**change))


@pytest.fixture
def transaction_world(world, monkeypatch):
    doc = world.doc
    doc.get = lambda **kwargs: doc
    tx = SimpleNamespace(update=lambda ref, update: ref.data.update(update))
    query = Query([doc])
    query.document = lambda _: doc
    world.db.collection = lambda _: query
    world.db.transaction = lambda: tx
    monkeypatch.setattr(consent_db, "get_firestore_client", lambda: world.db)
    monkeypatch.setattr(consent_db.firestore, "transactional", lambda f: f)
    async def get(_):
        return deepcopy(world.data)
    monkeypatch.setattr("app.api.contractors.get_contractor", get)
    return world


@pytest.mark.asyncio
async def test_transaction_success_enables_without_revision_reset(transaction_world):
    world = transaction_world
    world.data["owner_sms_enabled"] = False
    world.data.pop("owner_sms_consent")
    ok, result = await consent_db.collect_canada_owner_sms_consent(CID, **consent_body())
    assert ok and result["status"] == "pending"
    assert result["accepted_consent"]["delivery_state"] == "pending"
    assert valid_canada_consent(world.data) and not canada_delivery_ready(world.data)
    assert canada_consent_required(world.data) is True
    assert world.data["owner_sms_opt_out_revision"] == 0
    assert world.data["owner_sms_opted_out"] is False


@pytest.mark.parametrize("field,value", [("owner_phone_e164", SENDER), ("twilio_number", OWNER), ("expected_revision", 1)])
@pytest.mark.asyncio
async def test_transaction_rejects_stale_snapshot(transaction_world, field, value):
    before = deepcopy(transaction_world.data)
    ok, result = await consent_db.collect_canada_owner_sms_consent(CID, **consent_body(**{field: value}))
    assert not ok and result["error"] == "stale_consent_snapshot"
    assert before == transaction_world.data


@pytest.mark.parametrize("field,value", [("owner_sms_opted_out", True), ("deletion_requested_at", 1), ("active", False), ("provisioned_country_code", "US")])
@pytest.mark.asyncio
async def test_transaction_stop_lifecycle_hold(transaction_world, field, value):
    transaction_world.data[field] = value
    before = deepcopy(transaction_world.data)
    assert not (await consent_db.collect_canada_owner_sms_consent(CID, **consent_body()))[0]
    assert before == transaction_world.data


@pytest.mark.asyncio
async def test_endpoint_cross_tenant_no_transaction(world):
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id="other"))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_send_exact_ca_service_and_stop_disclosure(world, monkeypatch):
    async def get(_): return deepcopy(world.data)
    monkeypatch.setattr("app.db.contractors.get_contractor", get)
    client = MagicMock(); client.messages.create.return_value = SimpleNamespace(sid="synthetic")
    monkeypatch.setattr(owner_sms, "Client", lambda *a, **k: client)
    assert await owner_sms.send_owner_sms(CID, "Synthetic call summary") is True
    payload = client.messages.create.call_args.kwargs
    assert payload["from_"] == SENDER and payload["to"] == OWNER
    assert payload["messaging_service_sid"] == SERVICE
    assert payload["body"].count("Reply STOP to stop texts.") == 1
    assert "revision=0" in payload["status_callback"]
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.parametrize("kind", ["off", "consent", "stop", "appoff", "lifecycle", "readback", "fresh_stop"])
@pytest.mark.asyncio
async def test_send_ca_fail_closed(world, monkeypatch, kind):
    if kind == "off": monkeypatch.setattr(settings, "canada_sms_enabled", False)
    if kind == "consent": world.data.pop("owner_sms_consent")
    if kind == "stop": world.data["owner_sms_opted_out"] = True
    if kind == "appoff": world.data["owner_sms_enabled"] = False
    if kind == "lifecycle": world.data["deleted_app_detected_at"] = 1
    if kind == "readback": world.service.phone_numbers.return_value.fetch.side_effect = TimeoutError()
    calls = 0
    async def get(_):
        nonlocal calls
        calls += 1
        if kind == "fresh_stop" and calls > 1: world.data["owner_sms_opted_out"] = True
        return deepcopy(world.data)
    monkeypatch.setattr("app.db.contractors.get_contractor", get)
    assert await owner_sms.send_owner_sms(CID, "Synthetic") is None


@pytest.mark.asyncio
async def test_cancellation_during_provider_read_prevents_write(world):
    event = asyncio.Event()
    def fetch():
        event.set()
        raise missing()
    world.service.phone_numbers.return_value.fetch.side_effect = fetch
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER, cancel_event=event)
    assert not result.enrolled and result.reason == "operation_cancelled"
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.parametrize("kind", ["stop", "routing", "service"])
@pytest.mark.asyncio
async def test_postwrite_drift_never_claims_success(world, kind):
    world.service.phone_numbers.return_value.fetch.side_effect = [missing(), missing(), world.member]
    def create(**kwargs):
        if kind == "stop": world.data["owner_sms_opted_out"] = True
        if kind == "routing": world.incoming.sms_url = "https://other.example"
        if kind == "service": world.service_value.usecase = "marketing"
    world.service.phone_numbers.create.side_effect = create
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.status == "uncertain" and not result.enrolled
    world.service.phone_numbers.create.assert_called_once()


@pytest.mark.asyncio
async def test_service_inventory_bounded_fail_closed(world):
    world.client.messaging.v1.services.list.return_value = [world.service_value] * enrollment.MAX_SERVICES
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.reason == "service_inventory_limit"
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_transaction_duplicate_assignment_rejected(transaction_world):
    query = Query([transaction_world.doc, transaction_world.doc])
    query.document = lambda _: transaction_world.doc
    transaction_world.db.collection = lambda _: query
    assert not (await consent_db.collect_canada_owner_sms_consent(CID, **consent_body()))[0]


@pytest.mark.asyncio
async def test_endpoint_transaction_success_responds_authoritative(transaction_world):
    world = transaction_world
    world.data["owner_sms_enabled"] = False
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    response = await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert response["owner_sms_consent_required"] is False
    assert response["owner_sms_enabled"] is True and response["owner_sms_opt_out_revision"] == 0


@pytest.mark.asyncio
async def test_ca_provider_21610_preserves_existing_callback_contract(world, monkeypatch):
    async def get(_): return deepcopy(world.data)
    monkeypatch.setattr("app.db.contractors.get_contractor", get)
    client = MagicMock()
    client.messages.create.side_effect = TwilioRestException(status=400, uri="synthetic", code=21610)
    monkeypatch.setattr(owner_sms, "Client", lambda *a, **k: client)
    seen = []
    async def transition(cid, **kwargs):
        seen.append((cid, kwargs))
        return True, {}
    monkeypatch.setattr(consent_db, "apply_owner_sms_consent_transition", transition)
    assert await owner_sms.send_owner_sms(CID, "Synthetic") is None
    assert seen[0][1]["opt_out"] is True
    assert seen[0][1]["expected_revision"] == 0
    assert seen[0][1]["expected_owner_phone"] == OWNER
    assert seen[0][1]["expected_twilio_number"] == SENDER


@pytest.mark.asyncio
async def test_all_three_canada_recovery_hooks_preserve_assignment(world, monkeypatch):
    from app.db import contractors as db
    from app.api import contractors as api
    seen = []
    async def get(_): return deepcopy(world.data)
    async def update(_, values): world.data.update(values); return True
    async def ca_enroll(cid, number, **kwargs):
        seen.append((cid, number, world.data["twilio_number"]))
        return enrollment.CanadaEnrollmentResult("disabled", "feature_disabled")
    monkeypatch.setattr(db, "get_contractor", get)
    monkeypatch.setattr(api, "get_contractor", get)
    monkeypatch.setattr(db, "update_contractor", update)
    monkeypatch.setattr(enrollment, "ensure_canada_sms_sender_membership", ca_enroll)
    monkeypatch.setattr("app.services.sms_sender_enrollment.settings.sms_sender_enrollment_enabled", False)
    purchase_client = MagicMock()
    purchase_client.available_phone_numbers.return_value.local.list.return_value = [SimpleNamespace(phone_number=SENDER)]
    purchase_client.incoming_phone_numbers.create.return_value = world.incoming
    monkeypatch.setattr("twilio.rest.Client", lambda *a, **kw: purchase_client)
    world.data["twilio_number"] = ""
    assert await db.provision_twilio_number(CID, country_code="CA") == SENDER
    assert seen == [(CID, SENDER, SENDER)]
    assert await db.provision_twilio_number(CID, country_code="CA") == SENDER
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    assert (await api.api_provision_number(CID, request))["phone_number"] == SENDER
    assert len(seen) == 3
    purchase_client.incoming_phone_numbers.create.assert_called_once()


@pytest.mark.asyncio
async def test_generic_creation_strips_forged_consent(world, monkeypatch):
    from app.db import contractors as db
    seen = []
    fake_db = SimpleNamespace(collection=lambda _: SimpleNamespace(add=lambda data: (seen.append(deepcopy(data)), SimpleNamespace(id=CID))))
    monkeypatch.setattr(db, "get_firestore_client", lambda: fake_db)
    payload = {"owner_phone": OWNER, "country_code": "CA", "owner_sms_consent": profile()["owner_sms_consent"]}
    assert await db.create_contractor(payload) == CID
    assert "owner_sms_consent" not in seen[0]
    assert seen[0]["owner_sms_enabled"] is False


@pytest.mark.parametrize("value", [False, "true", "false", 1, 0, {}, []])
@pytest.mark.asyncio
async def test_malformed_supplied_suppression_fails_closed(world, value):
    world.service_value.suppression_enabled = value
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert not result.enrolled and result.reason == "service_suppression_invalid"
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.parametrize("path", ["already_member", "concurrent_member", "created"])
@pytest.mark.asyncio
async def test_cancellation_on_final_membership_never_claims_enrolled(world, path):
    event = asyncio.Event()
    calls = 0
    def fetch():
        nonlocal calls
        calls += 1
        if path in ("created", "concurrent_member") and calls == 1:
            raise missing()
        if path == "created" and calls == 2:
            raise missing()
        # Last membership fetch cancels while returning an otherwise valid response.
        if ((path in ("already_member", "concurrent_member") and calls == 2)
                or (path == "created" and calls == 3)):
            event.set()
        return world.member
    world.service.phone_numbers.return_value.fetch.side_effect = fetch
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER, cancel_event=event)
    assert not result.enrolled and result.reason == "operation_cancelled"
    if path == "created":
        assert result.status == "uncertain" and result.mutated is None
        world.service.phone_numbers.create.assert_called_once_with(phone_number_sid=PN)
    else:
        assert result.status == "rejected" and result.mutated is False
        world.service.phone_numbers.create.assert_not_called()


@pytest.mark.parametrize("drift", [
    "stop", "revision", "appoff", "owner", "sender", "tenant", "lifecycle",
    "consent", "missing", "readerror",
])
@pytest.mark.asyncio
async def test_consent_ack_rejects_drift_during_enrollment(transaction_world, monkeypatch, drift):
    from fastapi import HTTPException
    world = transaction_world
    reads = []
    async def enroll(*args, **kwargs):
        if drift == "stop":
            world.data["owner_sms_opted_out"] = True
            world.data["owner_sms_opt_out_revision"] += 1
        if drift == "revision": world.data["owner_sms_opt_out_revision"] += 1
        if drift == "appoff": world.data["owner_sms_enabled"] = False
        if drift == "owner": world.data["owner_phone_e164"] = SENDER
        if drift == "sender": world.data["twilio_number"] = OWNER
        if drift == "tenant": world.data["contractor_id"] = "another-tenant"
        if drift == "lifecycle": world.data["deactivated_at"] = 1
        if drift == "consent": world.data.pop("owner_sms_consent")
        return enrollment.CanadaEnrollmentResult("already_member", "readback_verified", True)
    async def get(cid):
        reads.append(cid)
        if drift == "readerror": raise RuntimeError("Synthetic read failure")
        if drift == "missing": return None
        return deepcopy(world.data)
    monkeypatch.setattr(enrollment, "ensure_canada_sms_sender_membership", enroll)
    monkeypatch.setattr("app.api.contractors.get_contractor", get)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    with pytest.raises(HTTPException) as error:
        await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert error.value.status_code == 409
    assert reads == ([CID] if drift in ("missing", "readerror", "tenant") else [])


@pytest.mark.asyncio
async def test_consent_ack_reads_current_state_after_enrollment(transaction_world, monkeypatch):
    world = transaction_world
    order = []
    async def enroll(*args, **kwargs):
        order.append("enrollment")
        return enrollment.CanadaEnrollmentResult("already_member", "readback_verified", True)
    async def get(cid):
        order.append("readback")
        return deepcopy(world.data)
    monkeypatch.setattr(enrollment, "ensure_canada_sms_sender_membership", enroll)
    monkeypatch.setattr("app.api.contractors.get_contractor", get)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    response = await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert order == ["enrollment", "readback"]
    assert response == {"status": "ok", "owner_sms_enabled": True, "owner_sms_opted_out": False,
        "owner_sms_opt_out_revision": 0, "owner_sms_settings_version": 1, "owner_sms_consent_required": False}


@pytest.mark.parametrize("status,reason", [
    ("disabled", "feature_disabled"), ("uncertain", "budget_exceeded"),
    ("uncertain", "create_failed_uncertain"), ("skipped", "membership_absent"),
    ("rejected", "invalid_configuration"), ("exception", "timeout"),
])
@pytest.mark.asyncio
async def test_pending_enrollment_keeps_review_and_suppresses_send(transaction_world, monkeypatch, status, reason):
    from fastapi import HTTPException
    world = transaction_world
    if status == "disabled": monkeypatch.setattr(settings, "canada_sms_enabled", False)
    async def enroll(*args, **kwargs):
        if status == "exception": raise TimeoutError("Synthetic enrollment timeout")
        return enrollment.CanadaEnrollmentResult(status, reason)
    monkeypatch.setattr(enrollment, "ensure_canada_sms_sender_membership", enroll)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    original_facts = {k: v for k, v in world.data["owner_sms_consent"].items()
        if k not in ("delivery_state", "account_sid", "messaging_service_sid", "verified_at")}
    with pytest.raises(HTTPException) as error:
        await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert error.value.status_code == 409
    assert world.data["owner_sms_consent"]["delivery_state"] == "pending"
    assert valid_canada_consent(world.data) and not canada_delivery_ready(world.data)
    for key, value in original_facts.items(): assert world.data["owner_sms_consent"][key] == value
    assert _redact_contractor(world.data)["owner_sms_consent_required"] is True
    async def get(_): return deepcopy(world.data)
    monkeypatch.setattr("app.db.contractors.get_contractor", get)
    assert await owner_sms.send_owner_sms(CID, "Synthetic pending message") is None
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_initial_disabled_then_activation_retry_promotes_existing_member(transaction_world, monkeypatch):
    from fastapi import HTTPException
    world = transaction_world
    world.data.pop("owner_sms_consent")
    monkeypatch.setattr(settings, "canada_sms_enabled", False)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    with pytest.raises(HTTPException) as error:
        await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert error.value.status_code == 409
    world.service.fetch.assert_not_called()
    saved = deepcopy(world.data["owner_sms_consent"])
    assert saved["delivery_state"] == "pending" and canada_consent_required(world.data)
    monkeypatch.setattr(settings, "canada_sms_enabled", True)
    response = await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert response["owner_sms_consent_required"] is False
    verified = world.data["owner_sms_consent"]
    assert verified["collected_at"] == saved["collected_at"]
    assert verified["delivery_state"] == "verified"
    assert verified["account_sid"] == ACCOUNT and verified["messaging_service_sid"] == SERVICE
    assert isinstance(verified["verified_at"], datetime) and verified["verified_at"].tzinfo is not None
    assert canada_delivery_ready(world.data) and not canada_consent_required(world.data)
    world.service.phone_numbers.create.assert_not_called()


@pytest.mark.asyncio
async def test_pending_retry_creates_once_then_promotes(transaction_world):
    world = transaction_world
    world.data["owner_sms_consent"]["delivery_state"] = "pending"
    world.service.phone_numbers.return_value.fetch.side_effect = [missing(), missing(), world.member]
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=CID))
    response = await api_owner_sms_consent(CID, OwnerSmsConsentRequest(**consent_body()), request)
    assert response["owner_sms_consent_required"] is False and canada_delivery_ready(world.data)
    world.service.phone_numbers.create.assert_called_once_with(phone_number_sid=PN)


@pytest.mark.parametrize("field,value", [
    ("delivery_state", "pending"), ("delivery_state", None),
    ("account_sid", "AC" + "8" * 32), ("messaging_service_sid", OTHER),
    ("verified_at", None), ("verified_at", "2026-10-13"),
    ("verified_at", datetime.now()), ("verified_at", datetime.now(timezone.utc) + timedelta(days=1)),
])
def test_delivery_readiness_requires_verified_server_binding(world, field, value):
    world.data["owner_sms_consent"][field] = value
    assert valid_canada_consent(world.data)
    assert not canada_delivery_ready(world.data)
    assert canada_consent_required(world.data)


def test_pending_state_alone_cannot_forge_readiness(world):
    pending = world.data["owner_sms_consent"]
    for key in ("account_sid", "messaging_service_sid", "verified_at"): pending.pop(key)
    pending["delivery_state"] = "verified"
    assert not canada_delivery_ready(world.data)
    assert "owner_sms_consent" not in ContractorUpdate(owner_sms_consent=pending).model_dump()


@pytest.mark.parametrize("field,value", [
    ("canada_sms_enabled", False), ("canada_messaging_service_sid", "MG" + "8" * 32),
    ("twilio_account_sid", "AC" + "8" * 32), ("environment", "staging"),
])
def test_current_config_drift_invalidates_delivery_readiness(world, monkeypatch, field, value):
    assert canada_delivery_ready(world.data)
    monkeypatch.setattr(settings, field, value)
    assert not canada_delivery_ready(world.data) and canada_consent_required(world.data)


def test_verified_appoff_keeps_normal_preference_control(world):
    world.data["owner_sms_enabled"] = False
    assert canada_delivery_ready(world.data)
    assert _redact_contractor(world.data)["owner_sms_consent_required"] is False


@pytest.mark.parametrize("countries", [
    {}, {"country_code": "US"}, {"provisioned_country_code": "US"},
    {"country_code": "US", "provisioned_country_code": "US"},
    {"country_code": "BR", "provisioned_country_code": "BR"},
    {"country_code": "GB", "provisioned_country_code": "GB"},
    {"country_code": "CA", "provisioned_country_code": "US"},
    {"country_code": "US", "provisioned_country_code": "CA"}, {"country_code": "CA"},
])
def test_only_explicit_ca_profiles_enter_consent_review_flow(countries):
    data = {"contractor_id": CID, "owner_sms_enabled": False, **countries}
    assert not canada_consent_required(data)
    assert _redact_contractor(data)["owner_sms_consent_required"] is False
    assert _redact_contractor(data)["owner_sms_enabled"] is False


@pytest.mark.parametrize("drift", ["stop", "revision", "sender", "owner", "consent_time", "service", "account", "appoff"])
@pytest.mark.asyncio
async def test_transactional_promotion_rechecks_accepted_binding(transaction_world, monkeypatch, drift):
    world = transaction_world
    ok, pending = await consent_db.collect_canada_owner_sms_consent(CID, **consent_body())
    assert ok
    accepted = deepcopy(pending["accepted_consent"])
    if drift == "stop": world.data["owner_sms_opted_out"] = True
    if drift == "revision": world.data["owner_sms_opt_out_revision"] += 1
    if drift == "sender": world.data["twilio_number"] = OWNER
    if drift == "owner": world.data["owner_phone_e164"] = SENDER
    if drift == "consent_time": world.data["owner_sms_consent"]["collected_at"] += timedelta(microseconds=1)
    if drift == "service": monkeypatch.setattr(settings, "canada_messaging_service_sid", "MG" + "8" * 32)
    if drift == "account": monkeypatch.setattr(settings, "twilio_account_sid", "AC" + "8" * 32)
    if drift == "appoff": world.data["owner_sms_enabled"] = False
    before = deepcopy(world.data)
    promoted, _ = await consent_db.verify_canada_owner_sms_delivery(
        CID, accepted_consent=accepted, account_sid=ACCOUNT, messaging_service_sid=SERVICE,
    )
    assert not promoted and world.data == before
    assert world.data["owner_sms_consent"]["delivery_state"] == "pending"


@pytest.mark.asyncio
async def test_recovery_enrollment_does_not_promote_pending_consent(world):
    world.data["owner_sms_consent"]["delivery_state"] = "pending"
    before = deepcopy(world.data["owner_sms_consent"])
    result = await enrollment.ensure_canada_sms_sender_membership(CID, SENDER)
    assert result.enrolled and world.data["owner_sms_consent"] == before
    assert canada_consent_required(world.data)
