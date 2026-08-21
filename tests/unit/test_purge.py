"""Data-purge pipeline (spec: docs/superpowers/specs/2026-08-20-data-purge-pipeline.md).

Owner decisions 2026-08-21: 30-day grace after deactivation; minimal tombstone
allowlist (billing reconciliation must survive purge); PURGE_ENABLED default
off. The purge must be idempotent, refuse active accounts structurally, and
traverse nested command_receipts before their parents (Firestore does not
cascade-delete subcollections).
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest


# ---------------------------------------------------------------------------
# In-memory Firestore fake (documents, subcollections, where/limit/stream,
# batches). Deliberately minimal but honest about Firestore semantics the
# purge depends on: deleting a document does NOT delete its subcollections.
# ---------------------------------------------------------------------------


class _FakeDoc:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path.split("/")[-1]

    def collection(self, name):
        return _FakeCollection(self._store, f"{self._path}/{name}")

    def get(self):
        data = self._store.docs.get(self._path)
        return _FakeSnapshot(self._path, data)

    def set(self, value, merge=False):
        if merge and self._path in self._store.docs:
            self._store.docs[self._path].update(value)
        else:
            self._store.docs[self._path] = dict(value)

    def update(self, value):
        self._store.docs[self._path].update(value)

    def delete(self):
        self._store.docs.pop(self._path, None)

    @property
    def reference(self):
        return self


class _FakeSnapshot:
    def __init__(self, path, data):
        self._path = path
        self._data = data
        self.exists = data is not None
        self.id = path.split("/")[-1]
        self.reference = None  # set by queries

    def to_dict(self):
        return dict(self._data) if self._data else None


class _FakeCollection:
    def __init__(self, store, path, filters=(), limit=None):
        self._store = store
        self._path = path
        self._filters = filters
        self._limit = limit

    def document(self, doc_id):
        return _FakeDoc(self._store, f"{self._path}/{doc_id}")

    def where(self, filter=None):
        return _FakeCollection(
            self._store, self._path, self._filters + (filter,), self._limit
        )

    def limit(self, n):
        return _FakeCollection(self._store, self._path, self._filters, n)

    def list_documents(self):
        depth = self._path.count("/") + 2
        ids = set()
        for path in self._store.docs:
            if not path.startswith(self._path + "/"):
                continue
            parts = path.split("/")
            want = self._path.count("/") + 1
            ids.add(parts[want])
        return [_FakeDoc(self._store, f"{self._path}/{i}") for i in sorted(ids)]

    def stream(self, **_kwargs):
        depth = self._path.count("/") + 2
        out = []
        for path, data in sorted(self._store.docs.items()):
            if not path.startswith(self._path + "/"):
                continue
            if path.count("/") + 1 != depth:
                continue  # not a direct child (subcollection docs excluded)
            ok = True
            for f in self._filters:
                field, op, val = f.field_path, f.op_string, f.value
                have = data.get(field)
                if op == "==":
                    ok = have == val
                elif op == "<":
                    ok = have is not None and have < val
                else:
                    raise NotImplementedError(op)
                if not ok:
                    break
            if ok:
                snap = _FakeSnapshot(path, data)
                snap.reference = _FakeDoc(self._store, path)
                out.append(snap)
                if self._limit and len(out) >= self._limit:
                    break
        return iter(out)


class _FakeBatch:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def delete(self, ref):
        self._ops.append(ref)

    def commit(self):
        for ref in self._ops:
            ref.delete()
        self._ops = []


class _FakeDb:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _FakeCollection(self, name)

    def batch(self):
        return _FakeBatch(self)


class _FakeBlob:
    def __init__(self, name):
        self.name = name


class _FakeBucket:
    def __init__(self, deleted):
        self._deleted = deleted
        self.blobs = []

    def list_blobs(self, prefix=""):
        return [b for b in self.blobs if b.name.startswith(prefix)]

    def delete_blobs(self, blobs, on_error=None):
        for b in blobs:
            self._deleted.append(b.name)
            self.blobs.remove(b)


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", lambda: fake)
    # purge binds the name at import time — patch the used binding too.
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module, "get_firestore_client", lambda: fake)
    return fake


@pytest.fixture
def gcs(monkeypatch):
    deleted = []
    bucket = _FakeBucket(deleted)

    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module, "_media_bucket", lambda: bucket)
    bucket.deleted = deleted
    return bucket


def _seed_contractor(db, cid="c1", **overrides):
    doc = {
        "active": False,
        "deactivated_at": 1_000_000,
        "deletion_requested_at": 1_000_000,
        "subscription_uuid": "uuid-1",
        "owner_phone": "+15559990000",
        "business_name": "Acme Plumbing",
        "post_deletion_billing": {"count": 1, "charges": 1, "last_type": "DID_RENEW"},
    }
    doc.update(overrides)
    db.docs[f"contractors/{cid}"] = doc
    return doc


# ---------------------------------------------------------------------------
# purge_contractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_refuses_active_contractor(db, gcs):
    from app.db.purge import purge_contractor

    _seed_contractor(db, active=True)
    db.docs["contractors/c1/contacts/p1"] = {"name": "Pat"}

    result = await purge_contractor("c1")

    assert result["refused"] == "not_deactivated"
    assert "contractors/c1/contacts/p1" in db.docs
    assert db.docs["contractors/c1"]["business_name"] == "Acme Plumbing"


@pytest.mark.asyncio
async def test_purge_refuses_system_deactivated_account(db, gcs):
    """THE critical guard: the 14-day deleted-app cleanup deactivates
    accounts through the same deactivate_contractor and even texts users
    'reinstall to reactivate'. Deactivation is NOT a deletion request —
    only the user's explicit DELETE (which stamps deletion_requested_at)
    may ever lead to a purge."""
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    del db.docs["contractors/c1"]["deletion_requested_at"]
    db.docs["contractors/c1/contacts/p1"] = {"name": "Pat"}

    result = await purge_contractor("c1")

    assert result["refused"] == "no_deletion_request"
    assert "contractors/c1/contacts/p1" in db.docs


@pytest.mark.asyncio
async def test_sweep_never_touches_system_deactivated_accounts(db, gcs, monkeypatch):
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module.settings, "purge_enabled", True)
    old_ts = _now() - 40 * 24 * 3600
    _seed_contractor(db, cid="lapsed", deactivated_at=old_ts)
    del db.docs["contractors/lapsed"]["deletion_requested_at"]

    purged = await purge_module.purge_sweep(now=_now())

    assert purged == []
    assert "purged_at" not in db.docs["contractors/lapsed"]


def test_user_delete_endpoint_stamps_the_deletion_request():
    """The marker's only writer is the user's own DELETE — reachability pin."""
    import inspect
    from app.api import contractors as contractors_api
    from app.db import contractors as contractors_db

    assert "user_requested=True" in inspect.getsource(
        contractors_api.api_delete_contractor
    )
    src = inspect.getsource(contractors_db.deactivate_contractor)
    assert "deletion_requested_at" in src


@pytest.mark.asyncio
async def test_purge_refuses_missing_active_field(db, gcs):
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    del db.docs["contractors/c1"]["active"]

    result = await purge_contractor("c1")

    assert result["refused"] == "not_deactivated"


@pytest.mark.asyncio
async def test_tombstone_is_an_exact_allowlist(db, gcs):
    """Assert as an allowlist so a future PII field cannot leak through."""
    from app.db.purge import TOMBSTONE_FIELDS, purge_contractor

    _seed_contractor(db, number_release_anomaly={"at": 5, "number": "+15551112222"},
                     deleted_app_detected_at=999, apple_user_id="apple-1")
    db.docs["contractors/c1/contacts/p1"] = {"name": "Pat", "phone": "+15558887777"}

    result = await purge_contractor("c1")

    assert "refused" not in result
    tomb = db.docs["contractors/c1"]
    assert set(tomb.keys()) <= set(TOMBSTONE_FIELDS)
    assert tomb["active"] is False
    assert tomb["purged_at"]
    assert tomb["subscription_uuid"] == "uuid-1"
    assert tomb["post_deletion_billing"]["charges"] == 1
    assert tomb["apple_user_id"] == "apple-1", (
        "rebound detection needs this after purge — without it a paying "
        "re-signed-up customer's renewals become unattributable"
    )
    assert "owner_phone" not in tomb
    assert "business_name" not in tomb
    assert "contractors/c1/contacts/p1" not in db.docs


@pytest.mark.asyncio
async def test_purge_deletes_all_subcollections_and_by_contractor_collections(db, gcs):
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    for sub in ("contacts", "caller_contacts", "service_requests",
                "inbound_messages", "devices", "settings", "knowledge_base"):
        db.docs[f"contractors/c1/{sub}/d1"] = {"x": 1}
    for coll in ("calls", "jobs", "post_call_handoffs", "conference_bindings"):
        db.docs[f"{coll}/a"] = {"contractor_id": "c1"}
        db.docs[f"{coll}/keep"] = {"contractor_id": "OTHER"}

    result = await purge_contractor("c1")

    for sub in ("contacts", "caller_contacts", "service_requests",
                "inbound_messages", "devices", "settings", "knowledge_base"):
        assert f"contractors/c1/{sub}/d1" not in db.docs
    for coll in ("calls", "jobs", "post_call_handoffs", "conference_bindings"):
        assert f"{coll}/a" not in db.docs
        assert f"{coll}/keep" in db.docs, "other tenants' data must be untouched"
    assert result["deleted"]["contacts"] == 1
    assert result["deleted"]["calls"] == 1


@pytest.mark.asyncio
async def test_nested_command_receipts_leave_no_orphans(db, gcs):
    """Receipts live at customer_memory/{key}/command_receipts/{key}; Firestore
    does not cascade-delete, so deleting the memory doc first would orphan
    them invisibly."""
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    db.docs["contractors/c1/customer_memory/m1"] = {"name": "Pat"}
    db.docs["contractors/c1/customer_memory/m1/command_receipts/r1"] = {"op": "set"}
    db.docs["contractors/c1/customer_memory/m1/command_receipts/r2"] = {"op": "set"}

    await purge_contractor("c1")

    orphans = [p for p in db.docs if "command_receipts" in p]
    assert orphans == []
    assert "contractors/c1/customer_memory/m1" not in db.docs


@pytest.mark.asyncio
async def test_receipts_under_phantom_parents_are_deleted(db, gcs):
    """The forget flow deletes the customer_memory doc while writing a
    receipt beneath it — real Firestore stream() never yields the missing
    parent, so traversal must use list_documents(), which does."""
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    # phantom parent: receipts exist, the parent doc does not
    db.docs["contractors/c1/customer_memory/ghost/command_receipts/r1"] = {"op": "forget"}

    await purge_contractor("c1")

    assert not [p for p in db.docs if "command_receipts" in p]


@pytest.mark.asyncio
async def test_tombstone_merges_fields_written_mid_purge(db, gcs, monkeypatch):
    """A post_deletion_billing write landing during the delete phase must
    survive the tombstone overwrite — that record is the billing evidence."""
    from app.db import purge as purge_module

    _seed_contractor(db)
    db.docs["contractors/c1/contacts/p1"] = {"name": "Pat"}

    real_batch = _FakeDb.batch

    def batch_with_side_write(self):
        # simulate the App Store webhook racing the purge
        self.docs["contractors/c1"]["post_deletion_billing"] = {
            "count": 9, "charges": 9, "last_type": "DID_RENEW"}
        return real_batch(self)

    monkeypatch.setattr(_FakeDb, "batch", batch_with_side_write)

    await purge_module.purge_contractor("c1")

    tomb = db.docs["contractors/c1"]
    assert tomb["post_deletion_billing"]["count"] == 9


@pytest.mark.asyncio
async def test_degraded_mode_records_skipped_media(db, monkeypatch):
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module, "_media_bucket", lambda: None)
    _seed_contractor(db)
    db.docs["estimates/tok1"] = {"contractor_id": "c1", "media_ids": ["m1", "m2"]}

    result = await purge_module.purge_contractor("c1")

    assert result["deleted"]["estimate_media_skipped"] == 2
    assert "estimates/tok1" not in db.docs


@pytest.mark.asyncio
async def test_estimates_media_deleted_by_token_hash_prefix(db, gcs):
    """GCS objects are keyed by token_hash (the estimate doc id), not by
    contractor — the purge must walk the contractor's estimate docs."""
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    db.docs["estimates/tok1"] = {"contractor_id": "c1", "caller_phone": "+15551"}
    db.docs["estimates/tok-other"] = {"contractor_id": "OTHER"}
    gcs.blobs.extend([_FakeBlob("tok1/media1.mp4"), _FakeBlob("tok1/media2.jpg"),
                      _FakeBlob("tok-other/media9.mp4")])

    result = await purge_contractor("c1")

    assert "estimates/tok1" not in db.docs
    assert "estimates/tok-other" in db.docs
    assert sorted(gcs.deleted) == ["tok1/media1.mp4", "tok1/media2.jpg"]
    assert result["deleted"]["estimate_media"] == 2


