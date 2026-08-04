"""Independent review-receipt request/parsing for bakeoff approvals.

The receipt this produces is advisory only — it can never by itself
authorize a run. It must come from a procedurally separate reviewer (a
distinct provenance_ref) than whoever signs the approval in
scripts/sign_voice_bakeoff_approval.py; the runner enforces that
separation before accepting either.

This module builds only the non-sensitive request package a reviewer
needs (digests and metadata, never raw approval contents) and validates
the reviewer's response. It does not itself dispatch a reviewer process —
obtaining a response (an independent human, or an independently launched
review agent with no access to the signing step) is an operational
choice made when this CLI is run, not baked in here.
"""

from __future__ import annotations

from app.services.voice_bakeoff_security_contracts import TechnicalReviewReceipt


def build_receipt_request(
    payload_digest: str,
    binding_digest: str,
    *,
    source_sha: str,
    manifest_digest: str,
) -> dict:
    """Build the non-sensitive package an independent reviewer needs.

    Only digests and metadata identifying what to review — never raw
    approval payload contents — so a compromised or careless reviewer
    process cannot leak detail it was never given.
    """
    return {
        "payload_digest": payload_digest,
        "binding_digest": binding_digest,
        "source_sha": source_sha,
        "manifest_digest": manifest_digest,
    }


def parse_review_response(
    response: dict,
    *,
    expected_payload_digest: str,
    expected_binding_digest: str,
) -> TechnicalReviewReceipt:
    """Validate an untrusted reviewer response and construct a receipt.

    Every field is read with .get(), not indexed directly, so a missing
    key raises ValueError here or in TechnicalReviewReceipt.__post_init__
    (the final authority on field format) rather than an unhandled
    KeyError. The response dict is untrusted input from a procedurally
    separate reviewer process.
    """
    reviewed_payload_digest = response.get("reviewed_payload_digest")
    if reviewed_payload_digest != expected_payload_digest:
        raise ValueError("review response payload digest does not match the request")

    reviewed_binding_digest = response.get("reviewed_binding_digest")
    if reviewed_binding_digest != expected_binding_digest:
        raise ValueError("review response binding digest does not match the request")

    if response.get("unresolved_p1_count") != 0:
        raise ValueError("review response has unresolved P1 findings")
    if response.get("advisory_only") is not True:
        raise ValueError("review response must be marked advisory_only")

    return TechnicalReviewReceipt(
        review_digest=response.get("review_digest"),
        provenance_ref=response.get("provenance_ref"),
        reviewed_payload_digest=reviewed_payload_digest,
        reviewed_binding_digest=reviewed_binding_digest,
        unresolved_p1_count=response.get("unresolved_p1_count"),
        advisory_only=response.get("advisory_only"),
    )


def reviewer_is_procedurally_separate(
    *,
    signer_provenance_ref: str,
    reviewer_provenance_ref: str,
) -> bool:
    """Reject a reviewer that is the same process/session as the signer.

    The runner calls this before accepting either the signature (from
    scripts/sign_voice_bakeoff_approval.py) or this review receipt, so an
    owner cannot satisfy both the signature and independent-review
    requirements from a single session.
    """
    return signer_provenance_ref != reviewer_provenance_ref
