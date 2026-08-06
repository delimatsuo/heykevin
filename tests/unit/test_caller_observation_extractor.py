"""Offline tests for candidate-final, provider-neutral observation extraction."""

from app.services.caller_observation_extractor import (
    BackendOutcome,
    BackendResponse,
    CandidateFinalTurn,
    ExtractionOutcome,
    Finality,
    ObservationExtractor,
)
from app.services.voice_lifecycle import VoiceSessionBinding


_DIGEST = "a" * 64


def _binding(epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", epoch)


def _turn(**changes: object) -> CandidateFinalTurn:
    values = {
        "binding": _binding(),
        "input_turn_id": "turn_1",
        "sequence": 1,
        "at_ms": 10,
        "finality": Finality.FINAL,
        "content": "synthetic caller content",
    }
    values.update(changes)
    return CandidateFinalTurn(**values)


def _backend(*, fields: dict[str, object] | None = None, confidences: dict[str, float] | None = None, outcome: BackendOutcome = BackendOutcome.OK, request_id: str | None = None, configuration_digest: str = _DIGEST):
    def extract(request):
        return BackendResponse(
            request_id=request.request_id if request_id is None else request_id,
            configuration_digest=configuration_digest,
            outcome=outcome,
            fields=fields or {"intent": "service_request", "service_action": "repair"},
            confidences=confidences or {"intent": 0.9, "service_action": 0.9},
        )

    return extract


def _extractor() -> ObservationExtractor:
    return ObservationExtractor(binding=_binding(), configuration_digest=_DIGEST, min_field_confidence=0.8, min_aggregate_confidence=0.85)


def test_accepts_final_closed_high_confidence_observation_without_state_mutation():
    result = _extractor().extract(_turn(), backend=_backend())
    assert result.outcome is ExtractionOutcome.ACCEPTED
    assert result.observation is not None
    assert result.observation.intent.value == "service_request"
    assert result.observation.service_action.value == "repair"
    assert result.reason is None


def test_rejects_nonfinal_extra_private_or_malformed_fields_without_observation():
    extractor = _extractor()
    assert extractor.extract(_turn(finality=Finality.PARTIAL), backend=_backend()).outcome is ExtractionOutcome.NOT_FINAL
    for sequence, fields in enumerate((
        {"intent": "service_request", "tenant_id": "forbidden"},
        {"intent": "not_an_intent"},
        {"callback_intent": "declined", "callback_confirmation": "confirmed"},
    ), start=2):
        result = extractor.extract(_turn(sequence=sequence), backend=_backend(fields=fields, confidences={key: 0.9 for key in fields}))
        assert result.outcome is ExtractionOutcome.MALFORMED
        assert result.observation is None
        assert result.reason is not None


def test_low_confidence_timeout_error_cancellation_and_late_results_are_payload_free():
    extractor = _extractor()
    low = extractor.extract(_turn(), backend=_backend(confidences={"intent": 0.7, "service_action": 0.9}))
    assert low.outcome is ExtractionOutcome.LOW_CONFIDENCE and low.observation is None
    assert extractor.extract(_turn(sequence=2), backend=_backend(outcome=BackendOutcome.TIMEOUT)).outcome is ExtractionOutcome.TIMEOUT
    assert extractor.extract(_turn(sequence=3), backend=_backend(outcome=BackendOutcome.ERROR)).outcome is ExtractionOutcome.PROVIDER_ERROR
    assert extractor.extract(_turn(sequence=4, cancelled=True), backend=_backend()).outcome is ExtractionOutcome.CANCELLED
    assert extractor.extract(_turn(sequence=5), backend=_backend(request_id="other_request")).outcome is ExtractionOutcome.LATE


def test_admission_is_once_only_even_if_a_later_backend_response_is_high_confidence():
    extractor = _extractor()
    turn = _turn()
    assert extractor.extract(turn, backend=_backend(confidences={"intent": 0.7, "service_action": 0.9})).outcome is ExtractionOutcome.LOW_CONFIDENCE
    assert extractor.extract(turn, backend=_backend()).outcome is ExtractionOutcome.LATE


def test_current_turn_guard_rejects_post_backend_cancellation_and_invalid_outcomes():
    extractor = _extractor()
    active = {"value": True}
    def cancel_during_backend(request):
        active["value"] = False
        return _backend()(request)
    assert extractor.extract(_turn(), backend=cancel_during_backend, current_turn=lambda turn: active["value"]).outcome is ExtractionOutcome.LATE

    invalid = _extractor().extract(
        _turn(),
        backend=lambda request: BackendResponse(request.request_id, _DIGEST, "unknown", {"intent": "service_request"}, {"intent": 0.9}),
    )
    assert invalid.outcome is ExtractionOutcome.MALFORMED


def test_accepts_correction_shaped_observation_but_leaves_supersession_to_coordinator():
    extractor = _extractor()
    first = extractor.extract(_turn(), backend=_backend(fields={"service_action": "repair"}, confidences={"service_action": 0.9}))
    corrected = extractor.extract(
        _turn(sequence=2, input_turn_id="turn_2"),
        backend=_backend(fields={"service_action": "replace"}, confidences={"service_action": 0.9}),
    )
    assert first.outcome is corrected.outcome is ExtractionOutcome.ACCEPTED
    assert first.observation.service_action.value == "repair"
    assert corrected.observation.service_action.value == "replace"


def test_rejects_stale_sequence_cross_binding_and_configuration_divergence():
    extractor = _extractor()
    assert extractor.extract(_turn(), backend=_backend()).outcome is ExtractionOutcome.ACCEPTED
    assert extractor.extract(_turn(), backend=_backend()).outcome is ExtractionOutcome.LATE
    assert extractor.extract(_turn(binding=_binding(epoch=2), sequence=2), backend=_backend()).outcome is ExtractionOutcome.LATE
    assert extractor.extract(_turn(sequence=2), backend=_backend(configuration_digest="b" * 64)).outcome is ExtractionOutcome.LATE
