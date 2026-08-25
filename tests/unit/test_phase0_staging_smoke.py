import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_smoke_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "phase0_staging_smoke.py"
    spec = importlib.util.spec_from_file_location("phase0_staging_smoke", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase0_staging_smoke"] = module
    spec.loader.exec_module(module)
    return module


class _FakeDocSnapshot:
    def __init__(self, exists: bool = False, data: dict[str, Any] | None = None):
        self.exists = exists
        self._data = dict(data) if data is not None else None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, data: dict[str, Any] | None = None, doc_id: str = "doc1"):
        self.id = doc_id
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self._collections: dict[str, dict[str, _FakeDocRef]] = {}

    @property
    def exists(self) -> bool:
        return self.data is not None and not self.deleted

    def get(self, transaction=None):
        return _FakeDocSnapshot(exists=self.exists, data=self.data)

    def create(self, data: dict[str, Any]):
        if self.exists:
            raise RuntimeError("Document already exists: secret_seed_sentinel_333")
        self.data = dict(data)
        self.deleted = False

    def delete(self):
        self.deleted = True
        self.data = None

    def collection(self, name: str):
        if name not in self._collections:
            self._collections[name] = {}
        collection_dict = self._collections[name]

        class _SubCol:
            def document(self, sub_id: str):
                if sub_id not in collection_dict:
                    collection_dict[sub_id] = _FakeDocRef(doc_id=sub_id)
                return collection_dict[sub_id]

        return _SubCol()


class _FakeTransaction:
    def __init__(self, db: "_FakeFirestoreClient"):
        self._db = db
        self._deletes: list[_FakeDocRef] = []
        self._read_only = False
        self._id = b"fake-transaction-id"
        self._max_attempts = 1

    def _clean_up(self):
        pass

    def _begin(self, retry_id=None):
        pass

    def delete(self, doc_ref: _FakeDocRef):
        self._deletes.append(doc_ref)

    def _commit(self):
        for ref in self._deletes:
            ref.delete()
        self._deletes.clear()

    def _rollback(self):
        self._deletes.clear()

    def commit(self):
        self._commit()


class _FakeFirestoreClient:
    def __init__(self):
        self._collections: dict[str, dict[str, _FakeDocRef]] = {
            "contractors": {},
            "calls": {},
        }

    def collection(self, name: str):
        if name not in self._collections:
            self._collections[name] = {}
        col_dict = self._collections[name]

        class _Col:
            def document(self, doc_id: str):
                if doc_id not in col_dict:
                    col_dict[doc_id] = _FakeDocRef(doc_id=doc_id)
                return col_dict[doc_id]

        return _Col()

    def transaction(self):
        return _FakeTransaction(self)


class _FakeRTDBReference:
    """Faithful fake of firebase_admin Reference.transaction that writes callback return value."""
    def __init__(self, data: Any = None, should_fail_cleanup: bool = False):
        self.data = data
        self.write_count = 0
        self.should_fail_cleanup = should_fail_cleanup

    def transaction(self, update_function):
        if self.should_fail_cleanup and self.data is not None:
            raise RuntimeError("RTDB network failure during cleanup: secret_rtdb_sentinel_777")
        new_val = update_function(self.data)
        self.data = new_val
        self.write_count += 1
        return new_val


class _HostileKeyObj:
    """Key object that allows initial insertion into a test dict but throws if rehashed or compared."""
    def __init__(self):
        self._inserted = False

    def __hash__(self):
        if not self._inserted:
            self._inserted = True
            return 42
        raise AssertionError("Hostile key hash invoked by smoke code!")

    def __eq__(self, other):
        raise AssertionError("Hostile key equality invoked by smoke code!")

    def __str__(self):
        raise AssertionError("Hostile key str check invoked by smoke code!")


class _HostileObj:
    def __eq__(self, other):
        raise AssertionError("Hostile equality check invoked!")

    def __bool__(self):
        raise AssertionError("Hostile bool check invoked!")

    def __str__(self):
        raise AssertionError("Hostile str check invoked!")


class _HostileStrSubclass(str):
    def __eq__(self, other):
        raise AssertionError("Hostile str subclass __eq__ check invoked!")

    def __ne__(self, other):
        raise AssertionError("Hostile str subclass __ne__ check invoked!")

    def __str__(self):
        raise AssertionError("Hostile str subclass __str__ check invoked!")


