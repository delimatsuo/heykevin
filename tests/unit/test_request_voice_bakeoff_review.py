"""Tests for scripts/request_voice_bakeoff_review.py.

TechnicalReviewReceipt (app/services/voice_bakeoff_security_contracts.py)
requires provenance_ref to be an opaque "ref_"-prefixed reference (see
_require_ref) and every *_digest field to be a 64-character lowercase hex
string (see _require_digest). Fixtures below use "ref_"-prefixed
provenance refs for that reason — matching the convention already used in
tests/unit/test_voice_bakeoff_security_contracts.py — rather than the
un-prefixed "review-session-42" shape a first draft might guess at.
"""

import ast
import pathlib

import pytest

from scripts.request_voice_bakeoff_review import (
    build_receipt_request,
    parse_review_response,
    reviewer_is_procedurally_separate,
)


def test_receipt_request_contains_no_raw_payload_fields():
    request = build_receipt_request(
        "a" * 64, "b" * 64, source_sha="c" * 40, manifest_digest="d" * 64
    )
    assert request == {
        "payload_digest": "a" * 64,
        "binding_digest": "b" * 64,
        "source_sha": "c" * 40,
        "manifest_digest": "d" * 64,
    }


def test_parses_a_matching_clean_response():
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 0,
        "advisory_only": True,
    }
    receipt = parse_review_response(
        response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
    )
    assert receipt.unresolved_p1_count == 0
    assert receipt.advisory_only is True
    assert receipt.review_digest == "e" * 64
    assert receipt.provenance_ref == "ref_review-session-42"


def test_rejects_response_with_mismatched_payload_digest():
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "wrong",
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 0,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_rejects_response_with_mismatched_binding_digest():
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "wrong",
        "unresolved_p1_count": 0,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_rejects_response_with_unresolved_p1s():
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 2,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_rejects_response_with_boolean_p1_count_masquerading_as_zero():
    """False == 0 in Python, so a naive `!= 0` check alone would silently
    accept a boolean here. TechnicalReviewReceipt.__post_init__ requires
    the field's exact type to be int (type(x) is not int), so this must
    still end up rejected end-to-end."""
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": False,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_rejects_response_not_marked_advisory_only():
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 0,
        "advisory_only": False,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_rejects_response_missing_review_digest_with_value_error_not_key_error():
    """review_digest isn't covered by the earlier explicit checks, so a
    naive response["review_digest"] indexing lookup would raise KeyError
    instead of ValueError if the key were absent. This pins the contract
    that *every* malformed response raises ValueError, which is what a
    caller in this defensive codebase expects to catch."""
    response = {
        "provenance_ref": "ref_review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 0,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_rejects_response_missing_provenance_ref_with_value_error_not_key_error():
    response = {
        "review_digest": "e" * 64,
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 0,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_reviewer_must_be_procedurally_separate_from_signer():
    assert reviewer_is_procedurally_separate(
        signer_provenance_ref="ref_signer-session-1",
        reviewer_provenance_ref="ref_review-session-42",
    ) is True
    assert reviewer_is_procedurally_separate(
        signer_provenance_ref="ref_signer-session-1",
        reviewer_provenance_ref="ref_signer-session-1",
    ) is False


def test_module_performs_no_network_calls_at_import_time():
    source = pathlib.Path("scripts/request_voice_bakeoff_review.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
