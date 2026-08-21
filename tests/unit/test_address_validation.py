"""Post-call address validation (Geocoding API).

Two live calls produced "175 Fox Run Road" and "175 Foxburn Road" for the
same job — a contractor can drive to a street that does not exist. The fix
(owner-approved in principle 2026-08-20): geocode the extracted address
post-call and flag what does not resolve. Never on the live call —
voice_pipeline forbids address read-back deliberately.

The trap, measured live 2026-08-20 (memory: project-address-validation): a
nonsense street returns status OK with the town centroid. `status` is not a
validity signal; the real signals are partial_match, location_type, and the
returned address. Validation is best-effort: no key, no address, or any
fetch failure must never block or fail the post-call path.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services import address_validation as av


def _geocode_payload(*, status="OK", location_type="ROOFTOP",
                     partial_match=None, formatted="175 Fox Run Rd, Hudson, NH 03051, USA"):
    result = {
        "formatted_address": formatted,
        "geometry": {"location_type": location_type},
    }
    if partial_match is not None:
        result["partial_match"] = partial_match
    return {"status": status, "results": [] if status == "ZERO_RESULTS" else [result]}


# ---------------------------------------------------------------------------
# interpret_geocode_response (pure)
# ---------------------------------------------------------------------------


def test_rooftop_match_resolves():
    out = av.interpret_geocode_response(_geocode_payload())
    assert out["resolved"] is True
    assert out["formatted_address"] == "175 Fox Run Rd, Hudson, NH 03051, USA"
    assert out["location_type"] == "ROOFTOP"


def test_range_interpolated_resolves():
    out = av.interpret_geocode_response(
        _geocode_payload(location_type="RANGE_INTERPOLATED")
    )
    assert out["resolved"] is True


def test_town_centroid_for_garbage_street_does_not_resolve():
    """The measured trap: '9999 Zzyzx Nonexistent Road' returns status OK,
    APPROXIMATE, partial_match true, town-only formatted address."""
    out = av.interpret_geocode_response(
        _geocode_payload(
            status="OK", location_type="APPROXIMATE",
            partial_match=True, formatted="Nashua, NH, USA",
        )
    )
    assert out["resolved"] is False
    assert out["partial_match"] is True


def test_partial_match_alone_does_not_resolve():
    out = av.interpret_geocode_response(
        _geocode_payload(partial_match=True)
    )
    assert out["resolved"] is False


def test_geometric_center_does_not_resolve():
    out = av.interpret_geocode_response(
        _geocode_payload(location_type="GEOMETRIC_CENTER")
    )
    assert out["resolved"] is False


def test_zero_results_does_not_resolve():
    out = av.interpret_geocode_response(_geocode_payload(status="ZERO_RESULTS"))
    assert out["resolved"] is False
    assert out["formatted_address"] == ""


def test_error_status_does_not_resolve():
    out = av.interpret_geocode_response({"status": "OVER_QUERY_LIMIT", "results": []})
    assert out["resolved"] is False


# ---------------------------------------------------------------------------
# validate_address (fetch wrapper — best-effort, never raises)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_api_key_returns_none_without_fetching(monkeypatch):
    calls = []

    async def fake_fetch(address, api_key):
        calls.append(address)
        return _geocode_payload()

    monkeypatch.setattr(av, "_fetch_geocode", fake_fetch)

    assert await av.validate_address("175 Fox Run Road", api_key="") is None
    assert calls == []


@pytest.mark.asyncio
async def test_blank_address_returns_none_without_fetching(monkeypatch):
    calls = []

    async def fake_fetch(address, api_key):
        calls.append(address)
        return _geocode_payload()

    monkeypatch.setattr(av, "_fetch_geocode", fake_fetch)

    assert await av.validate_address("   ", api_key="k") is None
    assert calls == []


@pytest.mark.asyncio
async def test_fetch_failure_returns_none_never_raises(monkeypatch):
    async def failing_fetch(address, api_key):
        raise RuntimeError("network down")

    monkeypatch.setattr(av, "_fetch_geocode", failing_fetch)

    assert await av.validate_address("175 Fox Run Road", api_key="k") is None


@pytest.mark.asyncio
async def test_successful_validation_returns_interpretation(monkeypatch):
    async def fake_fetch(address, api_key):
        return _geocode_payload()

    monkeypatch.setattr(av, "_fetch_geocode", fake_fetch)

    out = await av.validate_address("175 Fox Run Road, Hudson NH", api_key="k")
    assert out["resolved"] is True


# ---------------------------------------------------------------------------
# post_call integration: enrichment + owner SMS warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_address_enrichment_attaches_validation(monkeypatch):
    from app.services import post_call

    async def fake_validate(address, api_key):
        assert address == "175 Foxburn Road"
        return {"resolved": False, "formatted_address": "Nashua, NH, USA",
                "location_type": "APPROXIMATE", "partial_match": True}

    monkeypatch.setattr(post_call, "validate_address", fake_validate)
    monkeypatch.setattr(post_call.settings, "google_maps_api_key", "k")

    job_data = {"address": "175 Foxburn Road"}
    await post_call._validate_job_address(job_data)

    assert job_data["address_validation"]["resolved"] is False


@pytest.mark.asyncio
async def test_enrichment_skipped_without_key(monkeypatch):
    from app.services import post_call

    called = []

    async def fake_validate(address, api_key):
        called.append(address)
        return {"resolved": True}

    monkeypatch.setattr(post_call, "validate_address", fake_validate)
    monkeypatch.setattr(post_call.settings, "google_maps_api_key", "")

    job_data = {"address": "175 Foxburn Road"}
    await post_call._validate_job_address(job_data)

    assert called == []
    assert "address_validation" not in job_data


@pytest.mark.asyncio
async def test_enrichment_tolerates_validator_none(monkeypatch):
    from app.services import post_call

    async def fake_validate(address, api_key):
        return None

    monkeypatch.setattr(post_call, "validate_address", fake_validate)
    monkeypatch.setattr(post_call.settings, "google_maps_api_key", "k")

    job_data = {"address": "175 Foxburn Road"}
    await post_call._validate_job_address(job_data)

    assert "address_validation" not in job_data


@pytest.mark.asyncio
async def test_owner_sms_warns_on_unresolved_address():
    from app.services.post_call import _format_contractor_sms

    body = await _format_contractor_sms(
        job_data={
            "call_type": "service_request",
            "caller_name": "Pat",
            "caller_phone": "+15551234567",
            "address": "175 Foxburn Road",
            "address_validation": {
                "resolved": False, "formatted_address": "Nashua, NH, USA",
                "location_type": "APPROXIMATE", "partial_match": True,
            },
        },
        job_id="j1",
        contractor={"contractor_id": "c1"},
    )
    assert "175 Foxburn Road" in body
    assert "may not resolve" in body


@pytest.mark.asyncio
async def test_owner_sms_silent_on_resolved_address():
    from app.services.post_call import _format_contractor_sms

    body = await _format_contractor_sms(
        job_data={
            "call_type": "service_request",
            "caller_name": "Pat",
            "caller_phone": "+15551234567",
            "address": "175 Fox Run Rd",
            "address_validation": {
                "resolved": True,
                "formatted_address": "175 Fox Run Rd, Hudson, NH 03051, USA",
                "location_type": "RANGE_INTERPOLATED", "partial_match": False,
            },
        },
        job_id="j1",
        contractor={"contractor_id": "c1"},
    )
    assert "may not resolve" not in body