# ---------------------------------------------------------------------------
# Staging Binding & Argument Validation Tests
# ---------------------------------------------------------------------------

def test_staging_binding_accepts_only_exact_staging_targets():
    smoke = _load_smoke_module()

    # Exact staging values pass
    assert smoke.fail_if_not_staging(
        smoke.REQUIRED_STAGING_PROJECT,
        smoke.REQUIRED_STAGING_BASE_URL,
    ) == smoke.REQUIRED_STAGING_BASE_URL

    # Single trailing slash is canonicalized
    assert smoke.fail_if_not_staging(
        smoke.REQUIRED_STAGING_PROJECT,
        "https://kevin-api-staging-l63rergg7a-uc.a.run.app/",
    ) == smoke.REQUIRED_STAGING_BASE_URL

    # Multiple trailing slashes rejected
    for bad_slash_url in (
        "https://kevin-api-staging-l63rergg7a-uc.a.run.app//",
        "https://kevin-api-staging-l63rergg7a-uc.a.run.app///",
    ):
        with pytest.raises(smoke.SmokeFailure) as exc_info:
            smoke.fail_if_not_staging(smoke.REQUIRED_STAGING_PROJECT, bad_slash_url)
        assert str(exc_info.value) == "invalid_base_url"

    # Rejects production project or substring/empty projects
    for bad_proj in ("kevin-491315", "kevin-staging", "", "staging", 123):
        with pytest.raises(smoke.SmokeFailure) as exc_info:
            smoke.fail_if_not_staging(bad_proj, smoke.REQUIRED_STAGING_BASE_URL)
        assert str(exc_info.value) == "non_staging_project"

    # Rejects production URL or substring/port/path URLs
    for bad_url in (
        "https://kevin-api-752910912062.us-central1.run.app",
        "https://kevin-api-staging-l63rergg7a-uc.a.run.app:8080",
        "https://kevin-api-staging-l63rergg7a-uc.a.run.app/api",
        "https://kevin-api-staging-l63rergg7a-uc.a.run.app?query=1",
        "http://kevin-api-staging-l63rergg7a-uc.a.run.app",
        "https://other-staging.run.app",
    ):
        with pytest.raises(smoke.SmokeFailure) as exc_info:
            smoke.fail_if_not_staging(smoke.REQUIRED_STAGING_PROJECT, bad_url)
        assert str(exc_info.value) == "invalid_base_url"


def test_rtdb_allowlist_is_empty_and_rejects_any_nonempty_url():
    smoke = _load_smoke_module()

    # Authoritative allowlist is empty
    assert len(smoke.ALLOWED_STAGING_DATABASE_URLS) == 0

    # Any provided RTDB URL must reject with closed diagnostic code
    for url in (
        "https://kevin-staging-491315-default-rtdb.firebaseio.com",
        "https://kevin-staging-491315-default-rtdb.us-central1.firebasedatabase.app",
        "https://kevin-491315-default-rtdb.firebaseio.com",
        "https://some-custom-rtdb.firebaseio.com",
    ):
        with pytest.raises(smoke.SmokeFailure) as exc:
            smoke.fail_if_not_staging(smoke.REQUIRED_STAGING_PROJECT, smoke.REQUIRED_STAGING_BASE_URL, url)
        assert str(exc.value) == "no_authoritative_staging_rtdb_url"

    # Empty RTDB URL returns a safe SKIP step without attempting connection
    step = smoke.mutable_text_reply_smoke(
        smoke.REQUIRED_STAGING_BASE_URL,
        "",
        "codex_phase0_smoke_" + "0" * 32,
        "dummy_token",
        run_nonce="0" * 32,
        call_sid="CA" + "0" * 32,
    )
    assert step.status == "SKIP"
    assert step.detail == "rtdb_unconfigured"


