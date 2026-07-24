"""Deterministic tests for the revision-bound bakeoff call lifecycle."""

from app.services.voice_call_lifecycle import CallIntentKind, CallLifecycle, PlaybackEvidence, QuestionIntent, SilencePhase
from app.services.voice_lifecycle import VoiceEvent, VoiceEventKind, VoiceLifecycle, VoicePayload, VoiceSemanticActKind, VoiceSensitivity, VoiceSessionBinding, VoiceSource


def _binding(epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", epoch)


def _lifecycle() -> CallLifecycle:
    return CallLifecycle(binding=_binding(), voice_lifecycle=VoiceLifecycle(binding=_binding()), first_silence_ms=10, second_silence_ms=20)


def _receipt(lifecycle: VoiceLifecycle, act_id: str, kind: VoiceSemanticActKind, at_ms: int, *, playback: bool = True) -> VoiceEvent:
    values = {"schema_version": 1, "source": VoiceSource.LOCAL_AUTHORITATIVE, "sensitivity": VoiceSensitivity.OPERATIONAL, "binding": _binding(), "input_turn_id": "turn_1", "generation_id": "generation_1", "semantic_act_id": act_id, "semantic_act_kind": kind}
    start = getattr(lifecycle, "_test_sequence", 1)
    def event(kind, offset, payload=VoicePayload()):
        source = VoiceSource.TWILIO_AUTHENTICATED if kind is VoiceEventKind.TRANSPORT_RESOLVED else VoiceSource.LOCAL_AUTHORITATIVE
        return VoiceEvent(kind=kind, sequence=start + offset, at_ms=at_ms, payload=payload, source=source, **{key: value for key, value in values.items() if key != "source"})
    digest = "a" * 64
    payload = VoicePayload(text_digest=digest, audio_id="audio_1", playout_id="playout_1")
    prior = lifecycle._acts.get(act_id)
    offset = 0
    if prior is None:
        assert lifecycle.ingest(event(VoiceEventKind.RESPONSE_AUTHORIZED, 0))
        assert lifecycle.ingest(event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 1))
        offset = 2
    else:
        assert prior[0] is VoiceEventKind.SEMANTIC_ACT_CONFIRMED
    assert lifecycle.ingest(event(VoiceEventKind.TTS_BOUND, offset, VoicePayload(text_digest=digest, audio_id="audio_1")))
    assert lifecycle.ingest(event(VoiceEventKind.PLAYOUT_BOUND, offset + 1, payload))
    receipt = event(VoiceEventKind.TRANSPORT_RESOLVED, offset + 2, payload)
    assert lifecycle.ingest(receipt)
    lifecycle._test_sequence = start + offset + 3
    if not playback:
        return receipt
    receipt = event(VoiceEventKind.CALLER_PLAYBACK_OBSERVED, offset + 3, payload)
    assert lifecycle.ingest(receipt)
    lifecycle._test_sequence = start + offset + 4
    return receipt


def _observed(call: CallLifecycle, *, event_id: str, sequence: int, act_id: str, kind: VoiceSemanticActKind, at_ms: int):
    receipt = _receipt(call.voice_lifecycle, act_id, kind, at_ms)
    return call.observed_playback(event_id=event_id, sequence=sequence, event=receipt)


def _start(lifecycle: CallLifecycle):
    question = QuestionIntent(slot="service", turn_id="turn_1", act_id="question_1")
    assert lifecycle.reserve_question(binding=_binding(), event_id="reserve_1", sequence=1, at_ms=0, question=question)
    response = _confirmation_event(VoiceEventKind.RESPONSE_AUTHORIZED, 1)
    confirmation = _confirmation_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 2)
    assert lifecycle.voice_lifecycle.ingest(response)
    assert lifecycle.voice_lifecycle.ingest(confirmation)
    lifecycle.voice_lifecycle._test_sequence = 3
    assert lifecycle.semantic_confirmed(event_id="confirm_1", sequence=2, event=confirmation)


def _confirmation_event(kind: VoiceEventKind, sequence: int, **changes: object) -> VoiceEvent:
    values = {
        "schema_version": 1,
        "kind": kind,
        "source": VoiceSource.LOCAL_AUTHORITATIVE,
        "sensitivity": VoiceSensitivity.OPERATIONAL,
        "binding": _binding(),
        "sequence": sequence,
        "at_ms": sequence,
        "input_turn_id": "turn_1",
        "generation_id": "generation_1",
        "semantic_act_id": "question_1",
        "semantic_act_kind": VoiceSemanticActKind.QUESTION,
        "payload": VoicePayload(),
    }
    values.update(changes)
    return VoiceEvent(**values)


