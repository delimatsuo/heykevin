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


def _playout_chain(lifecycle: VoiceLifecycle, *, first_sequence: int = 1) -> int:
    digest = "a" * 64
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, first_sequence))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, first_sequence + 1))
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, first_sequence + 2, payload=VoicePayload(text_digest=digest, audio_id="audio_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, first_sequence + 3, payload=VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, first_sequence + 4, source=VoiceSource.TWILIO_AUTHENTICATED, payload=VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1")))
    return first_sequence + 5


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
    sequence = _playout_chain(lifecycle)
    assert not lifecycle.pending_question_active
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, sequence, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")))
    assert lifecycle.pending_question_active


def test_act_order_is_per_act_and_question_only():
    lifecycle = VoiceLifecycle(binding=_binding())
    assert not lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 2, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 3, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER))
    payload = VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 4, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 5, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER, payload=payload))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 6, source=VoiceSource.TWILIO_AUTHENTICATED, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER, payload=payload))
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 7, semantic_act_id="answer_1", semantic_act_kind=VoiceSemanticActKind.ANSWER, payload=payload))
    assert not lifecycle.pending_question_active


def test_semantic_confirmation_verifier_rejects_raw_and_noncanonical_events():
    lifecycle = VoiceLifecycle(binding=_binding())
    response = _event(VoiceEventKind.RESPONSE_AUTHORIZED, 1)
    confirmation = _event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2)
    assert not lifecycle.accepts_semantic_confirmation(confirmation)
    assert lifecycle.ingest(response)
    assert lifecycle.ingest(confirmation)
    assert lifecycle.accepts_semantic_confirmation(confirmation)
    assert not lifecycle.accepts_semantic_confirmation(
        _event(
            VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
            3,
            at_ms=30,
        )
    )


def test_command_rejects_wrong_binding_expiry_and_idempotency_collision():
    lifecycle = VoiceLifecycle(binding=_binding())
    sequence = _playout_chain(lifecycle)
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, sequence, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")))
    assert lifecycle.accept_command(_command(), now_ms=10)
    assert not lifecycle.accept_command(_command(binding=_binding(stream_binding="stream_b")), now_ms=10)
    assert not lifecycle.accept_command(_command(expires_at_ms=9), now_ms=10)
    assert lifecycle.accept_command(_command(), now_ms=10)
    with pytest.raises(ValueError, match="capability mismatch"):
        _command(capability=VoiceCapability.TERMINAL)
    assert lifecycle.idempotent_command_count == 1


def test_playout_contract_binds_text_audio_and_playout_before_terminal_fact():
    lifecycle = VoiceLifecycle(binding=_binding())
    digest = "a" * 64
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(
        _event(
            VoiceEventKind.TTS_BOUND,
            3,
            payload=VoicePayload(text_digest=digest, audio_id="audio_1"),
        )
    )
    assert lifecycle.ingest(
        _event(
            VoiceEventKind.PLAYOUT_BOUND,
            4,
            payload=VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1"),
        )
    )
    assert lifecycle.ingest(
        _event(
            VoiceEventKind.PLAYOUT_INTERRUPTED,
            5,
            source=VoiceSource.TWILIO_AUTHENTICATED,
            payload=VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1"),
        )
    )
    assert not lifecycle.ingest(
        _event(
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
            6,
            payload=VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1"),
        )
    )


def test_playout_rejects_mismatched_audio_identity_and_accepts_caller_observation():
    lifecycle = VoiceLifecycle(binding=_binding())
    digest = "a" * 64
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 3, payload=VoicePayload(text_digest=digest, audio_id="audio_1")))
    assert not lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 4, payload=VoicePayload(text_digest=digest, audio_id="audio_2", playout_id="playout_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 5, payload=VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1")))
    payload = VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1")
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 6, source=VoiceSource.TWILIO_AUTHENTICATED, payload=payload))
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 7, payload=payload))


def test_caller_observation_rejects_semantic_and_playout_shortcuts():
    lifecycle = VoiceLifecycle(binding=_binding())
    payload = VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert not lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 3, source=VoiceSource.TWILIO_AUTHENTICATED, payload=payload))
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 4, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 5, payload=payload))
    assert not lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 6, payload=payload))


@pytest.mark.parametrize("terminal", [VoiceEventKind.PLAYOUT_PARTIAL, VoiceEventKind.PLAYOUT_CLEARED])
def test_partial_and_cleared_playout_are_terminal_without_caller_observation(terminal: VoiceEventKind):
    lifecycle = VoiceLifecycle(binding=_binding())
    payload = VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 3, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 4, payload=payload))
    assert lifecycle.ingest(_event(terminal, 5, source=VoiceSource.TWILIO_AUTHENTICATED, payload=payload))
    assert not lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 6, payload=payload))


def test_reconnect_requires_a_new_playout_binding_before_transport_resolution():
    lifecycle = VoiceLifecycle(binding=_binding())
    first = VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")
    second = VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_2")
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 3, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 4, payload=first))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_RECONNECTED, 5, payload=first))
    assert not lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 6, source=VoiceSource.TWILIO_AUTHENTICATED, payload=first))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 7, payload=second))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 8, source=VoiceSource.TWILIO_AUTHENTICATED, payload=second))


def test_repair_has_its_own_lifecycle_bound_playout_chain():
    lifecycle = VoiceLifecycle(binding=_binding())
    payload = VoicePayload(text_digest="a" * 64, audio_id="audio_repair", playout_id="playout_repair")
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1, semantic_act_id="repair_1", semantic_act_kind=VoiceSemanticActKind.REPAIR))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2, semantic_act_id="repair_1", semantic_act_kind=VoiceSemanticActKind.REPAIR))
    assert lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 3, semantic_act_id="repair_1", semantic_act_kind=VoiceSemanticActKind.REPAIR, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_repair")))
    assert lifecycle.ingest(_event(VoiceEventKind.PLAYOUT_BOUND, 4, semantic_act_id="repair_1", semantic_act_kind=VoiceSemanticActKind.REPAIR, payload=payload))
    assert lifecycle.ingest(_event(VoiceEventKind.TRANSPORT_RESOLVED, 5, source=VoiceSource.TWILIO_AUTHENTICATED, semantic_act_id="repair_1", semantic_act_kind=VoiceSemanticActKind.REPAIR, payload=payload))
    assert lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 6, semantic_act_id="repair_1", semantic_act_kind=VoiceSemanticActKind.REPAIR, payload=payload))


@pytest.mark.parametrize("failure", [VoiceEventKind.ACT_FAILED, VoiceEventKind.ACT_TIMED_OUT])
def test_failed_or_timed_out_act_is_terminal_before_playout(failure: VoiceEventKind):
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2))
    assert lifecycle.ingest(_event(failure, 3))
    assert not lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 4, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1")))


@pytest.mark.parametrize("failure", [VoiceEventKind.ACT_FAILED, VoiceEventKind.ACT_TIMED_OUT])
def test_preconfirmation_failure_is_terminal_and_cannot_progress_to_playout(failure: VoiceEventKind):
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1))
    assert lifecycle.ingest(_event(failure, 2))
    assert not lifecycle.ingest(_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 3))
    assert not lifecycle.ingest(_event(VoiceEventKind.TTS_BOUND, 4, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1")))
    assert not lifecycle.ingest(_event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, 5, payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1", playout_id="playout_1")))
