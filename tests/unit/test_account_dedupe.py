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

import time
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

    async def fake_by_phone(phone, *, country_code="US", region=None):
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

    async def fake_by_phone(phone, *, country_code="US", region=None):
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

    async def fake_by_phone(phone, *, country_code="US", region=None):
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


# ---- International normalization & dedupe ---------------------------------


def test_international_national_formats_normalize_with_proper_region():
    # UK
    assert normalize_phone("020 7946 0958", default_region="GB") == "+442079460958"
    assert normalize_phone("020 7946 0958", default_region="US") is None
    assert normalize_phone("020 7946 0958", default_region=None) is None

    # BR
    assert normalize_phone("(11) 98765-4321", default_region="BR") == "+5511987654321"
    assert normalize_phone("(11) 98765-4321", default_region="US") is None
    assert normalize_phone("(11) 98765-4321", default_region=None) is None

    # DE
    assert normalize_phone("030 1234567", default_region="DE") == "+49301234567"
    assert normalize_phone("030 1234567", default_region="US") is None
    assert normalize_phone("030 1234567", default_region=None) is None

    # E.164 formats are region-independent
    for e164 in ["+442079460958", "+5511987654321", "+49301234567", "+14155551234"]:
        assert normalize_phone(e164, default_region=None) == e164
        assert normalize_phone(e164, default_region="US") == e164
        assert normalize_phone(e164, default_region="GB") == e164


def test_get_contractor_by_owner_phone_is_keyword_only():
    from app.db import contractors as contractors_db
    import inspect
    sig = inspect.signature(contractors_db.get_contractor_by_owner_phone)
    assert sig.parameters["country_code"].kind == inspect.Parameter.KEYWORD_ONLY
    assert "region" not in sig.parameters, "region alias parameter must be removed"


class _FakeDoc:
    def __init__(self, data, doc_id):
        self._data = dict(data)
        self.id = doc_id

    def to_dict(self):
        return dict(self._data)


