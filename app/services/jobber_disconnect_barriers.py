"""Strict sticky Jobber account-barrier admission; no provider calls."""
from __future__ import annotations
from typing import Any

from app.config import settings
from app.db.jobber_disconnect_receipts import (
    MAX_RECEIPT_COUNTER, JobberAccountBarrierError, is_valid_opaque_id,
    read_barrier_in_txn, read_control_in_txn,
)

from app.services.integration_tokens import (
    jobber_local_metadata_usable, parse_durable_lifecycle_counters,
    parse_provider_operation_intent, resolve_usable_token_pair, validate_token_string,
)


class JobberBarrierBlockedError(Exception):
    pass


class JobberEnforcementConfigurationError(Exception):
    pass


ENFORCEMENT_FIELDS = frozenset({
    "jobber_app_id", "jobber_accepted_disconnect_barrier", "jobber_disconnect_state",
    "jobber_disconnect_processed_barrier", "jobber_disconnect_pending",
    "jobber_disconnect_pending_barrier", "jobber_disconnect_pending_at",
    "jobber_disconnect_pending_receipt_id",
})


def is_jobber_disconnect_enforced_in_config() -> bool:
    mode = settings.jobber_disconnect_enforcement_mode
    if type(mode) is not str or mode not in ("off", "enforce", "true", "enabled", "on"):
        raise JobberEnforcementConfigurationError("Invalid enforcement mode")
    return settings.jobber_disconnect_enforce is True or mode != "off"


def get_effective_jobber_barrier_in_txn(transaction: Any, db: Any,
                                       app_id: str | None = None,
                                       account_id: str | None = None) -> tuple[bool, int, str | None]:
    # Even preactivation operations observe initialization transactionally.
    control = read_control_in_txn(transaction, db)
    configured = is_jobber_disconnect_enforced_in_config()
    if control is None:
        if configured:
            raise JobberEnforcementConfigurationError("Configured control missing")
        return False, 0, None
    expected = control["expected_app_id"]
    configured_app = settings.jobber_disconnect_expected_app_id
    if configured_app and configured_app != expected:
        raise JobberEnforcementConfigurationError("Configured app mismatch")
    if app_id is not None and app_id != expected:
        raise JobberAccountBarrierError("App mismatch")
    if account_id is None:
        return True, 0, expected
    if not is_valid_opaque_id(account_id):
        raise JobberAccountBarrierError("Invalid account")
    barrier = read_barrier_in_txn(transaction, db, expected, account_id)
    value = barrier["barrier"] if barrier else 0
    if value > control["receipt_counter"]:
        raise JobberAccountBarrierError("Barrier exceeds control")
    return True, value, expected


def _bounded(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_RECEIPT_COUNTER


def _require_ordinary_pair(data: dict[str, Any], contractor_id: str | None, *, preactivation: bool) -> None:
    valid_lifecycle, _, _, lifecycle_present, _ = parse_durable_lifecycle_counters(data, "jobber")
    legacy = preactivation and valid_lifecycle and not lifecycle_present
    if not legacy and data.get("jobber_connected") is not True:
        raise JobberBarrierBlockedError("Binding is not connected")
    # Envelope authentication needs the actual contractor identity, never a synthetic AAD.
    if contractor_id is not None or "contractor_id" in data or "id" in data:
        access, refresh = resolve_usable_token_pair(data, "jobber", contractor_id=contractor_id)
        if access is None or refresh is None:
            raise JobberBarrierBlockedError("Binding has no usable credential pair")
        return
    # Plaintext-only callers can apply the same pure predicates without an AAD identity.
    valid_lifecycle, *_ = parse_durable_lifecycle_counters(data, "jobber")
    intent_status, _, _ = parse_provider_operation_intent(data, "jobber")
    floor = data.get("jobber_token_envelope_required", False)
    if (not valid_lifecycle or intent_status != "absent" or not jobber_local_metadata_usable(data)
            or type(floor) is not bool or floor):
        raise JobberBarrierBlockedError("Binding is not usable")
    try:
        for key in ("jobber_access_token", "jobber_refresh_token"):
            if type(data.get(key)) is not str:
                raise ValueError("Missing plaintext pair or contractor identity")
            validate_token_string(data[key], name=key)
    except Exception:
        raise JobberBarrierBlockedError("Binding has no usable credential pair") from None


def verify_jobber_barrier_in_txn(transaction: Any, db: Any, contractor_data: dict[str, Any],
                               observed_app_id: str | None = None,
                               observed_account_id: str | None = None, *,
                               contractor_id: str | None = None) -> int:
    if type(contractor_data) is not dict:
        raise JobberBarrierBlockedError("Invalid binding")
    app_id = contractor_data.get("jobber_app_id")
    account_id = contractor_data.get("jobber_account_id")
    enforced, barrier, expected = get_effective_jobber_barrier_in_txn(
        transaction, db, app_id, account_id)
    if observed_app_id is not None and observed_app_id != app_id:
        raise JobberBarrierBlockedError("Observed app changed")
    if observed_account_id is not None and observed_account_id != account_id:
        raise JobberBarrierBlockedError("Observed account changed")
    if not enforced and ENFORCEMENT_FIELDS.intersection(contractor_data):
        raise JobberEnforcementConfigurationError("Binding metadata without control")
    for key in ("jobber_reauthorization_required", "jobber_disconnect_pending"):
        if key in contractor_data:
            if type(contractor_data[key]) is not bool or contractor_data[key]:
                raise JobberBarrierBlockedError("Binding suspended or malformed")
    state_present = "jobber_disconnect_state" in contractor_data
    processed_present = "jobber_disconnect_processed_barrier" in contractor_data
    if state_present or processed_present:
        if (not state_present or not processed_present
                or contractor_data["jobber_disconnect_state"] != "reauthorization_required"
                or not _bounded(contractor_data["jobber_disconnect_processed_barrier"])
                or not 1 <= contractor_data["jobber_disconnect_processed_barrier"] <= barrier):
            raise JobberBarrierBlockedError("Invalid suspension metadata")
        raise JobberBarrierBlockedError("Fresh reauthorization required")
    if enforced:
        accepted = contractor_data.get("jobber_accepted_disconnect_barrier")
        if app_id != expected or not is_valid_opaque_id(app_id) or not is_valid_opaque_id(account_id):
            raise JobberBarrierBlockedError("Missing or foreign binding identity")
        if not _bounded(accepted) or accepted != barrier:
            raise JobberBarrierBlockedError("Binding barrier is not current")
    _require_ordinary_pair(contractor_data, contractor_id, preactivation=not enforced)
    return barrier
