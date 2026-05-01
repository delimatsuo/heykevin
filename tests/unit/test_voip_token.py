"""F-07 — Twilio Voice access tokens must bind the identity to the contractor.

These tests assert that:
- `_generate_access_token(contractor_id=...)` produces a JWT whose `identity`
  claim is `contractor_<contractor_id>` rather than the legacy static value
  (`kevin-contractor`).
- The `/api/voip-token` REST handler (`get_voip_token`) issues a token whose
  identity is `contractor_<contractor_id>` for the authenticated caller.

The Twilio Voice access token is a JWT signed with the Twilio API key secret.
We decode the JWT (without signature verification) and inspect the `grants.identity`
claim — that's where the Twilio Python SDK puts the identity.
"""

import jwt
import pytest

from app.api import voip as voip_module


def _decode_identity(token: str) -> str:
    """Decode a Twilio access token JWT and return its identity claim.

    The Twilio SDK encodes identity inside `grants.identity`. We decode without
    signature verification because the test only cares about the claim payload,
    not Twilio's signature.
    """
    decoded = jwt.decode(token, options={"verify_signature": False})
    return decoded.get("grants", {}).get("identity", "")


def _set_twilio_settings(monkeypatch):
    """Stub Twilio settings so AccessToken construction succeeds without real creds."""
    monkeypatch.setattr(voip_module.settings, "twilio_account_sid", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(voip_module.settings, "twilio_api_key_sid", "SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(voip_module.settings, "twilio_api_key_secret", "test-secret-not-real")
    monkeypatch.setattr(voip_module.settings, "twilio_twiml_app_sid", "APxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def test_generate_access_token_binds_identity_to_contractor(monkeypatch):
    _set_twilio_settings(monkeypatch)

    contractor_id = "contractor-abc-123"
    token = voip_module._generate_access_token(contractor_id=contractor_id)

    identity = _decode_identity(token)
    assert identity == f"contractor_{contractor_id}"
    # Regression: must not fall back to the legacy static identity.
    assert identity != "kevin-contractor"


def test_generate_access_token_distinct_identities_per_contractor(monkeypatch):
    """Two different contractors must get two different token identities."""
    _set_twilio_settings(monkeypatch)

    token_a = voip_module._generate_access_token(contractor_id="alice-001")
    token_b = voip_module._generate_access_token(contractor_id="bob-002")

    assert _decode_identity(token_a) == "contractor_alice-001"
    assert _decode_identity(token_b) == "contractor_bob-002"
    assert _decode_identity(token_a) != _decode_identity(token_b)


@pytest.mark.asyncio
async def test_get_voip_token_endpoint_binds_identity_to_authenticated_contractor(monkeypatch):
    """The /api/voip-token handler must issue tokens scoped to the authed contractor."""
    _set_twilio_settings(monkeypatch)

    # Bypass the auth dependency — we're unit-testing the handler logic directly.
    monkeypatch.setattr(voip_module, "require_contractor_access", lambda request, cid: None)

    contractor_id = "contractor-xyz-789"

    class _DummyRequest:
        class state:
            pass

    body = voip_module.VoIPTokenRequest(call_sid="CA123", conference_name="pickup_abc")

    response = await voip_module.get_voip_token(
        request=_DummyRequest(),
        body=body,
        contractor_id=contractor_id,
    )

    assert "token" in response, response
    assert _decode_identity(response["token"]) == f"contractor_{contractor_id}"
    # Regression: must not fall back to the legacy static identity.
    assert _decode_identity(response["token"]) != "kevin-user"