class _FakeDB:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.query_count = 0
        self.requested_limits: list[int | None] = []
        self.delivered_counts: list[int] = []

    def collection(self, name):
        parent = self

        class _FakeDocRef:
            def __init__(self, doc_id):
                self.id = doc_id

            def get(self):
                for d in parent.docs:
                    if d.get("contractor_id") == self.id:
                        return _FakeDoc(d, self.id)
                class _MissingDoc:
                    exists = False
                    def to_dict(self):
                        return None
                return _MissingDoc()

            def set(self, data, merge=False):
                for d in parent.docs:
                    if d.get("contractor_id") == self.id:
                        d.update(data)
                        return
                parent.docs.append(dict(data, contractor_id=self.id))

        class _FakeCollection:
            def __init__(self, filters=None, limit_val=None):
                self.filters = list(filters or [])
                self.limit_val = limit_val

            def where(self, filter=None, *args, **kwargs):
                f_name = getattr(filter, "field_path", None)
                f_val = getattr(filter, "value", None)
                return _FakeCollection(self.filters + [(f_name, f_val)], self.limit_val)

            def limit(self, n):
                return _FakeCollection(self.filters, n)

            def document(self, doc_id):
                return _FakeDocRef(doc_id)

            def add(self, data):
                new_id = f"created-doc-{len(parent.docs) + 1}"
                parent.docs.append(dict(data, contractor_id=new_id))
                return (time.time(), _FakeDocRef(new_id))

            def stream(self):
                parent.query_count += 1
                parent.requested_limits.append(self.limit_val)
                matches = []
                for d in parent.docs:
                    match = True
                    for f_name, f_val in self.filters:
                        if f_name and d.get(f_name) != f_val:
                            match = False
                            break
                    if match:
                        matches.append(_FakeDoc(d, d.get("contractor_id", "doc-id")))
                delivered = matches[:self.limit_val] if self.limit_val is not None else matches
                parent.delivered_counts.append(len(delivered))
                return delivered

        return _FakeCollection()


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_queries_companion_and_canonical_e164(monkeypatch):
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "uk-1",
            "owner_phone": "+442079460958",
            "owner_phone_e164": "+442079460958",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is not None
    assert res["contractor_id"] == "uk-1"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_cross_format_legacy_us(monkeypatch):
    """Stored legacy formats and submitted formats differ yet normalize to the same number."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {"contractor_id": "us-1", "owner_phone": "(415) 555-1234", "country_code": "US", "active": True},
        {"contractor_id": "us-2", "owner_phone": "+1 415-555-9876", "country_code": "US", "active": True},
        {"contractor_id": "us-3", "owner_phone": "14155554321", "country_code": "US", "active": True},
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    # 1. Stored "(415) 555-1234" vs submitted bare digits "4155551234"
    res1 = await contractors_db.get_contractor_by_owner_phone("4155551234", country_code="US")
    assert res1 is not None
    assert res1["contractor_id"] == "us-1"

    # 2. Stored "+1 415-555-9876" vs submitted "(415) 555-9876"
    res2 = await contractors_db.get_contractor_by_owner_phone("(415) 555-9876", country_code="US")
    assert res2 is not None
    assert res2["contractor_id"] == "us-2"

    # 3. Stored "14155554321" vs submitted "4155554321"
    res3 = await contractors_db.get_contractor_by_owner_phone("4155554321", country_code="US")
    assert res3 is not None
    assert res3["contractor_id"] == "us-3"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_cross_format_legacy_gb(monkeypatch):
    """Stored legacy UK formats and submitted formats differ yet normalize to the same number."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {"contractor_id": "gb-1", "owner_phone": "020 7946 0958", "country_code": "GB", "active": True},
        {"contractor_id": "gb-2", "owner_phone": "02079460999", "country_code": "GB", "active": True},
        {"contractor_id": "gb-3", "owner_phone": "+44 20 7946 0111", "country_code": "GB", "active": True},
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    # 1. Stored "020 7946 0958" vs submitted unspaced "02079460958"
    res1 = await contractors_db.get_contractor_by_owner_phone("02079460958", country_code="GB")
    assert res1 is not None
    assert res1["contractor_id"] == "gb-1"

    # 2. Stored "02079460999" vs submitted spaced "020 7946 0999"
    res2 = await contractors_db.get_contractor_by_owner_phone("020 7946 0999", country_code="GB")
    assert res2 is not None
    assert res2["contractor_id"] == "gb-2"

    # 3. Stored "+44 20 7946 0111" vs submitted "02079460111"
    res3 = await contractors_db.get_contractor_by_owner_phone("02079460111", country_code="GB")
    assert res3 is not None
    assert res3["contractor_id"] == "gb-3"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_wrong_country_first_correct_country_second(monkeypatch):
    """Evaluating bounded documents must examine beyond wrong-country first row to find the matching second row."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "us-first-doc",
            "owner_phone": "2079460958",
            "country_code": "US",
            "active": True,
        },
        {
            "contractor_id": "gb-second-doc",
            "owner_phone": "2079460958",
            "country_code": "GB",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    # GB request matches 2079460958 with country_code GB
    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is not None
    assert res["contractor_id"] == "gb-second-doc", "Must find the second matching GB row"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_overflow_raises_ambiguity_error(monkeypatch):
    """Document cap overflow (>5 records) must raise PhoneDedupeAmbiguityError."""
    from app.db import contractors as contractors_db
    from app.db.contractors import PhoneDedupeAmbiguityError

    # 6 documents (exceeds cap of 5)
    overflow_docs = [
        {
            "contractor_id": f"doc-{i}",
            "owner_phone": "2079460958",
            "country_code": "GB",
            "active": True,
        }
        for i in range(6)
    ]
    fake_db = _FakeDB(overflow_docs)
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    with pytest.raises(PhoneDedupeAmbiguityError, match="ambiguous unexamined"):
        await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")


@pytest.mark.asyncio
async def test_api_create_contractor_overflow_fails_closed_with_409_and_no_mutation(monkeypatch):
    """API must catch PhoneDedupeAmbiguityError and return HTTP 409 with zero mutations."""
    from app.api import contractors as contractors_api
    from app.db import contractors as contractors_db
    from fastapi import HTTPException

    overflow_docs = [
        {
            "contractor_id": f"doc-{i}",
            "owner_phone": "2079460958",
            "country_code": "GB",
            "active": True,
        }
        for i in range(6)
    ]
    fake_db = _FakeDB(overflow_docs)
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    async def fail_create(data):
        pytest.fail("create_contractor must not be called on overflow ambiguity")

    async def fail_update(cid, updates):
        pytest.fail("update_contractor must not be called on overflow ambiguity")

    async def fail_ensure_uuid(cid, existing):
        pytest.fail("ensure_subscription_uuid must not be called on overflow ambiguity")

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fail_ensure_uuid)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                owner_name="Alice",
                business_name="UK Services",
                owner_phone="020 7946 0958",
                country_code="GB",
                apple_user_id="apple-user-alice",
                apple_identity_token="tok",
            ),
            request=None,
        )

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_multiple_verified_canonical_matches_raises_ambiguity_error(monkeypatch):
    """Multiple active records matching the same candidate and verifying to same canonical phone must fail closed."""
    from app.db import contractors as contractors_db
    from app.db.contractors import PhoneDedupeAmbiguityError

    fake_db = _FakeDB([
        {
            "contractor_id": "gb-1",
            "owner_phone": "020 7946 0958",
            "country_code": "GB",
            "active": True,
        },
        {
            "contractor_id": "gb-2",
            "owner_phone": "020 7946 0958",
            "country_code": "GB",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    with pytest.raises(PhoneDedupeAmbiguityError, match="multiple matching"):
        await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_two_gb_rows_across_different_legacy_spellings_raises_ambiguity(monkeypatch):
    """Two active records for same canonical phone stored as different spellings ('020 7946 0958' and '02079460958')
    must raise PhoneDedupeAmbiguityError rather than returning one arbitrarily.
    """
    from app.db import contractors as contractors_db
    from app.db.contractors import PhoneDedupeAmbiguityError

    fake_db = _FakeDB([
        {
            "contractor_id": "gb-spaced",
            "owner_phone": "020 7946 0958",
            "country_code": "GB",
            "active": True,
        },
        {
            "contractor_id": "gb-unspaced",
            "owner_phone": "02079460958",
            "country_code": "GB",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    with pytest.raises(PhoneDedupeAmbiguityError, match="multiple matching"):
        await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_companion_and_legacy_different_docs_raises_ambiguity(monkeypatch):
    """One record found via companion query and a different record found via legacy query must raise PhoneDedupeAmbiguityError."""
    from app.db import contractors as contractors_db
    from app.db.contractors import PhoneDedupeAmbiguityError

    fake_db = _FakeDB([
        {
            "contractor_id": "companion-doc",
            "owner_phone_e164": "+442079460958",
            "owner_phone": "+442079460958",
            "active": True,
        },
        {
            "contractor_id": "legacy-doc",
            "owner_phone": "020 7946 0958",
            "country_code": "GB",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    with pytest.raises(PhoneDedupeAmbiguityError, match="multiple matching"):
        await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_duplicate_companion_exact_query_raises_ambiguity(monkeypatch):
    """Duplicate records in owner_phone_e164 exact query must raise PhoneDedupeAmbiguityError."""
    from app.db import contractors as contractors_db
    from app.db.contractors import PhoneDedupeAmbiguityError

    fake_db = _FakeDB([
        {
            "contractor_id": "c-1",
            "owner_phone_e164": "+442079460958",
            "active": True,
        },
        {
            "contractor_id": "c-2",
            "owner_phone_e164": "+442079460958",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    with pytest.raises(PhoneDedupeAmbiguityError, match="multiple matching"):
        await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_duplicate_canonical_exact_query_raises_ambiguity(monkeypatch):
    """Duplicate records in owner_phone canonical exact query must raise PhoneDedupeAmbiguityError."""
    from app.db import contractors as contractors_db
    from app.db.contractors import PhoneDedupeAmbiguityError

    fake_db = _FakeDB([
        {
            "contractor_id": "can-1",
            "owner_phone": "+442079460958",
            "active": True,
        },
        {
            "contractor_id": "can-2",
            "owner_phone": "+442079460958",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    with pytest.raises(PhoneDedupeAmbiguityError, match="multiple matching"):
        await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_same_doc_reachable_via_companion_and_canonical_is_single_match(monkeypatch):
    """The same document ID matched via companion and canonical queries must deduplicate to a single unique match."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "single-doc",
            "owner_phone_e164": "+442079460958",
            "owner_phone": "+442079460958",
            "country_code": "GB",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is not None
    assert res["contractor_id"] == "single-doc"


