"""Regulatory number provisioning: bundle edge cases and error sanitization.

`provision_twilio_number` is exercised against a fake Twilio client so every
branch is observable — which calls happen, in what order, with which
parameters — and the endpoint's exception mapping is checked for the property
that matters: raw Twilio text (account SIDs, tokens, street addresses,
customer names) never reaches the client, only the canned messages do.
"""

import inspect
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import twilio.rest  # noqa: E402  (must exist so the fake can replace the real client)
from twilio.rest.api.v2010.account.address import AddressList  # noqa: E402
from twilio.rest.api.v2010.account.available_phone_number_country.local import LocalList  # noqa: E402
from twilio.rest.api.v2010.account.incoming_phone_number import IncomingPhoneNumberList  # noqa: E402
from twilio.rest.numbers.v2.regulatory_compliance.bundle import BundleContext, BundleList  # noqa: E402
from twilio.rest.numbers.v2.regulatory_compliance.bundle.item_assignment import ItemAssignmentList  # noqa: E402
from twilio.rest.numbers.v2.regulatory_compliance.regulation import RegulationList  # noqa: E402

from app.api import contractors as contractors_api  # noqa: E402
from app.config import settings as app_settings  # noqa: E402
from app.db import contractors as contractors_db  # noqa: E402


# ---------------------------------------------------------------------------
# Fake Twilio client — records every call, returns scripted results
# ---------------------------------------------------------------------------


class FakeBundleHandle:
    def __init__(self, client, sid):
        self._client = client
        self.sid = sid
        self.item_assignments = SimpleNamespace(create=self._assign)

    def _assign(self, **kwargs):
        self._client.calls.append(("item_assignment", self.sid, kwargs))
        return SimpleNamespace(sid="BV" + "0" * 32)

    def update(self, **kwargs):
        self._client.calls.append(("bundle_update", self.sid, kwargs))
        return SimpleNamespace(sid=self.sid, status=kwargs.get("status"))

    def fetch(self):
        status = (
            self._client.bundle_statuses.pop(0)
            if self._client.bundle_statuses
            else "pending-review"
        )
        self._client.calls.append(("bundle_fetch", self.sid, status))
        return SimpleNamespace(sid=self.sid, status=status)


class FakeBundles:
    def __init__(self, client):
        self._client = client

    def create(self, **kwargs):
        self._client.calls.append(("bundle_create", kwargs))
        return SimpleNamespace(sid="BU" + "1" * 32)

    def __call__(self, sid):
        return FakeBundleHandle(self._client, sid)


class FakeClient:
    """Stands in for twilio.rest.Client. Script results on the class before use."""

    instances = []
    regulations = [SimpleNamespace(sid="RN" + "2" * 32)]
    search_results = None  # list of lists, one per search call
    bundle_statuses = None  # list of statuses, one per fetch

    def __init__(self, account_sid, auth_token):
        type(self).instances.append(self)
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.calls = []
        # Per-instance copies so scripted lists are consumed per test.
        self.search_results = list(type(self).search_results or [])
        self.bundle_statuses = list(type(self).bundle_statuses or [])
        self.addresses = SimpleNamespace(create=self._create_address)
        self.incoming_phone_numbers = SimpleNamespace(create=self._purchase)
        self.numbers = SimpleNamespace(
            v2=SimpleNamespace(
                regulatory_compliance=SimpleNamespace(
                    regulations=SimpleNamespace(list=self._list_regulations),
                    bundles=FakeBundles(self),
                )
            )
        )

    def available_phone_numbers(self, country_code):
        client = self

        def _list(**kwargs):
            result = client.search_results.pop(0) if client.search_results else []
            client.calls.append(("search", country_code, kwargs, len(result)))
            return result

        return SimpleNamespace(local=SimpleNamespace(list=_list))

    def _list_regulations(self, **kwargs):
        self.calls.append(("regulations", kwargs))
        return list(type(self).regulations)

    def _create_address(self, **kwargs):
        self.calls.append(("address_create", kwargs))
        return SimpleNamespace(sid="AD" + "3" * 32)

    def _purchase(self, **kwargs):
        self.calls.append(("purchase", kwargs))
        return SimpleNamespace(sid="PN" + "4" * 32, phone_number=kwargs["phone_number"])


def _numbers(*values):
    return [SimpleNamespace(phone_number=v) for v in values]


