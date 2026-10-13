"""Constant-cardinality private admission ledger. Uncertain work never replays."""

from dataclasses import dataclass
import math
import secrets

from google.cloud import firestore
from google.cloud.firestore_v1.transaction import Transaction

COLLECTION = "private_background_jobs"
JOB_INTERVALS = {
    "orphan_calls": 300,
    "call_retention": 21600,
    "lapsed_trials": 21600,
    "expired_contractors": 21600,
    "data_purge": 21600,
    "post_calls": 60,
    "estimates": 60,
    "service_requests": 60,
    "jobber_disconnect": 60,
}


RPC_TIMEOUT_SECONDS = 2.0


class BoundedTransaction(Transaction):
    """Bound every SDK RPC, including the decorator's begin/commit/rollback.

    One attempt fails closed on contention; the next scheduler request can
    retry a failed admission without ever having executed provider work.
    """
    def _begin(self, retry_id=None):
        if self.in_progress:
            raise ValueError("Transaction already started")
        response = self._client._firestore_api.begin_transaction(
            request={"database": self._client._database_string,
                     "options": self._options_protobuf(retry_id)},
            metadata=self._client._rpc_metadata, retry=None, timeout=RPC_TIMEOUT_SECONDS)
        self._id = response.transaction

    def _commit(self):
        if not self.in_progress:
            raise ValueError("Transaction not started")
        response = self._client._firestore_api.commit(
            request={"database": self._client._database_string, "writes": self._write_pbs,
                     "transaction": self._id}, metadata=self._client._rpc_metadata,
            retry=None, timeout=RPC_TIMEOUT_SECONDS)
        self._clean_up()
        self.write_results = list(response.write_results)
        self.commit_time = response.commit_time
        return self.write_results

    def _rollback(self):
        if not self.in_progress:
            return
        try:
            self._client._firestore_api.rollback(
                request={"database": self._client._database_string, "transaction": self._id},
                metadata=self._client._rpc_metadata, retry=None, timeout=RPC_TIMEOUT_SECONDS)
        finally:
            self._clean_up()


@dataclass(frozen=True)
class Admission:
    status: str
    nonce: str = ""
    continuation: dict | None = None


def _server_now(snapshot, trusted_clock):
    value = trusted_clock() if trusted_clock is not None else snapshot.read_time.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Missing trusted admission time")
    return value


class BackgroundJobStore:
    def __init__(self, db, *, trusted_clock=None, transaction_factory=None):
        self.db = db
        self.trusted_clock = trusted_clock
        self.transaction_factory = transaction_factory or (lambda: BoundedTransaction(db, max_attempts=1))

    def _ref(self, job):
        if job not in JOB_INTERVALS:
            raise ValueError("Unknown background job")
        return self.db.collection(COLLECTION).document(job)

    def _snapshot(self, ref, transaction):
        # DocumentReference.get synthesizes a missing snapshot with local time
        # when batch_get returns nothing. Require an actual server response.
        snapshots = iter(self.db.get_all(
            [ref], transaction=transaction, retry=None, timeout=RPC_TIMEOUT_SECONDS,
        ))
        snapshot = next(snapshots, None)
        if snapshot is None or next(snapshots, None) is not None:
            raise ValueError("Missing authoritative admission snapshot")
        return snapshot

    def admit(self, job: str) -> Admission:
        ref = self._ref(job)
        nonce = secrets.token_hex(24)

        @firestore.transactional
        def body(transaction):
            snapshot = self._snapshot(ref, transaction)
            now = _server_now(snapshot, self.trusted_clock)
            data = snapshot.to_dict() or {}
            if snapshot.exists:
                if data.get("status") == "running":
                    started = data.get("started_at")
                    if (isinstance(started, (int, float)) and not isinstance(started, bool)
                            and math.isfinite(started) and 0 <= now - started < 300):
                        return Admission("in_progress")
                    return Admission("needs_attention")
                if data.get("status") != "completed":
                    return Admission("needs_attention")
                due = data.get("next_due_at")
                if not isinstance(due, (int, float)) or isinstance(due, bool) or not math.isfinite(due):
                    return Admission("needs_attention")
                if now < due:
                    return Admission("not_due")
            continuation = data.get("continuation") or {}
            if not isinstance(continuation, dict):
                return Admission("needs_attention")
            transaction.set(ref, {
                "status": "running", "nonce": nonce, "started_at": now,
                "next_due_at": (math.floor(now / JOB_INTERVALS[job]) + 1) * JOB_INTERVALS[job],
                "continuation": continuation,
            })
            return Admission("admitted", nonce, continuation)

        return body(self.transaction_factory())

    def finish(self, job: str, nonce: str, *, completed: bool, continuation=None) -> bool:
        ref = self._ref(job)

        @firestore.transactional
        def body(transaction):
            snapshot = self._snapshot(ref, transaction)
            data = snapshot.to_dict() or {}
            if not snapshot.exists or data.get("status") != "running" or data.get("nonce") != nonce:
                return False
            _server_now(snapshot, self.trusted_clock)
            updates = {"status": "completed" if completed else "needs_attention"}
            if completed:
                updates.update(continuation=continuation or {})
            transaction.update(ref, updates)
            return True

        return body(self.transaction_factory())