def test_contractor_id_and_call_sid_generation():
    smoke = _load_smoke_module()

    cid = smoke.generate_contractor_id()
    assert smoke.CONTRACTOR_ID_PATTERN.fullmatch(cid)

    nonce = smoke.generate_run_nonce()
    assert smoke.NONCE_PATTERN.fullmatch(nonce)

    call_sid = smoke.generate_call_sid()
    assert smoke.CALL_SID_PATTERN.fullmatch(call_sid)

    # Cross-tenant contractor ID deterministic computation
    other_cid = smoke.compute_cross_tenant_contractor_id(call_sid)
    assert other_cid == f"codex_phase0_other_{call_sid[2:18]}"

    # Rejects old deterministic ID or invalid pattern
    for bad_cid in ("codex_phase0_smoke", "codex_phase0_smoke_123", "codex_phase0_smoke_G" + "0"*31):
        assert not smoke.CONTRACTOR_ID_PATTERN.fullmatch(bad_cid)


# ---------------------------------------------------------------------------
# Create-Only Seed & Nonce Ownership Tests
# ---------------------------------------------------------------------------

def test_seed_contractor_create_only_success_and_omissions():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()

    token = smoke.seed_contractor(db, cid, run_nonce=nonce)
    assert token.startswith(f"kv_ct_{cid[:8]}_")

    doc = db.collection("contractors").document(cid)
    assert doc.exists
    data = doc.data
    assert data["contractor_id"] == cid
    assert data["codex_managed"] is True
    assert data["codex_purpose"] == "phase0_staging_smoke"
    assert data["codex_schema_version"] == 1
    assert data["codex_run_nonce"] == nonce
    assert data["active"] is True

    # Omitted integration token fields and flags
    for field in (
        "jobber_connected", "jobber_access_token", "jobber_refresh_token",
        "jobber_generation", "jobber_lifecycle_epoch", "jobber_token_envelope_required",
        "google_calendar_connected", "google_calendar_access_token",
        "google_calendar_refresh_token", "google_calendar_generation",
        "google_calendar_lifecycle_epoch", "google_calendar_token_envelope_required",
        "automation_approvals", "gated_actions", "integration_write_status",
    ):
        assert field not in data


def test_seed_contractor_fails_closed_on_existing_document_without_mutation():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()

    existing_data = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 42,
        "jobber_access_token": {"schema_version": 1, "key_version": 1},
        "custom_important_field": "keep_me",
    }
    doc = db.collection("contractors").document(cid)
    doc.data = dict(existing_data)

    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.seed_contractor(db, cid, run_nonce=nonce)
    assert str(exc_info.value) == "contractor_seed_failed"

    # Document must remain byte-for-byte unchanged
    assert doc.data == existing_data


# ---------------------------------------------------------------------------
# Atomic Proof-Bound Firestore Cleanup & Call Ownership Tests
# ---------------------------------------------------------------------------

def test_cleanup_firestore_deletes_only_verified_owned_records():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()

    smoke.seed_contractor(db, cid, run_nonce=nonce)
    smoke.seed_cross_tenant_call(db, call_sid, smoke_contractor_id=cid, run_nonce=nonce)

    contractor_doc = db.collection("contractors").document(cid)
    call_doc = db.collection("calls").document(call_sid)
    assert contractor_doc.exists
    assert call_doc.exists

    smoke.cleanup_firestore(db, cid, run_nonce=nonce, created_call_sids=[call_sid])
    assert not contractor_doc.exists
    assert not call_doc.exists


def test_cleanup_firestore_preserves_orphan_preferences_document():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()

    smoke.seed_contractor(db, cid, run_nonce=nonce)
    contractor_doc = db.collection("contractors").document(cid)

    prefs_doc = contractor_doc.collection("settings").document("preferences")
    prefs_doc.data = {"custom_preference": "keep_intact"}
    assert prefs_doc.exists

    smoke.cleanup_firestore(db, cid, run_nonce=nonce)
    assert not contractor_doc.exists
    assert prefs_doc.exists
    assert prefs_doc.data == {"custom_preference": "keep_intact"}


def test_cleanup_firestore_aborts_when_nonce_or_owner_mismatched():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce_seed = smoke.generate_run_nonce()
    nonce_other = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()

    smoke.seed_contractor(db, cid, run_nonce=nonce_seed)
    smoke.seed_cross_tenant_call(db, call_sid, smoke_contractor_id=cid, run_nonce=nonce_seed)

    contractor_doc = db.collection("contractors").document(cid)
    call_doc = db.collection("calls").document(call_sid)

    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.cleanup_firestore(db, cid, run_nonce=nonce_other, created_call_sids=[call_sid])
    assert str(exc_info.value) == "contractor_ownership_mismatch"

    assert contractor_doc.exists
    assert call_doc.exists


