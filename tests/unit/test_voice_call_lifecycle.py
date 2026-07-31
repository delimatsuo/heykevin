"""Deterministic tests for the revision-bound bakeoff call lifecycle."""

from dataclasses import replace

from app.services.voice_call_lifecycle import (
    CallIntentKind,
    CallLifecycle,
    PlaybackEvidence,
    QuestionIntent,
    SilencePhase,
)
from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSensitivity,
    VoiceSessionBinding,
    VoiceSource,
)


def _binding(epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", epoch)


def _lifecycle() -> CallLifecycle:
    return CallLifecycle(binding=_binding(), voice_lifecycle=VoiceLifecycle(binding=_binding()), first_silence_ms=10, second_silence_ms=20)


def _receipt(lifecycle: VoiceLifecycle, act_id: str, kind: VoiceSemanticActKind, at_ms: int, *, playback: bool = True) -> VoiceEvent:
    values = {"schema_version": 1, "source": VoiceSource.LOCAL_AUTHORITATIVE, "sensitivity": VoiceSensitivity.OPERATIONAL, "binding": _binding(), "input_turn_id": "turn_1", "generation_id": "generation_1", "semantic_act_id": act_id, "semantic_act_kind": kind}
    start = getattr(lifecycle, "_test_sequence", 1)
    def event(kind, offset, payload=None):
        source = VoiceSource.TWILIO_AUTHENTICATED if kind is VoiceEventKind.TRANSPORT_RESOLVED else VoiceSource.LOCAL_AUTHORITATIVE
        selected_payload = (
            VoicePayload() if payload is None else payload
        )
        return VoiceEvent(kind=kind, sequence=start + offset, at_ms=at_ms, payload=selected_payload, source=source, **{key: value for key, value in values.items() if key != "source"})
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
    if kind is not VoiceSemanticActKind.QUESTION and act_id not in call._materialized_act_ids:
        intent = next(
            (
                candidate
                for candidate in call._issued_intents.values()
                if candidate.act_id == act_id
            ),
            None,
        )
        assert intent is not None
        assert call.consume_intent(intent)
    receipt = _receipt(call.voice_lifecycle, act_id, kind, at_ms)
    return call.observed_playback(event_id=event_id, sequence=sequence, event=receipt)


def _start(lifecycle: CallLifecycle):
    question = QuestionIntent(
        slot="service",
        turn_id="turn_1",
        act_id="question_1",
        turn_sequence=0,
    )
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


def test_speech_at_exact_timer_boundary_wins_by_canonical_sequence():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(
        lifecycle,
        event_id="playback_1",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]
    assert arm.deadline_ms == 12

    cancelled = lifecycle.cancel(
        binding=_binding(),
        event_id="boundary_speech",
        sequence=4,
        at_ms=arm.deadline_ms,
    )
    assert {intent.kind for intent in cancelled} == {
        CallIntentKind.CANCEL_TIMER,
        CallIntentKind.CANCEL_ACT,
    }
    assert lifecycle.timer_fired(
        binding=_binding(),
        event_id="late_timer_callback",
        sequence=5,
        action_id=arm.action_id,
        revision=arm.revision,
        now_ms=arm.deadline_ms,
    ) == ()
    assert lifecycle.phase is SilencePhase.IDLE


def test_more_time_before_presence_is_once_and_uses_immutable_extension():
    lifecycle = _lifecycle()
    _start(lifecycle)
    original = _observed(
        lifecycle,
        event_id="playback_1",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]
    intents = lifecycle.request_more_time(
        binding=_binding(),
        event_id="more_time_1",
        sequence=4,
        at_ms=5,
    )
    assert tuple(intent.kind for intent in intents) == (
        CallIntentKind.CANCEL_TIMER,
        CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT,
    )
    acknowledgement = intents[1]
    assert lifecycle.consume_intent(acknowledgement)
    extension = _observed(
        lifecycle,
        event_id="more_time_playback",
        sequence=5,
        act_id=acknowledgement.act_id,
        kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
        at_ms=6,
    )[0]
    assert lifecycle.phase is SilencePhase.MORE_TIME_ARMED
    assert extension.deadline_ms == 20_006
    assert lifecycle.timer_fired(
        binding=_binding(),
        event_id="old_timer",
        sequence=6,
        action_id=original.action_id,
        revision=original.revision,
        now_ms=20_006,
    ) == ()
    immutable = (
        extension.action_id,
        extension.revision,
        extension.deadline_ms,
    )
    assert lifecycle.request_more_time(
        binding=_binding(),
        event_id="more_time_2",
        sequence=6,
        at_ms=7,
    ) == ()
    assert lifecycle.phase is SilencePhase.MORE_TIME_ARMED
    assert (
        lifecycle._timer_action_id(),
        lifecycle.revision,
        lifecycle._deadline_ms,
    ) == immutable
    presence = lifecycle.timer_fired(
        binding=_binding(),
        event_id="extended_timer",
        sequence=7,
        action_id=extension.action_id,
        revision=extension.revision,
        now_ms=extension.deadline_ms,
    )[0]
    assert presence.kind is CallIntentKind.REQUEST_PRESENCE_CHECK
    second = _observed(
        lifecycle,
        event_id="presence_playback",
        sequence=8,
        act_id=presence.act_id,
        kind=VoiceSemanticActKind.PRESENCE_CHECK,
        at_ms=20_007,
    )[0]
    assert second.deadline_ms == 20_027


def test_more_time_after_presence_expires_to_closing_without_repeating_presence():
    lifecycle = _lifecycle()
    _start(lifecycle)
    first = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]
    presence = lifecycle.timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    second = _observed(
        lifecycle,
        event_id="presence_playback",
        sequence=5,
        act_id=presence.act_id,
        kind=VoiceSemanticActKind.PRESENCE_CHECK,
        at_ms=13,
    )[0]
    assert lifecycle.phase is SilencePhase.SECOND_ARMED
    assert second.deadline_ms == 33
    intents = lifecycle.request_more_time(
        binding=_binding(),
        event_id="more_time_after_presence",
        sequence=6,
        at_ms=14,
    )
    acknowledgement = intents[1]
    assert lifecycle.consume_intent(acknowledgement)
    extension = _observed(
        lifecycle,
        event_id="more_time_playback",
        sequence=7,
        act_id=acknowledgement.act_id,
        kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
        at_ms=15,
    )[0]
    closing = lifecycle.timer_fired(
        binding=_binding(),
        event_id="extended_timer",
        sequence=8,
        action_id=extension.action_id,
        revision=extension.revision,
        now_ms=extension.deadline_ms,
    )[0]
    assert closing.kind is CallIntentKind.REQUEST_CLOSING


