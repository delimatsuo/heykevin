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

import os

_SNAPSHOT_VAR = "_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES"

if _SNAPSHOT_VAR not in os.environ:
    os.environ[_SNAPSHOT_VAR] = "\x1f".join(sorted(os.environ))
