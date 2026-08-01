import ast
import hashlib
import pathlib

import pytest

from app.services.voice_bakeoff_credential_broker import NonproductionCredentialBroker


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_resolves_when_env_matches_approved_digests():
    env = {
        "BAKEOFF_NONPROD_CREDENTIAL__DEEPGRAM": "sandbox-key-123",
        "BAKEOFF_NONPROD_ACCOUNT_REGION__DEEPGRAM": "sandbox-project:us-central1",
    }
    broker = NonproductionCredentialBroker(env=env)

    grant = broker.resolve(
        dependency_role="deepgram",
        approved_credential_ref=_digest("sandbox-key-123"),
        approved_account_region_ref=_digest("sandbox-project:us-central1"),
    )

    assert grant is not None
    assert grant.dependency_role == "deepgram"
    assert grant.credential_digest == _digest("sandbox-key-123")
    assert not hasattr(grant, "credential_value")


def test_rejects_when_env_var_missing():
    broker = NonproductionCredentialBroker(env={})
    assert broker.resolve(
        dependency_role="deepgram",
        approved_credential_ref=_digest("anything"),
        approved_account_region_ref=_digest("anything"),
    ) is None


def test_rejects_credential_swap_even_if_digest_looks_close():
    env = {
        "BAKEOFF_NONPROD_CREDENTIAL__DEEPGRAM": "swapped-key",
        "BAKEOFF_NONPROD_ACCOUNT_REGION__DEEPGRAM": "sandbox-project:us-central1",
    }
    broker = NonproductionCredentialBroker(env=env)
    assert broker.resolve(
        dependency_role="deepgram",
        approved_credential_ref=_digest("sandbox-key-123"),
        approved_account_region_ref=_digest("sandbox-project:us-central1"),
    ) is None


def test_rejects_known_production_account_region_unconditionally():
    env = {
        "BAKEOFF_NONPROD_CREDENTIAL__DEEPGRAM": "prod-key",
        "BAKEOFF_NONPROD_ACCOUNT_REGION__DEEPGRAM": "kevin-491315:us-central1",
    }
    broker = NonproductionCredentialBroker(env=env)
    assert broker.resolve(
        dependency_role="deepgram",
        approved_credential_ref=_digest("prod-key"),
        approved_account_region_ref=_digest("kevin-491315:us-central1"),
    ) is None


def test_module_performs_no_network_or_subprocess_calls():
    source = pathlib.Path("app/services/voice_bakeoff_credential_broker.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "subprocess", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