def test_cleanup_firestore_aborts_and_leaves_unowned_document_untouched():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()

    unowned_data = {
        "contractor_id": cid,
        "owner_name": "Real User",
        "jobber_connected": True,
        "jobber_generation": 5,
    }
    doc = db.collection("contractors").document(cid)
    doc.data = dict(unowned_data)

    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.cleanup_firestore(db, cid, run_nonce=nonce)
    assert str(exc_info.value) == "contractor_ownership_mismatch"

    assert doc.exists
    assert doc.data == unowned_data


def test_cleanup_firestore_handles_hostile_objects_and_keys_safely():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()

    hostile_doc_data = {
        "contractor_id": cid,
        "codex_managed": True,
        "codex_purpose": _HostileObj(),
        "codex_schema_version": 1,
        "codex_run_nonce": nonce,
    }
    doc = db.collection("contractors").document(cid)
    doc.data = dict(hostile_doc_data)

    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.cleanup_firestore(db, cid, run_nonce=nonce)
    assert str(exc_info.value) == "contractor_ownership_mismatch"
    assert doc.exists

    hostile_key = _HostileKeyObj()
    hostile_key_dict = {hostile_key: "val"}
    hostile_key._inserted = True
    doc.data = hostile_key_dict

    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.cleanup_firestore(db, cid, run_nonce=nonce)
    assert str(exc_info.value) == "contractor_ownership_mismatch"
    assert doc.exists


def test_call_create_only_and_mismatch_cleanup():
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    cid = smoke.generate_contractor_id()
    call_sid = smoke.generate_call_sid()
    nonce = smoke.generate_run_nonce()

    smoke.seed_cross_tenant_call(db, call_sid, smoke_contractor_id=cid, run_nonce=nonce)
    doc = db.collection("calls").document(call_sid)
    assert doc.exists
    original_data = dict(doc.data)

    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.seed_cross_tenant_call(db, call_sid, smoke_contractor_id=cid, run_nonce=nonce)
    assert str(exc_info.value) == "call_seed_failed"

    assert doc.data == original_data

    other_cid = smoke.generate_contractor_id()
    with pytest.raises(smoke.SmokeFailure) as exc_info2:
        smoke.cleanup_firestore(
            db,
            other_cid,
            run_nonce=nonce,
            created_call_sids=[call_sid],
        )
    assert str(exc_info2.value) == "call_ownership_mismatch"

    assert doc.exists
    assert doc.data == original_data


# ---------------------------------------------------------------------------
# RTDB Transaction Helper & Exact Exception Class Tests
# ---------------------------------------------------------------------------

def test_rtdb_exact_owned_payload_matcher():
    smoke = _load_smoke_module()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()

    valid_payload = smoke.build_rtdb_call_payload(call_sid, cid, run_nonce=nonce, now=1000.0)
    assert smoke.is_exact_owned_rtdb_payload(valid_payload, contractor_id=cid, call_sid=call_sid, run_nonce=nonce) is True

    partial_payload = {k: v for k, v in valid_payload.items() if k != "transcript_buffer"}
    assert smoke.is_exact_owned_rtdb_payload(partial_payload, contractor_id=cid, call_sid=call_sid, run_nonce=nonce) is False


def test_rtdb_create_transaction_aborts_on_conflict_with_zero_write():
    smoke = _load_smoke_module()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()

    payload = smoke.build_rtdb_call_payload(call_sid, cid, run_nonce=nonce, now=1000.0)

    ref_empty = _FakeRTDBReference(data=None)
    res = ref_empty.transaction(lambda curr: smoke.rtdb_create_transaction_update(curr, payload))
    assert res == payload
    assert ref_empty.data == payload
    assert ref_empty.write_count == 1

    existing_node = {"call_sid": call_sid, "existing_field": "val"}
    ref_existing = _FakeRTDBReference(data=dict(existing_node))
    with pytest.raises(smoke.RTDBCreateConflictError):
        ref_existing.transaction(lambda curr: smoke.rtdb_create_transaction_update(curr, payload))

    assert ref_existing.data == existing_node
    assert ref_existing.write_count == 0


