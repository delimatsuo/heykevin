"""Constant-cardinality private admission ledger. Uncertain work never replays."""

from dataclasses import dataclass
import json
import math
import re
import secrets

from google.cloud import firestore
from google.cloud.firestore_v1.transaction import Transaction

COLLECTION = "private_background_jobs"
CUTOVER_COLLECTION = "private_background_worker_control"
CUTOVER_DOCUMENT = "cutover"
CUTOVER_PROTOCOL_VERSION = 1
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
MAX_CONTINUATION_BYTES = 4096
CUTOVER_MIN_LEASE_SECONDS = 60
CUTOVER_MAX_LEASE_SECONDS = 24 * 60 * 60
_CUTOVER_GENERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{15,127}\Z")


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


def validate_cutover_generation(value: str) -> str:
    """Validate the non-secret identifier for one external-worker handoff."""
    if not isinstance(value, str) or not _CUTOVER_GENERATION_RE.fullmatch(value):
        raise ValueError("Invalid background-worker cutover generation")
    return value


def _valid_timestamp(value: object, now: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        and value <= now
    )


def validate_continuation(job: str, continuation: object) -> dict:
    """Validate the fixed, durable cursor shape for one background job."""
    if job not in JOB_INTERVALS or not isinstance(continuation, dict):
        raise ValueError("Invalid continuation")
    allowed = {
        "orphan_calls": {"cursor"},
        "lapsed_trials": {"cursor"},
        "expired_contractors": {"cursor"},
        "post_calls": {"pending", "in_progress"},
        "estimates": {"processing"},
    }.get(job, set())
    if not set(continuation).issubset(allowed):
        raise ValueError("Unexpected continuation")
    for value in continuation.values():
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 512
                                  or "/" in value or any(ord(char) < 32 for char in value)):
            raise ValueError("Invalid continuation cursor")
    if len(json.dumps(continuation).encode()) > MAX_CONTINUATION_BYTES:
        raise ValueError("Oversized continuation")
    return continuation


def _settled_job_snapshot(job: str, snapshot) -> bool:
    """Whether a ledger row is safe to leave behind before embedded fallback.

    This deliberately matches the completed-record shape accepted by normal
    admission. A status string alone is never enough to let a cutover restart
    legacy loops: a partial/manual record could otherwise hide uncertain work.
    """
    if not snapshot.exists:
        return True
    data = snapshot.to_dict()
    if not isinstance(data, dict) or data.get("status") != "completed":
        return False
    due = data.get("next_due_at")
    if (not isinstance(due, (int, float)) or isinstance(due, bool)
            or not math.isfinite(due) or due < 0):
        return False
    continuation = data["continuation"] if "continuation" in data else {}
    try:
        validate_continuation(job, continuation)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _job_hold_reason(job: str, snapshot) -> str | None:
    """A fixed label for why a ledger row holds embedded fallback, or None.

    Only these labels are logged, never stored values, so a malformed row
    cannot write arbitrary text into the drain log.
    """
    if _settled_job_snapshot(job, snapshot):
        return None
    data = snapshot.to_dict()
    status = data.get("status") if isinstance(data, dict) else None
    return next((label for label in ("running", "needs_attention") if status == label), "malformed")


def _authoritative_batch_snapshots(db, refs, *, transaction=None, error_message: str) -> list:
    """Return batch reads in caller order, never trusting Firestore stream order."""
    expected = {ref.path for ref in refs}
    if len(expected) != len(refs):
        raise ValueError(error_message)
    kwargs = {"retry": None, "timeout": RPC_TIMEOUT_SECONDS}
    if transaction is not None:
        kwargs["transaction"] = transaction
    by_path = {}
    for snapshot in db.get_all(refs, **kwargs):
        reference = getattr(snapshot, "reference", None)
        path = getattr(reference, "path", None)
        if snapshot is None or path not in expected or path in by_path:
            raise ValueError(error_message)
        by_path[path] = snapshot
    if set(by_path) != expected:
        raise ValueError(error_message)
    return [by_path[ref.path] for ref in refs]