@pytest.mark.asyncio
async def test_api_create_contractor_cross_representation_ambiguity_fails_closed_with_409(monkeypatch):
    """API must catch cross-representation ambiguity, return HTTP 409, and perform zero mutations."""
    from app.api import contractors as contractors_api
    from app.db import contractors as contractors_db
    from fastapi import HTTPException

    fake_db = _FakeDB([
        {
            "contractor_id": "gb-spaced",
            "owner_phone": "020 7946 0958",
            "country_code": "GB",
            "active": True,
        },
        {
            "contractor_id": "gb-unspaced",
            "owner_phone": "02079460958",
            "country_code": "GB",
            "active": True,
        },
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    async def fail_create(data):
        pytest.fail("create_contractor must not be called on cross-representation ambiguity")

    async def fail_update(cid, updates):
        pytest.fail("update_contractor must not be called on cross-representation ambiguity")

    async def fail_ensure_uuid(cid, existing):
        pytest.fail("ensure_subscription_uuid must not be called on cross-representation ambiguity")

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fail_ensure_uuid)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                owner_name="Alice",
                business_name="UK Services",
                owner_phone="020 7946 0958",
                country_code="GB",
                apple_user_id="apple-user-alice",
                apple_identity_token="tok",
            ),
            request=None,
        )

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail


@pytest.mark.parametrize("hostile_apple_id", [True, 123, 45.6, ["apple-id"], {"apple": "id"}])
@pytest.mark.asyncio
async def test_api_create_contractor_hostile_persisted_apple_user_id_fails_closed_with_409(hostile_apple_id, monkeypatch):
    """Non-string persisted apple_user_id (e.g. bool, int, list, dict) must fail closed as unbound with HTTP 409 and zero mutations."""
    from app.api import contractors as contractors_api
    from app.db import contractors as contractors_db
    from fastapi import HTTPException

    fake_db = _FakeDB([
        {
            "contractor_id": "hostile-apple-doc",
            "owner_phone": "+442079460958",
            "owner_phone_e164": "+442079460958",
            "apple_user_id": hostile_apple_id,
            "country_code": "GB",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    async def fail_create(data):
        pytest.fail("create_contractor must not be called on hostile apple_user_id fail-closed")

    async def fail_update(cid, updates):
        pytest.fail("update_contractor must not be called on hostile apple_user_id fail-closed")

    async def fail_ensure_uuid(cid, existing):
        pytest.fail("ensure_subscription_uuid must not be called on hostile apple_user_id fail-closed")

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fail_ensure_uuid)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                owner_name="Alice",
                business_name="UK Services",
                owner_phone="020 7946 0958",
                country_code="GB",
                apple_user_id="apple-user-alice",
                apple_identity_token="tok",
            ),
            request=None,
        )

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_hostile_country_types(monkeypatch):
    """Hostile stored country types (int, list, dict, None) must not raise AttributeError."""
    from app.db import contractors as contractors_db

    # 1. National phone with hostile int country_code -> must not raise and must not match
    fake_db1 = _FakeDB([
        {
            "contractor_id": "int-country-doc",
            "owner_phone": "020 7946 0958",
            "country_code": 123,
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db1)
    res1 = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res1 is None

    # 2. National phone with hostile list country_code -> must not raise and must not match
    fake_db2 = _FakeDB([
        {
            "contractor_id": "list-country-doc",
            "owner_phone": "020 7946 0958",
            "country_code": ["GB"],
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db2)
    res2 = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res2 is None

    # 3. National phone with hostile dict country_code -> must not raise and must not match
    fake_db3 = _FakeDB([
        {
            "contractor_id": "dict-country-doc",
            "owner_phone": "020 7946 0958",
            "country_code": {"country": "GB"},
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db3)
    res3 = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res3 is None

    # 4. Self-declaring E.164 phone with hostile int country_code -> does not raise and DOES match
    fake_db4 = _FakeDB([
        {
            "contractor_id": "e164-hostile-country-doc",
            "owner_phone": "+442079460958",
            "country_code": 999,
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db4)
    res4 = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res4 is not None
    assert res4["contractor_id"] == "e164-hostile-country-doc"


@pytest.mark.parametrize("unsupported_country", ["XX", "UNSUPPORTED", "ZZ"])
@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_nonempty_unsupported_country_rejected(unsupported_country, monkeypatch):
    """Persisted legacy row with nonempty unsupported string country_code (e.g. 'XX', 'UNSUPPORTED')
    with national owner_phone '020 7946 0958' and GB request must return None,
    while a self-declaring E.164 stored owner_phone remains accepted.
    """
    from app.db import contractors as contractors_db

    # 1. National format with unsupported country -> must return None
    fake_db1 = _FakeDB([
        {
            "contractor_id": "unsupported-country-national-doc",
            "owner_phone": "020 7946 0958",
            "country_code": unsupported_country,
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db1)
    res1 = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res1 is None, f"National phone with country_code={unsupported_country} must be rejected"

    # 2. Self-declaring E.164 with unsupported country -> accepted
    fake_db2 = _FakeDB([
        {
            "contractor_id": "unsupported-country-e164-doc",
            "owner_phone": "+442079460958",
            "country_code": unsupported_country,
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db2)
    res2 = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res2 is not None
    assert res2["contractor_id"] == "unsupported-country-e164-doc"


@pytest.mark.parametrize("hostile_country", [123, ["US"], {"country": "US"}, None, 45.6])
@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_direct_db_hostile_country_arg(hostile_country, monkeypatch):
    """Direct DB call get_contractor_by_owner_phone with non-string country_code must not raise and deterministically fallback to US."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "us-doc",
            "owner_phone": "+14155551234",
            "country_code": "US",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    # 4155551234 resolves with US fallback to +14155551234
    res = await contractors_db.get_contractor_by_owner_phone("4155551234", country_code=hostile_country)
    assert res is not None
    assert res["contractor_id"] == "us-doc"


@pytest.mark.parametrize("hostile_country", [123, ["US"], {"country": "US"}, None, 45.6])
@pytest.mark.asyncio
async def test_create_contractor_direct_db_hostile_country_persists_us_fallback(hostile_country, monkeypatch):
    """Direct DB call create_contractor with non-string country_code must not raise and deterministically persist US."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    data = {
        "business_name": "Test Co",
        "owner_name": "Bob",
        "owner_phone": "(415) 555-1234",
        "country_code": hostile_country,
    }
    cid = await contractors_db.create_contractor(data)
    assert cid is not None
    assert data["country_code"] == "US"
    assert data["owner_phone"] == "+14155551234"
    assert data["owner_phone_e164"] == "+14155551234"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_gb_does_not_match_us_national_collision(monkeypatch):
    """GB national number 020 7946 0958 (+442079460958) has 10 national digits (2079460958),
    which coincides with US Maine area code 207-946-0958 (+12079460958).
    A legacy record stored under US country must NOT match the GB request.
    """
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "us-maine-doc",
            "owner_phone": "2079460958",
            "country_code": "US",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    # GB caller presents 020 7946 0958
    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is None, "US Maine record must not be returned for GB national number request"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_stored_country_mismatch_rejected(monkeypatch):
    """If candidate matches a doc whose stored country resolves to a different canonical number, reject it."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "br-doc",
            "owner_phone": "020 7946 0958",
            "country_code": "BR",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is None


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_missing_country_ambiguous_national_rejected(monkeypatch):
    """Non-E.164 legacy records with missing/unsupported country cannot be safely bound and must be rejected."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "ambiguous-doc",
            "owner_phone": "020 7946 0958",
            "country_code": "",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is None


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_missing_country_e164_accepted(monkeypatch):
    """E.164 legacy records are self-declaring and match even if stored country_code is blank."""
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {
            "contractor_id": "unambiguous-e164-doc",
            "owner_phone": "+442079460958",
            "country_code": "",
            "active": True,
        }
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is not None
    assert res["contractor_id"] == "unambiguous-e164-doc"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_query_ceiling_on_miss(monkeypatch):
    """Total Firestore queries on a complete miss must not exceed ceiling of 8 queries,
    every single query must causally request exactly DOC_QUERY_CAP+1 (=6) limit,
    each delivered count must be <= 6, and aggregate delivered rows must be <= 48.
    """
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    # GB miss
    fake_db.query_count = 0
    fake_db.requested_limits.clear()
    fake_db.delivered_counts.clear()
    res_gb = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res_gb is None
    assert fake_db.query_count <= 8, f"GB miss exceeded query ceiling: {fake_db.query_count}"
    assert len(fake_db.requested_limits) == fake_db.query_count
    assert all(lim == 6 for lim in fake_db.requested_limits), f"Every query must request limit=6: {fake_db.requested_limits}"
    assert len(fake_db.delivered_counts) == fake_db.query_count
    assert all(c <= 6 for c in fake_db.delivered_counts), f"Delivered count per query must be <= 6: {fake_db.delivered_counts}"
    assert sum(fake_db.delivered_counts) <= 48, f"Aggregate delivered rows must be <= 48: {sum(fake_db.delivered_counts)}"

    # US miss
    fake_db.query_count = 0
    fake_db.requested_limits.clear()
    fake_db.delivered_counts.clear()
    res_us = await contractors_db.get_contractor_by_owner_phone("(415) 555-1234", country_code="US")
    assert res_us is None
    assert fake_db.query_count <= 8, f"US miss exceeded query ceiling: {fake_db.query_count}"
    assert len(fake_db.requested_limits) == fake_db.query_count
    assert all(lim == 6 for lim in fake_db.requested_limits), f"Every query must request limit=6: {fake_db.requested_limits}"
    assert len(fake_db.delivered_counts) == fake_db.query_count
    assert all(c <= 6 for c in fake_db.delivered_counts), f"Delivered count per query must be <= 6: {fake_db.delivered_counts}"
    assert sum(fake_db.delivered_counts) <= 48, f"Aggregate delivered rows must be <= 48: {sum(fake_db.delivered_counts)}"


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_bounded_read_delivery_ceiling_populated(monkeypatch):
    """When queries return non-matching candidate rows (below cap), every query must deliver <= 6 rows and aggregate <= 48."""
    from app.db import contractors as contractors_db

    docs = [
        {
            "contractor_id": f"wrong-us-{i}",
            "owner_phone": "2079460958",
            "country_code": "US",
            "active": True,
        }
        for i in range(3)
    ]
    fake_db = _FakeDB(docs)
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    fake_db.query_count = 0
    fake_db.requested_limits.clear()
    fake_db.delivered_counts.clear()

    res = await contractors_db.get_contractor_by_owner_phone("020 7946 0958", country_code="GB")
    assert res is None
    assert fake_db.query_count <= 8
    assert len(fake_db.requested_limits) == fake_db.query_count
    assert all(lim == 6 for lim in fake_db.requested_limits)
    assert len(fake_db.delivered_counts) == fake_db.query_count
    assert all(c <= 6 for c in fake_db.delivered_counts)
    assert sum(fake_db.delivered_counts) <= 48
    assert any(c > 0 for c in fake_db.delivered_counts)


@pytest.mark.asyncio
async def test_get_contractor_by_owner_phone_invalid_country_falls_back_to_us(monkeypatch):
    from app.db import contractors as contractors_db

    fake_db = _FakeDB([
        {"contractor_id": "us-1", "owner_phone": "+14155551234", "country_code": "US", "active": True}
    ])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    res = await contractors_db.get_contractor_by_owner_phone("4155551234", country_code="INVALID_COUNTRY")
    assert res is not None
    assert res["contractor_id"] == "us-1"


@pytest.mark.asyncio
async def test_create_contractor_persists_canonical_international_phone(monkeypatch):
    from app.db import contractors as contractors_db

    stored_docs = []

    class FakeDocRef:
        id = "new-contractor-id"

    class FakeCollection:
        def add(self, data):
            stored_docs.append(dict(data))
            return None, FakeDocRef()

    class FakeDB:
        def collection(self, name):
            return FakeCollection()

    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: FakeDB())

    # UK national
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "UK Plumber",
        "owner_phone": "020 7946 0958",
        "country_code": "GB",
    })
    assert stored_docs[0]["owner_phone"] == "+442079460958"
    assert stored_docs[0]["owner_phone_e164"] == "+442079460958"
    assert stored_docs[0]["country_code"] == "GB"

    # BR national
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "BR Eletricista",
        "owner_phone": "(11) 98765-4321",
        "country_code": "BR",
    })
    assert stored_docs[0]["owner_phone"] == "+5511987654321"
    assert stored_docs[0]["owner_phone_e164"] == "+5511987654321"
    assert stored_docs[0]["country_code"] == "BR"

    # DE national
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "DE Handwerker",
        "owner_phone": "030 1234567",
        "country_code": "DE",
    })
    assert stored_docs[0]["owner_phone"] == "+49301234567"
    assert stored_docs[0]["owner_phone_e164"] == "+49301234567"
    assert stored_docs[0]["country_code"] == "DE"

    # Region-independent E.164 with missing/US country
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "E164 International",
        "owner_phone": "+442079460958",
    })
    assert stored_docs[0]["owner_phone"] == "+442079460958"
    assert stored_docs[0]["owner_phone_e164"] == "+442079460958"


@pytest.mark.asyncio
async def test_create_contractor_direct_helper_rejects_invalid_phone_before_firestore(monkeypatch):
    """create_contractor must self-validate owner_phone before acquiring Firestore client."""
    from app.db import contractors as contractors_db

    def fail_firestore():
        pytest.fail("get_firestore_client must not be called when owner_phone is invalid")

    monkeypatch.setattr(contractors_db, "get_firestore_client", fail_firestore)

    # Invalid US phone
    with pytest.raises(ValueError, match="Invalid owner phone"):
        await contractors_db.create_contractor({
            "business_name": "Bad Co",
            "owner_phone": "12345",
            "country_code": "US",
        })

    # Invalid UK phone
    with pytest.raises(ValueError, match="Invalid owner phone"):
        await contractors_db.create_contractor({
            "business_name": "Bad Co",
            "owner_phone": "invalid-text",
            "country_code": "GB",
        })


@pytest.mark.asyncio
async def test_create_contractor_direct_helper_sanitizes_lowercase_supported_country(monkeypatch):
    """Direct helper must sanitize lowercase supported country (e.g. 'gb' -> 'GB')."""
    from app.db import contractors as contractors_db

    stored_docs = []

    class FakeDocRef:
        id = "new-contractor-id"

    class FakeCollection:
        def add(self, data):
            stored_docs.append(dict(data))
            return None, FakeDocRef()

    class FakeDB:
        def collection(self, name):
            return FakeCollection()

    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: FakeDB())

    # Lowercase 'gb'
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "UK Co",
        "owner_phone": "020 7946 0958",
        "country_code": "gb",
    })
    assert stored_docs[0]["country_code"] == "GB"
    assert stored_docs[0]["owner_phone"] == "+442079460958"
    assert stored_docs[0]["owner_phone_e164"] == "+442079460958"

    # Lowercase 'br'
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "BR Co",
        "owner_phone": "(11) 98765-4321",
        "country_code": "br",
    })
    assert stored_docs[0]["country_code"] == "BR"
    assert stored_docs[0]["owner_phone"] == "+5511987654321"
    assert stored_docs[0]["owner_phone_e164"] == "+5511987654321"

    # Lowercase 'de'
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "DE Co",
        "owner_phone": "030 1234567",
        "country_code": "de",
    })
    assert stored_docs[0]["country_code"] == "DE"
    assert stored_docs[0]["owner_phone"] == "+49301234567"
    assert stored_docs[0]["owner_phone_e164"] == "+49301234567"