def test_rtdb_cleanup_transaction_exact_exception_classes():
    """Assert exact RTDB exception classes: absence sentinel only for None; mismatch error only for hostile/mismatched nodes."""
    smoke = _load_smoke_module()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()

    owned_payload = smoke.build_rtdb_call_payload(call_sid, cid, run_nonce=nonce, now=1000.0)

    # 1. Exact match -> returns None (deletes node)
    ref_owned = _FakeRTDBReference(data=dict(owned_payload))
    res = ref_owned.transaction(
        lambda curr: smoke.rtdb_cleanup_transaction_update(
            curr,
            contractor_id=cid,
            call_sid=call_sid,
            run_nonce=nonce,
        )
    )
    assert res is None
    assert ref_owned.data is None

    # 2. Absent node -> raises exact RTDBCleanupAbsenceSentinel
    ref_absent = _FakeRTDBReference(data=None)
    with pytest.raises(smoke.RTDBCleanupAbsenceSentinel):
        ref_absent.transaction(
            lambda curr: smoke.rtdb_cleanup_transaction_update(
                curr,
                contractor_id=cid,
                call_sid=call_sid,
                run_nonce=nonce,
            )
        )
    assert ref_absent.data is None

    # 3. Mismatched node -> raises exact RTDBCleanupMismatchError and preserves node
    bad_nonce_payload = dict(owned_payload, codex_run_nonce="other_nonce_0000000000000000000")
    ref_bad_nonce = _FakeRTDBReference(data=dict(bad_nonce_payload))
    with pytest.raises(smoke.RTDBCleanupMismatchError):
        ref_bad_nonce.transaction(
            lambda curr: smoke.rtdb_cleanup_transaction_update(
                curr,
                contractor_id=cid,
                call_sid=call_sid,
                run_nonce=nonce,
            )
        )
    assert ref_bad_nonce.data == bad_nonce_payload

    # 4. Hostile object in payload -> raises exact RTDBCleanupMismatchError and preserves node
    hostile_payload = dict(owned_payload, codex_purpose=_HostileObj())
    ref_hostile = _FakeRTDBReference(data=dict(hostile_payload))
    with pytest.raises(smoke.RTDBCleanupMismatchError):
        ref_hostile.transaction(
            lambda curr: smoke.rtdb_cleanup_transaction_update(
                curr,
                contractor_id=cid,
                call_sid=call_sid,
                run_nonce=nonce,
            )
        )
    assert ref_hostile.data == hostile_payload


# ---------------------------------------------------------------------------
# Sentinel Injection & Privacy Hardening Tests
# ---------------------------------------------------------------------------

