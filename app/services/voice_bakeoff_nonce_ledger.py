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


def _require_string_list(value: object, field_name: str) -> None:
    """Raise ValueError unless `value` is a list of only str elements.

    Checks `isinstance(value, list)` before checking element types: a bare
    str is itself an iterable of (single-character) str, so testing
    element type first would silently accept a string as a list of its
    own characters — making `admit()` fail OPEN on exactly the
    corrupt-shape input this ledger exists to guard against.
    """
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{field_name} must be a list of strings")


def _require_string_mapping(value: object, field_name: str) -> None:
    """Raise ValueError unless `value` is a dict with only str keys/values."""
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(f"{field_name} must be a mapping of strings to strings")


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
        """Strictly validate `payload`'s shape before building a state.

        `json.loads` only guarantees syntactically valid JSON, not the
        shape this ledger needs. Validating every field here — rather
        than letting a malformed value surface later as a silent
        miscoercion (see `_require_string_list`) or a downstream crash in
        `_write_atomic` — means bad input fails as a clean ValueError at
        the boundary.
        """
        if not isinstance(payload, dict):
            raise ValueError("ledger payload must be a JSON object")

        consumed_nonces = payload.get("consumed_nonces", [])
        _require_string_list(consumed_nonces, "consumed_nonces")
        consumed_approval_ids = payload.get("consumed_approval_ids", [])
        _require_string_list(consumed_approval_ids, "consumed_approval_ids")
        binding_epochs = payload.get("binding_epochs", {})
        _require_string_mapping(binding_epochs, "binding_epochs")

        return cls(
            consumed_nonces=frozenset(consumed_nonces),
            consumed_approval_ids=frozenset(consumed_approval_ids),
            binding_epochs=dict(binding_epochs),
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
        # A separate, NEVER-REPLACED sidecar file used only for fcntl
        # locking. fcntl.flock() locks are bound to the open file
        # description's inode at open() time — not dynamically re-resolved
        # from the path. If admit() flocked self._path directly, a second
        # caller that already opened its handle on the OLD inode before the
        # first caller's os.replace() in _write_atomic() landed — and was
        # blocked on flock() waiting — would, after unblocking, still hold
        # a handle to the old, now-orphaned inode: its read would return a
        # stale, pre-replace snapshot (missing the first caller's
        # just-committed write), and its own atomic replace would silently
        # overwrite that committed admission. Locking this sidecar file
        # instead — which admit() never passes to os.replace() — means
        # every caller always flocks the same, never-swapped inode, so a
        # blocked waiter is guaranteed to re-read self._path fresh (see
        # admit() below) only after it has acquired the lock.
        self._lock_path = ledger_path.parent / f"{ledger_path.name}.lock"

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
        self._lock_path.touch(exist_ok=True)
        with self._lock_path.open("r+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                # Read self._path only AFTER acquiring the lock, never
                # from a handle opened earlier: self._path may have just
                # been replaced by the previous lock holder, and a
                # pre-lock read would miss that write. See the
                # self._lock_path comment in __init__.
                raw = self._path.read_text(encoding="utf-8").strip()
                if not raw:
                    state = _LedgerState.empty()
                else:
                    try:
                        state = _LedgerState.from_json(json.loads(raw))
                    except (json.JSONDecodeError, AttributeError, TypeError, ValueError, RecursionError):
                        # Ledger contents unusable: not valid JSON (e.g. a
                        # prior writer killed mid-write before
                        # _write_atomic's replace landed), or valid JSON in
                        # the wrong shape. from_json's own isinstance
                        # checks now raise ValueError for every shape
                        # violation; AttributeError/TypeError stay in this
                        # tuple only as defense in depth against a future
                        # failure mode inside from_json, not because
                        # they're currently expected. Either way, fail
                        # closed rather than risk a replay by treating the
                        # corrupt file as empty.
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
                fcntl.flock(lock_handle, fcntl.LOCK_UN)

    def _write_atomic(self, state: _LedgerState) -> None:
        """Durably replace the ledger contents with `state`.

        Writes to a fresh temp file in the same directory, fsyncs it, then
        os.replace()s it onto self._path — an atomic POSIX rename, so any
        observer (even one opening the path after a kill -9 of this
        process) always sees either the fully-old or fully-new contents,
        never a partial write. Must be called only while holding the
        caller's fcntl.flock on self._lock_path — never on self._path
        itself, which this method replaces (see the self._lock_path
        comment in __init__).
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