@pytest.mark.asyncio
async def test_purge_is_idempotent(db, gcs):
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    db.docs["contractors/c1/contacts/p1"] = {"name": "Pat"}

    first = await purge_contractor("c1")
    second = await purge_contractor("c1")

    assert "refused" not in first
    assert second["refused"] == "already_purged"
    assert db.docs["contractors/c1"]["purged_at"] == first["purged_at"]


@pytest.mark.asyncio
async def test_batched_deletes_handle_more_than_500_docs(db, gcs):
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    for i in range(750):
        db.docs[f"contractors/c1/contacts/p{i}"] = {"n": i}

    result = await purge_contractor("c1")

    assert result["deleted"]["contacts"] == 750
    assert not [p for p in db.docs if "/contacts/" in p]


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def _now():
    return 4_000_000


@pytest.mark.asyncio
async def test_sweep_respects_grace_period(db, gcs, monkeypatch):
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module.settings, "purge_enabled", True)
    recent = _now() - 5 * 24 * 3600
    old = _now() - 31 * 24 * 3600
    _seed_contractor(db, cid="fresh", deactivated_at=recent, deletion_requested_at=recent)
    _seed_contractor(db, cid="ripe", deactivated_at=old, deletion_requested_at=old)

    purged = await purge_module.purge_sweep(now=_now())

    assert purged == ["ripe"]
    assert "purged_at" in db.docs["contractors/ripe"]
    assert "purged_at" not in db.docs["contractors/fresh"]


