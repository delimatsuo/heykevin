"""Bounded Firestore transactions for Jobber receipts and recovery.

The SDK transaction decorator does not bound begin/commit/rollback RPCs. This
adapter keeps the transaction ID on every commit and never changes the shared
client. Tests inject a transaction factory explicitly on their fake database.
"""
from __future__ import annotations

import asyncio
import datetime
import math
from collections import deque
from dataclasses import dataclass
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
        try:
            valid = type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0
        except OverflowError:
            valid = False
        if not valid:
            raise JobberDeadlineError("Jobber datastore deadline must be finite and positive")
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


# Waiting tickets never enter the executor's unbounded work-item queue. All
# state uses a thread lock, so callers on independent event loops share one FIFO.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jobber-store")
_OUTSTANDING: set[Future] = set()
_OUTSTANDING_LOCK = Lock()
_MAX_WAITING = 128
_POLL_SECONDS = 0.02
_DRAINERS = 0


@dataclass(eq=False)
class _WaitingTicket:
    deadline: Deadline
    owner: asyncio.Task | None = None
    rejected: bool = False


_WAITING: deque[_WaitingTicket] = deque()


def _prune_waiting() -> None:
    # Called under the lock; do not depend on a ticket's event loop resuming.
    now = time.monotonic()
    live = []
    for ticket in _WAITING:
        if (ticket.rejected or ticket.deadline.cancelled.is_set() or ticket.deadline.ends <= now
                or (ticket.owner is not None and (ticket.owner.cancelled() or ticket.owner.cancelling()))):
            ticket.rejected = True
        else:
            live.append(ticket)
    _WAITING.clear()
    _WAITING.extend(live)


def _finished(future: Future) -> None:
    with _OUTSTANDING_LOCK:
        _OUTSTANDING.discard(future)
        _prune_waiting()


def _observe_future(future):
    # Consume late errors without cancelling the underlying concurrent Future.
    if not future.cancelled():
        future.exception()


def _invoke(callback, deadline, owner):
    # Cancellation/expiry can occur after submit but before this worker starts,
    # even before the owning event loop resumes its exception handler.
    if owner is not None and (owner.cancelled() or owner.cancelling()):
        deadline.cancelled.set()
    deadline.timeout()
    return callback(deadline)


async def run_bounded_async(callback: Callable[[Deadline], T], *, seconds: float) -> T:
    deadline = Deadline(seconds)
    ticket = _WaitingTicket(deadline, asyncio.current_task())
    future = None
    try:
        with _OUTSTANDING_LOCK:
            _prune_waiting()
            if _DRAINERS or len(_WAITING) >= _MAX_WAITING:
                raise JobberDeadlineError("Jobber datastore capacity unavailable")
            _WAITING.append(ticket)
        while future is None:
            deadline.timeout()
            with _OUTSTANDING_LOCK:
                _prune_waiting()
                if _DRAINERS or ticket.rejected:
                    raise JobberDeadlineError("Jobber datastore admission unavailable")
                if _WAITING[0] is ticket and len(_OUTSTANDING) < 2:
                    deadline.timeout()
                    _WAITING.popleft()
                    future = _EXECUTOR.submit(_invoke, callback, deadline, ticket.owner)
                    _OUTSTANDING.add(future)
            if future is None:
                await asyncio.sleep(min(_POLL_SECONDS, deadline.timeout()))
        # An already-completed Future invokes its callback synchronously. Never
        # register it while holding the lock acquired by _finished.
        future.add_done_callback(_finished)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(_observe_future)
        deadline.timeout()
        return await asyncio.wait_for(asyncio.shield(wrapped),
                                      timeout=max(0.0, deadline.ends - time.monotonic()))
    except BaseException:
        deadline.cancelled.set()
        raise
    finally:
        with _OUTSTANDING_LOCK:
            if ticket in _WAITING:
                _WAITING.remove(ticket)


async def drain_jobber_operations() -> None:
    global _DRAINERS
    with _OUTSTANDING_LOCK:
        _DRAINERS += 1
        for ticket in _WAITING:
            ticket.rejected = True
        _WAITING.clear()
        pending = list(_OUTSTANDING)
    try:
        if pending:
            wrapped = [asyncio.wrap_future(future) for future in pending]
            for future in wrapped:
                future.add_done_callback(_observe_future)
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(future) for future in wrapped),
                               return_exceptions=True),
                timeout=22.0,
            )
    finally:
        # Each drain owns one registration, even when cancelled or timed out.
        # Actual running futures remain tracked until their completion callback.
        with _OUTSTANDING_LOCK:
            _DRAINERS -= 1
