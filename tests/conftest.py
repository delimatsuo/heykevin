"""Snapshot of environment-variable NAMES present before any test module
under tests/ runs its own module-level environment mutations.

pytest imports the conftest.py closest to the collection root before any
test module beneath it, and this sits at tests/ itself (not tests/unit/)
because tests/test_apple_auth.py -- collected before pytest ever descends
into tests/unit/ -- is one of roughly three dozen files across the suite
that set dummy provider-credential env vars (TWILIO_ACCOUNT_SID and
similar) at their own module level via ``os.environ.setdefault(...)``,
indistinguishable from a real secret leak to a name-based forbidden-env-var
scan running later in the same process.

Exposed via an environment variable rather than a Python import so guard
checks in sibling test files don't depend on package/sys.path import
mechanics.
"""

import base64
import json
import os

import pytest

_SNAPSHOT_VAR = "_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES"

if _SNAPSHOT_VAR not in os.environ:
    os.environ[_SNAPSHOT_VAR] = "\x1f".join(sorted(os.environ))

_TEST_DUMMY_KEYRING = json.dumps({"1": base64.b64encode(b"k" * 32).decode("ascii")})
os.environ["INTEGRATION_TOKEN_ENCRYPTION_KEYS"] = _TEST_DUMMY_KEYRING
os.environ["INTEGRATION_TOKEN_ACTIVE_KEY_VERSION"] = "1"
os.environ["INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _ensure_test_keyring_configured(monkeypatch):
    try:
        from app.config import settings
        monkeypatch.setattr(settings, "integration_token_encryption_keys", _TEST_DUMMY_KEYRING)
        monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
        monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    except Exception:
        pass