@pytest.mark.asyncio
async def test_sweep_worst_case_stays_inside_the_promised_window(db, gcs, monkeypatch):
    """The UI promises deletion 'within 30 days'; the 6h sweep interval must
    come out of the grace window, not extend it — eligibility begins at
    30d minus one interval so the worst case lands exactly on day 30."""
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module.settings, "purge_enabled", True)
    just_inside = _now() - (30 * 24 * 3600 - 5 * 3600)   # 29d19h ago
    just_outside = _now() - (30 * 24 * 3600 - 7 * 3600)  # 29d17h ago
    _seed_contractor(db, cid="due", deactivated_at=just_inside,
                     deletion_requested_at=just_inside)
    _seed_contractor(db, cid="not-yet", deactivated_at=just_outside,
                     deletion_requested_at=just_outside)

    purged = await purge_module.purge_sweep(now=_now())

    assert purged == ["due"]


@pytest.mark.asyncio
async def test_sweep_disabled_by_default(db, gcs, monkeypatch):
    from app.db import purge as purge_module
    from app.config import Settings

    assert Settings.model_fields["purge_enabled"].default is False

    old = _now() - 31 * 24 * 3600
    _seed_contractor(db, cid="ripe", deactivated_at=old, deletion_requested_at=old)
    monkeypatch.setattr(purge_module.settings, "purge_enabled", False)

    purged = await purge_module.purge_sweep(now=_now())

    assert purged == []
    assert "purged_at" not in db.docs["contractors/ripe"]


