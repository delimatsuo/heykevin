"""Google Calendar mutation adapter for product-owned service requests.

The repository owns provider bindings and customer identity.  This module only
translates a structurally compatible binding and service request into the existing
Google Calendar mutation functions.  It never logs resource IDs or request payloads.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.services import calendar
from app.services.service_request import ServiceRequest
from app.services.service_request_repository import ProviderBinding, ProviderMutationAdapter
from app.utils.logging import get_logger

logger = get_logger(__name__)

GOOGLE_CALENDAR_PROVIDER_KIND = "google_calendar"
MAX_RESOURCE_ID_LENGTH = 512
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_SERVICE_LABEL_LENGTH = 120
MAX_PROVIDER_SERVICES = 20
MAX_PROVIDER_METADATA_VALUE_BYTES = calendar.MAX_PRIVATE_PROPERTY_VALUE_BYTES
MAX_PROVIDER_TITLE_LENGTH = 200
MAX_PROVIDER_DESCRIPTION_LENGTH = 1_500


class GoogleCalendarRequestProvider(ProviderMutationAdapter):
    """Apply service-request mutations to one contractor's Google Calendar."""

    def __init__(self, contractor_config: dict[str, Any]) -> None:
        if not isinstance(contractor_config, dict):
            raise TypeError("contractor_config must be a dictionary")
        # Calendar token refresh updates this dictionary in place. Retaining the
        # injected object lets later mutations use the refreshed token.
        self._contractor_config = contractor_config

    async def create(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        title: str,
        description: str,
        idempotency_key: str,
    ) -> bool:
        """Create the event selected by a durable provider-create proposal."""

        resource_id = _google_resource_id(binding)
        schedule = _request_schedule(request)
        normalized_title = _provider_text(
            title,
            max_length=MAX_PROVIDER_TITLE_LENGTH,
            required=True,
        )
        normalized_description = _provider_description(description)
        if (
            resource_id is None
            or schedule is None
            or normalized_title is None
            or normalized_description is None
            or not _valid_logical_operation_id(idempotency_key)
        ):
            return False
        start_time, end_time = schedule
        return await self._call(
            "create",
            calendar.create_managed_appointment(
                self._contractor_config,
                event_id=resource_id,
                title=normalized_title,
                start_time=start_time,
                end_time=end_time,
                description=normalized_description,
                logical_operation_id=idempotency_key,
            ),
        )

    async def cancel(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        idempotency_key: str,
    ) -> bool:
        """Cancel the event bound to a service request."""

        resource_id = _google_resource_id(binding)
        if resource_id is None or not _valid_idempotency_key(idempotency_key):
            return False
        return await self._call(
            "cancel",
            calendar.cancel_appointment(self._contractor_config, resource_id),
        )

    async def reschedule(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        scheduled_start: datetime,
        scheduled_end: datetime,
        idempotency_key: str,
    ) -> bool:
        """Move the bound event using read-before-write optimistic fencing."""

        resource_id = _google_resource_id(binding)
        base_schedule = _request_schedule(request)
        desired_schedule = _normalized_schedule(scheduled_start, scheduled_end)
        if (
            resource_id is None
            or base_schedule is None
            or desired_schedule is None
            or not _valid_logical_operation_id(idempotency_key)
        ):
            return False
        base_start, base_end = base_schedule
        desired_start, desired_end = desired_schedule
        return await self._call(
            "reschedule",
            calendar.reschedule_appointment(
                self._contractor_config,
                event_id=resource_id,
                base_start=base_start,
                base_end=base_end,
                desired_start=desired_start,
                desired_end=desired_end,
                logical_operation_id=idempotency_key,
            ),
        )

    async def reconcile_reschedule(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        scheduled_start: datetime,
        scheduled_end: datetime,
        logical_operation_id: str,
    ) -> calendar.CalendarReconciliationResult:
        """Perform GET-only reconciliation for a bound reschedule operation."""

        resource_id = _google_resource_id(binding)
        desired_schedule = _normalized_schedule(scheduled_start, scheduled_end)
        if (
            resource_id is None
            or desired_schedule is None
            or not _valid_logical_operation_id(logical_operation_id)
        ):
            return calendar.CalendarReconciliationResult(
                has_matching_claim=False,
                confirmed=False,
            )
        desired_start, desired_end = desired_schedule
        return await calendar.reconcile_reschedule_appointment(
            self._contractor_config,
            event_id=resource_id,
            desired_start=desired_start,
            desired_end=desired_end,
            logical_operation_id=logical_operation_id,
        )

    async def clear_reconciled_claim(
        self,
        *,
        claim_id: str,
        logical_operation_id: str,
    ) -> bool:
        """Clear an exact reconciled claim after recovery finalization."""

        if not _valid_logical_operation_id(logical_operation_id):
            return False
        try:
            return await calendar.clear_reconciled_reschedule_claim(
                self._contractor_config,
                claim_id=claim_id,
                logical_operation_id=logical_operation_id,
            )
        except Exception:
            return False

    async def add_service(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        service: str,
        idempotency_key: str,
    ) -> bool:
        """Add a service to bounded, product-owned private event metadata."""

        resource_id = _google_resource_id(binding)
        metadata_value = _service_metadata_value(request, service)
        if (
            resource_id is None
            or metadata_value is None
            or not _valid_idempotency_key(idempotency_key)
        ):
            return False
        return await self._call(
            "add_service",
            calendar.set_appointment_service_metadata(
                self._contractor_config,
                resource_id,
                metadata_value,
            ),
        )

    async def _call(self, operation: str, pending_call) -> bool:
        try:
            result = await pending_call
        except Exception as error:  # noqa: BLE001 - provider errors fail closed
            logger.warning(
                "google_calendar_request_provider operation=%s outcome=failed exception_type=%s",
                operation,
                type(error).__name__,
            )
            return False
        return result is True


def _google_resource_id(binding: object) -> str | None:
    if getattr(binding, "kind", None) != GOOGLE_CALENDAR_PROVIDER_KIND:
        return None
    resource_id = getattr(binding, "resource_id", None)
    if not isinstance(resource_id, str):
        return None
    resource_id = resource_id.strip()
    if (
        not resource_id
        or len(resource_id) > MAX_RESOURCE_ID_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in resource_id)
    ):
        return None
    return resource_id


def _valid_idempotency_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.strip()) <= MAX_IDEMPOTENCY_KEY_LENGTH
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _valid_logical_operation_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _provider_text(
    value: object,
    *,
    max_length: int,
    required: bool,
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", value).split())
    if required and not normalized:
        return None
    if len(normalized) > max_length:
        return None
    return normalized


def _provider_description(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(" ".join(line.split()) for line in normalized.split("\n")).strip()
    if len(normalized) > MAX_PROVIDER_DESCRIPTION_LENGTH:
        return None
    return normalized


def _aware_datetime(value: datetime | str) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and 0 < len(value.strip()) <= 80:
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _normalized_schedule(
    start: datetime | str,
    end: datetime | str,
) -> tuple[str, str] | None:
    parsed_start = _aware_datetime(start)
    parsed_end = _aware_datetime(end)
    if parsed_start is None or parsed_end is None or parsed_end <= parsed_start:
        return None
    return parsed_start.isoformat(), parsed_end.isoformat()


def _request_schedule(request: object) -> tuple[str, str] | None:
    return _normalized_schedule(
        getattr(request, "scheduled_start", None),
        getattr(request, "scheduled_end", None),
    )


def _service_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", value).split())
    if not normalized:
        return None
    return normalized[:MAX_SERVICE_LABEL_LENGTH].strip()


def _service_metadata_value(request: object, service: str) -> str | None:
    addition = _service_label(service)
    request_services = getattr(request, "services", None)
    if (
        addition is None
        or not isinstance(request_services, Sequence)
        or isinstance(
            request_services,
            (str, bytes),
        )
    ):
        return None

    # Always reserve room for the newly requested service. The aggregate may be
    # supplied either before or after its mutation, so remove a matching entry
    # before appending the addition exactly once. JSON is used as a versioned,
    # deterministic value so an exact semantic replay can be recognized after
    # a process restart.
    labels: list[str] = []
    seen: set[str] = set()
    addition_key = addition.casefold()
    for value in request_services:
        label = _service_label(value)
        if label is None:
            continue
        key = label.casefold()
        if key == addition_key or key in seen:
            continue
        if len(labels) >= MAX_PROVIDER_SERVICES - 1:
            continue
        candidate = [*labels, label, addition]
        encoded = json.dumps(
            {"v": 1, "services": candidate},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > MAX_PROVIDER_METADATA_VALUE_BYTES:
            continue
        labels.append(label)
        seen.add(key)

    labels.append(addition)
    encoded = json.dumps(
        {"v": 1, "services": labels},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_PROVIDER_METADATA_VALUE_BYTES:
        return None
    return encoded
