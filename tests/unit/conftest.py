"""Snapshot of environment-variable NAMES present before any test module in
this directory runs its own module-level environment mutations.

pytest imports conftest.py before collecting any test module in the same
directory, so this snapshot reflects only what the actual process/CI job
environment provided -- not names added later by another test file's
module-level ``os.environ.setdefault(...)`` calls (several files in this
directory do this for dummy provider-credential test values, which would
otherwise look identical to a real secret leak to a name-based scan).

Exposed via an environment variable rather than a Python import so guard
checks in sibling test files don't depend on package/sys.path import
mechanics.
"""

import os

_SNAPSHOT_VAR = "_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES"

if _SNAPSHOT_VAR not in os.environ:
    os.environ[_SNAPSHOT_VAR] = "\x1f".join(sorted(os.environ))