@pytest.mark.asyncio
async def test_sweep_skips_already_purged(db, gcs, monkeypatch):
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module.settings, "purge_enabled", True)
    old = _now() - 40 * 24 * 3600
    _seed_contractor(db, cid="done", deactivated_at=old, deletion_requested_at=old, purged_at=123)

    purged = await purge_module.purge_sweep(now=_now())

    assert purged == []


@pytest.mark.asyncio
async def test_sweep_isolates_a_poisoned_account(db, gcs, monkeypatch):
    from app.db import purge as purge_module

    monkeypatch.setattr(purge_module.settings, "purge_enabled", True)
    old = _now() - 31 * 24 * 3600
    _seed_contractor(db, cid="bad", deactivated_at=old, deletion_requested_at=old)
    _seed_contractor(db, cid="good", deactivated_at=old, deletion_requested_at=old)

    real = purge_module.purge_contractor

    async def sometimes_failing(cid):
        if cid == "bad":
            raise RuntimeError("poison")
        return await real(cid)

    monkeypatch.setattr(purge_module, "purge_contractor", sometimes_failing)

    purged = await purge_module.purge_sweep(now=_now())

    assert purged == ["good"]


@pytest.mark.asyncio
async def test_reconciliation_survives_purge(db, gcs):
    """A purged account's tombstone must still let the App Store notification
    handler attribute post-deletion charges (active False + subscription_uuid
    both present)."""
    from app.db.purge import purge_contractor

    _seed_contractor(db)
    await purge_contractor("c1")

    tomb = db.docs["contractors/c1"]
    assert tomb["active"] is False
    assert tomb["subscription_uuid"] == "uuid-1"


