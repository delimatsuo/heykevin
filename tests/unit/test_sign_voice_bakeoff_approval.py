import ast
import json
import pathlib

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.services.voice_bakeoff_security_contracts import (
    _APPROVAL_DOMAIN,
    _verify_ed25519,
)
from scripts.sign_voice_bakeoff_approval import load_or_create_owner_key, sign_payload


def test_creates_key_with_restrictive_permissions(tmp_path):
    key_path = tmp_path / "owner_key.pem"
    load_or_create_owner_key(key_path)

    assert key_path.exists()
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_reuses_existing_key_across_calls(tmp_path):
    key_path = tmp_path / "owner_key.pem"
    first = load_or_create_owner_key(key_path)
    second = load_or_create_owner_key(key_path)

    first_public = first.public_key().public_bytes_raw()
    second_public = second.public_key().public_bytes_raw()
    assert first_public == second_public


def test_signature_verifies_against_the_matching_public_key(tmp_path):
    key_path = tmp_path / "owner_key.pem"
    private_key = load_or_create_owner_key(key_path)
    payload = {"approval_id": "abc123", "arm": "A"}
    domain = b"hey-kevin/bakeoff/owner-signature/v1"

    signature = sign_payload(private_key, domain=domain, payload=payload)

    private_key.public_key().verify(
        signature,
        domain + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )  # raises cryptography.exceptions.InvalidSignature on mismatch — no assert needed


def test_different_payloads_produce_different_signatures(tmp_path):
    key_path = tmp_path / "owner_key.pem"
    private_key = load_or_create_owner_key(key_path)
    domain = b"hey-kevin/bakeoff/owner-signature/v1"

    sig_a = sign_payload(private_key, domain=domain, payload={"approval_id": "a"})
    sig_b = sign_payload(private_key, domain=domain, payload={"approval_id": "b"})
    assert sig_a != sig_b


def test_signature_interoperates_with_the_real_approval_verifier(tmp_path):
    """Prove sign_payload() actually interoperates with the production
    verification code path, not just with the signing key's own public half.

    OfflineApprovalVerifier.verify() (in
    app/services/voice_bakeoff_security_contracts.py) hardcodes
    _APPROVAL_DOMAIN and authenticates signatures with _verify_ed25519. This
    test imports both, unmodified, from that real module and confirms a
    signature minted by this CLI's sign_payload() actually verifies there.
    """
    key_path = tmp_path / "owner_key.pem"
    private_key = load_or_create_owner_key(key_path)
    payload = {"approval_id": "abc123", "arm": "A"}

    signature = sign_payload(private_key, domain=_APPROVAL_DOMAIN, payload=payload)
    public_key_bytes = private_key.public_key().public_bytes_raw()

    assert _verify_ed25519(public_key_bytes, _APPROVAL_DOMAIN, payload, signature)

    # Domain separation must actually matter: a signature bound to the real
    # approval domain must be rejected under a different domain string, so a
    # verifier that silently ignored domain separation wouldn't pass this test.
    other_domain = b"hey-kevin/bakeoff/owner-signature/v1"
    assert not _verify_ed25519(public_key_bytes, other_domain, payload, signature)


def test_refuses_to_load_key_whose_permissions_were_loosened(tmp_path):
    key_path = tmp_path / "owner_key.pem"
    load_or_create_owner_key(key_path)

    key_path.chmod(0o644)

    with pytest.raises(PermissionError):
        load_or_create_owner_key(key_path)


def test_module_performs_no_network_calls():
    source = pathlib.Path("scripts/sign_voice_bakeoff_approval.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