def test_transport_is_not_an_activation_api_but_observed_playback_is():
    lifecycle = _lifecycle()
    _start(lifecycle)
    assert lifecycle.phase is SilencePhase.QUESTION_CONFIRMED
    assert lifecycle.playback(binding=_binding(), event_id="forged_1", sequence=3, act_id="question_1", evidence=PlaybackEvidence.CALLER_PLAYBACK_OBSERVED, at_ms=2) == ()
    arm = _observed(lifecycle, event_id="playback_1", sequence=3, act_id="question_1", kind=VoiceSemanticActKind.QUESTION, at_ms=2)
    assert arm[0].kind is CallIntentKind.ARM_TIMER and arm[0].deadline_ms == 12


def test_question_reservation_rejects_invalid_intent_without_consuming_state():
    lifecycle = _lifecycle()
    assert not lifecycle.reserve_question(
        binding=_binding(),
        event_id="invalid_question",
        sequence=100,
        at_ms=100,
        question=object(),
    )
    assert lifecycle.phase is SilencePhase.IDLE
    assert lifecycle.reserve_question(
        binding=_binding(),
        event_id="valid_question",
        sequence=1,
        at_ms=0,
        question=QuestionIntent("service", "turn_1", "question_1"),
    )


def test_presence_closing_and_terminal_each_require_their_own_playback_evidence():
    lifecycle = _lifecycle()
    _start(lifecycle)
    transport = _receipt(lifecycle.voice_lifecycle, "question_1", VoiceSemanticActKind.QUESTION, 2, playback=False)
    assert lifecycle.transport_resolved(event_id="transport_1", sequence=3, event=transport)
    arm = lifecycle.playback(binding=_binding(), event_id="playback_1", sequence=4, act_id="question_1", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_1", transport_id="playout_1", at_ms=3)[0]
    presence = lifecycle.timer_fired(binding=_binding(), event_id="timer_1", sequence=5, action_id=arm.action_id, revision=arm.revision, now_ms=13)[0]
    assert presence.kind is CallIntentKind.REQUEST_PRESENCE_CHECK
    second = _observed(lifecycle, event_id="playback_2", sequence=6, act_id=presence.act_id, kind=VoiceSemanticActKind.PRESENCE_CHECK, at_ms=14)[0]
    closing = lifecycle.timer_fired(binding=_binding(), event_id="timer_2", sequence=7, action_id=second.action_id, revision=second.revision, now_ms=34)[0]
    assert closing.kind is CallIntentKind.REQUEST_CLOSING
    terminal = _observed(lifecycle, event_id="playback_3", sequence=8, act_id=closing.act_id, kind=VoiceSemanticActKind.CLOSING, at_ms=35)
    assert terminal[0].kind is CallIntentKind.TERMINAL_ELIGIBLE


def test_activity_invalidates_timer_presence_closing_and_terminal_intents():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(lifecycle, event_id="playback_1", sequence=3, act_id="question_1", kind=VoiceSemanticActKind.QUESTION, at_ms=2)[0]
    cancelled = lifecycle.cancel(binding=_binding(), event_id="activity_1", sequence=4, at_ms=3)
    assert {intent.kind for intent in cancelled} == {CallIntentKind.CANCEL_TIMER, CallIntentKind.CANCEL_ACT}
    assert lifecycle.phase is SilencePhase.IDLE
    assert lifecycle.timer_fired(binding=_binding(), event_id="timer_1", sequence=5, action_id=arm.action_id, revision=arm.revision, now_ms=100) == ()


def test_inferred_playback_requires_matching_transport_and_conservative_deadline():
    lifecycle = _lifecycle()
    _start(lifecycle)
    assert lifecycle.playback(binding=_binding(), event_id="infer_early", sequence=3, act_id="question_1", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_1", at_ms=2) == ()
    transport = _receipt(lifecycle.voice_lifecycle, "question_1", VoiceSemanticActKind.QUESTION, 3, playback=False)
    assert lifecycle.transport_resolved(event_id="transport_1", sequence=4, event=transport)
    assert lifecycle.playback(binding=_binding(), event_id="infer_too_soon", sequence=5, act_id="question_1", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_2", transport_id="playout_1", at_ms=3) == ()
    assert lifecycle.playback(binding=_binding(), event_id="infer_ok", sequence=6, act_id="question_1", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_3", transport_id="playout_1", at_ms=4)[0].kind is CallIntentKind.ARM_TIMER


def test_invalid_inferred_event_does_not_consume_ordering_state():
    lifecycle = _lifecycle()
    _start(lifecycle)
    transport = _receipt(lifecycle.voice_lifecycle, "question_1", VoiceSemanticActKind.QUESTION, 2, playback=False)
    assert lifecycle.transport_resolved(event_id="transport_1", sequence=3, event=transport)
    assert lifecycle.playback(binding=_binding(), event_id="forged_100", sequence=100, act_id="question_1", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_bad", transport_id="wrong", at_ms=3) == ()
    assert lifecycle.playback(binding=_binding(), event_id="infer_ok", sequence=4, act_id="question_1", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_ok", transport_id="playout_1", at_ms=3)[0].kind is CallIntentKind.ARM_TIMER


def test_invalid_high_sequence_timer_does_not_block_the_active_timer():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(lifecycle, event_id="playback_1", sequence=3, act_id="question_1", kind=VoiceSemanticActKind.QUESTION, at_ms=2)[0]
    assert lifecycle.timer_fired(binding=_binding(), event_id="forged_timer", sequence=100, action_id="wrong", revision=arm.revision, now_ms=12) == ()
    assert lifecycle.timer_fired(binding=_binding(), event_id="timer_ok", sequence=4, action_id=arm.action_id, revision=arm.revision, now_ms=12)[0].kind is CallIntentKind.REQUEST_PRESENCE_CHECK


def test_activity_returns_to_ready_state_for_a_later_question():
    lifecycle = _lifecycle()
    _start(lifecycle)
    _observed(lifecycle, event_id="playback_1", sequence=3, act_id="question_1", kind=VoiceSemanticActKind.QUESTION, at_ms=2)
    lifecycle.cancel(binding=_binding(), event_id="activity_1", sequence=4, at_ms=3)
    assert lifecycle.reserve_question(binding=_binding(), event_id="reserve_2", sequence=5, at_ms=4, question=QuestionIntent("service", "turn_1", "question_2"))
    response = _confirmation_event(VoiceEventKind.RESPONSE_AUTHORIZED, 100, semantic_act_id="question_2", at_ms=4)
    confirmation = _confirmation_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 101, semantic_act_id="question_2", at_ms=5)
    assert lifecycle.voice_lifecycle.ingest(response)
    assert lifecycle.voice_lifecycle.ingest(confirmation)
    lifecycle.voice_lifecycle._test_sequence = 102
    assert lifecycle.semantic_confirmed(event_id="confirm_2", sequence=6, event=confirmation)
    assert _observed(lifecycle, event_id="playback_2", sequence=7, act_id="question_2", kind=VoiceSemanticActKind.QUESTION, at_ms=6)[0].kind is CallIntentKind.ARM_TIMER


def test_transport_evidence_expires_on_cancellation_and_cannot_cross_acts():
    lifecycle = _lifecycle()
    _start(lifecycle)
    transport = _receipt(lifecycle.voice_lifecycle, "question_1", VoiceSemanticActKind.QUESTION, 2, playback=False)
    assert lifecycle.transport_resolved(event_id="transport_1", sequence=3, event=transport)
    lifecycle.cancel(binding=_binding(), event_id="activity_1", sequence=4, at_ms=3)
    assert lifecycle.reserve_question(binding=_binding(), event_id="reserve_2", sequence=5, at_ms=4, question=QuestionIntent("service", "turn_1", "question_2"))
    response = _confirmation_event(VoiceEventKind.RESPONSE_AUTHORIZED, 100, semantic_act_id="question_2", at_ms=4)
    confirmation = _confirmation_event(VoiceEventKind.SEMANTIC_ACT_CONFIRMED, 101, semantic_act_id="question_2", at_ms=5)
    assert lifecycle.voice_lifecycle.ingest(response)
    assert lifecycle.voice_lifecycle.ingest(confirmation)
    assert lifecycle.semantic_confirmed(event_id="confirm_2", sequence=6, event=confirmation)
    assert lifecycle.playback(binding=_binding(), event_id="infer_stale", sequence=7, act_id="question_2", evidence=PlaybackEvidence.PLAYBACK_INFERRED, inference_id="infer_1", transport_id="playout_1", at_ms=10) == ()
