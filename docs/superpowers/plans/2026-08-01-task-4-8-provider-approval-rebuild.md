# Task 4.8 Provider-Approval Mechanism Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `scripts/run_voice_architecture_bakeoff.py`'s shape-only preflight and its unconditional `--execute-provider` rejection with a real, solo-owner-executable authorization mechanism — wiring the runner to the already-built (but currently unwired) Ed25519 envelope verification, adding a persisted one-use nonce ledger, a real nonproduction-only credential broker, real production-denylist enforcement, and a real residue audit — so that Task 3.4/4.8 can genuinely be authorized by one person, instead of by nine institutional roles that can never exist.

**Architecture:** Reuse the existing offline cryptographic apparatus in `app/services/voice_bakeoff_security_contracts.py` (`SignedApproval`, `OfflineApprovalVerifier`, `DetachedApprovalSignature`, `TechnicalReviewReceipt`) and the existing declarative denylist/broker-policy shapes in `app/services/voice_bakeoff_execution_firewall_contracts.py` (`DeclaredProductionDenylist`, `ExecutionFirewallResolver`) — none of this is new cryptography, it is *wiring* already-tested primitives into the runner for the first time. Five small new modules fill the genuinely missing pieces (persisted nonce ledger, credential broker, residue audit, an owner-signing CLI, a review-receipt-request CLI); a sixth task rewires the runner to use all of them; a seventh documents the change and formally supersedes the PR #133 stub.

**Tech Stack:** Python 3.12, `cryptography` (`cryptography.hazmat.primitives.asymmetric.ed25519` — already a dependency, used exactly as-is, no new library introduced), stdlib `json`/`hashlib`/`fcntl`/`pathlib`, `pytest`.

## Global Constraints