def test_lifecycle_act_intent_rejects_equal_clone_and_is_single_use():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]
    presence = lifecycle.timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=arm.action_id,
        revision=arm.revision,
        now_ms=arm.deadline_ms,
    )[0]
    assert not lifecycle.consume_intent(replace(presence))
    assert not lifecycle.consume_intent(presence)


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
    assert (
        lifecycle.playback(
            binding=_binding(),
            event_id="infer_stale",
            sequence=7,
            act_id="question_2",
            evidence=PlaybackEvidence.PLAYBACK_INFERRED,
            inference_id="infer_1",
            transport_id="playout_1",
            at_ms=10,
        )
        == ()
    )


def test_activity_after_deadline_does_not_steal_timer_authority():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]

    assert (
        lifecycle.cancel(
            binding=_binding(),
            event_id="late_activity",
            sequence=4,
            at_ms=arm.deadline_ms + 1,
        )
        == ()
    )
    assert lifecycle.phase is SilencePhase.FIRST_ARMED
    presence = lifecycle.timer_fired(
        binding=_binding(),
        event_id="timer_wins",
        sequence=4,
        action_id=arm.action_id,
        revision=arm.revision,
        now_ms=arm.deadline_ms,
    )
    assert len(presence) == 1
    assert presence[0].kind is CallIntentKind.REQUEST_PRESENCE_CHECK


def test_more_time_after_deadline_does_not_steal_timer_authority():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]

    assert (
        lifecycle.request_more_time(
            binding=_binding(),
            event_id="late_more_time",
            sequence=4,
            at_ms=arm.deadline_ms + 1,
        )
        == ()
    )
    assert lifecycle.phase is SilencePhase.FIRST_ARMED
    presence = lifecycle.timer_fired(
        binding=_binding(),
        event_id="timer_wins",
        sequence=4,
        action_id=arm.action_id,
        revision=arm.revision,
        now_ms=arm.deadline_ms,
    )
    assert len(presence) == 1
    assert presence[0].kind is CallIntentKind.REQUEST_PRESENCE_CHECK


