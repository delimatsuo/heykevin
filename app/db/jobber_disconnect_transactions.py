"""Bounded Firestore transactions for Jobber receipts and recovery.

The SDK transaction decorator does not bound begin/commit/rollback RPCs. This
adapter keeps the transaction ID on every commit and never changes the shared
client. Tests inject a transaction factory explicitly on their fake database.
"""
from __future__ import annotations

import asyncio
import datetime
import math
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from threading import Event, Lock
import time
from typing import Any, Callable, TypeVar

from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.transaction import Transaction

T = TypeVar("T")
RPC_SECONDS = 2.0
MAX_ATTEMPTS = 3


class JobberDisconnectError(Exception):
    """Base error for Jobber disconnect persistence and validation."""


class JobberSnapshotTimeError(JobberDisconnectError):
    """An authoritative document read has no trustworthy server clock."""


def extract_snapshot_time(snap: Any) -> float:
    read_time = getattr(snap, "read_time", None)
    if not isinstance(read_time, datetime.datetime) or read_time.tzinfo is None:
        raise JobberSnapshotTimeError("Snapshot requires aware server datetime")
    if read_time.utcoffset() is None:
        raise JobberSnapshotTimeError("Snapshot requires aware server datetime")
    ts = read_time.timestamp()
    if type(ts) not in (float, int) or not math.isfinite(ts) or ts <= 0:
        raise JobberSnapshotTimeError("Invalid server timestamp")
    return float(ts)


class JobberDeadlineError(TimeoutError):
    pass


class Deadline:
    def __init__(self, seconds: float = 6.0):
        self.ends = time.monotonic() + seconds
        self.cancelled = Event()

    def timeout(self) -> float:
        remaining = self.ends - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise JobberDeadlineError("Jobber datastore deadline exceeded")
        return min(RPC_SECONDS, remaining)


class BoundedTransaction(Transaction):
    def __init__(self, client: Any, *, deadline: Deadline, read_only: bool = False):
        super().__init__(client, max_attempts=MAX_ATTEMPTS, read_only=read_only)
        self.jobber_deadline = deadline

    def _begin(self, retry_id: bytes | None = None) -> None:
        if self.in_progress:
            raise ValueError("Transaction already started")
        response = self._client._firestore_api.begin_transaction(
            request={"database": self._client._database_string,
                     "options": self._options_protobuf(retry_id)},
            metadata=self._client._rpc_metadata, retry=None,
            timeout=self.jobber_deadline.timeout(),
        )
        self._id = response.transaction

    def _commit(self) -> list:
        if not self.in_progress:
            raise ValueError("Transaction not started")
        response = self._client._firestore_api.commit(
            request={"database": self._client._database_string,
                     "writes": self._write_pbs, "transaction": self._id},
            metadata=self._client._rpc_metadata, retry=None,
            timeout=self.jobber_deadline.timeout(),
        )
        self._clean_up()
        self.write_results = list(response.write_results)
        self.commit_time = response.commit_time
        return self.write_results

    def _rollback(self) -> None:
        if not self.in_progress:
            return
        try:
            # Cleanup remains bounded even after the operation's deadline.
            self._client._firestore_api.rollback(
                request={"database": self._client._database_string,
                         "transaction": self._id},
                metadata=self._client._rpc_metadata, retry=None, timeout=0.25,
            )
        finally:
            self._clean_up()


def run_transaction(db: Any, callback: Callable[[Any], T], *,
                    deadline: Deadline | None = None, read_only: bool = False) -> T:
    deadline = deadline or Deadline()
    factory = getattr(db, "jobber_transaction_factory", None)
    retry_id = None
    for attempt in range(MAX_ATTEMPTS):
        deadline.timeout()
        txn = (factory(deadline=deadline, read_only=read_only) if factory is not None
               else BoundedTransaction(db, deadline=deadline, read_only=read_only))
        try:
            txn._begin(retry_id=retry_id)
            retry_id = txn.id
            result = callback(txn)
            txn._commit()
            return result
        except Aborted:
            with suppress(Exception):
                txn._rollback()
            if read_only or attempt == MAX_ATTEMPTS - 1:
                raise
        except BaseException:
            with suppress(Exception):
                txn._rollback()
            raise
    raise JobberDeadlineError("Transaction attempts exhausted")


def read_document(ref: Any, txn: Any) -> Any:
    """A bounded read that also works with existing started SDK transactions."""
    deadline = getattr(txn, "jobber_deadline", None)
    timeout = deadline.timeout() if deadline is not None else RPC_SECONDS
    # DocumentReference.get fabricates a local read_time when batch-get is empty.
    # Require one actual server response (including an explicit missing snapshot).
    responses = txn.get(ref, retry=None, timeout=timeout)
    try:
        iterator = iter(responses)
        snapshot = next(iterator, None)
        if snapshot is None or next(iterator, None) is not None:
            raise ValueError("Expected exactly one server document snapshot")
        if getattr(snapshot, "id", None) != ref.id:
            raise ValueError("Server document identity mismatch")
        # Missing documents are authoritative too; validate every retry/readback.
        extract_snapshot_time(snapshot)
        return snapshot
    finally:
        close = getattr(responses, "close", None)
        if close is not None:
            close()


def stream_query(query: Any, deadline: Deadline) -> list:
    deadline.timeout()
    rows = []
    stream = query.stream(retry=None, timeout=deadline.timeout())
    try:
        for row in stream:
            deadline.timeout()
            rows.append(row)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    return rows


# Shared finite capacity; a cancelled await never makes a running thread disappear.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jobber-store")
_OUTSTANDING: set[Future] = set()
_OUTSTANDING_LOCK = Lock()


def _finished(future: Future) -> None:
    with _OUTSTANDING_LOCK:
        _OUTSTANDING.discard(future)


async def run_bounded_async(callback: Callable[[Deadline], T], *, seconds: float) -> T:
    deadline = Deadline(seconds)
    with _OUTSTANDING_LOCK:
        if len(_OUTSTANDING) >= 2:
            raise JobberDeadlineError("Jobber datastore capacity exhausted")
        future = _EXECUTOR.submit(callback, deadline)
        _OUTSTANDING.add(future)
    future.add_done_callback(_finished)
    wrapped = asyncio.wrap_future(future)
    # Retrieve exceptions even if its caller is cancelled.
    wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(wrapped), timeout=seconds + 0.5)
    except BaseException:
        deadline.cancelled.set()
        raise


async def drain_jobber_operations() -> None:
    with _OUTSTANDING_LOCK:
        pending = list(_OUTSTANDING)
    if pending:
        await asyncio.wait_for(
            asyncio.gather(*(asyncio.shield(asyncio.wrap_future(f)) for f in pending),
                           return_exceptions=True),
            timeout=22.0,
        )
