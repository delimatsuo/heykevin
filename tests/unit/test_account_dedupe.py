"""Tests for duplicate-account prevention at signup.

Production accumulated 19 Apple IDs owning more than one contractor record, and
18 phone numbers with multiple accounts. Two compounding causes:

1. `owner_phone` is normalized when queried but never when stored, so the
   Firestore exact-match lookup could not find records written in raw formats
   like "(415) 555-1234".
2. Dedupe keyed only on `owner_phone`. Signups that reach account creation
   before the phone-entry step (22 records have no phone at all) skipped the
   check entirely, even though a verified `apple_user_id` was available.
"""

import pytest

from app.utils.phone import normalize_phone


# ---- Normalization on write ------------------------------------------------


def test_the_formats_found_in_production_all_normalize_to_one_value():
    """These are the shapes actually present in the contractors collection."""
    target = "+14155551234"
    for raw in [
        "+14155551234",
        "4155551234",
        "14155551234",
        "(415) 555-1234",
        "415-555-1234",
        "415.555.1234",
        " +1 415 555 1234 ",
    ]:
        assert normalize_phone(raw) == target, raw


def test_unparseable_input_returns_none_rather_than_guessing():
    assert normalize_phone("not-a-number") is None
    assert normalize_phone("") is None


# ---- Dedupe at the creation endpoint ---------------------------------------


@pytest.mark.asyncio
async def test_existing_apple_user_is_returned_not_duplicated(monkeypatch):
    """The main bug: a signup with no phone yet created a fresh record every time.

    apple_user_id comes from a verified Apple identity token, so it is a
    stronger identity key than owner_phone and cannot be spoofed by the caller.
    """
    from app.api import contractors as contractors_api

    created = []

    async def fake_create(data):
        created.append(data)
        return "brand-new-id"

    async def fake_by_apple(apple_user_id):
        if apple_user_id == "apple-existing":
            return {"contractor_id": "existing-id", "apple_user_id": apple_user_id}
        return None

    async def fake_by_phone(phone):
        return None

    async def fake_update(cid, updates):
        return True

    async def fake_uuid(cid, existing):
        return "uuid-1234"

    async def fake_enforce(request, apple_user_id, token):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fake_uuid)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )
    monkeypatch.setattr("app.db.contractors.get_contractor_by_owner_phone", fake_by_phone)

    body = contractors_api.ContractorCreate(
        owner_name="Test", business_name="Test Co", apple_user_id="apple-existing", apple_identity_token="tok"
    )
    result = await contractors_api.api_create_contractor(body, request=None)

    assert result["contractor_id"] == "existing-id"
    assert result["existing"] is True
    assert created == [], "a duplicate contractor was created"


@pytest.mark.asyncio
async def test_new_apple_user_still_creates_an_account(monkeypatch):
    from app.api import contractors as contractors_api

    created = []

    async def fake_create(data):
        created.append(data)
        return "brand-new-id"

    async def fake_by_apple(apple_user_id):
        return None

    async def fake_by_phone(phone):
        return None

    async def fake_update(cid, updates):
        return True

    async def fake_enforce(request, apple_user_id, token):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )
    monkeypatch.setattr("app.db.contractors.get_contractor_by_owner_phone", fake_by_phone)

    body = contractors_api.ContractorCreate(
        owner_name="Fresh", business_name="Fresh Co", apple_user_id="apple-brand-new", apple_identity_token="tok"
    )
    result = await contractors_api.api_create_contractor(body, request=None)

    assert result["contractor_id"] == "brand-new-id"
    assert len(created) == 1


@pytest.mark.asyncio
async def test_no_apple_id_does_not_match_records_missing_one(monkeypatch):
    """9 production records have no apple_user_id. An empty key must never
    collide with them and hand back somebody else's account."""
    from app.api import contractors as contractors_api

    calls = []

    async def fake_by_apple(apple_user_id):
        calls.append(apple_user_id)
        return {"contractor_id": "someone-else"}

    async def fake_by_phone(phone):
        return None

    async def fake_create(data):
        return "new-id"

    async def fake_update(cid, updates):
        return True

    async def fake_enforce(request, apple_user_id, token):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )
    monkeypatch.setattr("app.db.contractors.get_contractor_by_owner_phone", fake_by_phone)

    body = contractors_api.ContractorCreate(owner_name="Anon", business_name="Anon Co")
    result = await contractors_api.api_create_contractor(body, request=None)

    assert calls == [], "lookup must not run with an empty apple_user_id"
    assert result["contractor_id"] == "new-id"