@pytest.fixture
def fake_twilio(monkeypatch):
    FakeClient.instances = []
    FakeClient.regulations = [SimpleNamespace(sid="RN" + "2" * 32)]
    FakeClient.search_results = [_numbers("+4930123456")]
    FakeClient.bundle_statuses = ["twilio-approved"]
    monkeypatch.setattr(twilio.rest, "Client", FakeClient)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(contractors_db.asyncio, "sleep", fake_sleep)
    # provision_twilio_number imports settings locally, so patch the real object.
    monkeypatch.setattr(app_settings, "cloud_run_url", "https://kevin.example.test")
    monkeypatch.setattr(
        app_settings, "twilio_regulatory_contact_email", "compliance@example.test", raising=False
    )
    FakeClient.sleeps = sleeps
    return FakeClient


@pytest.fixture
def contractor_store(monkeypatch):
    store = {"doc": {}, "updates": []}

    async def fake_get_contractor(contractor_id):
        return dict(store["doc"]) if store["doc"] else None

    async def fake_update_contractor(contractor_id, updates):
        store["updates"].append((contractor_id, dict(updates)))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update_contractor)
    return store


def _german_business(**overrides):
    doc = {
        "contractor_id": "c-de",
        "twilio_number": "",
        "country_code": "DE",
        "owner_phone": "+4915112345678",
        "business_name": "Müller Sanitär GmbH",
        "business_address": "Hauptstraße 1",
        "business_city": "Berlin",
    }
    doc.update(overrides)
    return doc


def _only(calls, kind):
    return [c for c in calls if c[0] == kind]


# ---------------------------------------------------------------------------
# provision_twilio_number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_country_raises_before_any_twilio_call(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business(country_code="JP")

    with pytest.raises(Exception, match="Unsupported country: JP"):
        await contractors_db.provision_twilio_number("c-de", country_code="JP")

    assert fake_twilio.instances == []
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_regulatory_country_without_address_raises_before_any_twilio_call(
    fake_twilio, contractor_store
):
    contractor_store["doc"] = _german_business(business_address="")

    with pytest.raises(Exception, match="Business address and city required"):
        await contractors_db.provision_twilio_number("c-de", country_code="DE")

    # The client object is constructed before this guard (construction is
    # local, no I/O); what matters is that nothing is asked of Twilio.
    assert all(client.calls == [] for client in fake_twilio.instances)
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_non_regulatory_country_skips_the_bundle_and_buys_without_bundle_sid(
    fake_twilio, contractor_store
):
    contractor_store["doc"] = _german_business(
        country_code="GB", business_address="", business_city=""
    )
    fake_twilio.search_results = [_numbers("+442071234567")]

    number = await contractors_db.provision_twilio_number("c-gb", country_code="GB")

    assert number == "+442071234567"
    client = fake_twilio.instances[0]
    assert _only(client.calls, "regulations") == []
    assert _only(client.calls, "address_create") == []
    assert _only(client.calls, "bundle_create") == []
    (search,) = _only(client.calls, "search")
    assert search[1] == "GB"
    assert search[2] == {"voice_enabled": True, "limit": 1}  # no sms_enabled outside US/CA
    (purchase,) = _only(client.calls, "purchase")
    assert "bundle_sid" not in purchase[1]
    assert contractor_store["updates"] == [("c-gb", {"twilio_number": "+442071234567"})]


@pytest.mark.parametrize("country", ["US", "CA"])
@pytest.mark.asyncio
async def test_us_and_ca_searches_require_sms(fake_twilio, contractor_store, country):
    contractor_store["doc"] = _german_business(
        country_code=country, business_address="", business_city=""
    )
    fake_twilio.search_results = [_numbers("+16505551212")]

    await contractors_db.provision_twilio_number("c-na", country_code=country)

    (search,) = _only(fake_twilio.instances[0].calls, "search")
    assert search[2] == {"voice_enabled": True, "sms_enabled": True, "limit": 1}


