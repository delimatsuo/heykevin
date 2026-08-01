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
import os
import pathlib
import tempfile


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
                if not raw:
                    state = _LedgerState.empty()
                else:
                    try:
                        state = _LedgerState.from_json(json.loads(raw))
                    except json.JSONDecodeError:
                        # The ledger exists but its contents are not valid
                        # JSON (e.g. a prior writer was killed mid-write,
                        # before the atomic replace in _write_atomic below
                        # landed). We cannot recover what was already
                        # consumed, so fail closed instead of risking a
                        # replay by treating the corrupt file as empty.
                        return False

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
                self._write_atomic(new_state)
                return True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write_atomic(self, state: _LedgerState) -> None:
        """Durably replace the ledger contents with `state`.

        Writes to a fresh temp file in the same directory, fsyncs it, then
        os.replace()s it onto self._path. os.replace() is an atomic rename
        on POSIX, so any observer (including a process that opens the path
        after a kill -9 of this one) always sees either the previous fully
        valid contents or the new fully valid contents — never a truncated
        or partially written file. Must be called from within the caller's
        fcntl.flock critical section on self._path.
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_handle:
                json.dump(state.to_json(), tmp_handle, indent=2, sort_keys=True)
                tmp_handle.flush()
                os.fsync(tmp_handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise
