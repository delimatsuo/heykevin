"""Bounded recovery worker for durable service-request provider proposals."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.db.contractors import get_contractor
from app.db.service_requests import (
    DEFAULT_RECOVERY_BATCH_SIZE,
    DEFAULT_RECOVERY_LEASE_SECONDS,
    MAX_PROVIDER_RECOVERY_ATTEMPTS,
    FirestoreServiceRequestRepository,
    LeasedProviderRecovery,
)
from app.services.google_calendar_request_provider import (
    GOOGLE_CALENDAR_PROVIDER_KIND,
    GoogleCalendarRequestProvider,
)
from app.services.service_request import ServiceRequestOperation
from app.services.service_request_repository import (
    PreparedProviderCreate,
    PreparedProviderOperation,
    ProviderMutationAdapter,
    _invoke_prepared_provider_operation,
    _parse_datetime,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

WORKER_INTERVAL_SECONDS = 30
CONTRACTOR_LOAD_TIMEOUT_SECONDS = 5.0
PROVIDER_CALL_TIMEOUT_SECONDS = 15.0


class ProviderRecoveryRepository(Protocol):
    async def claim_due_provider_recoveries(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> tuple[LeasedProviderRecovery, ...]: ...

    async def finalize_provider_recovery(self, lease: LeasedProviderRecovery) -> bool: ...

    async def finalize_reconciled_provider_recovery(
        self,
        lease: LeasedProviderRecovery,
        *,
        claim_id: str,
        bound_operation_id: str,
    ) -> bool: ...

    async def release_provider_recovery(
        self,
        lease: LeasedProviderRecovery,
        *,
        now: datetime,
        max_attempts: int,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProviderRecoveryRunResult:
    claimed: int
    finalized: int
    deferred: int


class ServiceRequestRecoveryWorker:
    """Recover provider sagas without relying on caller traffic.

    A pending durable proposal is the authority for recovery.  The contractor's
    current mutation feature flag is intentionally not consulted here: the
    provider may already have committed before the initiating process crashed.
    """

    def __init__(
        self,
        repository: ProviderRecoveryRepository,
        *,
        contractor_loader: Callable[[str], Awaitable[dict | None]] = get_contractor,
        adapter_factory: Callable[[dict[str, Any]], ProviderMutationAdapter] = (
            GoogleCalendarRequestProvider
        ),
        clock: Callable[[], datetime] | None = None,
        owner: str | None = None,
        batch_size: int = DEFAULT_RECOVERY_BATCH_SIZE,
        lease_seconds: int = DEFAULT_RECOVERY_LEASE_SECONDS,
        max_attempts: int = MAX_PROVIDER_RECOVERY_ATTEMPTS,
    ) -> None:
        self._repository = repository
        self._contractor_loader = contractor_loader
        self._adapter_factory = adapter_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner = owner or f"worker-{secrets.token_hex(12)}"
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def run_once(self) -> ProviderRecoveryRunResult:
        now = self._now()
        leases = await self._repository.claim_due_provider_recoveries(
            owner=self._owner,
            now=now,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        finalized = 0
        deferred = 0
        for lease in leases:
            canonical_finalized = False
            try:
                preparation = lease.preparation
                if preparation.binding.kind != GOOGLE_CALENDAR_PROVIDER_KIND:
                    await self._release(lease)
                    deferred += 1
                    continue

                contractor = await asyncio.wait_for(
                    self._contractor_loader(lease.contractor_id),
                    timeout=CONTRACTOR_LOAD_TIMEOUT_SECONDS,
                )
                if (
                    not isinstance(contractor, dict)
                    or contractor.get("contractor_id") != lease.contractor_id
                ):
                    await self._release(lease)
                    deferred += 1
                    continue

                adapter = self._adapter_factory(contractor)

                # Special uncertainty reconciliation for RESCHEDULE operations
                if (
                    isinstance(preparation, PreparedProviderOperation)
                    and preparation.operation is ServiceRequestOperation.RESCHEDULE
                ):
                    reconcile_reschedule = getattr(
                        adapter,
                        "reconcile_reschedule",
                        None,
                    )
                    if not callable(reconcile_reschedule):
                        await self._release(lease)
                        deferred += 1
                        continue
                    arguments = dict(preparation.arguments)
                    scheduled_start = _parse_datetime(arguments["scheduled_start"], "scheduled_start")
                    scheduled_end = _parse_datetime(arguments["scheduled_end"], "scheduled_end")
                    reconciliation = await asyncio.wait_for(
                        reconcile_reschedule(
                            binding=preparation.binding,
                            request=preparation.base_request,
                            scheduled_start=scheduled_start,
                            scheduled_end=scheduled_end,
                            logical_operation_id=preparation.logical_operation_id,
                        ),
                        timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                    )
                    authorization_status = getattr(
                        reconciliation,
                        "authorization_status",
                        None,
                    )
                    if authorization_status == "matching_claim":
                        claim_id = getattr(reconciliation, "claim_id", None)
                        if (
                            reconciliation.has_matching_claim is not True
                            or type(reconciliation.confirmed) is not bool
                            or getattr(reconciliation, "logical_operation_id", None)
                            != preparation.logical_operation_id
                            or type(claim_id) is not str
                            or re.fullmatch(r"[0-9A-Za-z_-]{1,128}", claim_id) is None
                        ):
                            await self._release(lease)
                            deferred += 1
                            continue
                        if not reconciliation.confirmed:
                            # Bound claim exists but remote desired is not confirmed:
                            # Issue zero PATCH, release/defer under attempt policy, retain claim.
                            await self._release(lease)
                            deferred += 1
                            continue

                        # Desired confirmed: one Firestore transaction must finalize
                        # canonical state and clear the exact provider fence together.
                        atomic_finalize = getattr(
                            self._repository,
                            "finalize_reconciled_provider_recovery",
                            None,
                        )
                        if not callable(atomic_finalize):
                            await self._release(lease)
                            deferred += 1
                            continue
                        finalize_ok = await atomic_finalize(
                            lease,
                            claim_id=claim_id,
                            bound_operation_id=preparation.logical_operation_id,
                        )
                        if finalize_ok:
                            canonical_finalized = True
                            finalized += 1
                        else:
                            await self._release(lease)
                            deferred += 1
                        continue
                    if (
                        authorization_status != "verified_absent"
                        or reconciliation.has_matching_claim is not False
                        or reconciliation.confirmed is not False
                        or getattr(reconciliation, "claim_id", None) is not None
                        or getattr(reconciliation, "logical_operation_id", None) is not None
                    ):
                        await self._release(lease)
                        deferred += 1
                        continue

                # Standard provider execution for other operations or reschedule without matching claim
                confirmed = False
                if isinstance(preparation, PreparedProviderCreate):
                    confirmed = await asyncio.wait_for(
                        adapter.create(
                            binding=preparation.binding,
                            request=preparation.result.request,
                            title=preparation.title,
                            description=preparation.description,
                            idempotency_key=preparation.logical_operation_id,
                        ),
                        timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                    )
                elif isinstance(preparation, PreparedProviderOperation):
                    confirmed = await asyncio.wait_for(
                        _invoke_prepared_provider_operation(adapter, preparation),
                        timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
                    )

                if confirmed is True:
                    if await self._repository.finalize_provider_recovery(lease):
                        finalized += 1
                    else:
                        deferred += 1
                    continue

                await self._release(lease)
                deferred += 1
            except asyncio.CancelledError:
                # Once canonical finalization commits, releasing the lease could
                # make a second worker replay an operation that is already done.
                if not canonical_finalized:
                    with suppress(Exception):
                        await self._release(lease)
                raise
            except Exception as error:  # noqa: BLE001 - recovery must fail closed
                if canonical_finalized:
                    continue
                logger.warning(
                    "service_request_recovery outcome=deferred exception_type=%s",
                    type(error).__name__,
                )
                with suppress(Exception):
                    await self._release(lease)
                deferred += 1
        return ProviderRecoveryRunResult(
            claimed=len(leases),
            finalized=finalized,
            deferred=deferred,
        )

    async def _invoke_provider(self, lease: LeasedProviderRecovery) -> bool:
        preparation = lease.preparation
        if preparation.binding.kind != GOOGLE_CALENDAR_PROVIDER_KIND:
            return False
        contractor = await asyncio.wait_for(
            self._contractor_loader(lease.contractor_id),
            timeout=CONTRACTOR_LOAD_TIMEOUT_SECONDS,
        )
        if (
            not isinstance(contractor, dict)
            or contractor.get("contractor_id") != lease.contractor_id
        ):
            return False
        adapter = self._adapter_factory(contractor)
        if isinstance(preparation, PreparedProviderCreate):
            return await asyncio.wait_for(
                adapter.create(
                    binding=preparation.binding,
                    request=preparation.result.request,
                    title=preparation.title,
                    description=preparation.description,
                    idempotency_key=preparation.logical_operation_id,
                ),
                timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
            )
        if isinstance(preparation, PreparedProviderOperation):
            return await asyncio.wait_for(
                _invoke_prepared_provider_operation(adapter, preparation),
                timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
            )
        return False

    async def _release(self, lease: LeasedProviderRecovery) -> bool:
        return await self._repository.release_provider_recovery(
            lease,
            now=self._now(),
            max_attempts=self._max_attempts,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("recovery clock must return an aware datetime")
        return value.astimezone(UTC)


async def service_request_recovery_worker_loop(
    *,
    worker: ServiceRequestRecoveryWorker | None = None,
    interval_seconds: int = WORKER_INTERVAL_SECONDS,
) -> None:
    """Continuously drain bounded due batches until application cancellation."""

    selected_worker = worker or ServiceRequestRecoveryWorker(FirestoreServiceRequestRepository())
    while True:
        try:
            result = await selected_worker.run_once()
            if result.claimed:
                logger.info(
                    "service_request_recovery claimed=%s finalized=%s deferred=%s",
                    result.claimed,
                    result.finalized,
                    result.deferred,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - next bounded pass may recover
            logger.warning(
                "service_request_recovery outcome=batch_failed exception_type=%s",
                type(error).__name__,
            )
        await asyncio.sleep(interval_seconds)


__all__ = [
    "ProviderRecoveryRunResult",
    "ServiceRequestRecoveryWorker",
    "service_request_recovery_worker_loop",
]
