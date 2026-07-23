"""Adversarial tests for the offline provider-neutral lifecycle contract."""

import pytest

from app.services.voice_lifecycle import (
    ConfirmationPolicy,
    VoiceCapability,
    VoiceCommand,
    VoiceCommandKind,
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSensitivity,
    VoiceSessionBinding,
    VoiceSource,
)


def _binding(epoch: int = 1, **changes: object) -> VoiceSessionBinding:
    values = {"environment": "bakeoff", "contractor_binding": "tenant_a", "call_binding": "call_a", "stream_binding": "stream_a", "epoch": epoch}
    values.update(changes)
    return VoiceSessionBinding(**values)


def _event(kind: VoiceEventKind, sequence: int, **changes: object) -> VoiceEvent:
    values = {"schema_version": 1, "kind": kind, "source": VoiceSource.LOCAL_AUTHORITATIVE, "sensitivity": VoiceSensitivity.OPERATIONAL, "binding": _binding(), "sequence": sequence, "at_ms": sequence * 10, "input_turn_id": "turn_1", "generation_id": "generation_1", "semantic_act_id": "act_1", "semantic_act_kind": VoiceSemanticActKind.QUESTION, "payload": VoicePayload()}
    values.update(changes)
    return VoiceEvent(**values)


def _command(**changes: object) -> VoiceCommand:
    values = {"schema_version": 1, "kind": VoiceCommandKind.ARM_SILENCE_TIMER, "binding": _binding(), "action_id": "action_1", "idempotency_key": "idem_1", "expires_at_ms": 100, "capability": VoiceCapability.SILENCE_TIMER, "sensitivity": VoiceSensitivity.OPERATIONAL, "confirmation": ConfirmationPolicy.CALLER_PLAYBACK_OR_INFERENCE, "semantic_act_id": "act_1", "arguments": (("timeout_ms", 10),)}
    values.update(changes)
    return VoiceCommand(**values)


def test_event_parser_requires_every_closed_field_and_rejects_raw_payload():
    with pytest.raises(ValueError, match="unknown voice event field"):
        VoiceEvent.from_dict({"schema_version": 1, "transcript": "caller words"})
    with pytest.raises(ValueError, match="missing voice event field"):
        VoiceEvent.from_dict({"schema_version": 1})


def test_event_parser_round_trips_closed_nested_schemas():
    event = VoiceEvent.from_dict(
        {"schema_version": 1, "kind": "response_authorized", "source": "local_authoritative", "sensitivity": "operational", "binding": {"environment": "bakeoff", "contractor_binding": "tenant_a", "call_binding": "call_a", "stream_binding": "stream_a", "epoch": 1}, "sequence": 1, "at_ms": 10, "input_turn_id": "turn_1", "generation_id": "generation_1", "semantic_act_id": "act_1", "semantic_act_kind": "question", "payload": {"ordinal": 1}}
    )
    assert event.binding == _binding()
    assert event.payload.ordinal == 1


def test_lifecycle_rejects_cross_binding_stale_epoch_and_time_regression():
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert not lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 2, binding=_binding(call_binding="call_b")))
    assert not lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 3, binding=_binding(epoch=2)))
    assert not lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 0))
    assert lifecycle.rejected_event_count == 3


def test_transport_cannot_commit_question_but_caller_playback_can():
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 3, source=VoiceSource.TWILIO_AUTHENTICATED))
    assert not lifecycle.pending_question_active
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 4))
    assert lifecycle.pending_question_active


def test_act_order_is_per_act_and_question_only():
    lifecycle = VoiceLifecycle(binding=_binding())
    assert not lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 2, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 3, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 4, source=VoiceSource.TWILIO_AUTHENTICATED, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER))
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 5, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER))
    assert not lifecycle.pending_question_active


def test_command_rejects_wrong_binding_expiry_and_idempotency_collision():
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 3, source=VoiceSource.TWILIO_AUTHENTICATED))
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 4))
    assert lifecycle.accept_command(_command(), now_ms=10)
    assert not lifecycle.accept_command(_command(binding=_binding(stream_binding="stream_b")), now_ms=10)
    assert not lifecycle.accept_command(_command(expires_at_ms=9), now_ms=10)
    assert lifecycle.accept_command(_command(), now_ms=10)
    with pytest.raises(ValueError, match="capability mismatch"):
        _command(capability=VoiceCapability.TERMINAL)
    assert lifecycle.idempotent_command_count == 1