def test_sentinels_non_disclosure_across_all_failure_paths(monkeypatch, capsys):
    """Inject unique secret sentinels across every failure surface and verify non-disclosure."""
    smoke = _load_smoke_module()

    secret_sentinels = [
        "secret_url_sentinel_111",
        "secret_firestore_const_222",
        "secret_seed_sentinel_333",
        "secret_http_sentinel_444",
        "secret_json_sentinel_555",
        "secret_revision_sentinel_666",
        "secret_rtdb_sentinel_777",
        "secret_cleanup_sentinel_888",
        "secret_main_primary_999",
        "secret_main_cleanup_000",
    ]

    # 1. URL parse error sentinel
    with pytest.raises(smoke.SmokeFailure) as exc_info:
        smoke.fail_if_not_staging(
            smoke.REQUIRED_STAGING_PROJECT,
            f"{smoke.REQUIRED_STAGING_BASE_URL}?secret={secret_sentinels[0]}",
        )
    assert secret_sentinels[0] not in str(exc_info.value)
    assert str(exc_info.value) == "invalid_base_url"

    # 2. Main execution with primary + cleanup failure sentinels
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    owned_doc = _FakeDocRef({
        "contractor_id": cid,
        "codex_managed": True,
        "codex_purpose": smoke.CODEX_PURPOSE,
        "codex_schema_version": smoke.CODEX_SCHEMA_VERSION,
        "codex_run_nonce": nonce,
    }, doc_id=cid)

    db = _FakeFirestoreClient()
    db._collections["contractors"][cid] = owned_doc

    class _FakeArgs:
        target = "staging"
        url = smoke.REQUIRED_STAGING_BASE_URL
        base_url = smoke.REQUIRED_STAGING_BASE_URL
        database_url = ""
        cleanup = True
        mutable_checks = False
        contractor_id = cid
        project = smoke.REQUIRED_STAGING_PROJECT
        expected_sha = ""
        require_expected_sha = False

    monkeypatch.setattr(smoke, "parse_args", lambda *a, **kw: _FakeArgs())
    monkeypatch.setattr("google.cloud.firestore.Client", lambda **kw: db)
    monkeypatch.setattr(smoke, "generate_run_nonce", lambda: nonce)
    monkeypatch.setattr(smoke, "health_check", lambda *a, **kw: ({"revision": secret_sentinels[5]}, True))
    monkeypatch.setattr(smoke, "seed_contractor", lambda *a, **kw: "test-token")

    def _failing_api_smoke(*args, **kwargs):
        raise smoke.SmokeFailure(f"primary_fail_{secret_sentinels[8]}")

    def _failing_cleanup(*args, **kwargs):
        raise RuntimeError(f"cleanup_fail_{secret_sentinels[9]}")

    monkeypatch.setattr(smoke, "read_only_api_smoke", _failing_api_smoke)
    monkeypatch.setattr(smoke, "cleanup_firestore", _failing_cleanup)

    exit_code = smoke.main()
    assert exit_code == 1

    captured = capsys.readouterr()
    # Check that primary failure code is preserved and cleanup code is reported
    assert captured.err == "FAIL: unknown_error\nCLEANUP_FAIL: unknown_error\n"

    # Assert NONE of the secret sentinels appear in stdout, stderr, or exceptions!
    for sentinel in secret_sentinels:
        assert sentinel not in captured.out
        assert sentinel not in captured.err


def test_exit_0_success_prints_bounded_steps_once(monkeypatch, capsys):
    """Test that an exit-0 smoke run emits each fixed step result once to stdout."""
    smoke = _load_smoke_module()
    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()

    db = _FakeFirestoreClient()

    class _FakeArgs:
        target = "staging"
        url = smoke.REQUIRED_STAGING_BASE_URL
        base_url = smoke.REQUIRED_STAGING_BASE_URL
        database_url = ""
        cleanup = True
        mutable_checks = False
        contractor_id = cid
        project = smoke.REQUIRED_STAGING_PROJECT
        expected_sha = ""
        require_expected_sha = False

    monkeypatch.setattr(smoke, "parse_args", lambda *a, **kw: _FakeArgs())
    monkeypatch.setattr("google.cloud.firestore.Client", lambda **kw: db)
    monkeypatch.setattr(smoke, "generate_run_nonce", lambda: nonce)
    monkeypatch.setattr(smoke, "health_check", lambda *a, **kw: ({"revision": "rev123"}, True))
    monkeypatch.setattr(smoke, "seed_contractor", lambda *a, **kw: "test-token")
    monkeypatch.setattr(smoke, "read_only_api_smoke", lambda *a, **kw: [smoke.Step("scoped contractor profile", "PASS")])

    exit_code = smoke.main()
    assert exit_code == 0

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) > 0

    # Ensure step names are printed and unique
    step_names = [line.split(":")[1].split("-")[0].strip() for line in lines if ":" in line]
    assert len(step_names) == len(set(step_names))
    assert "staging health" in step_names
    assert "staging contractor seeded" in step_names
    assert "scoped contractor profile" in step_names
    assert "staging cleanup" in step_names


