"""Focused tests for the persistence-independent service-request aggregate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.service_request import (
    MAX_SERVICE_COUNT,
    MAX_SERVICE_LABEL_LENGTH,
    ServiceRequest,
    ServiceRequestConcurrencyError,
    ServiceRequestIdempotencyConflict,
    ServiceRequestOperation,
    ServiceRequestStatus,
    ServiceRequestTransitionError,
    ServiceRequestValidationError,
    normalize_service_label,
)

CREATED_AT = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
START = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
END = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)


def _create(**overrides):
    values = {
        "request_id": "request-1",
        "services": ["Toilet repair"],
        "scheduled_start": START,
        "scheduled_end": END,
        "expected_revision": 0,
        "idempotency_key": "create-1",
        "occurred_at": CREATED_AT,
    }
    values.update(overrides)
    return ServiceRequest.create(**values)


def test_create_normalizes_labels_and_datetimes_into_a_versioned_aggregate():
    eastern = timezone(timedelta(hours=-4))

    result = _create(
        request_id=" request-1 ",
        services=["  Water   heater  repair ", "Ｔｏｉｌｅｔ replacement"],
        scheduled_start=datetime(2026, 8, 13, 9, 0, tzinfo=eastern),
        scheduled_end=datetime(2026, 8, 13, 10, 0, tzinfo=eastern),
    )

    request = result.request
    assert request.schema_version == 1
    assert request.request_id == "request-1"
    assert request.status is ServiceRequestStatus.OPEN
    assert request.revision == 1
    assert request.services == ("Water heater repair", "Toilet replacement")
    assert request.scheduled_start == START
    assert request.scheduled_start.tzinfo is UTC
    assert result.outcome.operation is ServiceRequestOperation.CREATE
    assert result.outcome == request.idempotency_records[0].outcome


def test_create_requires_revision_zero_and_reports_actual_absent_revision():
    with pytest.raises(ServiceRequestConcurrencyError) as caught:
        _create(expected_revision=2)

    assert caught.value.expected_revision == 2
    assert caught.value.actual_revision == 0


def test_create_retry_returns_the_original_outcome_without_incrementing_revision():
    first = _create()

    retry = _create(
        existing=first.request,
        occurred_at=CREATED_AT + timedelta(minutes=5),
    )

    assert retry == first
    assert retry.request is first.request
    assert retry.outcome == first.outcome
    assert retry.request.revision == 1


def test_create_rejects_conflicting_key_reuse_and_a_second_create():
    first = _create()

    with pytest.raises(ServiceRequestIdempotencyConflict):
        _create(existing=first.request, services=["Drain clearing"])

    with pytest.raises(ServiceRequestTransitionError, match="already exists"):
        _create(existing=first.request, idempotency_key="create-2")


@pytest.mark.parametrize(
    "value",
    ["", "   ", "bad\nlabel", "x" * (MAX_SERVICE_LABEL_LENGTH + 1)],
)
def test_service_label_rejects_empty_control_and_overlong_values(value):
    with pytest.raises(ServiceRequestValidationError):
        normalize_service_label(value)


def test_create_rejects_duplicate_or_unbounded_service_lists():
    with pytest.raises(ServiceRequestValidationError, match="unique"):
        _create(services=["Drain clearing", " drain   CLEARING "])

    with pytest.raises(ServiceRequestValidationError, match="at most"):
        _create(services=[f"Service {index}" for index in range(MAX_SERVICE_COUNT + 1)])


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (START.replace(tzinfo=None), END),
        (START, END.replace(tzinfo=None)),
        (START, START),
        (END, START),
    ],
)
def test_create_requires_aware_ordered_schedule(start, end):
    with pytest.raises(ServiceRequestValidationError):
        _create(scheduled_start=start, scheduled_end=end)


def test_commands_require_aware_monotonic_occurrence_times():
    request = _create().request

    with pytest.raises(ServiceRequestValidationError, match="timezone-aware"):
        request.cancel(
            expected_revision=1,
            idempotency_key="cancel-1",
            occurred_at=CREATED_AT.replace(tzinfo=None),
        )

    with pytest.raises(ServiceRequestValidationError, match="precede"):
        request.cancel(
            expected_revision=1,
            idempotency_key="cancel-1",
            occurred_at=CREATED_AT - timedelta(seconds=1),
        )


def test_cancel_is_terminal_and_an_identical_retry_returns_the_same_outcome():
    request = _create().request
    first = request.cancel(
        expected_revision=1,
        idempotency_key="cancel-1",
        occurred_at=CREATED_AT + timedelta(minutes=1),
    )

    retry = first.request.cancel(
        expected_revision=1,
        idempotency_key="cancel-1",
        occurred_at=CREATED_AT + timedelta(hours=1),
    )

    assert first.request.status is ServiceRequestStatus.CANCELLED
    assert first.request.revision == 2
    assert retry == first
    assert retry.request is first.request
    assert retry.outcome == first.outcome
    assert retry.request.revision == 2

    with pytest.raises(ServiceRequestTransitionError):
        first.request.cancel(
            expected_revision=2,
            idempotency_key="cancel-2",
            occurred_at=CREATED_AT + timedelta(minutes=2),
        )


def test_expected_revision_guards_each_mutation():
    request = _create().request

    with pytest.raises(ServiceRequestConcurrencyError) as cancel_error:
        request.cancel(
            expected_revision=0,
            idempotency_key="cancel-1",
            occurred_at=CREATED_AT,
        )
    assert cancel_error.value.actual_revision == 1

    with pytest.raises(ServiceRequestConcurrencyError):
        request.reschedule(
            scheduled_start=START + timedelta(days=1),
            scheduled_end=END + timedelta(days=1),
            expected_revision=0,
            idempotency_key="reschedule-1",
            occurred_at=CREATED_AT,
        )

    with pytest.raises(ServiceRequestConcurrencyError):
        request.add_service(
            service="Drain clearing",
            expected_revision=0,
            idempotency_key="add-1",
            occurred_at=CREATED_AT,
        )


def test_reschedule_normalizes_to_utc_and_rejects_a_noop():
    request = _create().request
    eastern = timezone(timedelta(hours=-4))

    result = request.reschedule(
        scheduled_start=datetime(2026, 8, 14, 10, 0, tzinfo=eastern),
        scheduled_end=datetime(2026, 8, 14, 11, 0, tzinfo=eastern),
        expected_revision=1,
        idempotency_key="reschedule-1",
        occurred_at=CREATED_AT + timedelta(minutes=1),
    )

    assert result.request.revision == 2
    assert result.request.scheduled_start == datetime(2026, 8, 14, 14, 0, tzinfo=UTC)
    assert result.outcome.operation is ServiceRequestOperation.RESCHEDULE

    with pytest.raises(ServiceRequestTransitionError, match="must change"):
        request.reschedule(
            scheduled_start=START,
            scheduled_end=END,
            expected_revision=1,
            idempotency_key="reschedule-noop",
            occurred_at=CREATED_AT,
        )


def test_add_service_normalizes_and_rejects_a_duplicate():
    request = _create().request

    result = request.add_service(
        service="  Accessible   drain clearing ",
        expected_revision=1,
        idempotency_key="add-1",
        occurred_at=CREATED_AT + timedelta(minutes=1),
    )

    assert result.request.services == ("Toilet repair", "Accessible drain clearing")
    assert result.request.revision == 2

    with pytest.raises(ServiceRequestTransitionError, match="already present"):
        result.request.add_service(
            service="accessible DRAIN clearing",
            expected_revision=2,
            idempotency_key="add-2",
            occurred_at=CREATED_AT + timedelta(minutes=2),
        )


@pytest.mark.parametrize("operation", ["reschedule", "add_service"])
def test_cancelled_request_rejects_non_retry_mutations(operation):
    request = (
        _create()
        .request.cancel(
            expected_revision=1,
            idempotency_key="cancel-1",
            occurred_at=CREATED_AT + timedelta(minutes=1),
        )
        .request
    )

    if operation == "reschedule":
        with pytest.raises(ServiceRequestTransitionError, match="cancelled"):
            request.reschedule(
                scheduled_start=START + timedelta(days=1),
                scheduled_end=END + timedelta(days=1),
                expected_revision=2,
                idempotency_key="reschedule-1",
                occurred_at=CREATED_AT + timedelta(minutes=2),
            )
    else:
        with pytest.raises(ServiceRequestTransitionError, match="cancelled"):
            request.add_service(
                service="Drain clearing",
                expected_revision=2,
                idempotency_key="add-1",
                occurred_at=CREATED_AT + timedelta(minutes=2),
            )


def test_conflicting_idempotency_reuse_is_rejected_before_state_or_revision_checks():
    request = _create().request

    with pytest.raises(ServiceRequestIdempotencyConflict):
        request.add_service(
            service="Drain clearing",
            expected_revision=1,
            idempotency_key="create-1",
            occurred_at=CREATED_AT,
        )


def test_retry_after_later_mutation_keeps_current_state_and_returns_original_outcome():
    created = _create().request
    added = created.add_service(
        service="Drain clearing",
        expected_revision=1,
        idempotency_key="add-1",
        occurred_at=CREATED_AT + timedelta(minutes=1),
    )
    rescheduled = added.request.reschedule(
        scheduled_start=START + timedelta(days=1),
        scheduled_end=END + timedelta(days=1),
        expected_revision=2,
        idempotency_key="reschedule-1",
        occurred_at=CREATED_AT + timedelta(minutes=2),
    )

    retry = rescheduled.request.add_service(
        service="Drain clearing",
        expected_revision=1,
        idempotency_key="add-1",
        occurred_at=CREATED_AT + timedelta(hours=1),
    )

    assert retry.request is rescheduled.request
    assert retry.request.revision == 3
    assert retry.outcome == added.outcome
    assert retry.outcome.revision == 2


def test_serialization_is_canonical_and_round_trips_all_idempotency_results():
    request = (
        _create()
        .request.add_service(
            service="Drain clearing",
            expected_revision=1,
            idempotency_key="add-1",
            occurred_at=CREATED_AT + timedelta(microseconds=1),
        )
        .request
    )
    serialized = request.to_json()

    restored = ServiceRequest.from_json(serialized)

    assert restored == request
    assert restored.to_dict() == request.to_dict()
    assert restored.to_json() == serialized
    assert '"scheduled_start":"2026-08-13T13:00:00.000000Z"' in serialized

    reordered = request.to_dict()
    reordered["idempotency_records"].reverse()
    assert ServiceRequest.from_dict(reordered).to_json() == serialized


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(schema_version=2),
        lambda data: data.update(unexpected=True),
        lambda data: data.update(status="cancelled"),
        lambda data: data["idempotency_records"].__setitem__(
            0,
            {
                **data["idempotency_records"][0],
                "command_fingerprint": "not-a-digest",
            },
        ),
    ],
)
def test_deserialization_rejects_unknown_versions_schema_and_tampered_history(mutation):
    data = _create().request.to_dict()
    mutation(data)

    with pytest.raises(ServiceRequestValidationError):
        ServiceRequest.from_dict(data)
