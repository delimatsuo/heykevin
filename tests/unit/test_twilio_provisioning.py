"""Twilio number provisioning safety checks."""

from types import SimpleNamespace

import pytest

from app.api import contractors as contractors_api
from app.db import contractors as contractors_db


@pytest.mark.asyncio
async def test_provision_twilio_number_reuses_existing_number(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+16505551212",
        }

    async def fail_update(*args, **kwargs):
        raise AssertionError("existing-number provisioning must not update Firestore")

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fail_update)

    number = await contractors_db.provision_twilio_number("contractor-1")

    assert number == "+16505551212"


@pytest.mark.asyncio
async def test_provision_number_endpoint_reuses_existing_number(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+16505551212",
            "country_code": "US",
        }

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": "+16505551212",
        "existing": True,
    }


@pytest.mark.asyncio
async def test_contractor_patch_cannot_change_twilio_number(monkeypatch):
    updates_seen = {}

    async def fake_update_contractor(contractor_id, updates):
        updates_seen["contractor_id"] = contractor_id
        updates_seen["updates"] = updates
        return True

    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_update_contractor(
        "contractor-1",
        contractors_api.ContractorUpdate(
            mode="personal",
            twilio_number="+16505559999",
        ),
        request,
    )

    assert response == {"status": "ok"}
    assert updates_seen == {
        "contractor_id": "contractor-1",
        "updates": {"mode": "personal"},
    }
