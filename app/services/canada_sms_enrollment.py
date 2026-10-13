"""Bounded Canada-only owner SMS enrollment. No purchases or routing mutations."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import threading
import time

from google.cloud.firestore_v1.base_query import FieldFilter
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.config import PRODUCTION_GCP_PROJECT_ID, settings
from app.db.firestore_client import get_firestore_client
from app.services.canada_sms_consent import (
    canada_configuration_valid, canadian_assignment_valid, is_canadian_phone, valid_canada_consent,
)
from app.services.sms_sender_enrollment import (
    _SILENT_TRANSPORT_LOGGER, _validate_incoming_resource, _validate_membership_capabilities,
)

MAX_BUDGET_SECONDS = 15.0
MAX_SERVICES = 20
SERVICE_FIELDS = (
    "sid", "account_sid", "use_inbound_webhook_on_number", "usecase", "inbound_request_url",
    "inbound_method", "fallback_url", "fallback_method", "status_callback", "sticky_sender",
    "area_code_geomatch", "mms_converter", "smart_encoding", "scaler", "validity_period",
    "synchronous_validation", "advanced_opt_out", "opt_out_management", "suppression_enabled",
)


@dataclass(frozen=True)
class CanadaEnrollmentResult:
    status: str
    reason: str
    enrolled: bool = False
    mutated: bool | None = False

    def to_dict(self):
        return vars(self).copy()


def configuration_valid() -> bool:
    return canada_configuration_valid()


class _Refused(Exception):
    pass


async def ensure_canada_sms_sender_membership(
    contractor_id: str, number: str, *, cancel_event=None, allow_create: bool = True,
) -> CanadaEnrollmentResult:
    """Read back exact CA assignment and service; at most one membership creation.

    Sending passes allow_create=False so delivery never mutates sender enrollment.
    Cancellation signals are checked inside executor closures before every I/O.
    """
    if settings.canada_sms_enabled is not True:
        return CanadaEnrollmentResult("disabled", "feature_disabled")
    if not configuration_valid():
        return CanadaEnrollmentResult("rejected", "invalid_configuration")
    if not isinstance(contractor_id, str) or not contractor_id or not is_canadian_phone(number):
        return CanadaEnrollmentResult("skipped", "invalid_ca_identity")
    started = time.monotonic()
    cancelled = threading.Event()
    attempted = threading.Event()
    account = settings.twilio_account_sid
    service_sid = settings.canada_messaging_service_sid

    def guard():
        if cancelled.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise _Refused("operation_cancelled")
        if time.monotonic() - started >= MAX_BUDGET_SECONDS:
            raise _Refused("budget_exceeded")
        if not configuration_valid() or account != settings.twilio_account_sid or service_sid != settings.canada_messaging_service_sid:
            raise _Refused("configuration_drift")

    def work():
        guard()
        db = get_firestore_client()
        if db.project != PRODUCTION_GCP_PROJECT_ID:
            raise _Refused("client_identity_mismatch")
        client = Client(account, settings.twilio_auth_token, http_client=TwilioHttpClient(
            timeout=2, max_retries=0, logger=_SILENT_TRANSPORT_LOGGER,
        ))
        if client.username != account or client.account_sid != account:
            raise _Refused("client_identity_mismatch")
        service = client.messaging.v1.services(service_sid)

        def assignment():
            guard()
            docs = list(db.collection("contractors").where(
                filter=FieldFilter("twilio_number", "==", number)
            ).limit(2).stream(timeout=2, retry=None))
            if len(docs) != 1 or docs[0].id != contractor_id:
                raise _Refused("assignment_ambiguous")
            data = docs[0].to_dict()
            data["contractor_id"] = docs[0].id
            if not canadian_assignment_valid(data) or not valid_canada_consent(data):
                raise _Refused("assignment_or_consent_invalid")
            return data

        def incoming():
            guard()
            values = client.incoming_phone_numbers.list(phone_number=number, limit=2)
            if len(values) != 1:
                raise _Refused("incoming_number_ambiguous")
            valid, _, snapshot = _validate_incoming_resource(values[0], number, account)
            if not valid:
                raise _Refused("incoming_routing_invalid")
            return values[0], snapshot

        def service_snapshot():
            guard()
            value = service.fetch()
            if (value.sid != service_sid or value.account_sid != account
                    or value.use_inbound_webhook_on_number is not True
                    or getattr(value, "usecase", None) != "notifications"):
                raise _Refused("service_invalid")
            snapshot = {key: getattr(value, key, None) for key in SERVICE_FIELDS}
            # SDK-absent properties remain unknown; supplied values must be true booleans.
            if any(snapshot[key] is not None and snapshot[key] is not True
                   for key in ("advanced_opt_out", "opt_out_management", "suppression_enabled")):
                raise _Refused("service_suppression_invalid")
            return snapshot

        def membership(pn, sid=service_sid):
            guard()
            try:
                value = client.messaging.v1.services(sid).phone_numbers(pn).fetch()
            except TwilioRestException as exc:
                if exc.status == 404 and exc.code == 20404:
                    return None
                raise
            if (value.sid != pn or value.account_sid != account or value.service_sid != sid
                    or value.phone_number != number or value.country_code != "CA"
                    or not _validate_membership_capabilities(value.capabilities)):
                raise _Refused("membership_invalid")
            return value

        def no_conflict(pn):
            guard()
            services = client.messaging.v1.services.list(limit=MAX_SERVICES, page_size=MAX_SERVICES)
            if len(services) >= MAX_SERVICES:
                raise _Refused("service_inventory_limit")
            seen = set()
            for value in services:
                sid = getattr(value, "sid", None)
                if (not isinstance(sid, str) or not re.fullmatch(r"MG[0-9a-fA-F]{32}", sid)
                        or value.account_sid != account or sid in seen):
                    raise _Refused("service_inventory_invalid")
                seen.add(sid)
                if sid != service_sid and membership(pn, sid) is not None:
                    raise _Refused("other_service_conflict")
            if service_sid not in seen:
                raise _Refused("service_inventory_invalid")

        first_assignment = assignment()
        first_incoming, frozen = incoming()
        first_service = service_snapshot()
        pn = first_incoming.sid
        present = membership(pn) is not None
        no_conflict(pn)

        def recheck():
            if assignment() != first_assignment:
                raise _Refused("assignment_drift")
            value, _ = incoming()
            if not frozen.matches(value) or service_snapshot() != first_service:
                raise _Refused("routing_or_service_drift")
            no_conflict(pn)

        recheck()
        if present:
            if membership(pn) is None:
                raise _Refused("membership_drift")
            guard()
            return CanadaEnrollmentResult("already_member", "readback_verified", True)
        if not allow_create:
            return CanadaEnrollmentResult("skipped", "membership_absent")
        # Fresh membership avoids a duplicate creation after a concurrent enrollment.
        if membership(pn) is not None:
            recheck()
            guard()
            return CanadaEnrollmentResult("already_member", "readback_verified", True)
        guard()
        attempted.set()
        try:
            service.phone_numbers.create(phone_number_sid=pn)
        except Exception:
            # Even a successful-looking readback cannot attribute a timed-out write.
            return CanadaEnrollmentResult("uncertain", "create_failed_uncertain", False, None)
        recheck()
        if membership(pn) is None:
            return CanadaEnrollmentResult("uncertain", "readback_absent", False, True)
        guard()
        return CanadaEnrollmentResult("enrolled", "readback_verified", True, True)

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=MAX_BUDGET_SECONDS)
    except asyncio.CancelledError:
        cancelled.set()
        raise
    except asyncio.TimeoutError:
        cancelled.set()
        return CanadaEnrollmentResult("uncertain", "budget_exceeded", False, None if attempted.is_set() else False)
    except _Refused as exc:
        return CanadaEnrollmentResult("uncertain" if attempted.is_set() else "rejected", str(exc), False, None if attempted.is_set() else False)
    except Exception:
        return CanadaEnrollmentResult("uncertain", "read_failed", False, None if attempted.is_set() else False)
