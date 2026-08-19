from datetime import UTC, datetime, timedelta

import pytest

from app.services.customer_memory import (
    CustomerMemory,
    CustomerMemoryConflict,
    IdentitySource,
    IdentityState,
    InMemoryCustomerMemoryRepository,
    customer_key_for_phone,
)

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)


async def _remember(repo, contractor="c1", phone="+16175550123", **overrides):
    values = {
        "display_name": "Jonathan Smith",
        "identity_state": IdentityState.CONFIRMED,
        "identity_source": IdentitySource.CALLER_CONFIRMED,
        "confidence": 0.95,
        "language": "en",
        "expected_revision": 0,
        "command_id": "call-1:name",
        "occurred_at": NOW,
    }
    values.update(overrides)
    return await repo.remember(contractor, phone, **values)


@pytest.mark.asyncio
async def test_memory_is_strictly_tenant_and_phone_scoped():
    repo = InMemoryCustomerMemoryRepository()
    saved = await _remember(repo)

    assert await repo.lookup("c1", "+1 (617) 555-0123", NOW) == saved
    assert await repo.lookup("c2", "+16175550123", NOW) is None
    assert await repo.lookup("c1", "+16175550999", NOW) is None
    assert saved.customer_key == customer_key_for_phone("+16175550123")


@pytest.mark.asyncio
async def test_only_confirmed_high_confidence_unexpired_identity_can_greet():
    repo = InMemoryCustomerMemoryRepository()
    confirmed = await _remember(repo)
    candidate = await _remember(
        repo,
        contractor="c2",
        identity_state=IdentityState.CANDIDATE,
        identity_source=IdentitySource.TRANSCRIPT_EXTRACTED,
        confidence=0.7,
    )

    assert confirmed.greeting_name(NOW) == "Jonathan Smith"
    assert candidate.greeting_name(NOW) == ""
    assert confirmed.greeting_name(confirmed.expires_at) == ""


@pytest.mark.asyncio
async def test_revision_and_command_id_are_replay_safe():
    repo = InMemoryCustomerMemoryRepository()
    first = await _remember(repo)
    retry = await _remember(repo, occurred_at=NOW + timedelta(minutes=5))

    assert retry == first
    with pytest.raises(CustomerMemoryConflict):
        await _remember(repo, display_name="Different Name")
    with pytest.raises(CustomerMemoryConflict):
        await _remember(repo, command_id="call-2", expected_revision=0)

    updated = await _remember(
        repo,
        display_name="Jon Smith",
        expected_revision=first.revision,
        command_id="call-2:name",
        occurred_at=NOW + timedelta(minutes=10),
    )
    assert updated.revision == 2
    assert await _remember(repo) == first


@pytest.mark.asyncio
async def test_update_round_trips_and_forget_removes_record():
    repo = InMemoryCustomerMemoryRepository()
    first = await _remember(repo)
    updated = await _remember(
        repo,
        display_name="Jon Smith",
        expected_revision=first.revision,
        command_id="call-2:name",
        occurred_at=NOW + timedelta(days=1),
    )

    assert updated.revision == 2
    assert CustomerMemory.from_dict(updated.to_dict()) == updated
    assert (
        await repo.forget(
            "c1",
            "+16175550123",
            expected_revision=updated.revision,
            command_id="forget-1",
        )
        is True
    )
    assert (
        await repo.forget(
            "c1",
            "+16175550123",
            expected_revision=updated.revision,
            command_id="forget-1",
        )
        is True
    )
    assert await repo.lookup("c1", "+16175550123", NOW + timedelta(days=1)) is None
    with pytest.raises(CustomerMemoryConflict):
        await _remember(repo)


@pytest.mark.asyncio
async def test_forget_enforces_expected_revision_and_is_tenant_scoped():
    repo = InMemoryCustomerMemoryRepository()
    saved = await _remember(repo)

    with pytest.raises(CustomerMemoryConflict):
        await repo.forget(
            "c1",
            "+16175550123",
            expected_revision=0,
            command_id="forget-stale",
        )

    assert (
        await repo.forget(
            "c2",
            "+16175550123",
            expected_revision=0,
            command_id="forget-other-tenant",
        )
        is False
    )
    assert await repo.lookup("c1", "+16175550123", NOW) == saved


@pytest.mark.asyncio
async def test_expired_records_are_not_returned():
    repo = InMemoryCustomerMemoryRepository()
    saved = await _remember(repo)

    assert await repo.lookup("c1", "+16175550123", saved.expires_at) is None


@pytest.mark.asyncio
async def test_expired_record_can_be_reconfirmed_before_ttl_physically_deletes_it():
    repo = InMemoryCustomerMemoryRepository()
    saved = await _remember(repo)

    refreshed = await _remember(
        repo,
        display_name="Jonathan Smith",
        expected_revision=0,
        command_id="call-after-expiry:name",
        occurred_at=saved.expires_at,
    )

    assert refreshed.revision == 1
    assert refreshed.created_at == saved.expires_at
    assert await repo.lookup("c1", "+16175550123", saved.expires_at) == refreshed


@pytest.mark.asyncio
async def test_contradictory_confirmed_name_becomes_conflicted_and_cannot_greet():
    repo = InMemoryCustomerMemoryRepository()
    first = await _remember(repo)

    conflicting = await _remember(
        repo,
        display_name="Different Person",
        expected_revision=first.revision,
        command_id="call-2:name",
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert conflicting.identity_state is IdentityState.CONFLICTED
    assert conflicting.greeting_name(NOW + timedelta(minutes=1)) == ""
