"""Offline qualification for fixed silence and more-time speech."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.receptionist_state import IntakeState
from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_materializer import (
    FixedProposalMaterializer,
)
from app.services.voice_bakeoff_silence import (
    LifecycleActStatus,
    SilenceLifecycleController,
)
from app.services.voice_bakeoff_turn_composition import (
    CompositionPolicy,
    VersionedIntakeStore,
)
from app.services.voice_call_lifecycle import (
    CallIntentKind,
    CallLifecycle,
    QuestionIntent,
    SilencePhase,
)
from app.services.voice_candidates import (
    CandidateLimits,
    CandidateUsage,
    EventContext,
)
from app.services.voice_candidates.chained_streaming import (
    ChainedSignal,
    ChainedSignalKind,
    ChainedStreamingAdapter,
)
from app.services.voice_lifecycle import (
    VOICE_SCHEMA_VERSION,
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSensitivity,
    VoiceSessionBinding,
    VoiceSource,
)
from app.services.voice_speech_control import (
    SpeechControl,
    SpeechPolicy,
)

_EXPECTED_ASSETS = {
    "en": (
        "Are you still there?",
        "Take your time. I’ll wait twenty more seconds.",
        "This voice test cannot use keypad or text calling. Please speak, or end the call.",
        "This test cannot record a message. You can end the call now.",
        "I can’t hear a response, so I’ll end this test call now. Goodbye.",
    ),
    "es": (
        "¿Sigue ahí?",
        "Tómese su tiempo. Esperaré veinte segundos más.",
        "Esta prueba de voz no admite el teclado ni llamadas de texto. Hable o finalice la llamada.",
        "Esta prueba no puede grabar un mensaje. Puede finalizar la llamada ahora.",
        "No escucho una respuesta, así que finalizaré esta llamada de prueba ahora. Adiós.",
    ),
    "pt": (
        "Você ainda está aí?",
        "Sem pressa. Vou esperar mais vinte segundos.",
        "Este teste de voz não permite usar o teclado nem chamadas por texto. Fale ou encerre a chamada.",
        "Este teste não pode gravar uma mensagem. Você pode encerrar a chamada agora.",
        "Não consigo ouvir uma resposta, então vou encerrar esta chamada de teste agora. Até logo.",
    ),
    "zh": (
        "请问您还在吗？",
        "您慢慢来。我会再等二十秒。",
        "本次语音测试不支持按键或文字通话。请直接说话，或结束通话。",
        "本次测试不能录制留言。您现在可以结束通话。",
        "我没有听到回应，所以现在结束这次测试通话。再见。",
    ),
}


def _binding() -> VoiceSessionBinding:
    return VoiceSessionBinding(
        "bakeoff_offline",
        "tenant_1",
        "call_1",
        "stream_1",
        1,
    )


def _event_after(
    lifecycle: VoiceLifecycle,
    authorization: VoiceEvent,
    *,
    kind: VoiceEventKind,
    source: VoiceSource,
    payload: VoicePayload,
    at_ms: int,
) -> VoiceEvent:
    sequence, canonical_at_ms = lifecycle.next_position(
        at_ms=at_ms
    )
    return replace(
        authorization,
        kind=kind,
        source=source,
        sequence=sequence,
        at_ms=canonical_at_ms,
        payload=payload,
    )


def _assembly(*, locale: str = "en"):
    binding = _binding()
    adapter = ChainedStreamingAdapter(
        binding=binding,
        limits=CandidateLimits(
            output_tokens=128,
            audio_ms=6_000,
            byte_count=1_000_000,
            wall_clock_ms=60_000,
            cost_minor_units=100,
            request_count=32,
        ),
    )
    lifecycle = VoiceLifecycle(binding=binding)
    assert adapter.bind_canonical_lifecycle(lifecycle)
    final_context = EventContext(
        binding=binding,
        sequence=0,
        at_ms=0,
        input_turn_id="turn_1",
        generation_id="input_generation_1",
        semantic_act_id="input_act_1",
        semantic_act_kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
    )
    final = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            final_context,
            CandidateUsage(),
            VoicePayload(text_digest="f" * 64),
        )
    )
    assert final.accepted
    assert lifecycle.ingest(final.events[0])
    initial_state = IntakeState.new(call_sid="call_1")
    initial_state.language = locale
    state = VersionedIntakeStore(
        binding=binding,
        initial_state=initial_state,
    )
    assert state.admit_turn(turn_id="turn_1", sequence=0)
    calls = CallLifecycle(
        binding=binding,
        voice_lifecycle=lifecycle,
        first_silence_ms=10_000,
        second_silence_ms=10_000,
        more_time_extension_ms=20_000,
    )
    speech = SpeechControl(
        SpeechPolicy(
            normal_word_budget=30,
            safety_word_budget=30,
            required_safety_fragments=(
                "call emergency services",
            ),
            terminal_fragments=("goodbye",),
        )
    )
    coordinator = VoiceBakeoffCoordinator(
        speech=speech,
        calls=calls,
    )
    controller = SilenceLifecycleController(
        binding=binding,
        adapter=adapter,
        lifecycle=lifecycle,
        state=state,
        coordinator=coordinator,
        materializer=FixedProposalMaterializer(),
        policy=CompositionPolicy(),
    )
    question = QuestionIntent(
        slot="service_action",
        turn_id="turn_1",
        act_id="question_1",
        turn_sequence=0,
    )
    assert calls.reserve_question(
        binding=binding,
        event_id="reserve_question",
        sequence=0,
        at_ms=0,
        question=question,
    )
    authorization = VoiceEvent(
        schema_version=VOICE_SCHEMA_VERSION,
        kind=VoiceEventKind.RESPONSE_AUTHORIZED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        sensitivity=VoiceSensitivity.OPERATIONAL,
        binding=binding,
        sequence=1,
        at_ms=1,
        input_turn_id="turn_1",
        generation_id="question_generation",
        semantic_act_id="question_1",
        semantic_act_kind=VoiceSemanticActKind.QUESTION,
        payload=VoicePayload(),
    )
    assert lifecycle.ingest(authorization)
    confirmation = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=2,
    )
    assert lifecycle.ingest(confirmation)
    assert calls.semantic_confirmed(
        event=confirmation,
        event_id="confirm_question",
        sequence=1,
    )
    payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="question_audio",
        playout_id="question_playout",
    )
    tts = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(
            text_digest=payload.text_digest,
            audio_id=payload.audio_id,
        ),
        at_ms=3,
    )
    assert lifecycle.ingest(tts)
    playout = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.PLAYOUT_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=payload,
        at_ms=4,
    )
    assert lifecycle.ingest(playout)
    transport = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TRANSPORT_RESOLVED,
        source=VoiceSource.TWILIO_AUTHENTICATED,
        payload=payload,
        at_ms=5,
    )
    assert lifecycle.ingest(transport)
    assert calls.transport_resolved(
        event=transport,
        event_id="transport_question",
        sequence=2,
    )
    playback = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=payload,
        at_ms=6,
    )
    assert lifecycle.ingest(playback)
    arm = calls.observed_playback(
        event=playback,
        event_id="playback_question",
        sequence=3,
    )[0]
    return {
        "adapter": adapter,
        "calls": calls,
        "controller": controller,
        "lifecycle": lifecycle,
        "speech": speech,
        "state": state,
        "arm": arm,
    }


def _deliver(
    harness,
    pending,
    *,
    at_ms: int,
):
    controller = harness["controller"]
    lifecycle = harness["lifecycle"]
    authorization = controller.authorization_receipt(
        pending.act_id
    )
    assert authorization is not None
    confirmation = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=at_ms,
    )
    assert lifecycle.ingest(confirmation)
    assert controller.accept_semantic_confirmation(event=confirmation)
    digest = pending.text_digest
    payload = VoicePayload(
        text_digest=digest,
        audio_id=f"audio_{digest[:16]}",
        playout_id=f"playout_{digest[16:32]}",
    )
    tts = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(
            text_digest=payload.text_digest,
            audio_id=payload.audio_id,
        ),
        at_ms=confirmation.at_ms + 1,
    )
    assert lifecycle.ingest(tts)
    assert controller.accept_tts_binding(event=tts)
    playout = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.PLAYOUT_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=payload,
        at_ms=tts.at_ms + 1,
    )
    assert lifecycle.ingest(playout)
    assert controller.accept_playout_binding(event=playout)
    transport = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TRANSPORT_RESOLVED,
        source=VoiceSource.TWILIO_AUTHENTICATED,
        payload=payload,
        at_ms=playout.at_ms + 1,
    )
    assert lifecycle.ingest(transport)
    assert controller.accept_transport_resolution(
        event=transport,
        event_id=f"transport_{transport.sequence}",
        sequence=transport.sequence,
    )
    playback = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=payload,
        at_ms=transport.at_ms + 1,
    )
    assert lifecycle.ingest(playback)
    observed = controller.observe_playback(
        event=playback,
        event_id=f"playback_{playback.sequence}",
        sequence=playback.sequence,
    )
    assert observed is not None
    return observed


@pytest.mark.parametrize(
    ("locale", "expected"),
    tuple(_EXPECTED_ASSETS.items()),
)
def test_lifecycle_materializer_uses_exact_reviewed_assets(
    locale: str,
    expected: tuple[str, str, str, str, str],
):
    materializer = FixedProposalMaterializer()
    kinds = (
        CallIntentKind.REQUEST_PRESENCE_CHECK,
        CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT,
        CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        CallIntentKind.REQUEST_CLOSING,
    )
    assert (
        tuple(
            materializer.lifecycle_act(
                intent_kind=kind,
                state_version=7,
                locale=locale,
            )
            .plan.acts[0]
            .text
            for kind in kinds
        )
        == expected
    )
    assert (
        materializer.lifecycle_act(
            intent_kind=CallIntentKind.REQUEST_PRESENCE_CHECK,
            state_version=7,
            locale="pt-BR",
        )
        .plan.acts[0]
        .text
        == _EXPECTED_ASSETS["pt"][0]
    )


def test_lifecycle_policy_is_separate_and_terminal_only_for_closure():
    materializer = FixedProposalMaterializer()
    policy = CompositionPolicy()
    presence = materializer.lifecycle_act(
        intent_kind=CallIntentKind.REQUEST_PRESENCE_CHECK,
        state_version=3,
        locale="en",
    )
    presence_authorization = policy.authorize_lifecycle(
        proposal=presence,
        binding=_binding(),
        turn_id="turn_1",
        state_version=3,
    )
    assert not presence_authorization.terminal_allowed
    assert presence_authorization.authorized_kinds == (
        VoiceSemanticActKind.PRESENCE_CHECK,
    )
    for kind in (
        CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
    ):
        fallback = materializer.lifecycle_act(
            intent_kind=kind,
            state_version=3,
            locale="en",
        )
        fallback_authorization = policy.authorize_lifecycle(
            proposal=fallback,
            binding=_binding(),
            turn_id="turn_1",
            state_version=3,
        )
        assert not fallback_authorization.terminal_allowed
        assert fallback_authorization.authorized_kinds == (
            VoiceSemanticActKind.ACKNOWLEDGEMENT,
        )
    closing = materializer.lifecycle_act(
        intent_kind=CallIntentKind.REQUEST_CLOSING,
        state_version=3,
        locale="en",
    )
    closing_authorization = policy.authorize_lifecycle(
        proposal=closing,
        binding=_binding(),
        turn_id="turn_1",
        state_version=3,
    )
    assert closing_authorization.terminal_allowed
    assert closing_authorization.authorized_kinds == (
        VoiceSemanticActKind.CLOSING,
    )
    with pytest.raises(ValueError):
        policy.authorize(
            action=None,
            proposal=presence,
            binding=_binding(),
            turn_id="turn_1",
            state_version=3,
        )
    with pytest.raises(ValueError):
        policy.authorize_lifecycle(
            proposal=materializer.input_repair(
                state_version=3,
                locale="en",
            ),
            binding=_binding(),
            turn_id="turn_1",
            state_version=3,
        )


@pytest.mark.parametrize(
    "kind",
    (
        CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
    ),
)
def test_fixed_fallback_cancels_question_timer_and_completes_nonterminal(
    kind: CallIntentKind,
):
    harness = _assembly(locale="pt")
    calls = harness["calls"]
    timer = harness["arm"]
    sequence, at_ms = calls.next_position(at_ms=timer.deadline_ms)
    intents = calls.request_fixed_fallback(
        kind=kind,
        binding=_binding(),
        event_id=f"request_{kind.value}",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )
    assert tuple(intent.kind for intent in intents) == (
        CallIntentKind.CANCEL_TIMER,
        CallIntentKind.CANCEL_ACT,
        kind,
    )
    assert calls.timer_receipt() is None
    pending = harness["controller"].prepare(
        intents[2],
        at_ms=at_ms + 1,
    )
    assert pending is not None
    assert (
        pending.semantic_act_kind
        is VoiceSemanticActKind.ACKNOWLEDGEMENT
    )
    observed = _deliver(harness, pending, at_ms=at_ms + 2)
    assert observed.status is LifecycleActStatus.OBSERVED
    assert observed.emitted_intents == ()
    assert calls.phase is SilencePhase.IDLE
    assert calls.is_quiescent
    assert calls.timer_fired(
        binding=_binding(),
        event_id=f"stale_timer_{kind.value}",
        sequence=calls.next_position(at_ms=at_ms + 10)[0],
        action_id=timer.action_id,
        revision=timer.revision,
        now_ms=at_ms + 10,
    ) == ()
    assert not harness["controller"].is_terminal


def test_fixed_fallback_playback_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    timer = harness["arm"]
    sequence, at_ms = calls.next_position(at_ms=timer.deadline_ms)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="request_simulated_voicemail",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None

    def fail(**kwargs):
        raise RuntimeError("synthetic playback reducer failure")

    monkeypatch.setattr(
        harness["controller"].coordinator,
        "caller_playback",
        fail,
    )
    with pytest.raises(AssertionError):
        _deliver(harness, pending, at_ms=at_ms + 2)
    assert calls.phase is SilencePhase.TERMINATED
    assert harness["controller"].pending_count == 0
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


def test_materialized_fallback_rejects_fresh_replacement_and_remains_deliverable():
    harness = _assembly()
    calls = harness["calls"]
    timer = harness["arm"]
    sequence, at_ms = calls.next_position(at_ms=7)
    first = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_UNSUPPORTED_ACCESS_MODE,
        binding=_binding(),
        event_id="first_fixed_fallback",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        first,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(
        pending.act_id
    )
    assert authorization is not None
    assert harness["adapter"].has_permit(authorization)

    replacement_sequence, replacement_at_ms = (
        calls.next_position(at_ms=at_ms + 2)
    )
    assert calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="fresh_replacement_fallback",
        sequence=replacement_sequence,
        at_ms=replacement_at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    ) == ()
    assert calls.phase is SilencePhase.FALLBACK_PENDING
    assert harness["controller"].pending_count == 1
    assert harness["adapter"].has_permit(authorization)
    assert calls.timer_fired(
        binding=_binding(),
        event_id="stale_original_timer",
        sequence=replacement_sequence,
        action_id=timer.action_id,
        revision=timer.revision,
        now_ms=timer.deadline_ms,
    ) == ()

    observed = _deliver(
        harness,
        pending,
        at_ms=replacement_at_ms + 1,
    )
    assert observed.status is LifecycleActStatus.OBSERVED
    assert observed.emitted_intents == ()
    assert calls.is_quiescent


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_abort_uses_per_act_fallback_and_seals_pending_permit(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id=f"abort_fallback_{failure_mode}",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(
        pending.act_id
    )
    assert authorization is not None
    assert harness["adapter"].has_permit(authorization)
    assert harness["speech"].is_live(pending.act_id)

    def replacement(binding):
        if failure_mode == "raise":
            raise RuntimeError("synthetic binding cleanup failure")
        return False

    monkeypatch.setattr(
        harness["speech"],
        "hard_terminalize_binding",
        replacement,
    )

    assert harness["controller"].abort(at_ms=at_ms + 2)
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert calls.is_quiescent
    assert harness["adapter"].terminally_closed
    assert not harness["adapter"].has_permit(authorization)
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_abort_forces_pending_batch_retirement_after_normal_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id=f"batch_abort_{failure_mode}",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    coordinator = harness["controller"].coordinator
    assert coordinator.reservation_batch_count(_binding()) == 1

    def fail_retirement(reserved):
        if failure_mode == "raise":
            raise RuntimeError(
                "synthetic normal batch retirement failure"
            )
        return False

    monkeypatch.setattr(
        coordinator,
        "retire_batch",
        fail_retirement,
    )

    assert harness["controller"].abort(at_ms=at_ms + 2)
    assert coordinator.reservation_batch_count(_binding()) == 0
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


def test_abort_retains_pending_authority_when_forced_batch_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="retained_batch_abort",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    coordinator = harness["controller"].coordinator
    assert coordinator.reservation_batch_count(_binding()) == 1
    monkeypatch.setattr(
        coordinator,
        "retire_batch",
        lambda reserved: False,
    )
    monkeypatch.setattr(
        coordinator,
        "force_retire_batch",
        lambda reserved: False,
    )

    assert not harness["controller"].abort(at_ms=at_ms + 2)
    assert coordinator.reservation_batch_count(_binding()) == 1
    assert harness["controller"].pending_count == 1
    assert not harness["controller"].is_terminal
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_internal_fail_closed_forces_batch_retirement_before_clearing_pending(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id=f"internal_batch_failure_{failure_mode}",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(
        pending.act_id
    )
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=at_ms + 2,
    )
    assert harness["lifecycle"].ingest(confirmation)
    coordinator = harness["controller"].coordinator

    def fail_retirement(reserved):
        if failure_mode == "raise":
            raise RuntimeError(
                "synthetic internal retirement failure"
            )
        return False

    monkeypatch.setattr(
        coordinator,
        "retire_batch",
        fail_retirement,
    )
    monkeypatch.setattr(
        harness["adapter"],
        "accept_semantic_confirmation",
        lambda event, *, lifecycle: False,
    )

    assert not harness["controller"].accept_semantic_confirmation(
        event=confirmation
    )
    assert coordinator.reservation_batch_count(_binding()) == 0
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


def test_internal_fail_closed_retains_handle_until_abort_can_retry_batch(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="internal_batch_retry",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(
        pending.act_id
    )
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=at_ms + 2,
    )
    assert harness["lifecycle"].ingest(confirmation)
    coordinator = harness["controller"].coordinator
    original_force_retirement = coordinator.force_retire_batch
    monkeypatch.setattr(
        coordinator,
        "retire_batch",
        lambda reserved: False,
    )
    monkeypatch.setattr(
        coordinator,
        "force_retire_batch",
        lambda reserved: False,
    )
    monkeypatch.setattr(
        harness["adapter"],
        "accept_semantic_confirmation",
        lambda event, *, lifecycle: False,
    )

    assert not harness["controller"].accept_semantic_confirmation(
        event=confirmation
    )
    assert coordinator.reservation_batch_count(_binding()) == 1
    assert harness["controller"].pending_count == 1
    monkeypatch.setattr(
        coordinator,
        "force_retire_batch",
        original_force_retirement,
    )

    assert harness["controller"].abort(at_ms=at_ms + 3)
    assert coordinator.reservation_batch_count(_binding()) == 0
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal


def test_internal_fail_closed_retains_handle_until_speech_inventory_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="internal_speech_inventory_retry",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(
        pending.act_id
    )
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=at_ms + 2,
    )
    assert harness["lifecycle"].ingest(confirmation)
    speech = harness["speech"]
    original_inventory = speech.act_ids_for_binding
    original_terminalize = speech.hard_terminalize
    original_binding_terminalize = (
        speech.hard_terminalize_binding
    )
    inventory_calls = 0

    def fail_second_inventory(binding):
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 2:
            raise RuntimeError(
                "synthetic speech inventory verification failure"
            )
        return original_inventory(binding)

    monkeypatch.setattr(
        speech,
        "act_ids_for_binding",
        fail_second_inventory,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize",
        lambda act_id: False,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize_binding",
        lambda binding: False,
    )
    monkeypatch.setattr(
        harness["adapter"],
        "accept_semantic_confirmation",
        lambda event, *, lifecycle: False,
    )

    assert not harness["controller"].accept_semantic_confirmation(
        event=confirmation
    )
    assert inventory_calls == 2
    assert harness["controller"].pending_count == 1
    assert not harness["controller"].is_terminal
    assert speech.is_live(pending.act_id)
    monkeypatch.setattr(
        speech,
        "act_ids_for_binding",
        original_inventory,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize",
        original_terminalize,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize_binding",
        original_binding_terminalize,
    )

    assert harness["controller"].abort(at_ms=at_ms + 3)
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert calls.is_quiescent
    assert harness["adapter"].terminally_closed
    assert harness["controller"].coordinator.reservation_batch_count(
        _binding()
    ) == 0
    assert all(
        not speech.is_live(act_id)
        for act_id in original_inventory(_binding())
    )


def test_fail_closed_latches_retry_for_unknown_historical_speech_inventory(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    first = harness["arm"]
    presence_intent = calls.timer_fired(
        binding=_binding(),
        event_id="historical_presence_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    presence = harness["controller"].prepare(
        presence_intent,
        at_ms=first.deadline_ms,
    )
    assert presence is not None
    second = _deliver(
        harness,
        presence,
        at_ms=first.deadline_ms + 1,
    ).emitted_intents[0]
    speech = harness["speech"]
    assert speech.is_live(presence.act_id)
    assert harness["controller"].pending_count == 0
    assert harness["controller"].coordinator.reservation_batch_count(
        _binding()
    ) == 0
    timer_sequence, timer_at_ms = calls.next_position(
        at_ms=second.deadline_ms
    )
    closing_intent = calls.timer_fired(
        binding=_binding(),
        event_id="historical_closing_timer",
        sequence=timer_sequence,
        action_id=second.action_id,
        revision=second.revision,
        now_ms=timer_at_ms,
    )[0]
    original_inventory = speech.act_ids_for_binding
    original_terminalize = speech.hard_terminalize
    original_binding_terminalize = (
        speech.hard_terminalize_binding
    )
    inventory_calls = 0

    def fail_second_inventory(binding):
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 2:
            raise RuntimeError(
                "synthetic historical inventory failure"
            )
        return original_inventory(binding)

    monkeypatch.setattr(
        harness["controller"].coordinator,
        "reserve_batch",
        lambda **kwargs: (),
    )
    monkeypatch.setattr(
        speech,
        "act_ids_for_binding",
        fail_second_inventory,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize",
        lambda act_id: False,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize_binding",
        lambda binding: False,
    )

    assert harness["controller"].prepare(
        closing_intent,
        at_ms=timer_at_ms + 1,
    ) is None
    assert inventory_calls == 2
    assert harness["controller"].pending_count == 1
    assert not harness["controller"].is_terminal
    assert speech.is_live(presence.act_id)
    monkeypatch.setattr(
        speech,
        "act_ids_for_binding",
        original_inventory,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize",
        original_terminalize,
    )
    monkeypatch.setattr(
        speech,
        "hard_terminalize_binding",
        original_binding_terminalize,
    )

    assert harness["controller"].abort(
        at_ms=timer_at_ms + 2
    )
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert calls.is_quiescent
    assert harness["adapter"].terminally_closed
    assert not speech.is_live(presence.act_id)


def test_abort_retains_handle_until_speech_inventory_can_be_verified(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id="abort_speech_inventory_retry",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    pending = harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    )
    assert pending is not None
    speech = harness["speech"]
    original_inventory = speech.act_ids_for_binding

    def fail_inventory(binding):
        raise RuntimeError(
            "synthetic speech inventory verification failure"
        )

    monkeypatch.setattr(
        speech,
        "act_ids_for_binding",
        fail_inventory,
    )

    assert not harness["controller"].abort(at_ms=at_ms + 2)
    assert harness["controller"].pending_count == 1
    assert not harness["controller"].is_terminal
    assert harness["adapter"].terminally_closed
    monkeypatch.setattr(
        speech,
        "act_ids_for_binding",
        original_inventory,
    )

    assert harness["controller"].abort(at_ms=at_ms + 3)
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert calls.is_quiescent
    assert harness["adapter"].terminally_closed
    assert all(
        not speech.is_live(act_id)
        for act_id in original_inventory(_binding())
    )


@pytest.mark.parametrize(
    "failure_stage",
    ("authorization", "text", "intent", "permit"),
)
def test_prepare_retains_preinstall_cleanup_authority_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
):
    harness = _assembly()
    calls = harness["calls"]
    sequence, at_ms = calls.next_position(at_ms=7)
    intent = calls.request_fixed_fallback(
        kind=CallIntentKind.REQUEST_SIMULATED_VOICEMAIL,
        binding=_binding(),
        event_id=f"prepare_cleanup_{failure_stage}",
        sequence=sequence,
        at_ms=at_ms,
        turn_id="turn_1",
        turn_sequence=0,
    )[2]
    coordinator = harness["controller"].coordinator
    original_force_retirement = coordinator.force_retire_batch
    monkeypatch.setattr(
        coordinator,
        "retire_batch",
        lambda reserved: False,
    )
    monkeypatch.setattr(
        coordinator,
        "force_retire_batch",
        lambda reserved: False,
    )
    if failure_stage == "authorization":
        original_next_position = (
            harness["lifecycle"].next_position
        )
        calls_to_next_position = 0

        def fail_authorization_position(*, at_ms):
            nonlocal calls_to_next_position
            calls_to_next_position += 1
            if calls_to_next_position == 2:
                raise RuntimeError(
                    "synthetic authorization construction failure"
                )
            return original_next_position(at_ms=at_ms)

        monkeypatch.setattr(
            harness["lifecycle"],
            "next_position",
            fail_authorization_position,
        )
    elif failure_stage == "text":
        monkeypatch.setattr(
            harness["speech"],
            "authorize_text",
            lambda act_id, text: False,
        )
    elif failure_stage == "intent":
        monkeypatch.setattr(
            calls,
            "consume_intent",
            lambda intent, *, materialized_act_id=None: False,
        )
    else:
        monkeypatch.setattr(
            harness["adapter"],
            "accept_permit",
            lambda event, *, lifecycle: False,
        )

    assert harness["controller"].prepare(
        intent,
        at_ms=at_ms + 1,
    ) is None
    assert coordinator.reservation_batch_count(_binding()) == 1
    assert harness["controller"].pending_count == 1
    assert not harness["controller"].is_terminal
    monkeypatch.setattr(
        coordinator,
        "force_retire_batch",
        original_force_retirement,
    )

    assert harness["controller"].abort(at_ms=at_ms + 2)
    assert coordinator.reservation_batch_count(_binding()) == 0
    assert harness["controller"].pending_count == 0
    assert harness["controller"].is_terminal
    assert harness["adapter"].terminally_closed


def test_more_time_acknowledgement_uses_canonical_chain_and_fixed_deadline():
    harness = _assembly()
    calls = harness["calls"]
    extension_request = calls.request_more_time(
        binding=_binding(),
        event_id="more_time_request",
        sequence=4,
        at_ms=10,
    )[1]
    pending = harness["controller"].prepare(
        extension_request,
        at_ms=11,
    )
    assert pending is not None
    assert pending.status is LifecycleActStatus.PENDING
    assert (
        pending.semantic_act_kind
        is VoiceSemanticActKind.ACKNOWLEDGEMENT
    )
    observed = _deliver(harness, pending, at_ms=12)
    assert observed.status is LifecycleActStatus.OBSERVED
    extension = observed.emitted_intents[0]
    assert extension.kind is CallIntentKind.ARM_TIMER
    assert extension.deadline_ms == 20_016
    assert calls.phase is SilencePhase.MORE_TIME_ARMED
    immutable = (
        extension.action_id,
        extension.revision,
        extension.deadline_ms,
    )
    assert calls.request_more_time(
        binding=_binding(),
        event_id="second_more_time_request",
        sequence=20,
        at_ms=17,
    ) == ()
    assert (
        calls._timer_action_id(),
        calls.revision,
        calls._deadline_ms,
    ) == immutable


def test_presence_then_silence_closure_requires_exact_terminal_receipt():
    harness = _assembly()
    calls = harness["calls"]
    first = harness["arm"]
    presence_intent = calls.timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    presence = harness["controller"].prepare(
        presence_intent,
        at_ms=first.deadline_ms,
    )
    assert presence is not None
    presence_observed = _deliver(
        harness,
        presence,
        at_ms=first.deadline_ms + 1,
    )
    second = presence_observed.emitted_intents[0]
    timer_sequence, timer_at_ms = calls.next_position(
        at_ms=second.deadline_ms
    )
    closing_intent = calls.timer_fired(
        binding=_binding(),
        event_id="second_timer",
        sequence=timer_sequence,
        action_id=second.action_id,
        revision=second.revision,
        now_ms=timer_at_ms,
    )[0]
    closing = harness["controller"].prepare(
        closing_intent,
        at_ms=second.deadline_ms,
    )
    assert closing is not None
    closing_observed = _deliver(
        harness,
        closing,
        at_ms=second.deadline_ms + 1,
    )
    assert (
        closing_observed.status
        is LifecycleActStatus.TERMINAL_ELIGIBLE
    )
    terminal = closing_observed.emitted_intents[0]
    assert terminal.kind is CallIntentKind.TERMINAL_ELIGIBLE
    assert harness["controller"].terminalize(terminal)
    assert harness["controller"].is_terminal
    assert calls.is_quiescent
    assert not harness["controller"].terminalize(terminal)


def test_equal_intent_clone_spends_authority_and_fails_closed():
    harness = _assembly()
    first = harness["arm"]
    intent = harness["calls"].timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    assert harness["controller"].prepare(
        replace(intent),
        at_ms=first.deadline_ms,
    ) is None
    assert harness["controller"].pending_count == 0
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["controller"].prepare(
        intent,
        at_ms=first.deadline_ms + 1,
    ) is None


def test_stale_turn_lifecycle_intent_closes_all_local_authority():
    harness = _assembly()
    first = harness["arm"]
    intent = harness["calls"].timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    assert harness["state"].admit_turn(
        turn_id="turn_2",
        sequence=1,
    )

    assert harness["controller"].prepare(
        intent,
        at_ms=first.deadline_ms,
    ) is None
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


def test_adapter_permit_failure_after_intent_consumption_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    first = harness["arm"]
    intent = harness["calls"].timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    monkeypatch.setattr(
        harness["adapter"],
        "accept_permit",
        lambda event, *, lifecycle: False,
    )

    assert harness["controller"].prepare(
        intent,
        at_ms=first.deadline_ms,
    ) is None
    assert harness["controller"].pending_count == 0
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            _binding()
        )
    )


def test_newer_turn_after_prepare_revokes_lifecycle_delivery():
    harness = _assembly()
    first = harness["arm"]
    intent = harness["calls"].timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    pending = harness["controller"].prepare(
        intent,
        at_ms=first.deadline_ms,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(pending.act_id)
    assert authorization is not None
    assert harness["state"].admit_turn(
        turn_id="turn_2",
        sequence=1,
    )
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=first.deadline_ms + 1,
    )
    assert harness["lifecycle"].ingest(confirmation)

    assert not harness["controller"].accept_semantic_confirmation(event=confirmation)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(_binding())
    )


def test_tts_digest_must_match_exact_reserved_lifecycle_text():
    harness = _assembly()
    first = harness["arm"]
    intent = harness["calls"].timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    pending = harness["controller"].prepare(
        intent,
        at_ms=first.deadline_ms,
    )
    assert pending is not None
    authorization = harness["controller"].authorization_receipt(pending.act_id)
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
        at_ms=first.deadline_ms + 1,
    )
    assert harness["lifecycle"].ingest(confirmation)
    assert harness["controller"].accept_semantic_confirmation(event=confirmation)
    tts = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(
            text_digest="0" * 64,
            audio_id="forged_audio",
        ),
        at_ms=confirmation.at_ms + 1,
    )
    assert harness["lifecycle"].ingest(tts)

    assert not harness["controller"].accept_tts_binding(event=tts)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert not harness["speech"].is_live(pending.act_id)


def test_newer_turn_invalidates_observed_closure_before_terminalization():
    harness = _assembly()
    calls = harness["calls"]
    first = harness["arm"]
    presence_intent = calls.timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    presence = harness["controller"].prepare(
        presence_intent,
        at_ms=first.deadline_ms,
    )
    assert presence is not None
    second = _deliver(
        harness,
        presence,
        at_ms=first.deadline_ms + 1,
    ).emitted_intents[0]
    timer_sequence, timer_at_ms = calls.next_position(at_ms=second.deadline_ms)
    closing_intent = calls.timer_fired(
        binding=_binding(),
        event_id="second_timer",
        sequence=timer_sequence,
        action_id=second.action_id,
        revision=second.revision,
        now_ms=timer_at_ms,
    )[0]
    closing = harness["controller"].prepare(
        closing_intent,
        at_ms=second.deadline_ms,
    )
    assert closing is not None
    terminal = _deliver(
        harness,
        closing,
        at_ms=second.deadline_ms + 1,
    ).emitted_intents[0]
    assert harness["state"].admit_turn(
        turn_id="turn_2",
        sequence=1,
    )

    assert not harness["controller"].terminalize(terminal)
    assert calls.phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(_binding())
    )


def test_binding_cleanup_failure_falls_back_for_historical_acts(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    first = harness["arm"]
    presence_intent = calls.timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    presence = harness["controller"].prepare(
        presence_intent,
        at_ms=first.deadline_ms,
    )
    assert presence is not None
    second = _deliver(
        harness,
        presence,
        at_ms=first.deadline_ms + 1,
    ).emitted_intents[0]
    timer_sequence, timer_at_ms = calls.next_position(at_ms=second.deadline_ms)
    closing_intent = calls.timer_fired(
        binding=_binding(),
        event_id="second_timer",
        sequence=timer_sequence,
        action_id=second.action_id,
        revision=second.revision,
        now_ms=timer_at_ms,
    )[0]
    closing = harness["controller"].prepare(
        closing_intent,
        at_ms=second.deadline_ms,
    )
    assert closing is not None
    terminal = _deliver(
        harness,
        closing,
        at_ms=second.deadline_ms + 1,
    ).emitted_intents[0]
    act_ids = harness["speech"].act_ids_for_binding(_binding())
    assert len(act_ids) >= 2
    assert all(harness["speech"].is_live(act_id) for act_id in act_ids)
    monkeypatch.setattr(
        harness["speech"],
        "hard_terminalize_binding",
        lambda binding: False,
    )

    assert harness["controller"].terminalize(terminal)
    assert all(not harness["speech"].is_live(act_id) for act_id in act_ids)


def test_fail_closed_cleanup_falls_back_for_historical_acts(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _assembly()
    calls = harness["calls"]
    first = harness["arm"]
    presence_intent = calls.timer_fired(
        binding=_binding(),
        event_id="first_timer",
        sequence=4,
        action_id=first.action_id,
        revision=first.revision,
        now_ms=first.deadline_ms,
    )[0]
    presence = harness["controller"].prepare(
        presence_intent,
        at_ms=first.deadline_ms,
    )
    assert presence is not None
    second = _deliver(
        harness,
        presence,
        at_ms=first.deadline_ms + 1,
    ).emitted_intents[0]
    timer_sequence, timer_at_ms = calls.next_position(at_ms=second.deadline_ms)
    closing_intent = calls.timer_fired(
        binding=_binding(),
        event_id="second_timer",
        sequence=timer_sequence,
        action_id=second.action_id,
        revision=second.revision,
        now_ms=timer_at_ms,
    )[0]
    monkeypatch.setattr(
        harness["adapter"],
        "accept_permit",
        lambda event, *, lifecycle: False,
    )
    monkeypatch.setattr(
        harness["speech"],
        "hard_terminalize_binding",
        lambda binding: False,
    )

    assert (
        harness["controller"].prepare(
            closing_intent,
            at_ms=second.deadline_ms,
        )
        is None
    )
    act_ids = harness["speech"].act_ids_for_binding(_binding())
    assert len(act_ids) >= 2
    assert calls.phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert all(not harness["speech"].is_live(act_id) for act_id in act_ids)
