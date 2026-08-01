"""Nonproduction-only credential broker for the bakeoff runner.

Resolves a credential grant only when the approval's own digest-pinned
references match environment-provided nonproduction values exactly, and
the resolved account/region is not on the hardcoded production denylist.
Never returns, logs, or stores the raw credential value — only a digest
proving the correct value was present.

Scope note: the denylist check below guards a single, specific failure
mode (see PRODUCTION_ACCOUNT_REGION_DENYLIST's docstring). It is not a
comprehensive production guard for every provider this broker resolves
credentials for.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Mapping

# Hardcoded, not derived from any input the approval envelope controls —
# kevin-491315 is this project's one production GCP project (see CLAUDE.md).
#
# Scope boundary: this denylist exists to catch exactly one failure mode —
# a resolution whose account/region value is *this project's own* GCP
# hosting account/region. This broker resolves credentials for several
# dependency roles backed by unrelated providers (telephony/Twilio,
# speech_to_text/Deepgram, text_generation, text_to_speech/ElevenLabs, and
# more — see FirewallDependency in voice_bakeoff_execution_firewall_contracts.py),
# each with its own production-account identifiers that this module has no
# legitimate source for and therefore does not attempt to enumerate here.
# Do not add fabricated entries to "cover" those providers.
#
# Comprehensive, per-provider production-identity/destination denylisting
# is a separate, independent mechanism: DeclaredProductionDenylist /
# ExecutionFirewallResolver in voice_bakeoff_execution_firewall_contracts.py.
# Task 6 investigated wiring that mechanism into the runner alongside this
# broker, and deliberately deferred it: ExecutionFirewallResolver requires
# real production destination/identity data (as SHA-256 digests) for every
# provider dependency, which does not exist anywhere in this plan and was
# not something Task 6 should fabricate. That decision means this denylist
# is, for now, the sole production guard the runner actually enforces — not
# a narrow backstop alongside a broader mechanism. Wiring in the broader
# mechanism remains a live option, pending a future decision with real
# per-provider production data to populate it; it is not scheduled as part
# of this plan.
PRODUCTION_ACCOUNT_REGION_DENYLIST: tuple[str, ...] = (
    "kevin-491315:us-central1",
)

# Case/whitespace-normalized view of the denylist, used only for the
# membership test in resolve(). A differently-cased or whitespace-padded
# variant of a denylisted identifier (e.g. "Kevin-491315:us-central1" or
# "kevin-491315:us-central1 ") is semantically the same production
# identifier and must not be able to slip past the check. This normalized
# view is never used for digest computation — see resolve() below.
_NORMALIZED_PRODUCTION_ACCOUNT_REGION_DENYLIST: frozenset[str] = frozenset(
    entry.strip().casefold() for entry in PRODUCTION_ACCOUNT_REGION_DENYLIST
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
        # Normalize only for the denylist membership test; the digest
        # checks below must still hash the exact, unnormalized value the
        # approval envelope pinned.
        if (
            account_region_value.strip().casefold()
            in _NORMALIZED_PRODUCTION_ACCOUNT_REGION_DENYLIST
        ):
            return None
        if _digest(credential_value) != approved_credential_ref:
            return None
        if _digest(account_region_value) != approved_account_region_ref:
            return None

        return ResolvedNonproductionCredential(
            dependency_role=dependency_role,
            credential_digest=_digest(credential_value),
        )