def test_fixed_fallback_at_exact_boundary_is_nonterminal_and_single_use():
    for kind in (
        CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
    ):
        lifecycle = _lifecycle()
        _start(lifecycle)
        timer = _observed(
            lifecycle,
            event_id="question_playback",
            sequence=3,
            act_id="question_1",
            kind=VoiceSemanticActKind.QUESTION,
            at_ms=2,
        )[0]
        intents = lifecycle.request_fixed_fallback(
            kind=kind,
            binding=_binding(),
            event_id=f"fallback_{kind.value}",
            sequence=4,
            at_ms=timer.deadline_ms,
            turn_id="turn_1",
            turn_sequence=0,
        )
        assert tuple(intent.kind for intent in intents) == (
            CallIntentKind.CANCEL_TIMER,
            CallIntentKind.CANCEL_ACT,
            kind,
        )
        request = intents[2]
        assert lifecycle.request_fixed_fallback(
            kind=kind,
            binding=_binding(),
            event_id=f"fallback_{kind.value}",
            sequence=5,
            at_ms=timer.deadline_ms,
            turn_id="turn_1",
            turn_sequence=0,
        ) == ()
        assert _observed(
            lifecycle,
            event_id=f"fallback_playback_{kind.value}",
            sequence=5,
            act_id=request.act_id,
            kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
            at_ms=timer.deadline_ms + 1,
        ) == ()
        assert lifecycle.phase is SilencePhase.IDLE
        assert lifecycle.is_quiescent
        assert lifecycle.timer_fired(
            binding=_binding(),
            event_id=f"stale_timer_{kind.value}",
            sequence=6,
            action_id=timer.action_id,
            revision=timer.revision,
            now_ms=timer.deadline_ms,
        ) == ()


def test_fixed_fallback_supports_exact_inferred_playback_receipt():
    lifecycle = _lifecycle()
    _start(lifecycle)
    timer = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]
    request = lifecycle.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        binding=_binding(),
        event_id="fallback_request",
        sequence=4,
        at_ms=3,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    assert lifecycle.consume_intent(request)
    transport = _receipt(
        lifecycle.voice_lifecycle,
        request.act_id,
        VoiceSemanticActKind.ACKNOWLEDGEMENT,
        4,
        playback=False,
    )
    assert lifecycle.transport_resolved(
        event_id="fallback_transport",
        sequence=5,
        event=transport,
    )
    assert lifecycle.playback(
        binding=_binding(),
        event_id="fallback_inferred",
        sequence=6,
        act_id=request.act_id,
        evidence=PlaybackEvidence.PLAYBACK_INFERRED,
        inference_id="fallback_inference",
        transport_id="playout_1",
        at_ms=5,
    ) == ()
    assert lifecycle.phase is SilencePhase.IDLE
    assert lifecycle.is_quiescent
    assert lifecycle.timer_fired(
        binding=_binding(),
        event_id="stale_timer",
        sequence=7,
        action_id=timer.action_id,
        revision=timer.revision,
        now_ms=timer.deadline_ms,
    ) == ()


def test_fixed_fallback_bounds_generated_ids_for_maximum_question_id():
    lifecycle = _lifecycle()
    question_id = "q" * 128
    assert lifecycle.reserve_question(
        binding=_binding(),
        event_id="reserve_maximum_question",
        sequence=1,
        at_ms=0,
        question=QuestionIntent(
            "service",
            "turn_1",
            question_id,
            turn_sequence=0,
        ),
    )
    response = _confirmation_event(
        VoiceEventKind.RESPONSE_AUTHORIZED,
        1,
        semantic_act_id=question_id,
    )
    confirmation = _confirmation_event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        2,
        semantic_act_id=question_id,
    )
    assert lifecycle.voice_lifecycle.ingest(response)
    assert lifecycle.voice_lifecycle.ingest(confirmation)
    lifecycle.voice_lifecycle._test_sequence = 3
    assert lifecycle.semantic_confirmed(
        event_id="confirm_maximum_question",
        sequence=2,
        event=confirmation,
    )
    timer = _observed(
        lifecycle,
        event_id="playback_maximum_question",
        sequence=3,
        act_id=question_id,
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]

    intents = lifecycle.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        binding=_binding(),
        event_id="maximum_question_fallback",
        sequence=4,
        at_ms=timer.deadline_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )

    assert tuple(intent.kind for intent in intents) == (
        CallIntentKind.CANCEL_TIMER,
        CallIntentKind.CANCEL_ACT,
        CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
    )
    assert all(len(intent.action_id) <= 128 for intent in intents)


