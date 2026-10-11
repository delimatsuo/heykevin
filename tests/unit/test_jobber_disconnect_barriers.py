"""Strict ordinary-admission barrier contract; no provider or real datastore."""
from tests.unit.jobber_disconnect_test_support import seed_required_environment
seed_required_environment()

import pytest
from tests.unit.jobber_disconnect_test_support import jobber_settings, active_db, admit, binding, FakeFirestoreDb
from app.db.jobber_disconnect_transactions import run_transaction
from app.services.jobber_disconnect_barriers import verify_jobber_barrier_in_txn


def verify(db, data, **kwargs):
    kwargs.setdefault("contractor_id", "contractor")
    return run_transaction(db, lambda t: verify_jobber_barrier_in_txn(t, db, data, **kwargs), read_only=True)


def test_preactivation_only_without_metadata(jobber_settings):
    db = FakeFirestoreDb()
    legacy = {"jobber_connected": True, "jobber_generation": 0, "jobber_lifecycle_epoch": 0,
              "jobber_access_token": "fictional-access", "jobber_refresh_token": "fictional-refresh"}
    assert verify(db, legacy) == 0
    jobber_settings.jobber_disconnect_enforce = True
    with pytest.raises(Exception):
        verify(db, {})


@pytest.mark.parametrize("key", ["jobber_app_id", "jobber_accepted_disconnect_barrier",
    "jobber_disconnect_state", "jobber_disconnect_processed_barrier", "jobber_disconnect_pending"])
def test_even_null_metadata_without_control_fails(key):
    with pytest.raises(Exception):
        verify(FakeFirestoreDb(), {key: None})


def test_sticky_control_and_distinct_oauth_client(jobber_settings):
    db = active_db()
    data = binding(db)
    assert verify(db, data) == 0
    jobber_settings.jobber_disconnect_expected_app_id = ""
    assert verify(db, data) == 0
    admit(db)
    with pytest.raises(Exception):
        verify(db, data)
    data["jobber_accepted_disconnect_barrier"] = 1
    assert verify(db, data) == 1


@pytest.mark.parametrize("key,value", [
    ("jobber_accepted_disconnect_barrier", None), ("jobber_accepted_disconnect_barrier", True),
    ("jobber_accepted_disconnect_barrier", 1), ("jobber_accepted_disconnect_barrier", -1),
    ("jobber_app_id", "other"), ("jobber_app_id", None), ("jobber_account_id", None),
    ("jobber_disconnect_state", "unexpected"), ("jobber_disconnect_processed_barrier", True),
    ("jobber_disconnect_pending", "false"), ("jobber_reauthorization_required", "false"),
])
def test_hostile_binding_fails(key, value):
    db = active_db()
    data = binding(db)
    data[key] = value
    with pytest.raises(Exception):
        verify(db, data)


def test_foreign_observation_cannot_override_binding():
    db = active_db()
    data = binding(db)
    with pytest.raises(Exception):
        verify(db, data, observed_account_id="other")


def test_inactive_control_fails_with_all_flags_off():
    db = active_db()
    data = binding(db)
    db.collection("jobber_disconnect_control").document("current").update({"enforcement_active": False})
    with pytest.raises(Exception):
        verify(db, data)


@pytest.mark.parametrize("mode", ["empty", "disconnected", "tokenless", "one_sided", "malformed_floor"])
def test_ordinary_requires_connected_usable_pair(mode):
    db = active_db() if mode != "empty" else FakeFirestoreDb()
    data = binding(db) if mode != "empty" else {}
    if mode == "disconnected":
        data["jobber_connected"] = False
    elif mode == "tokenless":
        data.pop("jobber_access_token")
        data.pop("jobber_refresh_token")
    elif mode == "one_sided":
        data.pop("jobber_refresh_token")
    elif mode == "malformed_floor":
        data["jobber_token_envelope_required"] = 0
    with pytest.raises(Exception):
        verify(db, data)


def test_plaintext_identity_free_positive_and_envelope_identity_required():
    db = active_db()
    data = binding(db)
    with pytest.raises(Exception):
        verify(db, data, contractor_id=None)
    data.update(jobber_access_token="fictional-access", jobber_refresh_token="fictional-refresh",
                jobber_token_envelope_required=False)
    assert verify(db, data, contractor_id=None) == 0


@pytest.mark.parametrize("enforced", [False, True])
def test_missing_server_clock_even_on_missing_control_fails(enforced):
    db = active_db() if enforced else FakeFirestoreDb()
    data = binding(db)
    db.set_server_time(None)
    with pytest.raises(Exception):
        verify(db, data)


def test_preactivation_all_absent_lifecycle_legacy_pair_only():
    db = FakeFirestoreDb()
    legacy = {"jobber_access_token": "fictional-access", "jobber_refresh_token": "fictional-refresh"}
    assert verify(db, legacy) == 0
    assert verify(db, legacy, contractor_id=None) == 0
    for partial in ({"jobber_connected": True}, {"jobber_generation": 0}, {"jobber_connected": False}):
        with pytest.raises(Exception):
            verify(db, {**legacy, **partial})
    with pytest.raises(Exception):
        verify(active_db(), legacy)