def test_sweep_is_wired_into_main():
    """Reachability pin — this project's recurring failure class is features
    nothing calls."""
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert "purge_sweep" in source


def test_dry_run_reports_without_deleting(db, capsys):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "purge_dry_run", root / "scripts" / "purge_dry_run.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    old = 4_000_000 - 31 * 24 * 3600
    _seed_contractor(db, cid="ripe", deactivated_at=old, deletion_requested_at=old)
    db.docs["contractors/ripe/contacts/p1"] = {"name": "Pat"}
    before = dict(db.docs)

    import time as _time
    rc = mod.main(
        ["--project", "test", "--grace-days", "30"],
        client_factory=lambda: db,
    )

    assert rc == 0
    assert db.docs == before, "dry run must delete nothing"
    out = capsys.readouterr().out
    assert "Nothing was deleted" in out
    assert "'contacts': 1" in out
    assert "Pat" not in out, "no PII in dry-run output"


def test_purge_one_script_exists_and_refuses_bulk():
    """The single-target tool must exist for the rollout's manual-test step
    and must have no bulk mode — bulk erasure only happens via the sweep."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "scripts" / "purge_one.py").read_text()
    assert 'add_argument("--contractor-id", required=True)' in src
    assert '"--apply"' in src
    # no bulk-mode argument, and no sweep over the contractors collection
    assert 'add_argument("--all"' not in src
    assert 'collection("contractors").stream' not in src