class BackgroundWorkerCutoverStore:
    """Durable, staging-only handoff between embedded and external workers.

    No public route can prepare a cutover. A controlled operator workflow must
    first prove that old embedded instances are drained and that Scheduler has
    reached the private worker, then call :meth:`prepare_external`. The API and
    worker use the same transaction-backed record; a partial rollout therefore
    retains embedded processing instead of allowing duplicate work.
    """

    def __init__(self, db, *, trusted_clock=None, transaction_factory=None):
        self.db = db
        self.trusted_clock = trusted_clock
        self.transaction_factory = transaction_factory or (lambda: BoundedTransaction(db, max_attempts=1))
        # (job, reason) for each ledger row that held the last committed
        # begin_embedded(); a failed call leaves the previous value.
        self.held_jobs: tuple[tuple[str, str], ...] = ()

    def _ref(self):
        return self.db.collection(CUTOVER_COLLECTION).document(CUTOVER_DOCUMENT)

    def _job_refs(self):
        return [self.db.collection(COLLECTION).document(job) for job in JOB_INTERVALS]

    def _snapshots(self, refs, transaction=None):
        # Firestore does not guarantee get_all() order. Match snapshots to their
        # references before making any cutover decision.
        return _authoritative_batch_snapshots(
            self.db, refs, transaction=transaction,
            error_message="Missing authoritative background-worker snapshot",
        )

    @staticmethod
    def _active(data: dict, generation: str, now: float) -> bool:
        expires = data.get("lease_expires_at")
        return (
            data.get("state") == "external_active"
            and data.get("protocol_version") == CUTOVER_PROTOCOL_VERSION
            and data.get("generation") == generation
            and _valid_timestamp(data.get("api_acknowledged_at"), now)
            and isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and math.isfinite(expires)
            and now < expires
        )

    def prepare_external(
        self,
        generation: str,
        *,
        lease_seconds: int,
        embedded_instances_drained: bool,
        scheduler_delivery_verified: bool,
    ) -> None:
        """Record one explicitly observed, bounded external-worker handoff.

        This method is intentionally not exposed by the API or worker. Its two
        boolean confirmations are supplied only by the controlled staging
        procedure after independent readback; they are not inferred from a
        deployment request or a health response.
        """
        generation = validate_cutover_generation(generation)
        if type(lease_seconds) is not int or not CUTOVER_MIN_LEASE_SECONDS <= lease_seconds <= CUTOVER_MAX_LEASE_SECONDS:
            raise ValueError("Invalid background-worker cutover lease")
        if embedded_instances_drained is not True or scheduler_delivery_verified is not True:
            raise ValueError("External cutover requires explicit drain and Scheduler confirmations")
        ref = self._ref()

        @firestore.transactional
        def body(transaction):
            snapshot, = self._snapshots([ref], transaction)
            now = _server_now(snapshot, self.trusted_clock)
            data = snapshot.to_dict() or {}
            if (
                not snapshot.exists
                or data.get("state") != "embedded"
                or data.get("protocol_version") != CUTOVER_PROTOCOL_VERSION
            ):
                raise ValueError("Background-worker cutover has no verified embedded baseline")
            transaction.set(ref, {
                "state": "external_requested",
                "protocol_version": CUTOVER_PROTOCOL_VERSION,
                "generation": generation,
                "lease_expires_at": now + lease_seconds,
                "embedded_drained_at": now,
                "scheduler_verified_at": now,
            })

        body(self.transaction_factory())

    def activate_external(self, generation: str) -> bool:
        """Let the API acknowledge an already prepared cutover exactly once."""
        generation = validate_cutover_generation(generation)
        ref = self._ref()

        @firestore.transactional
        def body(transaction):
            snapshot, = self._snapshots([ref], transaction)
            now = _server_now(snapshot, self.trusted_clock)
            data = snapshot.to_dict() or {}
            if self._active(data, generation, now):
                return True
            expires = data.get("lease_expires_at")
            prepared = (
                data.get("state") == "external_requested"
                and data.get("protocol_version") == CUTOVER_PROTOCOL_VERSION
                and data.get("generation") == generation
                and _valid_timestamp(data.get("embedded_drained_at"), now)
                and _valid_timestamp(data.get("scheduler_verified_at"), now)
                and isinstance(expires, (int, float))
                and not isinstance(expires, bool)
                and math.isfinite(expires)
                and now < expires
            )
            if not prepared:
                return False
            transaction.set(ref, {
                **data,
                "state": "external_active",
                "api_acknowledged_at": now,
            })
            return True

        return body(self.transaction_factory())

    def begin_embedded(self) -> bool:
        """Fence new external claims, then permit legacy loops only when idle."""
        ref = self._ref()
        refs = [ref, *self._job_refs()]
        held = ()

        @firestore.transactional
        def body(transaction):
            nonlocal held
            control, *jobs = self._snapshots(refs, transaction)
            data = control.to_dict() or {}
            held = tuple(
                (job, reason)
                for job, snapshot in zip(JOB_INTERVALS, jobs)
                if (reason := _job_hold_reason(job, snapshot)) is not None
            )
            if held:
                if not control.exists or data.get("state") != "external_draining":
                    transaction.set(ref, {**data, "state": "external_draining"})
                return False
            # Preserve an explicit draining turn even when the final admitted
            # worker already completed. It makes a rollback/cutover fence
            # durable and gives any concurrent external admission a control
            # write to conflict with before embedded loops can start.
            if data.get("state") in {"external_requested", "external_active"}:
                transaction.set(ref, {**data, "state": "external_draining"})
                return False
            if (
                control.exists
                and data.get("state") == "embedded"
                and data.get("protocol_version") == CUTOVER_PROTOCOL_VERSION
            ):
                return True
            transaction.set(ref, {
                "state": "embedded",
                "protocol_version": CUTOVER_PROTOCOL_VERSION,
            })
            return True

        ready = body(self.transaction_factory())
        self.held_jobs = held
        return ready

    def external_active(self, generation: str) -> bool:
        """Read whether the current external lease remains admitted."""
        generation = validate_cutover_generation(generation)
        snapshot, = self._snapshots([self._ref()])
        now = _server_now(snapshot, self.trusted_clock)
        return snapshot.exists and self._active(snapshot.to_dict() or {}, generation, now)


