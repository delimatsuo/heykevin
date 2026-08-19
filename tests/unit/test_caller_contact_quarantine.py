"""Legacy caller-contact records remain read-only and tenant-unbound."""

from __future__ import annotations

import logging
import os
import sys

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import post_call
from scripts import benchmark_cnam_coverage, migrate_caller_contacts
from tests.unit.test_security_audit_medium import (
    _InMemoryFirestore,
    _install_fake_firestore,
)


def test_legacy_caller_contact_inventory_never_writes_or_deletes(caplog):
    fake = _InMemoryFirestore()
    fake._docs["caller_contacts/15551234567"] = {
        "caller_name": "Unbound Legacy Name",
    }
    fake._docs["calls/CA-tenant-a"] = {
        "caller_phone": "+15551234567",
        "contractor_id": "tenant-a",
    }
    original = {path: dict(data) for path, data in fake._docs.items()}

    with caplog.at_level(logging.WARNING):
        stats = migrate_caller_contacts._inventory(
            fake,
            log=logging.getLogger("test-caller-contact-quarantine"),
        )

    assert stats == {"legacy_docs_quarantined": 1}
    assert fake._docs == original
    assert not any(path.startswith("contractors/") for path in fake._docs)
    assert "no tenant copy or purge was performed" in caplog.text


def test_legacy_migration_apply_flag_is_rejected_before_firestore_access(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["migrate_caller_contacts.py", "--apply"])

    with pytest.raises(SystemExit) as exc:
        migrate_caller_contacts.main()

    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_post_call_replaces_unproven_tenant_record_instead_of_merging(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    path = "contractors/tenant-a/caller_contacts/15551234567"
    fake._docs[path] = {
        "caller_name": "Possibly Foreign Name",
        "call_history": [{"call_sid": "CA_foreign", "summary": "Foreign details"}],
    }

    saved = await post_call._update_caller_contact(
        {
            "caller_phone": "+15551234567",
            "caller_name": "Current Tenant Caller",
            "issue_description": "Current tenant request",
        },
        "tenant-a",
        "CA_current",
    )

    assert saved is True
    rewritten = fake._docs[path]
    assert rewritten["caller_name"] == "Current Tenant Caller"
    assert rewritten["provenance_schema"] == 1
    assert rewritten["provenance_source"] == "tenant_post_call"
    assert rewritten["provenance_contractor_id"] == "tenant-a"
    assert [item["call_sid"] for item in rewritten["call_history"]] == ["CA_current"]
    assert "Foreign details" not in str(rewritten)


@pytest.mark.asyncio
async def test_cnam_benchmark_excludes_tenant_unbound_samples(monkeypatch):
    async def fail_if_contact_is_read(*_args, **_kwargs):
        raise AssertionError("an unbound sample must not consult global contacts")

    monkeypatch.setattr("app.db.contacts.get_contact", fail_if_contact_is_read)

    kept, dropped = await benchmark_cnam_coverage.filter_to_unknown_callers(
        [{"phone": "+15551234567", "contractor_id": ""}]
    )

    assert kept == []
    assert dropped == 0
