"""Tests for the payload-safe Task 4.8 pre-auth store observation record."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.voice_bakeoff_preauth_reference import (
    PreAuthReferenceError,
    canonical_digest,
    validate_preauth_store_reference,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "tests/fixtures/voice_architecture_bakeoff"
    / "task_4_8_gate_package.template.json"
)


def _reference() -> dict[str, object]:
    package = json.loads(_PACKAGE_PATH.read_text(encoding="utf-8"))
    reference = package["preauth_store_reference"]
    assert isinstance(reference, dict)
    return reference


def _rehash(reference: dict[str, object]) -> None:
    reference["observation_digest"] = canonical_digest(
        {
            key: reference[key]
            for key in sorted(reference)
            if key != "observation_digest"
        }
    )


def test_reference_is_non_authorizing_and_payload_safe() -> None:
    reference = _reference()
    validate_preauth_store_reference(reference)

    assert reference["isolation_scope"] == "administratively_separate_project"
    assert reference["documents_written_by_preparation"] == 0
    assert reference["document_readback"] == "not_performed"
    assert "does_not_authorize_execution" in reference["limitations"]
    serialized = json.dumps(reference, sort_keys=True)
    for prohibited in (
        "hk-voice-bakeoff-preauth-iso",
        "016AF3-DCA145-D2D640",
        "delimatsuo@gmail.com",
        "access_token",
        "private_key",
        "credential_value",
        "transcript",
        "https://",
    ):
        assert prohibited not in serialized


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("project_ref", "hk-voice-bakeoff-preauth-iso"),
        ("documents_written_by_preparation", False),
        ("document_readback", "database_empty"),
        ("region", "us-east1"),
        ("observation_digest", "0" * 64),
    ],
)
def test_reference_rejects_unsafe_or_overclaiming_mutations(
    key: str,
    replacement: object,
) -> None:
    reference = deepcopy(_reference())
    reference[key] = replacement

    with pytest.raises(PreAuthReferenceError):
        validate_preauth_store_reference(reference)


def test_reference_rejects_secret_like_or_extra_fields() -> None:
    reference = deepcopy(_reference())
    reference["credential_value"] = "not-a-secret"

    with pytest.raises(PreAuthReferenceError, match="closed"):
        validate_preauth_store_reference(reference)


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("project_ref", "ref_AKIAABCDEFGHIJKLMNOP"),
        ("database_ref", "ref_hk-voice-bakeoff-preauth-iso"),
    ],
)
def test_reference_rejects_rehashed_identifier_or_credential_like_refs(
    key: str,
    replacement: str,
) -> None:
    reference = deepcopy(_reference())
    reference[key] = replacement
    _rehash(reference)

    with pytest.raises(PreAuthReferenceError, match="approved opaque"):
        validate_preauth_store_reference(reference)


def test_reference_rejects_boolean_schema_version() -> None:
    reference = deepcopy(_reference())
    reference["schema_version"] = True
    _rehash(reference)

    with pytest.raises(PreAuthReferenceError, match="schema version"):
        validate_preauth_store_reference(reference)