- Solo developer, no team. Every "authority" this plan builds must be something the owner personally runs (a CLI command, a local keypair, a recorded file) — never a reference to a role, department, or vendor that doesn't exist for this project.
- **Building this mechanism authorizes nothing by itself.** No task in this plan makes a real provider API call, resolves a real production credential, or performs live execution. Every new/modified module must remain fail-closed by default and importable/testable with zero network access — this is enforced by the existing AST-based firewall test pattern (see `scripts/voice_bakeoff_caller.py`'s self-check and `tests/unit/test_run_voice_architecture_bakeoff.py`'s no-network-import assertions) and every new test file in this plan must add an equivalent AST check for its own module.
- **Out of scope, explicitly: `tests/support/voice_bakeoff_task_4_8_gate_validator.py`.** This is a separate, pre-existing, independently-tested paperwork/evidence-tracking artifact (the "gate package" schema) whose own type system (`PreparationResult.verdict`) has no authorized state at all, by design — it is not consulted by the runner today and this plan does not touch it, extend it, or attempt to satisfy it. Task 7 documents this boundary explicitly so nobody later assumes this plan was supposed to make that validator pass.
- **Out of scope, explicitly: the actual bounded capability probe's live network logic** (making real Gemini Live / Deepgram / ElevenLabs / Twilio calls). This plan builds only the authorization gate a real probe run would have to pass through. `--execute-provider` remains rejected at the earliest boundary for every case this plan doesn't explicitly authorize.
- The advisory technical-review receipt must come from a procedurally separate review pass — Task 5's script dispatches a fresh reviewer with no access to the signing process, and the runner (Task 6) must reject a receipt whose `provenance_ref` matches the same process/session that produced the signature.
- Every negative-path test must assert failure occurs at the *earliest possible boundary* (before credential resolution, before any file/network I/O for that dependency) — matching Task 3.4's spec text verbatim.
- Reuse existing dataclasses (`SignedApproval`, `DetachedApprovalSignature`, `TechnicalReviewReceipt`, `ApprovalCaps`, `DeclaredProductionDenylist`) exactly as defined in `app/services/voice_bakeoff_security_contracts.py` and `app/services/voice_bakeoff_execution_firewall_contracts.py` — do not redefine parallel versions of these types.
- **Explicitly out of scope: live verification of per-dependency logging/data-sharing/tracing/retention/recording/cache-resumption settings** (Task 3.4's "per-entry logging/data-sharing/tracing, retention, recording, and cache/resumption state"). The spec requires checking a provider's *actual effective dashboard settings*, which means a real provider API call — this plan builds no such call. The envelope structurally carries these fields (already true today via the existing shape checks) but nothing in this plan verifies them against a live provider. That gap is real and intentional, not an oversight; closing it is a separate, later piece of work that necessarily involves real provider contact.

---

## File Structure

**New files:**
- `app/services/voice_bakeoff_nonce_ledger.py` — persisted, file-locked, one-use nonce/approval-id consumption ledger (the runner is a fresh process per invocation, so `InMemoryExecutionControlStore`'s in-memory semantics alone can't prevent replay across CLI runs — this task gives it durable backing).
- `app/services/voice_bakeoff_credential_broker.py` — resolves credentials *only* from an explicit nonproduction allowlist; hard-fails on anything else.
- `app/services/voice_bakeoff_residue_audit.py` — verifies no artifacts remain at a residue destination past their TTL; produces a pass/fail audit record.
- `scripts/sign_voice_bakeoff_approval.py` — CLI the owner runs personally to generate/load their own Ed25519 keypair and produce a `DetachedApprovalSignature` over an approval payload.
- `scripts/request_voice_bakeoff_review.py` — CLI that dispatches an independent review pass over an approval payload and records a `TechnicalReviewReceipt`.
- `tests/unit/test_voice_bakeoff_nonce_ledger.py`
- `tests/unit/test_voice_bakeoff_credential_broker.py`
- `tests/unit/test_voice_bakeoff_residue_audit.py`
- `tests/unit/test_sign_voice_bakeoff_approval.py`
- `tests/unit/test_request_voice_bakeoff_review.py`
- `docs/security/task-4-8-provider-approval-mechanism.md` — the new mechanism's operator doc.

**Modified files:**
- `scripts/run_voice_architecture_bakeoff.py` — wire real Ed25519 verification, nonce consumption, credential broker, denylist enforcement, and residue audit into `validate()` / the execute-provider path.
- `tests/unit/test_run_voice_architecture_bakeoff.py` — add the negative-path tests the current shape-only runner can't fail on yet (forged signature, wrong-owner, replayed nonce, revoked-key, break-glass, credential-swapped, secondary-credential-swapped, destination-mismatched).
- `docs/security/voice-architecture-bakeoff-controls.md` — update the "current state" section (currently states no crypto/nonce/credential/network happens) to describe the wired mechanism.
- `docs/security/task-4-8-synthetic-preparation.md` — add a superseding note at the top pointing to the new doc; the file is kept, not deleted, to preserve the review audit trail.

---

### Task 1: Persisted one-use nonce ledger

**Files:**
- Create: `app/services/voice_bakeoff_nonce_ledger.py`
- Test: `tests/unit/test_voice_bakeoff_nonce_ledger.py`

**Interfaces:**
- Consumes: nothing from other new tasks.
- Produces: `class FileBackedNonceLedger` with `__init__(self, ledger_path: pathlib.Path)` and `def admit(self, *, nonce_digest: str, approval_id_digest: str, binding_digest: str, epoch: int) -> bool` — returns `True` and durably records consumption the first time a given `(nonce_digest, approval_id_digest)` pair is seen; returns `False` (does not raise) if either digest was already consumed, or if `(binding_digest, epoch)` was already reserved by a *different* `approval_id_digest`. Later tasks (Task 6) call `admit()` and treat `False` as "reject at earliest boundary." This is the mechanism satisfying Task 3.4's "atomic one-use-consumption record" and (in minimal form — a reserved `(binding_digest, epoch)` slot) its "bounded Task-2.1 active-execution record" language; it does not attempt to replicate every field a full Task 2.1 active-execution record might carry, only the replay-prevention property this plan's scope actually needs.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_voice_bakeoff_nonce_ledger.py
import ast
import pathlib

import pytest

from app.services.voice_bakeoff_nonce_ledger import FileBackedNonceLedger


def test_first_admission_succeeds_and_persists(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)

    assert ledger.admit(
        nonce_digest="a" * 64,
        approval_id_digest="b" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is True
    assert ledger_path.exists()


def test_replayed_nonce_is_rejected(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    replay_ledger = FileBackedNonceLedger(ledger_path)
    assert replay_ledger.admit(
        nonce_digest="a" * 64,
        approval_id_digest="d" * 64,
        binding_digest="e" * 64,
        epoch=1,
    ) is False


def test_replayed_approval_id_is_rejected_even_with_new_nonce(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    assert ledger.admit(
        nonce_digest="f" * 64,
        approval_id_digest="b" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is False


def test_same_binding_different_epoch_is_allowed(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    assert ledger.admit(
        nonce_digest="f" * 64,
        approval_id_digest="g" * 64,
        binding_digest="c" * 64,
        epoch=2,
    ) is True


def test_same_binding_same_epoch_different_approval_is_rejected(tmp_path):
    ledger_path = tmp_path / "nonce_ledger.json"
    ledger = FileBackedNonceLedger(ledger_path)
    ledger.admit(nonce_digest="a" * 64, approval_id_digest="b" * 64, binding_digest="c" * 64, epoch=1)

    assert ledger.admit(
        nonce_digest="f" * 64,
        approval_id_digest="g" * 64,
        binding_digest="c" * 64,
        epoch=1,
    ) is False


def test_module_performs_no_network_or_subprocess_calls():
    source = pathlib.Path("app/services/voice_bakeoff_nonce_ledger.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "subprocess", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_voice_bakeoff_nonce_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.voice_bakeoff_nonce_ledger'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/voice_bakeoff_nonce_ledger.py
"""Persisted, file-locked, one-use nonce/approval-id consumption ledger.

The bakeoff runner is a fresh process on every invocation, so an in-memory
replay guard (InMemoryExecutionControlStore in voice_bakeoff_security_contracts.py)
cannot by itself stop the same approval envelope being replayed across
separate runs. This module gives that same replay-rejection semantics a
durable, local, file-backed store. It performs no network or subprocess
calls; it never resolves a credential.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import pathlib


@dataclasses.dataclass(frozen=True, slots=True)
class _LedgerState:
    consumed_nonces: frozenset[str]
    consumed_approval_ids: frozenset[str]
    binding_epochs: dict[str, str]  # f"{binding_digest}:{epoch}" -> approval_id_digest

    @classmethod
    def empty(cls) -> "_LedgerState":
        return cls(frozenset(), frozenset(), {})

    @classmethod
    def from_json(cls, payload: dict) -> "_LedgerState":
        return cls(
            consumed_nonces=frozenset(payload.get("consumed_nonces", [])),
            consumed_approval_ids=frozenset(payload.get("consumed_approval_ids", [])),
            binding_epochs=dict(payload.get("binding_epochs", {})),
        )

    def to_json(self) -> dict:
        return {
            "consumed_nonces": sorted(self.consumed_nonces),
            "consumed_approval_ids": sorted(self.consumed_approval_ids),
            "binding_epochs": self.binding_epochs,
        }


class FileBackedNonceLedger:
    """Durable one-use admission control for signed bakeoff approvals."""

    def __init__(self, ledger_path: pathlib.Path) -> None:
        self._path = ledger_path

    def _read_locked(self) -> _LedgerState:
        if not self._path.exists():
            return _LedgerState.empty()
        with self._path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            try:
                raw = handle.read().strip()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if not raw:
            return _LedgerState.empty()
        return _LedgerState.from_json(json.loads(raw))

    def admit(
        self,
        *,
        nonce_digest: str,
        approval_id_digest: str,
        binding_digest: str,
        epoch: int,
    ) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        with self._path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                raw = handle.read().strip()
                state = _LedgerState.from_json(json.loads(raw)) if raw else _LedgerState.empty()

                if nonce_digest in state.consumed_nonces:
                    return False
                if approval_id_digest in state.consumed_approval_ids:
                    return False
                binding_key = f"{binding_digest}:{epoch}"
                existing_owner = state.binding_epochs.get(binding_key)
                if existing_owner is not None and existing_owner != approval_id_digest:
                    return False

                new_state = _LedgerState(
                    consumed_nonces=state.consumed_nonces | {nonce_digest},
                    consumed_approval_ids=state.consumed_approval_ids | {approval_id_digest},
                    binding_epochs={**state.binding_epochs, binding_key: approval_id_digest},
                )
                handle.seek(0)
                handle.truncate()
                json.dump(new_state.to_json(), handle, indent=2, sort_keys=True)
                return True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_voice_bakeoff_nonce_ledger.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_bakeoff_nonce_ledger.py tests/unit/test_voice_bakeoff_nonce_ledger.py
git commit -m "feat: add persisted one-use nonce ledger for bakeoff approvals"
```

---

### Task 2: Nonproduction-only credential broker

**Files:**
- Create: `app/services/voice_bakeoff_credential_broker.py`
- Test: `tests/unit/test_voice_bakeoff_credential_broker.py`

**Interfaces:**
- Consumes: nothing from other new tasks.
- Produces: `class NonproductionCredentialBroker` with `__init__(self, *, env: Mapping[str, str])` (defaults to `os.environ` at call sites, injected here for testability) and `def resolve(self, *, dependency_role: str, approved_credential_ref: str, approved_account_region_ref: str) -> ResolvedNonproductionCredential | None`. Returns `None` (never raises, never partially resolves) unless *every* check passes: an env var named `BAKEOFF_NONPROD_CREDENTIAL__{dependency_role upper}` exists and its value's SHA-256 hex digest equals `approved_credential_ref`; an env var named `BAKEOFF_NONPROD_ACCOUNT_REGION__{dependency_role upper}` exists and its value's SHA-256 hex digest equals `approved_account_region_ref`; and the account/region value does not match any entry in `PRODUCTION_ACCOUNT_REGION_DENYLIST` (a closed tuple of known-production identifiers, hardcoded — not derived from input, so it can't be bypassed by supplying a matching approval). `class ResolvedNonproductionCredential` is a frozen dataclass with `dependency_role: str` and `credential_digest: str` only — **it never carries the raw credential value**, so a caller can prove a broker grant happened without the plan (or its tests, or its logs) ever holding the underlying secret.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_voice_bakeoff_credential_broker.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_voice_bakeoff_credential_broker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.voice_bakeoff_credential_broker'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/voice_bakeoff_credential_broker.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_voice_bakeoff_credential_broker.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_bakeoff_credential_broker.py tests/unit/test_voice_bakeoff_credential_broker.py
git commit -m "feat: add nonproduction-only credential broker for bakeoff runner"
```

---

### Task 3: Residue audit

**Files:**
- Create: `app/services/voice_bakeoff_residue_audit.py`
- Test: `tests/unit/test_voice_bakeoff_residue_audit.py`

**Interfaces:**
- Consumes: nothing from other new tasks.
- Produces: `class ResidueAuditResult` (frozen dataclass: `passed: bool`, `checked_at_ms: int`, `remaining_paths: tuple[str, ...]`) and `def audit_residue(destination: pathlib.Path, *, artifact_ttl_ms: int, now_ms: int) -> ResidueAuditResult` — lists every file under `destination`, computes each file's age from its mtime, and reports `passed=True` only if zero files exceed `artifact_ttl_ms`; otherwise `passed=False` with `remaining_paths` listing the offenders. Does not delete anything itself (a human or a separate explicit cleanup step decides deletion — this function's job is only to prove residue is or isn't present, matching the spec's "residue audit" as a verification step, not an auto-deletion step).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_voice_bakeoff_residue_audit.py
import ast
import os
import pathlib

import pytest

from app.services.voice_bakeoff_residue_audit import audit_residue


def test_passes_when_no_files_present(tmp_path):
    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=10_000)
    assert result.passed is True
    assert result.remaining_paths == ()


def test_passes_when_files_are_within_ttl(tmp_path):
    stale = tmp_path / "fresh.json"
    stale.write_text("{}")
    now_ms = int(stale.stat().st_mtime * 1000) + 500

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is True


def test_fails_when_a_file_exceeds_ttl(tmp_path):
    old = tmp_path / "old.json"
    old.write_text("{}")
    mtime_ms = int(old.stat().st_mtime * 1000)
    now_ms = mtime_ms + 5000

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is False
    assert str(old) in result.remaining_paths


def test_checks_nested_directories(tmp_path):
    nested = tmp_path / "nested" / "deep"
    nested.mkdir(parents=True)
    old = nested / "old.json"
    old.write_text("{}")
    mtime_ms = int(old.stat().st_mtime * 1000)
    now_ms = mtime_ms + 5000

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is False
    assert str(old) in result.remaining_paths


def test_module_performs_no_network_or_subprocess_calls():
    source = pathlib.Path("app/services/voice_bakeoff_residue_audit.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "subprocess", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_voice_bakeoff_residue_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.voice_bakeoff_residue_audit'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/voice_bakeoff_residue_audit.py
"""Residue audit: proves whether artifacts remain past their TTL.

This module only inspects and reports — it never deletes. Deciding to
delete confirmed residue is a separate, explicit, human-reviewed step.
"""

from __future__ import annotations

import dataclasses
import pathlib


@dataclasses.dataclass(frozen=True, slots=True)
class ResidueAuditResult:
    passed: bool
    checked_at_ms: int
    remaining_paths: tuple[str, ...]


def audit_residue(
    destination: pathlib.Path,
    *,
    artifact_ttl_ms: int,
    now_ms: int,
) -> ResidueAuditResult:
    remaining: list[str] = []
    if destination.exists():
        for path in sorted(destination.rglob("*")):
            if not path.is_file():
                continue
            mtime_ms = int(path.stat().st_mtime * 1000)
            age_ms = now_ms - mtime_ms
            if age_ms > artifact_ttl_ms:
                remaining.append(str(path))

    return ResidueAuditResult(
        passed=not remaining,
        checked_at_ms=now_ms,
        remaining_paths=tuple(remaining),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_voice_bakeoff_residue_audit.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/voice_bakeoff_residue_audit.py tests/unit/test_voice_bakeoff_residue_audit.py
git commit -m "feat: add residue audit for bakeoff artifact destinations"
```

---

### Task 4: Owner sole-signature CLI

**Files:**
- Create: `scripts/sign_voice_bakeoff_approval.py`
- Test: `tests/unit/test_sign_voice_bakeoff_approval.py`

**Interfaces:**
- Consumes: `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey` (stdlib-adjacent, already a project dependency; same primitive `voice_bakeoff_security_contracts.py`'s `_sign_ed25519` already uses, so signatures this script produces are byte-for-byte verifiable by the existing `OfflineApprovalVerifier`).
- Produces: `def load_or_create_owner_key(key_path: pathlib.Path) -> Ed25519PrivateKey` (creates with `0o600` permissions if absent, otherwise loads); `def sign_payload(private_key: Ed25519PrivateKey, *, domain: bytes, payload: dict) -> bytes` (mirrors `_sign_ed25519`'s exact wire format: `private_key.sign(domain + canonical_json_bytes(payload))`, so Task 6's verification path — which reuses the existing `_verify_ed25519`-equivalent public API — accepts it without any format translation). A `main()` CLI entry point reads a JSON payload file and a domain string, prints the raw signature as hex to stdout, and never touches the network.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_sign_voice_bakeoff_approval.py
import ast
import json
import pathlib

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_sign_voice_bakeoff_approval.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.sign_voice_bakeoff_approval'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/sign_voice_bakeoff_approval.py
"""Owner sole-signature CLI for bakeoff provider-approval envelopes.

Run this yourself, on your own machine, to sign an approval payload with
your own personal Ed25519 key. This script never contacts a network, never
reads a provider credential, and never itself authorizes anything — it
produces a detached signature you then attach to an approval envelope.

Usage:
    python scripts/sign_voice_bakeoff_approval.py \\
        --key ~/.config/hey-kevin/bakeoff_owner_key.pem \\
        --payload /path/to/approval_payload.json \\
        --domain "hey-kevin/bakeoff/owner-signature/v1"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from cryptography.hazmat.primitives.asymmetric import ed25519


def load_or_create_owner_key(key_path: pathlib.Path) -> ed25519.Ed25519PrivateKey:
    if key_path.exists():
        raw = key_path.read_bytes()
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)

    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    key_path.write_bytes(raw)
    key_path.chmod(0o600)
    return private_key


def sign_payload(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    domain: bytes,
    payload: dict,
) -> bytes:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return private_key.sign(domain + canonical)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, type=pathlib.Path)
    parser.add_argument("--payload", required=True, type=pathlib.Path)
    parser.add_argument("--domain", required=True)
    args = parser.parse_args(argv)

    private_key = load_or_create_owner_key(args.key)
    payload = json.loads(args.payload.read_text())
    signature = sign_payload(private_key, domain=args.domain.encode("utf-8"), payload=payload)

    print(signature.hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_sign_voice_bakeoff_approval.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/sign_voice_bakeoff_approval.py tests/unit/test_sign_voice_bakeoff_approval.py
git commit -m "feat: add owner sole-signature CLI for bakeoff approvals"
```

---

### Task 5: Independent review-receipt request CLI

**Files:**
- Create: `scripts/request_voice_bakeoff_review.py`
- Test: `tests/unit/test_request_voice_bakeoff_review.py`

**Interfaces:**
- Consumes: `TechnicalReviewReceipt` from `app.services.voice_bakeoff_security_contracts` (reuse the existing dataclass exactly — do not redefine it).
- Produces: `def build_receipt_request(payload_digest: str, binding_digest: str, *, source_sha: str, manifest_digest: str) -> dict` — the exact JSON prompt/context package an independent reviewer needs (contains only digests and non-sensitive metadata, never the raw approval payload contents, so the reviewer's process can't leak sensitive detail). `def parse_review_response(response: dict, *, expected_payload_digest: str, expected_binding_digest: str) -> TechnicalReviewReceipt` — validates the response matches the expected digests and constructs a real `TechnicalReviewReceipt`; raises `ValueError` on any mismatch or `unresolved_p1_count != 0`. `def reviewer_is_procedurally_separate(*, signer_provenance_ref: str, reviewer_provenance_ref: str) -> bool` — returns `False` (reject) if the two provenance references are equal, enforcing Task 6's requirement that the reviewer isn't the same process/session as the signer.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_request_voice_bakeoff_review.py
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
        "provenance_ref": "review-session-42",
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


def test_rejects_response_with_mismatched_payload_digest():
    response = {
        "review_digest": "e" * 64,
        "provenance_ref": "review-session-42",
        "reviewed_payload_digest": "wrong",
        "reviewed_binding_digest": "b" * 64,
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
        "provenance_ref": "review-session-42",
        "reviewed_payload_digest": "a" * 64,
        "reviewed_binding_digest": "b" * 64,
        "unresolved_p1_count": 2,
        "advisory_only": True,
    }
    with pytest.raises(ValueError):
        parse_review_response(
            response, expected_payload_digest="a" * 64, expected_binding_digest="b" * 64
        )


def test_reviewer_must_be_procedurally_separate_from_signer():
    assert reviewer_is_procedurally_separate(
        signer_provenance_ref="signer-session-1",
        reviewer_provenance_ref="review-session-42",
    ) is True
    assert reviewer_is_procedurally_separate(
        signer_provenance_ref="signer-session-1",
        reviewer_provenance_ref="signer-session-1",
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_request_voice_bakeoff_review.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.request_voice_bakeoff_review'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/request_voice_bakeoff_review.py
"""Independent review-receipt request/parsing for bakeoff approvals.

The receipt this produces is advisory only — per Task 3.4's spec text, it
can never by itself authorize a run. It must come from a procedurally
separate reviewer (a distinct provenance_ref) than whoever signs the
approval in scripts/sign_voice_bakeoff_approval.py; the runner (Task 6)
enforces that separation before accepting either.

This module builds only the non-sensitive request package a reviewer
needs (digests and metadata, never raw approval contents) and validates
the reviewer's response. It does not itself dispatch a reviewer process —
how you obtain a response (an independent human, or an independently
launched review agent with no access to the signing step) is an
operational choice made when this CLI is actually run, not baked in here.
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
    if response.get("reviewed_payload_digest") != expected_payload_digest:
        raise ValueError("review response payload digest does not match the request")
    if response.get("reviewed_binding_digest") != expected_binding_digest:
        raise ValueError("review response binding digest does not match the request")
    if response.get("unresolved_p1_count") != 0:
        raise ValueError("review response has unresolved P1 findings")
    if response.get("advisory_only") is not True:
        raise ValueError("review response must be marked advisory_only")

    return TechnicalReviewReceipt(
        review_digest=response["review_digest"],
        provenance_ref=response["provenance_ref"],
        reviewed_payload_digest=response["reviewed_payload_digest"],
        reviewed_binding_digest=response["reviewed_binding_digest"],
        unresolved_p1_count=response["unresolved_p1_count"],
        advisory_only=response["advisory_only"],
    )


def reviewer_is_procedurally_separate(
    *,
    signer_provenance_ref: str,
    reviewer_provenance_ref: str,
) -> bool:
    return signer_provenance_ref != reviewer_provenance_ref
```

*Note for the implementing subagent:* read `TechnicalReviewReceipt`'s exact constructor fields in `app/services/voice_bakeoff_security_contracts.py:406` before writing this step — its `__post_init__` may enforce additional invariants (e.g. digest format) beyond what's tested here, and this task's implementation must satisfy them exactly rather than duplicate/relax them.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_request_voice_bakeoff_review.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/request_voice_bakeoff_review.py tests/unit/test_request_voice_bakeoff_review.py
git commit -m "feat: add independent review-receipt request/parsing for bakeoff approvals"
```

---

### Task 6: Wire the runner to real verification

**Files:**
- Modify: `scripts/run_voice_architecture_bakeoff.py` — `validate()` (line 122) currently does shape/digest checks only and returns `list[str]` (empty = pass); `main()` (line 221) chains `run_offline_self_check` after `validate()` the same way (`if not errors and not run_offline_self_check(...): errors.append(...)`). New checks follow this exact existing pattern — appended as more `errors.append(...)` calls, not a parallel mechanism.
- Modify: `tests/unit/test_run_voice_architecture_bakeoff.py`

**Interfaces:**
- Consumes: `OfflineApprovalVerifier.verify(approval, snapshot, *, now_ms) -> VerifiedApproval | None` and `SignedApproval`/`DetachedApprovalSignature` from `voice_bakeoff_security_contracts.py`; `FileBackedNonceLedger.admit(...)` from Task 1; `NonproductionCredentialBroker.resolve(...)` from Task 2; `DeclaredProductionDenylist`/`ExecutionFirewallResolver.resolve_metadata(...)` from `voice_bakeoff_execution_firewall_contracts.py`; `audit_residue(...)` from Task 3; `reviewer_is_procedurally_separate(...)` from Task 5.
- Produces: the runner's exit codes are unchanged in meaning — `2` (`rejected_local_preflight`, now also covers cryptographic/replay/broker/denylist rejections) and `3` (`blocked_external_verification_required`, still the best case — `--execute-provider` stays rejected regardless). No new exit code.

**CRITICAL — read before writing any step:** `tests/unit/test_run_voice_architecture_bakeoff.py` (lines 231-570, already read in full while writing this plan) contains an exact AST-based import/digest firewall — `test_runner_and_reachable_offline_harness_have_no_execution_escape_hatches`, backed by `_offline_firewall_errors()`. It pins `_OFFLINE_APPROVED_SOURCE_DIGESTS["scripts.run_voice_architecture_bakeoff"]` to the file's *exact current SHA-256*, and checks the file's imports against a *closed* set `_OFFLINE_ALLOWED_IMPORTS["scripts.run_voice_architecture_bakeoff"]` (currently 9 exact entries — `argparse`, `hashlib`, `json`, `re`, `subprocess`, `time`, `pathlib.Path`, and the two `voice_bakeoff_caller` import forms). **Any new import this task adds to the runner will fail this test until it is deliberately updated** — this is the test doing its job, not a bug to work around.

`_import_contract()`'s local-dependency walker only recurses into modules whose name starts with `"scripts."` (or is literally `"voice_bakeoff_caller"`) — it does **not** currently recurse into `app.services.*`. Since this task wires in `app.services.voice_bakeoff_security_contracts`, `app.services.voice_bakeoff_nonce_ledger` (Task 1), `app.services.voice_bakeoff_credential_broker` (Task 2), `app.services.voice_bakeoff_execution_firewall_contracts`, and `app.services.voice_bakeoff_residue_audit` (Task 3), and the whole point of this firewall is proving the runner cannot transitively reach network/credential/subprocess capability, leaving `app.services.*` unrecursed would quietly weaken that guarantee. Step 3 below extends the walker to cover it — do not skip this to save time; it is the mechanism that makes every other check in this task trustworthy.

- [ ] **Step 1: Write the failing tests, using the real existing fixtures**

The file already defines `_approval()` (builds a full valid approval dict, `owner_authorization.signature` is the placeholder string `"detached_1"` — not real crypto yet), `_manifest(template_only=False)`, `_resign(approval)` (recomputes `self_digest`), `_bound_approval(manifest)` (binds dependency/manifest digests and resigns), `_rebind_manifest(approval, manifest)`, and module-level `runner` (the loaded script module) with `runner._canonical_digest`, `runner._dependency_inventory_digest`, `runner._manifest_digest_bytes`, `runner._ARTIFACT_DIGESTS`, `runner._DEPENDENCY_FIELDS`, `runner._ARM_ROLES`, `runner._CAPS`, `runner._RISKY_FEATURES`. Reuse these exactly; do not redefine parallel fixtures.

Add to `tests/unit/test_run_voice_architecture_bakeoff.py`:

```python
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.services.voice_bakeoff_credential_broker import NonproductionCredentialBroker
from app.services.voice_bakeoff_nonce_ledger import FileBackedNonceLedger
from scripts.sign_voice_bakeoff_approval import sign_payload


def _real_signature(approval: dict[str, object], private_key: ed25519.Ed25519PrivateKey) -> str:
    domain = b"hey-kevin/bakeoff/owner-signature/v1"
    payload = {k: v for k, v in approval.items() if k != "self_digest"}
    return sign_payload(private_key, domain=domain, payload=payload).hex()


def _signed_approval(manifest: dict[str, object]) -> tuple[dict[str, object], ed25519.Ed25519PrivateKey]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    approval = _bound_approval(manifest)
    approval["owner_authorization"]["signature"] = _real_signature(approval, private_key)
    _resign(approval)
    return approval, private_key


def test_forged_signature_is_rejected_before_credential_resolution(monkeypatch):
    manifest = _manifest()
    approval, _ = _signed_approval(manifest)
    approval["owner_authorization"]["signature"] = "f" * 128  # syntactically-valid hex, wrong signature
    _resign(approval)

    read_credential_keys: list[str] = []
    monkeypatch.setattr(
        runner.os.environ, "get",
        lambda key, *a: (read_credential_keys.append(key) or None) if "CREDENTIAL" in key else None,
    )

    errors = runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)
    assert any("signature" in e or "verification" in e for e in errors)
    assert read_credential_keys == []


def test_wrong_owner_key_is_rejected():
    manifest = _manifest()
    approval, _real_key = _signed_approval(manifest)
    wrong_key = ed25519.Ed25519PrivateKey.generate()
    approval["owner_authorization"]["signature"] = _real_signature(approval, wrong_key)
    _resign(approval)

    errors = runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)
    assert any("signature" in e or "verification" in e for e in errors)


def test_replayed_nonce_is_rejected_on_second_invocation(tmp_path):
    manifest = _manifest()
    approval, _key = _signed_approval(manifest)
    ledger = FileBackedNonceLedger(tmp_path / "ledger.json")

    first = ledger.admit(
        nonce_digest=approval["nonce"], approval_id_digest=approval["approval_id"],
        binding_digest=approval["self_digest"], epoch=1,
    )
    second = ledger.admit(
        nonce_digest=approval["nonce"], approval_id_digest=approval["approval_id"],
        binding_digest=approval["self_digest"], epoch=1,
    )
    assert first is True
    assert second is False


def test_credential_swapped_dependency_is_rejected():
    broker = NonproductionCredentialBroker(env={"BAKEOFF_NONPROD_CREDENTIAL__TELEPHONY": "wrong"})
    grant = broker.resolve(
        dependency_role="telephony",
        approved_credential_ref="0" * 64,
        approved_account_region_ref="0" * 64,
    )
    assert grant is None


def test_destination_mismatched_dependency_is_rejected():
    broker = NonproductionCredentialBroker(
        env={
            "BAKEOFF_NONPROD_CREDENTIAL__TELEPHONY": "cred",
            "BAKEOFF_NONPROD_ACCOUNT_REGION__TELEPHONY": "kevin-491315:us-central1",
        }
    )
    import hashlib
    grant = broker.resolve(
        dependency_role="telephony",
        approved_credential_ref=hashlib.sha256(b"cred").hexdigest(),
        approved_account_region_ref=hashlib.sha256(b"kevin-491315:us-central1").hexdigest(),
    )
    assert grant is None  # production account/region is denylisted unconditionally


def test_reviewer_same_as_signer_is_rejected():
    manifest = _manifest()
    approval, _key = _signed_approval(manifest)
    approval["owner_authorization"]["identity"] = "same_session_ref"
    approval["technical_review"]["provenance_ref"] = "same_session_ref"
    _resign(approval)

    errors = runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)
    assert any("reviewer" in e or "provenance" in e for e in errors)
```

*Note for the implementing subagent:* Task 1/2/4's real classes (`OfflineApprovalVerifier`, `FileBackedNonceLedger`, `NonproductionCredentialBroker`) are exercised directly above rather than only through `runner.validate()`, because `validate()`'s exact new internal call shape is what Step 3 defines — write these tests first against the *components*, then in Step 3 wire `validate()`/`main()` to call them and confirm the signature/reviewer-separation assertions above also hold when exercised through `runner.validate()` end-to-end (add that end-to-end assertion once Step 3 lands; don't skip it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_run_voice_architecture_bakeoff.py -v -k "forged_signature or wrong_owner or replayed_nonce or credential_swapped or destination_mismatched or reviewer_same_as_signer"`
Expected: FAIL — `ModuleNotFoundError` for the new imports until Tasks 1/2/4 are committed (confirm those tasks ran first), then FAIL on the signature/reviewer assertions specifically, since `runner.validate()` doesn't check them yet.

- [ ] **Step 3: Wire `validate()` to real verification, and extend the AST firewall to match**

In `scripts/run_voice_architecture_bakeoff.py`, add near the top (after the existing `from scripts.voice_bakeoff_caller import run_offline_self_check` block):

```python
import os

from app.services.voice_bakeoff_credential_broker import NonproductionCredentialBroker
from app.services.voice_bakeoff_nonce_ledger import FileBackedNonceLedger
from app.services.voice_bakeoff_residue_audit import audit_residue
from app.services.voice_bakeoff_security_contracts import OfflineApprovalVerifier, SignedApproval
```

Inside `validate()`, after the existing checks (before `return errors`), add — in this exact order, earliest-boundary-first, matching Task 3.4's spec text:

1. Import `reviewer_is_procedurally_separate` from `scripts.request_voice_bakeoff_review` (Task 5) — do not reimplement this check inline, that duplicates logic Task 5 already built and tests. Call `reviewer_is_procedurally_separate(signer_provenance_ref=owner_authorization["identity"], reviewer_provenance_ref=technical_review["provenance_ref"])`; if it returns `False`, `errors.append("reviewer is not procedurally separate from signer")`.
2. If `errors` is still empty so far, attempt `SignedApproval(...)` construction from the approval dict's fields (reusing the already-validated `owner_authorization`/`technical_review`/`caps` sub-structures) inside a `try/except (ValueError, TypeError) as exc: errors.append(f"signed approval construction failed: {exc}")`.
3. If construction succeeded, call `OfflineApprovalVerifier.verify(signed_approval, trust_snapshot, now_ms=current_ms)` (build/pass a minimal `trust_snapshot` per its real constructor — read it directly from `voice_bakeoff_security_contracts.py` before writing this call, do not guess its shape) — if it returns `None`, `errors.append("signature or trust verification failed")`. **This is a single boundary covering forged signature, wrong owner, revoked key, and expired trust — do not write separate ad hoc checks for each; let the existing verifier do it, that is the entire point of reusing it.**
4. If verification succeeded, for each dependency in `approval["dependencies"]`, call `NonproductionCredentialBroker(env=os.environ).resolve(dependency_role=dep["role"], approved_credential_ref=dep["credential_ref"], approved_account_region_ref=dep["account_region_ref"])` — if any returns `None`, `errors.append(f"credential broker denied dependency: {dep['role']}")`.
5. `main()` already only proceeds past `validate()` when `errors` is empty; add the nonce-ledger admission there (not inside `validate()`, since it's process-global state, matching where `run_offline_self_check` is already called): `if not errors and not FileBackedNonceLedger(Path(args.nonce_ledger)).admit(nonce_digest=approval["nonce"], approval_id_digest=approval["approval_id"], binding_digest=approval["self_digest"], epoch=1): errors.append("nonce already consumed")`. Add a new required `--nonce-ledger` CLI argument (`Path`) to `main()`'s `argparse` setup.
6. After the verdict is computed (regardless of pass/fail), call `audit_residue(...)` against a `--residue-destination` CLI argument (also new, required) and include its `passed`/`remaining_paths` in the printed JSON output under a `"residue_audit"` key — this never changes the exit code (residue from a *prior* run is a separate concern from *this* run's contract-consistency verdict).

**Then, extend the firewall test itself** (`tests/unit/test_run_voice_architecture_bakeoff.py`):
- Add `"app.services.voice_bakeoff_credential_broker"`, `"app.services.voice_bakeoff_nonce_ledger"`, `"app.services.voice_bakeoff_residue_audit"`, `"app.services.voice_bakeoff_security_contracts"` to `_OFFLINE_SOURCE_PATHS` (mapping to their real file paths).
- In `_import_contract()`, change the local-dependency recognition (currently `if alias.name.startswith("scripts."):` and `elif base.startswith("scripts."):`) to also match `"app.services.voice_bakeoff_"` so these four modules get recursively walked and digest-pinned the same way `voice_bakeoff_caller` already is.
- Read each of the four files' actual full import lists (`voice_bakeoff_nonce_ledger.py`, `voice_bakeoff_credential_broker.py`, `voice_bakeoff_residue_audit.py` were just written in Tasks 1-3 — their imports are known exactly from those tasks; `voice_bakeoff_security_contracts.py` is pre-existing — read its complete import list directly, not just the header, since only the first ~17 lines were confirmed while writing this plan) and add each as a new entry in `_OFFLINE_ALLOWED_IMPORTS`, and every existing entry unchanged.
- Run `python -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('scripts/run_voice_architecture_bakeoff.py').read_bytes()).hexdigest())"` (and the equivalent for each of the four newly-recursed files) after finalizing their content, and set `_OFFLINE_APPROVED_SOURCE_DIGESTS` to the printed values — this is a mechanical recompute, not a guess; do it last, after all other edits in this step are finished and stable.
- If any new `getattr`/file-I/O call shape was introduced (unlikely for this task, but check), update `_OFFLINE_ALLOWED_GETATTR`/`_OFFLINE_ALLOWED_FILE_IO` the same way.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_run_voice_architecture_bakeoff.py -v`
Expected: PASS — all pre-existing tests, the new tests from Step 1, and `test_runner_and_reachable_offline_harness_have_no_execution_escape_hatches` (confirms the firewall extension is internally consistent, not just that the digests match).

- [ ] **Step 5: Run the full focused suite to confirm no regression**

Run: `pytest tests/unit/test_run_voice_architecture_bakeoff.py tests/unit/test_voice_bakeoff_nonce_ledger.py tests/unit/test_voice_bakeoff_credential_broker.py tests/unit/test_voice_bakeoff_residue_audit.py tests/unit/test_sign_voice_bakeoff_approval.py tests/unit/test_request_voice_bakeoff_review.py -v`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add scripts/run_voice_architecture_bakeoff.py tests/unit/test_run_voice_architecture_bakeoff.py
git commit -m "feat: wire bakeoff runner to real Ed25519 verification, nonce ledger, credential broker, and denylist"
```

---

### Task 7: Documentation and formal supersession

**Files:**
- Create: `docs/security/task-4-8-provider-approval-mechanism.md`
- Modify: `docs/security/voice-architecture-bakeoff-controls.md`
- Modify: `docs/security/task-4-8-synthetic-preparation.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by later tasks (this is the final task).

- [ ] **Step 1: Write the new mechanism doc**

Create `docs/security/task-4-8-provider-approval-mechanism.md` covering, in this order: (a) what changed and why — the PR #133 package modeled Task 3.4/4.8 authorization as nine institutional roles that cannot exist for a solo developer, and was by its own design permanently `not_authorized`; this mechanism instead implements Task 3.4's own text verbatim ("one trusted sole-owner signature plus a mandatory envelope-bound advisory technical-review receipt"); (b) exactly how to run it — the two CLI scripts (`sign_voice_bakeoff_approval.py`, `request_voice_bakeoff_review.py`) plus the runner, in sequence, with real example commands; (c) an explicit statement that `tests/support/voice_bakeoff_task_4_8_gate_validator.py` (the separate "gate package" validator) is untouched by this work and its own type system has no authorized state — this mechanism does not attempt to satisfy it, and nobody should read a passing dry-run here as also meaning that validator would pass; (d) an explicit statement that `--execute-provider` (a real network call) remains rejected regardless of this mechanism, pending Task 4.7's offline gates and Task 3.5's caller-UX contract, neither of which this work touches.

- [ ] **Step 2: Update the controls doc**

Modify `docs/security/voice-architecture-bakeoff-controls.md`: replace the "does not... persist or consume a nonce" and "no crypto/nonce/credential/network" statements (lines ~9-11 per prior research) with the new true state — cryptographic verification, nonce persistence, and nonproduction-only credential resolution are now real; the production denylist is now consulted by the runner rather than a single string flag. Keep the document's existing structure; this is a content update, not a rewrite.

- [ ] **Step 3: Add a supersession note to the old package doc**

Modify `docs/security/task-4-8-synthetic-preparation.md`: add a note immediately after the title (do not delete or alter anything below it — this preserves the reviewed audit trail) stating this package has been superseded by `docs/security/task-4-8-provider-approval-mechanism.md` as of this plan's completion, and linking to it.

- [ ] **Step 4: Commit**

```bash
git add docs/security/task-4-8-provider-approval-mechanism.md docs/security/voice-architecture-bakeoff-controls.md docs/security/task-4-8-synthetic-preparation.md
git commit -m "docs: document the real Task 4.8 provider-approval mechanism and supersede the PR #133 stub"
```

---

## Post-plan verification (not a task — run once all seven tasks are committed)

```bash
pytest tests/unit/test_voice_bakeoff_nonce_ledger.py \
  tests/unit/test_voice_bakeoff_credential_broker.py \
  tests/unit/test_voice_bakeoff_residue_audit.py \
  tests/unit/test_sign_voice_bakeoff_approval.py \
  tests/unit/test_request_voice_bakeoff_review.py \
  tests/unit/test_run_voice_architecture_bakeoff.py \
  tests/unit/test_task_4_8_synthetic_preparation.py \
  tests/unit/test_voice_bakeoff_task_4_8_gate_package.py \
  tests/unit/test_voice_bakeoff_gate_report.py \
  -v
```

Expected: all pass — including the three PR #133-era suites, confirming this work did not weaken or break the (deliberately permanent) denial state of the artifacts it doesn't touch. Then: recompute manifest/tree digests for any files this plan modified that are covered by the existing `docs/security/task-4-8-synthetic-preparation.manifest.json` (most of this plan's files are new and outside that manifest's scope — check which, if any, modified files are actually listed in it before assuming a digest recompute is needed), and get a fresh independent review (staff/security/UX, matching how PR #133 itself was reviewed) before this branch is proposed for a PR. **This plan does not authorize opening that PR** — that remains a separate, explicit owner decision, same as it was for PR #133.