def test_18qf_mutable_text_reply_smoke_real_propagation_via_main(capsys, monkeypatch):
    """Drive real smoke.main() under simultaneous primary text-reply failure and RTDB cleanup mismatch."""
    smoke = _load_smoke_module()
    monkeypatch.setattr(smoke, "ALLOWED_STAGING_DATABASE_URLS", frozenset({"https://hey-kevin-staging-default-rtdb.firebaseio.com"}))

    cid = smoke.generate_contractor_id()
    nonce = smoke.generate_run_nonce()
    call_sid = smoke.generate_call_sid()
    db = _FakeFirestoreClient()

    class _FakeArgs:
        target = "staging"
        url = smoke.REQUIRED_STAGING_BASE_URL
        base_url = smoke.REQUIRED_STAGING_BASE_URL
        database_url = "https://hey-kevin-staging-default-rtdb.firebaseio.com"
        cleanup = True
        mutable_checks = True
        contractor_id = cid
        project = smoke.REQUIRED_STAGING_PROJECT
        expected_sha = "sha123"
        require_expected_sha = True

    class FakeRef:
        def transaction(self, txn_func):
            if "rtdb_create_transaction_update" in txn_func.__name__ or "_create_txn" in txn_func.__name__:
                return smoke.build_rtdb_call_payload(
                    call_sid,
                    cid,
                    run_nonce=nonce,
                    now=1000.0,
                )
            raise smoke.RTDBCleanupMismatchError("cleanup_mismatch")

    fake_app = object()
    monkeypatch.setattr("firebase_admin.initialize_app", lambda *a, **kw: fake_app)
    monkeypatch.setattr("firebase_admin.db.reference", lambda *a, **kw: FakeRef())
    monkeypatch.setattr("firebase_admin.delete_app", lambda app: None)

    monkeypatch.setattr(smoke, "parse_args", lambda *a, **kw: _FakeArgs())
    monkeypatch.setattr("google.cloud.firestore.Client", lambda **kw: db)
    monkeypatch.setattr(smoke, "generate_run_nonce", lambda: nonce)
    monkeypatch.setattr(smoke, "generate_call_sid", lambda: call_sid)
    monkeypatch.setattr(smoke, "health_check", lambda *a, **kw: ({"revision": "rev123"}, True))
    monkeypatch.setattr(smoke, "seed_contractor", lambda *a, **kw: "test-token")
    monkeypatch.setattr(smoke, "read_only_api_smoke", lambda *a, **kw: [])
    monkeypatch.setattr(smoke, "mutable_firestore_gate_smoke", lambda *a, **kw: [])

    def fake_request_json(*args, **kwargs):
        raise smoke.SmokeFailure("text_reply_http_500")
    monkeypatch.setattr(smoke, "request_json", fake_request_json)

    exit_code = smoke.main()
    assert exit_code == 1

    captured = capsys.readouterr()
    expected_stderr = "FAIL: text_reply_http_500\nCLEANUP_FAIL: rtdb_cleanup_failed\n"
    assert captured.err == expected_stderr
    reversed_stderr = "CLEANUP_FAIL: rtdb_cleanup_failed\nFAIL: text_reply_http_500\n"
    assert captured.err != reversed_stderr


def test_18qe_causal_print_step_hostile_and_invalid_inputs(capsys):
    """Test print_step with hostile/subclass/non-Step inputs and invalid/non-string name/status/detail."""
    smoke = _load_smoke_module()

    class HostileStepSubclass(smoke.Step):
        def __str__(self):
            raise RuntimeError("HOSTILE_STR")
        def __repr__(self):
            raise RuntimeError("HOSTILE_REPR")

    subclass_obj = HostileStepSubclass(name="staging health", status="PASS")
    smoke.print_step(subclass_obj)
    assert capsys.readouterr().out == "FAIL: unknown_step\n"

    smoke.print_step({"name": "staging health", "status": "PASS"})
    assert capsys.readouterr().out == "FAIL: unknown_step\n"

    smoke.print_step(smoke.Step(name="<script>alert(1)</script>", status="PASS"))
    assert capsys.readouterr().out == "PASS: unknown_step\n"

    smoke.print_step(smoke.Step(name="staging health", status="ATTACKER_STATUS"))
    assert capsys.readouterr().out == "FAIL: staging health\n"

    smoke.print_step(smoke.Step(name="staging health", status="PASS", detail="ATTACKER_DETAIL"))
    assert capsys.readouterr().out == "PASS: staging health\n"