@pytest.mark.asyncio
async def test_create_contractor_direct_helper_sanitizes_unsupported_country(monkeypatch):
    """Direct helper must resolve unsupported country input to 'US' default."""
    from app.db import contractors as contractors_db

    stored_docs = []

    class FakeDocRef:
        id = "new-contractor-id"

    class FakeCollection:
        def add(self, data):
            stored_docs.append(dict(data))
            return None, FakeDocRef()

    class FakeDB:
        def collection(self, name):
            return FakeCollection()

    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: FakeDB())

    # Unsupported country with E.164 phone
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "XX Co",
        "owner_phone": "+442079460958",
        "country_code": "XX",
    })
    assert stored_docs[0]["country_code"] == "US"
    assert stored_docs[0]["owner_phone"] == "+442079460958"
    assert stored_docs[0]["owner_phone_e164"] == "+442079460958"

    # Unsupported country with US national phone
    stored_docs.clear()
    await contractors_db.create_contractor({
        "business_name": "US Co",
        "owner_phone": "4155551234",
        "country_code": "UNSUPPORTED",
    })
    assert stored_docs[0]["country_code"] == "US"
    assert stored_docs[0]["owner_phone"] == "+14155551234"
    assert stored_docs[0]["owner_phone_e164"] == "+14155551234"