class BackgroundJobStore:
    def __init__(self, db, *, trusted_clock=None, transaction_factory=None, external_generation: str | None = None):
        self.db = db
        self.trusted_clock = trusted_clock
        self.transaction_factory = transaction_factory or (lambda: BoundedTransaction(db, max_attempts=1))
        self.external_generation = (
            validate_cutover_generation(external_generation)
            if external_generation is not None
            else None
        )

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

    def _snapshots(self, refs, transaction):
        return _authoritative_batch_snapshots(
            self.db, refs, transaction=transaction,
            error_message="Missing authoritative admission snapshot",
        )

    @staticmethod
    def _admit_from_snapshot(job: str, ref, snapshot, transaction, now: float, nonce: str) -> Admission:
        data = snapshot.to_dict() or {}
        # Validate before the due-time return. A malformed completed row must
        # become visible as a held ledger state immediately, rather than sit
        # quietly until its next calendar slot while embedded fallback starts.
        continuation = data["continuation"] if "continuation" in data else {}
        try:
            continuation = validate_continuation(job, continuation)
        except (TypeError, ValueError, RecursionError):
            return Admission("needs_attention")
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
            if (not isinstance(due, (int, float)) or isinstance(due, bool)
                    or not math.isfinite(due) or due < 0):
                return Admission("needs_attention")
            if now < due:
                return Admission("not_due")
        transaction.set(ref, {
            "status": "running", "nonce": nonce, "started_at": now,
            "next_due_at": (math.floor(now / JOB_INTERVALS[job]) + 1) * JOB_INTERVALS[job],
            "continuation": continuation,
        })
        return Admission("admitted", nonce, continuation)

    def admit(self, job: str) -> Admission:
        ref = self._ref(job)
        nonce = secrets.token_hex(24)

        if self.external_generation is not None:
            control_ref = self.db.collection(CUTOVER_COLLECTION).document(CUTOVER_DOCUMENT)

            @firestore.transactional
            def external_body(transaction):
                control, snapshot = self._snapshots([control_ref, ref], transaction)
                now = _server_now(control, self.trusted_clock)
                control_data = control.to_dict() or {}
                if not BackgroundWorkerCutoverStore._active(
                    control_data, self.external_generation, now,
                ):
                    if (
                        control_data.get("state") == "external_requested"
                        and control_data.get("generation") == self.external_generation
                    ):
                        return Admission("cutover_pending")
                    return Admission("cutover_blocked")
                return self._admit_from_snapshot(job, ref, snapshot, transaction, now, nonce)

            return external_body(self.transaction_factory())

        @firestore.transactional
        def body(transaction):
            snapshot = self._snapshot(ref, transaction)
            now = _server_now(snapshot, self.trusted_clock)
            return self._admit_from_snapshot(job, ref, snapshot, transaction, now, nonce)

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