def test_18qg_request_json_status_code_validation(monkeypatch):
    """Call request_json with invalid/hostile/out-of-policy status_code values asserting mapping to 500 without disclosure."""
    smoke = _load_smoke_module()

    class HostileStatusObject:
        def __str__(self):
            raise RuntimeError("HOSTILE_STATUS_STR")
        def __repr__(self):
            raise RuntimeError("HOSTILE_STATUS_REPR")
        def __eq__(self, other):
            raise RuntimeError("HOSTILE_STATUS_EQ")

    invalid_statuses = [
        True,
        "200",
        200.0,
        -1,
        600,
        HostileStatusObject(),
        201,
    ]

    for bad_status in invalid_statuses:
        class FakeResponse:
            status_code = bad_status
            def json(self):
                return {"result": "ok"}

        monkeypatch.setattr("requests.request", lambda *a, **kw: FakeResponse())

        for exp_status in (200, 500):
            with pytest.raises(smoke.SmokeFailure) as exc_info:
                smoke.request_json("GET", "https://kevin-api-staging-l63rergg7a-uc.a.run.app", "/health", "health", expected_status=exp_status)

            err_msg = str(exc_info.value)
            assert err_msg == "health_http_500"
            assert err_msg in smoke.DERIVED_HTTP_DIAGNOSTIC_CODES
            assert "HOSTILE" not in err_msg
            assert "201" not in err_msg
            assert "600" not in err_msg

    # Control 1: Exact integer 500 with expected_status=500 succeeds
    class Fake500Response:
        status_code = 500
        def json(self):
            return {"error": "internal"}

    monkeypatch.setattr("requests.request", lambda *a, **kw: Fake500Response())
    st, body = smoke.request_json("GET", "https://kevin-api-staging-l63rergg7a-uc.a.run.app", "/health", "health", expected_status=500)
    assert st == 500
    assert body == {"error": "internal"}

    # Control 2: Exact integer 200 with expected_status=200 succeeds
    class Fake200Response:
        status_code = 200
        def json(self):
            return {"status": "ok"}

    monkeypatch.setattr("requests.request", lambda *a, **kw: Fake200Response())
    st, body = smoke.request_json("GET", "https://kevin-api-staging-l63rergg7a-uc.a.run.app", "/health", "health", expected_status=200)
    assert st == 200
    assert body == {"status": "ok"}


def test_18qg_exact_authoritative_diagnostic_codes_set_literals():
    """Assert exact equality of smoke.AUTHORITATIVE_DIAGNOSTIC_CODES against independently declared test literals."""
    smoke = _load_smoke_module()

    test_literal_base_codes = {
        "non_staging_project",
        "invalid_base_url",
        "invalid_database_url",
        "non_staging_database_url",
        "non_canonical_database_url",
        "no_authoritative_staging_rtdb_url",
        "invalid_call_sid",
        "invalid_contractor_id",
        "invalid_run_nonce",
        "contractor_seed_failed",
        "call_seed_failed",
        "contractor_ownership_mismatch",
        "call_ownership_mismatch",
        "firestore_cleanup_failed",
        "health_not_ok",
        "health_not_staging",
        "sha_mismatch",
        "contractor_id_mismatch",
        "profile_leaked_token_hash",
        "active_call_not_inactive",
        "calls_not_list",
        "jobs_not_list",
        "settings_missing_defaults",
        "jobber_not_disconnected",
        "calendar_not_disconnected",
        "estimate_gate_not_denied",
        "rtdb_payload_verification_failed",
        "text_reply_gate_not_denied",
        "rtdb_create_conflict",
        "rtdb_cleanup_failed",
        "rtdb_cleanup_absence",
        "rtdb_cleanup_mismatch",
        "delete_app_cleanup_failed",
        "unknown_error",
        "firestore_client_construction_failed",
        "http_request_failed",
        "url_parse_error",
    }
    test_literal_route_labels = {
        "health",
        "contractor_profile",
        "cross_contractor_profile",
        "active_call",
        "calls",
        "jobs",
        "settings",
        "jobber_status",
        "google_calendar_status",
        "estimate_token_gate",
        "cross_tenant_call_action",
        "text_reply",
    }
    test_literal_allowed_statuses = (0, 200, 400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504)
    test_literal_derived_http = {
        f"{route}_http_{status}"
        for route in test_literal_route_labels
        for status in test_literal_allowed_statuses
    }
    test_literal_expected_set = test_literal_base_codes | test_literal_derived_http

    assert smoke.AUTHORITATIVE_DIAGNOSTIC_CODES == test_literal_expected_set