@pytest.mark.asyncio
async def test_api_create_contractor_cross_format_returning_user_creates_no_new_account(monkeypatch):
    """Real helper finds existing same-Apple-ID account across format difference and prevents new account creation."""
    from app.api import contractors as contractors_api
    from app.db import contractors as contractors_db

    existing_record = {
        "contractor_id": "existing-us-cid",
        "business_name": "Existing Business",
        "owner_name": "Alice",
        "owner_phone": "(415) 555-1234",  # Stored legacy format
        "apple_user_id": "apple-user-same",
        "country_code": "US",
        "subscription_uuid": "00000000-0000-0000-0000-000000000001",
        "active": True,
    }

    fake_db = _FakeDB([existing_record])
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: fake_db)

    async def fail_create(data):
        pytest.fail(f"create_contractor must not be called for returning account: {data}")

    async def fake_update(cid, updates):
        return True

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )

    # Submitting unformatted bare digits "4155551234"
    body = contractors_api.ContractorCreate(
        owner_name="Alice",
        business_name="Existing Business",
        owner_phone="4155551234",
        country_code="US",
        apple_user_id="apple-user-same",
        apple_identity_token="tok",
    )
    res = await contractors_api.api_create_contractor(body, request=None)

    assert res["status"] == "ok"
    assert res["contractor_id"] == "existing-us-cid"
    assert res["existing"] is True


