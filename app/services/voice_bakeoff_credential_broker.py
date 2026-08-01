"""Nonproduction-only credential broker for the bakeoff runner.

Resolves a credential grant only when the approval's own digest-pinned
references match environment-provided nonproduction values exactly, and
the resolved account/region is not on the hardcoded production denylist.
Never returns, logs, or stores the raw credential value — only a digest
proving the correct value was present.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Mapping

# Hardcoded, not derived from any input the approval envelope controls —
# kevin-491315 is this project's one production GCP project (see CLAUDE.md).
PRODUCTION_ACCOUNT_REGION_DENYLIST: tuple[str, ...] = (
    "kevin-491315:us-central1",
)


@dataclasses.dataclass(frozen=True, slots=True)
class ResolvedNonproductionCredential:
    dependency_role: str
    credential_digest: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class NonproductionCredentialBroker:
    def __init__(self, *, env: Mapping[str, str]) -> None:
        self._env = env

    def resolve(
        self,
        *,
        dependency_role: str,
        approved_credential_ref: str,
        approved_account_region_ref: str,
    ) -> ResolvedNonproductionCredential | None:
        role_key = dependency_role.upper()
        credential_value = self._env.get(f"BAKEOFF_NONPROD_CREDENTIAL__{role_key}")
        account_region_value = self._env.get(f"BAKEOFF_NONPROD_ACCOUNT_REGION__{role_key}")

        if credential_value is None or account_region_value is None:
            return None
        if account_region_value in PRODUCTION_ACCOUNT_REGION_DENYLIST:
            return None
        if _digest(credential_value) != approved_credential_ref:
            return None
        if _digest(account_region_value) != approved_account_region_ref:
            return None

        return ResolvedNonproductionCredential(
            dependency_role=dependency_role,
            credential_digest=_digest(credential_value),
        )