@pytest.mark.asyncio
async def test_area_code_is_retried_without_it_when_nothing_is_found(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business(
        country_code="US", business_address="", business_city=""
    )
    fake_twilio.search_results = [[], _numbers("+14155551212")]

    number = await contractors_db.provision_twilio_number(
        "c-us", country_code="US", area_code="650"
    )

    assert number == "+14155551212"
    searches = _only(fake_twilio.instances[0].calls, "search")
    assert [s[2].get("area_code") for s in searches] == ["650", None]


@pytest.mark.asyncio
async def test_no_numbers_raises_with_the_country_name(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business()
    fake_twilio.search_results = [[], []]

    with pytest.raises(Exception, match="No phone numbers available in Germany"):
        await contractors_db.provision_twilio_number("c-de", country_code="DE", area_code="30")

    assert _only(fake_twilio.instances[0].calls, "purchase") == []
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_regulatory_country_creates_the_bundle_and_buys_with_it(
    fake_twilio, contractor_store
):
    contractor_store["doc"] = _german_business()

    number = await contractors_db.provision_twilio_number("c-de", country_code="DE")

    assert number == "+4930123456"
    client = fake_twilio.instances[0]
    kinds = [c[0] for c in client.calls]
    assert kinds == [
        "regulations",
        "address_create",
        "bundle_create",
        "item_assignment",
        "bundle_update",
        "bundle_fetch",
        "search",
        "purchase",
    ]
    (regs,) = _only(client.calls, "regulations")
    assert regs[1] == {"iso_country": "DE", "number_type": "local", "limit": 1}
    (address,) = _only(client.calls, "address_create")
    assert address[1] == {
        "friendly_name": "Müller Sanitär GmbH - Berlin",
        "street": "Hauptstraße 1",
        "city": "Berlin",
        "region": "",
        "postal_code": "",
        "iso_country": "DE",
        "customer_name": "Müller Sanitär GmbH",
    }
    (bundle,) = _only(client.calls, "bundle_create")
    assert bundle[1] == {
        "friendly_name": "Müller Sanitär GmbH - Germany number",
        "email": "compliance@example.test",
        "regulation_sid": "RN" + "2" * 32,
        "iso_country": "DE",
        "number_type": "local",
    }
    (assignment,) = _only(client.calls, "item_assignment")
    assert assignment[1:] == ("BU" + "1" * 32, {"object_sid": "AD" + "3" * 32})
    (update,) = _only(client.calls, "bundle_update")
    assert update[2] == {"status": "pending-review"}
    (purchase,) = _only(client.calls, "purchase")
    assert purchase[1]["bundle_sid"] == "BU" + "1" * 32
    assert contractor_store["updates"] == [("c-de", {"twilio_number": "+4930123456"})]


@pytest.mark.asyncio
async def test_provisionally_approved_bundle_is_accepted(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business()
    fake_twilio.bundle_statuses = ["pending-review", "provisionally-approved"]

    await contractors_db.provision_twilio_number("c-de", country_code="DE")

    client = fake_twilio.instances[0]
    assert len(_only(client.calls, "bundle_fetch")) == 2
    assert fake_twilio.sleeps == [2, 2]
    assert _only(client.calls, "purchase")[0][1]["bundle_sid"] == "BU" + "1" * 32


@pytest.mark.asyncio
async def test_rejected_bundle_raises_an_address_error_and_never_buys(
    fake_twilio, contractor_store
):
    contractor_store["doc"] = _german_business()
    fake_twilio.bundle_statuses = ["pending-review", "twilio-rejected"]

    with pytest.raises(
        Exception, match="rejected for Germany. Please verify your business address"
    ):
        await contractors_db.provision_twilio_number("c-de", country_code="DE")

    client = fake_twilio.instances[0]
    assert _only(client.calls, "search") == []
    assert _only(client.calls, "purchase") == []
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_bundle_still_pending_after_polling_is_used_anyway(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business()
    fake_twilio.bundle_statuses = ["pending-review"] * 15

    number = await contractors_db.provision_twilio_number("c-de", country_code="DE")

    client = fake_twilio.instances[0]
    assert number == "+4930123456"
    assert len(_only(client.calls, "bundle_fetch")) == 15
    assert fake_twilio.sleeps == [2] * 15
    assert _only(client.calls, "purchase")[0][1]["bundle_sid"] == "BU" + "1" * 32


@pytest.mark.asyncio
async def test_missing_regulation_raises_before_creating_an_address(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business()
    fake_twilio.regulations = []

    with pytest.raises(Exception, match="No Twilio regulations found for Germany local numbers"):
        await contractors_db.provision_twilio_number("c-de", country_code="DE")

    client = fake_twilio.instances[0]
    assert _only(client.calls, "address_create") == []
    assert _only(client.calls, "bundle_create") == []
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_purchase_wires_the_webhooks_from_cloud_run_url(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business(
        country_code="GB", business_address="", business_city=""
    )
    fake_twilio.search_results = [_numbers("+442071234567")]

    await contractors_db.provision_twilio_number("c-gb", country_code="GB")

    (purchase,) = _only(fake_twilio.instances[0].calls, "purchase")
    assert purchase[1] == {
        "phone_number": "+442071234567",
        "voice_url": "https://kevin.example.test/webhooks/twilio/incoming",
        "voice_method": "POST",
        "status_callback": "https://kevin.example.test/webhooks/twilio/status",
        "status_callback_method": "POST",
        "sms_url": "https://kevin.example.test/webhooks/twilio/mms-incoming",
        "sms_method": "POST",
    }
    # settings is frozen at first import, which another test module may have
    # done with a different env value; compare against what it actually holds.
    assert fake_twilio.instances[0].account_sid == app_settings.twilio_account_sid


# ---------------------------------------------------------------------------
# Endpoint: error sanitization
# ---------------------------------------------------------------------------

_RAW_FRAGMENTS = (
    "AC0123456789abcdef",
    "sk_live_SECRET",
    "Hauptstraße 1",
    "Mustermann",
    "api.twilio.com",
    "10115",
)


@pytest.mark.parametrize(
    "raised, expected",
    [
        (
            "Regulatory bundle rejected for Germany. Please verify your business address. (customer Max Mustermann, Hauptstraße 1, 10115)",
            "Address verification failed. Please check your business address.",
        ),
        (
            "HTTP 400 api.twilio.com: Address rejected: Hauptstraße 1 not found",
            "Address verification failed. Please check your business address.",
        ),
        (
            "No phone numbers available in Germany",
            "No phone numbers available in your area. Please try a different city.",
        ),
        (
            # Pins current behaviour: a missing regulation is reported as a numbers
            # problem. Semantically it is closer to "country not supported".
            "No Twilio regulations found for Germany local numbers",
            "No phone numbers available in your area. Please try a different city.",
        ),
        (
            "Regulatory contact email not configured for Germany number provisioning",
            "Failed to provision phone number. Please try again or contact support.",
        ),
        (
            "Unsupported country: JP",
            "Your country is not yet supported for number provisioning.",
        ),
        (
            "HTTP 500 from api.twilio.com for AC0123456789abcdef (auth sk_live_SECRET) street=Hauptstraße 1 customer=Mustermann",
            "Failed to provision phone number. Please try again or contact support.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_endpoint_returns_only_canned_messages(monkeypatch, raised, expected):
    async def fake_get_contractor(contractor_id):
        return _german_business(business_address="Hauptstraße 1", business_city="Berlin")

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def failing_provision(contractor_id, country_code="US"):
        raise Exception(raised)

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", failing_provision)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("c-de", request)

    assert response == {"status": "error", "message": expected}
    serialized = json.dumps(response, ensure_ascii=False)
    for fragment in _RAW_FRAGMENTS:
        assert fragment not in serialized, fragment


@pytest.mark.asyncio
async def test_endpoint_refuses_regulatory_country_without_address_before_calling_twilio(
    monkeypatch,
):
    async def fake_get_contractor(contractor_id):
        return _german_business(business_address="", business_city="Berlin")

    async def fake_update_contractor(contractor_id, updates):
        return True

    calls = []

    async def must_not_be_called(contractor_id, country_code="US"):
        # The endpoint swallows exceptions, so a raise here would be masked;
        # count instead and assert afterwards.
        calls.append((contractor_id, country_code))
        return "+4930000000"

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", must_not_be_called)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("c-de", request)

    assert response == {
        "status": "error",
        "message": "Business address and city are required for number provisioning in your country.",
    }
    assert calls == []


# ---------------------------------------------------------------------------
# Gaps found by the audit's mutation review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_number_short_circuits_before_any_twilio_use(fake_twilio, contractor_store):
    # The double-purchase guard: an account that already has a number must
    # never reach Twilio again.
    contractor_store["doc"] = _german_business(twilio_number="+4930999999")

    number = await contractors_db.provision_twilio_number("c-de", country_code="DE")

    assert number == "+4930999999"
    assert fake_twilio.instances == []
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_missing_contractor_raises_before_any_twilio_use(fake_twilio, contractor_store):
    contractor_store["doc"] = {}

    with pytest.raises(Exception, match="Contractor not found"):
        await contractors_db.provision_twilio_number("c-missing", country_code="DE")

    assert fake_twilio.instances == []


@pytest.mark.asyncio
async def test_empty_search_without_area_code_is_not_retried(fake_twilio, contractor_store):
    contractor_store["doc"] = _german_business(
        country_code="GB", business_address="", business_city=""
    )
    fake_twilio.search_results = [[], _numbers("+442071234567")]

    with pytest.raises(Exception, match="No phone numbers available in United Kingdom"):
        await contractors_db.provision_twilio_number("c-gb", country_code="GB")

    # One search only: the retry exists for a failed area-code preference.
    assert len(_only(fake_twilio.instances[0].calls, "search")) == 1
    assert _only(fake_twilio.instances[0].calls, "purchase") == []


@pytest.mark.asyncio
async def test_address_arm_takes_precedence_over_the_numbers_arm(monkeypatch):
    # A message matching both predicates must resolve to the first arm, so
    # the mapping order is pinned, not just the individual arms.
    async def fake_get_contractor(contractor_id):
        return _german_business()

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def failing_provision(contractor_id, country_code="US"):
        raise Exception("No phone numbers available at this address")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", failing_provision)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("c-de", request)

    assert response == {
        "status": "error",
        "message": "Address verification failed. Please check your business address.",
    }


# ---------------------------------------------------------------------------
# SDK fidelity: the kwargs production actually passes must bind to the real
# twilio client signatures. The fake above accepts anything; this is what
# keeps it honest.
# ---------------------------------------------------------------------------

_SDK_METHODS = {
    "regulations": RegulationList.list,
    "address_create": AddressList.create,
    "bundle_create": BundleList.create,
    "item_assignment": ItemAssignmentList.create,
    "bundle_update": BundleContext.update,
    "search": LocalList.list,
    "purchase": IncomingPhoneNumberList.create,
}


def _kwargs_of(call):
    # calls are ("kind", kwargs) or ("kind", sid, kwargs) or ("search", cc, kwargs, n)
    return next(part for part in call[1:] if isinstance(part, dict))


@pytest.mark.parametrize(
    "kind",
    [
        "regulations",
        "address_create",
        "bundle_create",
        "item_assignment",
        "bundle_update",
        "search",
        "purchase",
    ],
)
@pytest.mark.asyncio
async def test_production_kwargs_bind_to_the_real_sdk(fake_twilio, contractor_store, kind):
    contractor_store["doc"] = _german_business()

    await contractors_db.provision_twilio_number("c-de", country_code="DE")

    (call,) = _only(fake_twilio.instances[0].calls, kind)
    signature = inspect.signature(_SDK_METHODS[kind])
    signature.bind(None, **_kwargs_of(call))  # None stands in for self


# ---------------------------------------------------------------------------
# Regulatory contact email (twilio 9.x BundleList.create requires `email`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_is_created_with_the_configured_contact_email(
    fake_twilio, contractor_store, monkeypatch
):
    contractor_store["doc"] = _german_business()
    monkeypatch.setattr(
        app_settings, "twilio_regulatory_contact_email", "notices@example.test", raising=False
    )

    await contractors_db.provision_twilio_number("c-de", country_code="DE")

    (bundle,) = _only(fake_twilio.instances[0].calls, "bundle_create")
    assert bundle[1]["email"] == "notices@example.test"


@pytest.mark.asyncio
async def test_missing_contact_email_refuses_before_any_twilio_call(
    fake_twilio, contractor_store, monkeypatch
):
    # An unconfigured server must fail clearly, not with a TypeError inside
    # the executor, and must not create a regulation lookup, address or bundle.
    contractor_store["doc"] = _german_business()
    monkeypatch.setattr(app_settings, "twilio_regulatory_contact_email", "")

    with pytest.raises(Exception, match="Regulatory contact email not configured"):
        await contractors_db.provision_twilio_number("c-de", country_code="DE")

    # One client was constructed (before the guard) and asked nothing.
    assert len(fake_twilio.instances) == 1
    assert fake_twilio.instances[0].calls == []
    assert contractor_store["updates"] == []


@pytest.mark.asyncio
async def test_whitespace_only_contact_email_counts_as_unconfigured(
    fake_twilio, contractor_store, monkeypatch
):
    contractor_store["doc"] = _german_business()
    monkeypatch.setattr(app_settings, "twilio_regulatory_contact_email", "   ")

    with pytest.raises(Exception, match="Regulatory contact email not configured"):
        await contractors_db.provision_twilio_number("c-de", country_code="DE")

    assert fake_twilio.instances[0].calls == []


@pytest.mark.asyncio
async def test_contact_email_is_passed_stripped(fake_twilio, contractor_store, monkeypatch):
    contractor_store["doc"] = _german_business()
    monkeypatch.setattr(app_settings, "twilio_regulatory_contact_email", "  ops@example.test  ")

    await contractors_db.provision_twilio_number("c-de", country_code="DE")

    (bundle,) = _only(fake_twilio.instances[0].calls, "bundle_create")
    assert bundle[1]["email"] == "ops@example.test"