def test_invalid_or_late_fixed_fallback_preserves_timer_authority():
    lifecycle = _lifecycle()
    _start(lifecycle)
    timer = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]
    for index, invalid_at_ms in enumerate(
        (None, True, "12", object(), 12.0)
    ):
        assert lifecycle.request_fixed_fallback(
            kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
            binding=_binding(),
            event_id=f"invalid_fallback_{index}",
            sequence=4,
            at_ms=invalid_at_ms,
            turn_id="turn_1",
            turn_sequence=0,
        ) == ()
        assert lifecycle.timer_receipt() is timer
    for index, invalid_kind in enumerate(
        (
            CallIntentKind.REQUEST_SIMULATED_VOICEMAIL.value,
            [],
            {},
            object(),
        )
    ):
        assert lifecycle.request_fixed_fallback(
            kind=invalid_kind,
            binding=_binding(),
            event_id=f"invalid_fallback_kind_{index}",
            sequence=4,
            at_ms=timer.deadline_ms,
            turn_id="turn_1",
            turn_sequence=0,
        ) == ()
        assert lifecycle.phase is SilencePhase.FIRST_ARMED
        assert lifecycle.timer_receipt() is timer
        assert lifecycle.revision == timer.revision
        assert lifecycle._deadline_ms == timer.deadline_ms
    assert lifecycle.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_CLOSING,
        binding=_binding(),
        event_id="wrong_fallback_kind",
        sequence=4,
        at_ms=timer.deadline_ms,
        turn_id="turn_1",
        turn_sequence=0,
    ) == ()
    assert lifecycle.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="wrong_fallback_turn",
        sequence=4,
        at_ms=timer.deadline_ms,
        turn_id="turn_2",
        turn_sequence=1,
    ) == ()
    assert lifecycle.timer_receipt() is timer
    assert lifecycle.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="late_fallback",
        sequence=4,
        at_ms=timer.deadline_ms + 1,
        turn_id="turn_1",
        turn_sequence=0,
    ) == ()
    assert lifecycle.timer_receipt() is timer
    assert lifecycle.timer_fired(
        binding=_binding(),
        event_id="timer_wins",
        sequence=4,
        action_id=timer.action_id,
        revision=timer.revision,
        now_ms=timer.deadline_ms,
    )[0].kind is CallIntentKind.REQUEST_PRESENCE_CHECK


def test_invalid_activity_timestamp_preserves_exact_timer_authority():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]

    for index, invalid_at_ms in enumerate(
        (None, True, "13", object(), 13.0)
    ):
        assert lifecycle.cancel(
            binding=_binding(),
            event_id=f"invalid_activity_{index}",
            sequence=4,
            at_ms=invalid_at_ms,
        ) == ()
        assert lifecycle.phase is SilencePhase.FIRST_ARMED
        assert lifecycle.timer_receipt() is arm
        assert lifecycle.revision == arm.revision
        assert lifecycle._deadline_ms == arm.deadline_ms


def test_invalid_more_time_timestamp_preserves_exact_timer_authority():
    lifecycle = _lifecycle()
    _start(lifecycle)
    arm = _observed(
        lifecycle,
        event_id="question_playback",
        sequence=3,
        act_id="question_1",
        kind=VoiceSemanticActKind.QUESTION,
        at_ms=2,
    )[0]

    for index, invalid_at_ms in enumerate(
        (None, True, "13", object(), 13.0)
    ):
        assert lifecycle.request_more_time(
            binding=_binding(),
            event_id=f"invalid_more_time_{index}",
            sequence=4,
            at_ms=invalid_at_ms,
        ) == ()
        assert lifecycle.phase is SilencePhase.FIRST_ARMED
        assert lifecycle.timer_receipt() is arm
        assert lifecycle.revision == arm.revision
        assert lifecycle._deadline_ms == arm.deadline_ms