@pytest.mark.asyncio
async def test_effective_country_forwarded_to_phone_dedupe(monkeypatch):
    from app.api import contractors as contractors_api

    dedupe_calls = []

    async def fake_by_apple(apple_user_id):
        return None

    async def fake_by_phone(phone, *, country_code="US"):
        dedupe_calls.append({"phone": phone, "country_code": country_code})
        return None

    async def fake_create(data):
        return "created-id"

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
        owner_name="UK User",
        business_name="UK Services",
        owner_phone="020 7946 0958",
        country_code="GB",
        apple_user_id="apple-uk-new",
        apple_identity_token="tok",
    )
    await contractors_api.api_create_contractor(body, request=None)

    assert len(dedupe_calls) == 1
    assert dedupe_calls[0]["country_code"] == "GB"
    assert dedupe_calls[0]["phone"] == "020 7946 0958"


@pytest.mark.asyncio
async def test_canonical_international_matches_return_existing_contractor_not_duplicate(monkeypatch):
    from app.api import contractors as contractors_api

    created = []

    async def fake_by_apple(apple_user_id):
        return None

    async def fake_create(data):
        created.append(data)
        return "new-id"

    async def fake_by_phone(phone, *, country_code="US"):
        if phone == "020 7946 0958" and country_code == "GB":
            return {
                "contractor_id": "existing-uk-id",
                "apple_user_id": "apple-uk-user",
                "owner_phone": "+442079460958",
                "owner_phone_e164": "+442079460958",
                "country_code": "GB",
                "subscription_uuid": "uuid-uk",
            }
        return None

    async def fake_update(cid, updates):
        return True

    async def fake_uuid(cid, existing):
        return "uuid-uk"

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
        owner_name="UK User",
        business_name="UK Services",
        owner_phone="020 7946 0958",
        country_code="GB",
        apple_user_id="apple-uk-user",
        apple_identity_token="tok",
    )
    res = await contractors_api.api_create_contractor(body, request=None)

    assert res["status"] == "ok"
    assert res["contractor_id"] == "existing-uk-id"
    assert res["existing"] is True
    assert created == [], "must not create duplicate account for canonical international match"


