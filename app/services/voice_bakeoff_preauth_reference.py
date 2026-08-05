"""Validate the non-authorizing Task 4.8 pre-auth control-store reference.

This is a payload-safe, offline record of a control-plane observation.  It does
not resolve credentials, contact GCP, access Firestore documents, or authorize
provider execution.  In particular, an administratively separate project is
not an independent root of trust or a completed execution gate.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROJECT_REFERENCE = "ref_preauth_project_inventory_private_20260727"
_DATABASE_REFERENCE = "ref_preauth_database_inventory_private_20260727"
_KEYS = {
    "schema_version",
    "status",
    "isolation_scope",
    "project_ref",
    "database_ref",
    "region",
    "database_type",
    "concurrency_mode",
    "app_engine_integration",
    "point_in_time_recovery",
    "delete_protection",
    "version_retention_seconds",
    "free_tier",
    "no_user_managed_service_accounts_listed",
    "documents_written_by_preparation",
    "document_readback",
    "observed_at_ms",
    "limitations",
    "observation_digest",
}
_LIMITATIONS = (
    "not_an_independent_root_of_trust",
    "not_a_credential_broker",
    "not_a_durable_trust_or_revocation_store",
    "not_immutable_custody",
    "does_not_prove_database_empty",
    "does_not_authorize_execution",
)


class PreAuthReferenceError(ValueError):
    """Raised when a purported reference is not safe to report."""


def canonical_digest(value: object) -> str:
    """Return the canonical SHA-256 digest for payload-safe reference data."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reference_digest(reference: Mapping[str, object]) -> str:
    return canonical_digest(
        {
            key: reference[key]
            for key in sorted(reference)
            if key != "observation_digest"
        }
    )


def validate_preauth_store_reference(reference: Mapping[str, object]) -> None:
    """Require an exact, non-secret, non-authorizing observed snapshot."""
    if set(reference) != _KEYS:
        raise PreAuthReferenceError("reference schema is closed")
    if type(reference["schema_version"]) is not int or reference["schema_version"] != 1:
        raise PreAuthReferenceError("reference schema version is invalid")
    if reference["status"] != "reference_only_observed":
        raise PreAuthReferenceError("reference status is not non-authorizing")
    if reference["isolation_scope"] != "administratively_separate_project":
        raise PreAuthReferenceError("reference isolation scope is invalid")
    if reference["project_ref"] != _PROJECT_REFERENCE:
        raise PreAuthReferenceError("project_ref is not an approved opaque reference")
    if reference["database_ref"] != _DATABASE_REFERENCE:
        raise PreAuthReferenceError("database_ref is not an approved opaque reference")
    if reference["region"] != "us-central1":
        raise PreAuthReferenceError("reference region is invalid")
    if reference["database_type"] != "FIRESTORE_NATIVE":
        raise PreAuthReferenceError("reference database type is invalid")
    if reference["concurrency_mode"] != "PESSIMISTIC":
        raise PreAuthReferenceError("reference concurrency mode is invalid")
    if reference["app_engine_integration"] != "DISABLED":
        raise PreAuthReferenceError("reference App Engine posture is invalid")
    if reference["point_in_time_recovery"] != "DISABLED":
        raise PreAuthReferenceError("reference PITR posture is invalid")
    if reference["delete_protection"] != "DELETE_PROTECTION_DISABLED":
        raise PreAuthReferenceError("reference deletion posture is invalid")
    if reference["version_retention_seconds"] != 3600:
        raise PreAuthReferenceError("reference version retention is invalid")
    for key in (
        "free_tier",
        "no_user_managed_service_accounts_listed",
    ):
        if reference[key] is not True:
            raise PreAuthReferenceError(f"{key} must be true")
    writes = reference["documents_written_by_preparation"]
    if type(writes) is not int or writes != 0:
        raise PreAuthReferenceError("reference document writes are invalid")
    if reference["document_readback"] != "not_performed":
        raise PreAuthReferenceError("reference must not claim a database scan")
    observed_at_ms = reference["observed_at_ms"]
    if type(observed_at_ms) is not int or observed_at_ms < 1:
        raise PreAuthReferenceError("reference observation time is invalid")
    limitations = reference["limitations"]
    if not isinstance(limitations, list) or tuple(limitations) != _LIMITATIONS:
        raise PreAuthReferenceError("reference limitations are incomplete")
    digest = reference["observation_digest"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise PreAuthReferenceError("reference digest is invalid")
    if digest != _reference_digest(reference):
        raise PreAuthReferenceError("reference digest does not match")


__all__ = [
    "PreAuthReferenceError",
    "canonical_digest",
    "validate_preauth_store_reference",
]
