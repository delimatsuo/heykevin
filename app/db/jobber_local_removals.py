"""Immutable audit for an explicit local Jobber credential removal.

This is deliberately separate from provider-revocation outbox/audit records.
"""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any
from google.cloud.firestore_v1 import DELETE_FIELD
from app.db.integration_lifecycle_audit import VALID_DISCONNECT_ACTORS
from app.services.integration_tokens import MAX_KEY_VERSION, IntegrationTokenCASConflict, validate_token_string

COLLECTION = "jobber_local_removal_audit"
KEYS = frozenset({"schema_version", "action", "contractor_id", "provider", "generation",
    "lifecycle_epoch", "actor", "reason", "credential_deletion_disposition", "revocation_status", "timestamp"})


def audit_id(contractor_id, generation):
    validate_token_string(contractor_id, name="contractor_id")
    if type(generation) is not int or not 0 <= generation <= MAX_KEY_VERSION:
        raise IntegrationTokenCASConflict("Invalid local removal generation")
    payload = json.dumps(["jobber-local-removal-v1", contractor_id, "jobber", generation], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_audit(data):
    if type(data) is not dict or any(type(k) is not str for k in data) or set(data) != KEYS:
        raise IntegrationTokenCASConflict("Invalid local removal audit")
    fixed = dict(schema_version=1, action="credentials_deleted_locally", provider="jobber",
        reason="provider_disconnect", credential_deletion_disposition="executed", revocation_status="not_attempted_local_only")
    if any(type(data[k]) is not type(v) or data[k] != v for k, v in fixed.items()):
        raise IntegrationTokenCASConflict("Invalid local removal audit")
    if type(data["actor"]) is not str or data["actor"] not in VALID_DISCONNECT_ACTORS:
        raise IntegrationTokenCASConflict("Invalid local removal actor")
    validate_token_string(data["contractor_id"], name="contractor_id")
    for key in ("generation", "lifecycle_epoch"):
        if type(data[key]) is not int or not 1 <= data[key] <= MAX_KEY_VERSION:
            raise IntegrationTokenCASConflict("Invalid local removal lifecycle")
    stamp = data["timestamp"]
    if type(stamp) is not float or not math.isfinite(stamp) or stamp <= 0:
        raise IntegrationTokenCASConflict("Invalid local removal timestamp")
    return dict(data)


async def remove_if_blocked(*, contractor_id, actor="contractor_api", db=None, **unused):
    from app.services import jobber_admissions as admission
    mutation = admission._mutations()
    db = admission._database(db)
    if type(actor) is not str or actor not in VALID_DISCONNECT_ACTORS:
        raise IntegrationTokenCASConflict("Invalid local removal actor")
    def result(data, audit, identifier, *, attempted):
        return mutation.DisconnectProviderResult(contractor_id=contractor_id, provider="jobber",
            generation=audit["generation"], lifecycle_epoch=audit["lifecycle_epoch"], audit_id=identifier,
            outbox_id=None, credential_deletion="executed", revocation_status="not_attempted_local_only",
            audit_finalized=True, created_at=audit["timestamp"], updated_at=audit["timestamp"],
            expected_disconnected_at=data["jobber_disconnected_at"],
            expected_floor=data.get("jobber_token_envelope_required"), local_only=True,
            local_deletion_attempted=attempted)
    def body(txn, box):
        ref, data, now = admission._contractor(db, txn, contractor_id)
        gen, epoch, _ = admission._life(data)
        blocked = admission.provider_binding_blocked(db, txn, data)
        if mutation.is_durable_provider_tombstone(data, "jobber", contractor_id) and not admission.BINDING_KEYS.intersection(data):
            identifier = audit_id(contractor_id, gen)
            audit_ref = db.collection(COLLECTION).document(identifier)
            snap = admission._read(audit_ref, txn)
            if not snap.exists:
                return None
            audit = validate_audit(snap.to_dict())
            if audit["contractor_id"] != contractor_id or audit["generation"] != gen or audit["lifecycle_epoch"] != epoch or audit["timestamp"] != data["jobber_disconnected_at"]:
                raise IntegrationTokenCASConflict("Local removal audit collision")
            box.update(audit=(audit_ref, audit), updates={}, result_data=data)
            return result(data, audit, identifier, attempted=False)
        if not blocked:
            return None
        # This branch is reachable only through authenticated disconnect APIs.
        # It does not decrypt, reserialize or use retained provider credentials.
        gen, epoch = admission._next(gen), admission._next(epoch)
        identifier = audit_id(contractor_id, gen)
        audit_ref = db.collection(COLLECTION).document(identifier)
        if admission._read(audit_ref, txn).exists:
            raise IntegrationTokenCASConflict("Local removal audit collision")
        if "jobber_token_envelope_required" in data and type(data["jobber_token_envelope_required"]) is not bool:
            raise IntegrationTokenCASConflict("Malformed encryption floor")
        audit = validate_audit(dict(schema_version=1, action="credentials_deleted_locally",
            contractor_id=contractor_id, provider="jobber", generation=gen, lifecycle_epoch=epoch,
            actor=actor, reason="provider_disconnect", credential_deletion_disposition="executed",
            revocation_status="not_attempted_local_only", timestamp=now))
        updates = {k: DELETE_FIELD for k in mutation._build_tombstone_forbidden_fields("jobber") | admission.BINDING_KEYS}
        updates.update(jobber_connected=False, jobber_generation=gen, jobber_lifecycle_epoch=epoch,
                       jobber_disconnected_at=now, jobber_lead_capture_enabled=False)
        if any(type(data.get(k)) is dict for k in ("jobber_access_token", "jobber_refresh_token")):
            updates["jobber_token_envelope_required"] = True
        final = {k: v for k, v in data.items() if k not in updates}
        final.update({k: v for k, v in updates.items() if v is not DELETE_FIELD})
        box.update(audit=(audit_ref, audit), updates=updates, result_data=final)
        txn.create(audit_ref, audit)
        txn.update(ref, updates)
        return result(final, audit, identifier, attempted=True)
    def verify(txn, box):
        if box["result"] is None:
            # The normal disconnect transaction will re-read authority itself.
            return None
        data = admission._verify_updates(db, txn, contractor_id, box["updates"], audit=box["audit"],
            authority=lambda tx, d: admission._status_in_txn(db, tx, d, contractor_id))
        expected = box["result_data"]
        if (admission.BINDING_KEYS.intersection(data)
                or not mutation.is_durable_provider_tombstone(data, "jobber", contractor_id)
                or admission._life(data)[:2] != admission._life(expected)[:2]
                or data["jobber_disconnected_at"] != expected["jobber_disconnected_at"]
                or ("jobber_token_envelope_required" in data) != ("jobber_token_envelope_required" in expected)
                or data.get("jobber_token_envelope_required") is not expected.get("jobber_token_envelope_required")):
            raise IntegrationTokenCASConflict("Local removal superseded")
        return box["result"]
    return await admission._run(db, body, verify)


def orchestration_response(result):
    return dict(status="disconnected", contractor_id=result.contractor_id, provider="jobber", generation=result.generation,
        lifecycle_epoch=result.lifecycle_epoch, audit_id=result.audit_id, outbox_id=None,
        credential_deletion={"status": "executed", "attempted_by_this_request": result.local_deletion_attempted},
        provider_revocation={"status": "not_attempted_local_only", "attempted": False, "attempted_by_this_request": False},
        audit_finalization={"status": "finalized", "finalized": True, "attempted_by_this_request": False},
        revocation_status="not_attempted_local_only")