@pytest.mark.asyncio
async def test_invalid_nonblank_phone_rejected_before_firestore(monkeypatch):
    from app.api import contractors as contractors_api
    from fastapi import HTTPException

    firestore_accessed = []

    async def fail_apple_lookup(apple_user_id):
        firestore_accessed.append("apple_lookup")
        return None

    async def fail_phone_lookup(phone, **kwargs):
        firestore_accessed.append("phone_lookup")
        return None

    async def fail_create(data):
        firestore_accessed.append("create")
        return "id"

    async def fake_enforce(request, apple_user_id, token):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fail_apple_lookup)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_owner_phone", fail_phone_lookup)
    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)

    # Invalid UK phone (wrong national format)
    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                owner_name="Invalid",
                business_name="Bad Co",
                owner_phone="12345",
                country_code="GB",
                apple_user_id="apple-user-1",
                apple_identity_token="tok",
            ),
            request=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid owner phone number"
    assert firestore_accessed == [], "no Firestore operation must occur when phone is invalid"

    # Invalid text phone
    firestore_accessed.clear()
    with pytest.raises(HTTPException) as exc_info2:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                owner_name="Invalid",
                business_name="Bad Co",
                owner_phone="not-a-number",
                country_code="US",
                apple_user_id="apple-user-1",
                apple_identity_token="tok",
            ),
            request=None,
        )

    assert exc_info2.value.status_code == 400
    assert exc_info2.value.detail == "Invalid owner phone number"
    assert firestore_accessed == []


@pytest.mark.asyncio
async def test_blank_owner_phone_allowed_and_creates_account(monkeypatch):
    from app.api import contractors as contractors_api

    created = []

    async def fake_by_apple(apple_user_id):
        return None

    async def fake_create(data):
        created.append(dict(data))
        return "created-blank-id"

    async def fake_update(cid, updates):
        return True

    async def fake_enforce(request, apple_user_id, token):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple)

    body = contractors_api.ContractorCreate(
        owner_name="Blank Phone User",
        business_name="Blank Co",
        owner_phone="",
        country_code="",
        apple_user_id="apple-user-blank",
        apple_identity_token="tok",
    )
    res = await contractors_api.api_create_contractor(body, request=None)

    assert res["status"] == "ok"
    assert res["contractor_id"] == "created-blank-id"
    assert len(created) == 1
    assert created[0]["country_code"] == "US"
