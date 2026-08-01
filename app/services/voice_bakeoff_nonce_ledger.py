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
