"""Offline transaction tests for final-turn admission and response composition."""

from __future__ import annotations

import ast
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.caller_observation_extractor import (
    BackendOutcome,
    BackendResponse,
    CandidateFinalTurn,
    ExtractionOutcome,
    Finality,
    ObservationExtractor,
)
from app.services.receptionist_state import IntakeState
from app.services.voice_bakeoff_closure import (
    ClosureTrigger,
    OfflineAuthorityInventory,
)
from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_language_choice import (
    AdmissionPurpose,
    LanguageChoicePhase,
    LanguageRecoveryFinalTurnReceipt,
    OfflineLanguageChoiceLifecycle,
    materialize_language_choice,
)
from app.services.voice_bakeoff_materializer import FixedProposalMaterializer
from app.services.voice_bakeoff_turn_composition import (
    AdapterImplementationBinding,
    CompositionPhase,
    CompositionPolicy,
    CompositionStatus,
    FinalTurnAdmissionAuthority,
    TurnCompositionTransaction,
    VersionedIntakeStore,
    final_turn_content_digest,
)
from app.services.voice_call_lifecycle import CallLifecycle, SilencePhase
from app.services.voice_candidates import (
    AdapterRejectReason,
    CandidateLimits,
    CandidateUsage,
    EventContext,
)
from app.services.voice_candidates.chained_streaming import (
    ChainedSignal,
    ChainedSignalKind,
    ChainedStreamingAdapter,
)
from app.services.voice_candidates.conversation_relay import (
    ConversationRelayAdapter,
    RelaySignal,
    RelaySignalKind,
)
from app.services.voice_candidates.manual_native import (
    ManualNativeAdapter,
    ManualNativeSignal,
    ManualNativeSignalKind,
)
from app.services.voice_candidates.native_gemini import (
    NativeGeminiAdapter,
    NativeMode,
    NativeSignal,
    NativeSignalKind,
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
from app.services.voice_session_auth import CandidateArm
from app.services.voice_speech_control import (
    CancellationReason,
    ReplayMode,
    SpeechControl,
    SpeechPolicy,
)

_EXTRACTOR_DIGEST = "e" * 64
_IMPLEMENTATION_FILES = {
    CandidateArm.A: "app/services/voice_candidates/native_gemini.py",
    CandidateArm.B1: "app/services/voice_candidates/chained_streaming.py",
    CandidateArm.B2: "app/services/voice_candidates/conversation_relay.py",
    CandidateArm.C: "app/services/voice_candidates/manual_native.py",
}
_BASELINE_HASHES = {
    "app/experiments/voice_bakeoff_app.py":
        "082d82e73deff2db331ba120513327f6911f41f1c9f0e9e7279e8f711df13127",
    # Re-pinned 2026-08-12 for the default-off durable service-request recovery
    # worker. The pin's job is to force exactly this kind of deliberate
    # acknowledgment — update it only alongside a reviewed change to main.py,
    # never to silence a diff.
    "app/main.py":
        "ead351860384cdb4b377dac90ec8f119b770411b79c348d3ca870d716a67b202",
    # Re-pinned 2026-08-12: authenticated media streams now load the shared,
    # default-off customer-memory and service-request context.
    "app/webhooks/media_stream.py":
        "d614e95370aa91af90a754ac0f213fa312b017eceb73c72ff6958d6636455fe7",
}


def _binding(epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", epoch)


def _limits() -> CandidateLimits:
    return CandidateLimits(
        output_tokens=128,
        audio_ms=6_000,
        byte_count=1_000_000,
        wall_clock_ms=15_000,
        cost_minor_units=100,
        request_count=32,
    )


def _reviewed_implementation_digest(arm: CandidateArm) -> str:
    material = hashlib.sha256()
    material.update(
        b"hey-kevin/reviewed-offline-adapter-source-contract/v1\x00"
    )
    for path_string in (
        "app/services/voice_candidates/__init__.py",
        _IMPLEMENTATION_FILES[arm],
    ):
        material.update(path_string.encode())
        material.update(b"\x00")
        material.update(Path(path_string).read_bytes())
        material.update(b"\x00")
    return material.hexdigest()


def _implementation_bindings() -> tuple[
    AdapterImplementationBinding,
    ...,
]:
    return tuple(
        AdapterImplementationBinding(
            adapter_type=adapter_type,
            arm=arm,
            implementation_digest=_reviewed_implementation_digest(arm),
        )
        for adapter_type, arm in (
            (NativeGeminiAdapter, CandidateArm.A),
            (ChainedStreamingAdapter, CandidateArm.B1),
            (ConversationRelayAdapter, CandidateArm.B2),
            (ManualNativeAdapter, CandidateArm.C),
        )
    )


def _context(
    *,
    sequence: int,
    at_ms: int,
    turn_id: str,
    act_id: str = "input_act",
    semantic_act_kind: VoiceSemanticActKind = (
        VoiceSemanticActKind.ACKNOWLEDGEMENT
    ),
) -> EventContext:
    return EventContext(
        binding=_binding(),
        sequence=sequence,
        at_ms=at_ms,
        input_turn_id=turn_id,
        generation_id="input_generation",
        semantic_act_id=act_id,
        semantic_act_kind=semantic_act_kind,
    )


def _backend(
    *,
    fields: dict[str, object] | None = None,
    confidences: dict[str, float] | None = None,
    outcome: BackendOutcome = BackendOutcome.OK,
    effect=None,
):
    selected_fields = (
        fields
        if fields is not None
        else {
            "language": "en",
            "intent": "service_request",
            "service_action": "repair",
            "service_object": "furnace",
        }
    )
    selected_confidences = (
        confidences
        if confidences is not None
        else {key: 0.95 for key in selected_fields}
    )

    def extract(request):
        if effect is not None:
            effect()
        return BackendResponse(
            request_id=request.request_id,
            configuration_digest=_EXTRACTOR_DIGEST,
            outcome=outcome,
            fields=selected_fields,
            confidences=selected_confidences,
        )

    return extract


def test_observation_extractor_reentrant_backend_fails_closed_without_deadlock():
    extractor = ObservationExtractor(
        binding=_binding(),
        configuration_digest=_EXTRACTOR_DIGEST,
        min_field_confidence=0.8,
        min_aggregate_confidence=0.85,
    )
    turn = CandidateFinalTurn(
        binding=_binding(),
        input_turn_id="turn_reentrant",
        sequence=1,
        at_ms=1,
        finality=Finality.FINAL,
        content="synthetic reentrant content",
        admission_id="receipt_reentrant",
    )
    nested_results = []

    def backend(request):
        nested_results.append(
            extractor.extract(
                turn,
                backend=lambda nested_request: pytest.fail(
                    "reentrant duplicate reached backend"
                ),
                current_turn=lambda candidate: True,
            )
        )
        return _backend()(request)

    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            extractor.extract(
                turn,
                backend=backend,
                current_turn=lambda candidate: True,
            )
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=0.25)

    assert not worker.is_alive()
    assert len(nested_results) == 1
    assert nested_results[0].outcome is ExtractionOutcome.LATE
    assert len(results) == 1
    assert results[0].outcome is ExtractionOutcome.ACCEPTED


def _harness(
    *,
    content: str = "synthetic caller content",
    max_outcomes: int = 32,
    adapter_arm: str = "B1",
):
    binding = _binding()
    if adapter_arm == "A":
        adapter = NativeGeminiAdapter(
            binding=binding,
            mode=NativeMode.MANUAL_GATED,
            limits=_limits(),
        )
    elif adapter_arm == "B1":
        adapter = ChainedStreamingAdapter(
            binding=binding,
            limits=_limits(),
        )
    elif adapter_arm == "B2":
        adapter = ConversationRelayAdapter(
            binding=binding,
            limits=_limits(),
        )
    elif adapter_arm == "C":
        adapter = ManualNativeAdapter(
            binding=binding,
            limits=_limits(),
            generation_timeout_ms=100,
        )
    else:
        raise ValueError("unsupported test adapter arm")
    lifecycle = VoiceLifecycle(binding=binding)
    receipts = FinalTurnAdmissionAuthority(
        adapter=adapter,
        lifecycle=lifecycle,
        implementation_bindings=_implementation_bindings(),
        max_records=32,
        max_ttl_ms=1_000,
    )
    extractor = ObservationExtractor(
        binding=binding,
        configuration_digest=_EXTRACTOR_DIGEST,
        min_field_confidence=0.8,
        min_aggregate_confidence=0.85,
    )
    state = VersionedIntakeStore(
        binding=binding,
        initial_state=IntakeState.new(call_sid="call_1"),
    )
    calls = CallLifecycle(
        binding=binding,
        voice_lifecycle=lifecycle,
        first_silence_ms=10,
        second_silence_ms=20,
    )
    speech = SpeechControl(
        SpeechPolicy(
            normal_word_budget=20,
            safety_word_budget=20,
            required_safety_fragments=("call emergency services",),
            terminal_fragments=("goodbye",),
            localized_safety_fragments=(
                ("en", ("call emergency services",)),
                ("es", ("servicios de emergencia",)),
                ("pt", ("serviços de emergência",)),
                ("zh", ("紧急服务",)),
            ),
        )
    )
    coordinator = VoiceBakeoffCoordinator(speech=speech, calls=calls)
    transaction = TurnCompositionTransaction(
        binding=binding,
        adapter=adapter,
        lifecycle=lifecycle,
        extractor=extractor,
        receipts=receipts,
        state=state,
        coordinator=coordinator,
        materializer=FixedProposalMaterializer(),
        policy=CompositionPolicy(),
        max_outcomes=max_outcomes,
    )
    context = _context(sequence=1, at_ms=10, turn_id="turn_1")
    if type(adapter) is NativeGeminiAdapter:
        adapter_result = adapter.handle(
            NativeSignal(
                NativeSignalKind.INPUT_FINAL,
                context,
                CandidateUsage(),
                VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    elif type(adapter) is ManualNativeAdapter:
        for sequence, signal_kind in (
            (1, ManualNativeSignalKind.ACTIVITY_STARTED),
            (2, ManualNativeSignalKind.ACTIVITY_ENDED),
        ):
            activity = adapter.handle(
                ManualNativeSignal(
                    signal_kind,
                    _context(
                        sequence=sequence,
                        at_ms=sequence,
                        turn_id="turn_1",
                    ),
                )
            )
            assert activity.accepted
            assert lifecycle.ingest(activity.events[0])
        context = _context(
            sequence=3,
            at_ms=10,
            turn_id="turn_1",
        )
        adapter_result = adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.INPUT_FINAL,
                context,
                payload=VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    elif type(adapter) is ConversationRelayAdapter:
        adapter_result = adapter.handle(
            RelaySignal(
                RelaySignalKind.PROMPT_FINAL,
                context,
                CandidateUsage(),
                VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    else:
        adapter_result = adapter.handle(
            ChainedSignal(
                ChainedSignalKind.INPUT_FINAL,
                context,
                CandidateUsage(),
                VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    assert adapter_result.accepted
    final_event = adapter_result.events[0]
    assert lifecycle.ingest(final_event)
    receipt = receipts.mint(
        adapter=adapter,
        lifecycle=lifecycle,
        result=adapter_result,
        event=final_event,
        content=content,
        now_ms=10,
        ttl_ms=100,
    )
    assert receipt is not None
    return {
        "adapter": adapter,
        "lifecycle": lifecycle,
        "receipts": receipts,
        "state": state,
        "calls": calls,
        "speech": speech,
        "transaction": transaction,
        "receipt": receipt,
        "content": content,
    }


def _event_after(
    lifecycle: VoiceLifecycle,
    authorization: VoiceEvent,
    *,
    kind: VoiceEventKind,
    source: VoiceSource,
    payload: VoicePayload,
) -> VoiceEvent:
    sequence, at_ms = lifecycle.next_position(at_ms=authorization.at_ms)
    return replace(
        authorization,
        kind=kind,
        source=source,
        sequence=sequence,
        at_ms=at_ms,
        payload=payload,
    )


def _manual_context_after(
    lifecycle: VoiceLifecycle,
    authorization: VoiceEvent,
    *,
    at_ms: int,
) -> EventContext:
    sequence, canonical_at_ms = lifecycle.next_position(at_ms=at_ms)
    return EventContext(
        binding=authorization.binding,
        sequence=sequence,
        at_ms=canonical_at_ms,
        input_turn_id=authorization.input_turn_id,
        generation_id=authorization.generation_id,
        semantic_act_id=authorization.semantic_act_id,
        semantic_act_kind=authorization.semantic_act_kind,
    )


def _confirm_and_resolve_transport(harness, act_id: str) -> VoiceEvent:
    transaction = harness["transaction"]
    lifecycle = harness["lifecycle"]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    confirmation = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
    )
    assert lifecycle.ingest(confirmation)
    assert transaction.accept_semantic_confirmation(
        event=confirmation,
        event_id=f"confirmation_{act_id}",
        sequence=confirmation.sequence,
    )
    pending = transaction._pending_by_act[act_id]
    reserved = next(
        item
        for item in pending.reserved
        if item.act_id == act_id
    )
    text_digest = hashlib.sha256(
        reserved.text.encode("utf-8")
    ).hexdigest()
    audio_id = f"audio_{act_id[:16]}"
    playout_id = f"playout_{act_id[:16]}"
    tts = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(text_digest=text_digest, audio_id=audio_id),
    )
    assert lifecycle.ingest(tts)
    assert transaction.accept_tts_binding(event=tts)
    payload = VoicePayload(
        text_digest=text_digest,
        audio_id=audio_id,
        playout_id=playout_id,
    )
    playout = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.PLAYOUT_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=payload,
    )
    assert lifecycle.ingest(playout)
    assert transaction.accept_playout_binding(event=playout)
    transport = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TRANSPORT_RESOLVED,
        source=VoiceSource.TWILIO_AUTHENTICATED,
        payload=payload,
    )
    assert lifecycle.ingest(transport)
    assert transaction.accept_transport_resolution(
        event=transport,
        event_id=f"transport_{act_id}",
        sequence=transport.sequence,
    )
    return transport


def _playback_after_transport(
    harness,
    act_id: str,
) -> VoiceEvent:
    transport = _confirm_and_resolve_transport(harness, act_id)
    lifecycle = harness["lifecycle"]
    authorization = harness["transaction"].authorization_receipt(
        act_id
    )
    assert authorization is not None
    playback = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=transport.payload,
    )
    assert lifecycle.ingest(playback)
    return playback


def _mint_next_turn(
    harness,
    *,
    turn_id: str,
    content: str,
    at_ms: int,
    semantic_act_kind: VoiceSemanticActKind = (
        VoiceSemanticActKind.ACKNOWLEDGEMENT
    ),
):
    lifecycle = harness["lifecycle"]
    sequence, canonical_at_ms = lifecycle.next_position(at_ms=at_ms)
    context = _context(
        sequence=sequence,
        at_ms=canonical_at_ms,
        turn_id=turn_id,
        act_id=f"input_{turn_id}",
        semantic_act_kind=semantic_act_kind,
    )
    if type(harness["adapter"]) is NativeGeminiAdapter:
        result = harness["adapter"].handle(
            NativeSignal(
                NativeSignalKind.INPUT_FINAL,
                context,
                CandidateUsage(),
                VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    elif type(harness["adapter"]) is ConversationRelayAdapter:
        result = harness["adapter"].handle(
            RelaySignal(
                RelaySignalKind.PROMPT_FINAL,
                context,
                CandidateUsage(),
                VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    elif type(harness["adapter"]) is ManualNativeAdapter:
        for signal_kind in (
            ManualNativeSignalKind.ACTIVITY_STARTED,
            ManualNativeSignalKind.ACTIVITY_ENDED,
        ):
            activity_sequence, activity_at_ms = (
                lifecycle.next_position(at_ms=canonical_at_ms)
            )
            activity = harness["adapter"].handle(
                ManualNativeSignal(
                    signal_kind,
                    _context(
                        sequence=activity_sequence,
                        at_ms=activity_at_ms,
                        turn_id=turn_id,
                        semantic_act_kind=semantic_act_kind,
                    ),
                )
            )
            assert activity.accepted
            assert lifecycle.ingest(activity.events[0])
        sequence, canonical_at_ms = lifecycle.next_position(
            at_ms=canonical_at_ms
        )
        result = harness["adapter"].handle(
            ManualNativeSignal(
                ManualNativeSignalKind.INPUT_FINAL,
                _context(
                    sequence=sequence,
                    at_ms=canonical_at_ms,
                    turn_id=turn_id,
                    act_id=f"input_{turn_id}",
                    semantic_act_kind=semantic_act_kind,
                ),
                payload=VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    else:
        result = harness["adapter"].handle(
            ChainedSignal(
                ChainedSignalKind.INPUT_FINAL,
                context,
                CandidateUsage(),
                VoicePayload(
                    text_digest=final_turn_content_digest(content)
                ),
            )
        )
    assert result.accepted
    event = result.events[0]
    assert lifecycle.ingest(event)
    receipt = harness["receipts"].mint(
        adapter=harness["adapter"],
        lifecycle=lifecycle,
        result=result,
        event=event,
        content=content,
        now_ms=canonical_at_ms,
        ttl_ms=100,
    )
    assert receipt is not None
    return receipt


def _mint_language_recovery(
    harness,
    *,
    language_choice: OfflineLanguageChoiceLifecycle,
    turn_id: str,
    content: str,
    detected_locale: str | None,
    at_ms: int,
):
    lifecycle = harness["lifecycle"]
    sequence, canonical_at_ms = lifecycle.next_position(
        at_ms=at_ms
    )
    context = _context(
        sequence=sequence,
        at_ms=canonical_at_ms,
        turn_id=turn_id,
        act_id=f"input_{turn_id}",
    )
    payload = VoicePayload(
        text_digest=final_turn_content_digest(content)
    )
    adapter = harness["adapter"]
    if type(adapter) is NativeGeminiAdapter:
        result = adapter.handle(
            NativeSignal(
                NativeSignalKind.INPUT_FINAL,
                context,
                CandidateUsage(),
                payload,
            )
        )
    elif type(adapter) is ConversationRelayAdapter:
        result = adapter.handle(
            RelaySignal(
                RelaySignalKind.PROMPT_FINAL,
                context,
                CandidateUsage(),
                payload,
            )
        )
    elif type(adapter) is ManualNativeAdapter:
        for signal_kind in (
            ManualNativeSignalKind.ACTIVITY_STARTED,
            ManualNativeSignalKind.ACTIVITY_ENDED,
        ):
            activity_sequence, activity_at_ms = (
                lifecycle.next_position(at_ms=canonical_at_ms)
            )
            activity = adapter.handle(
                ManualNativeSignal(
                    signal_kind,
                    _context(
                        sequence=activity_sequence,
                        at_ms=activity_at_ms,
                        turn_id=turn_id,
                    ),
                )
            )
            assert activity.accepted
            activity_event = activity.events[0]
            assert lifecycle.ingest(activity_event)
            if (
                signal_kind
                is ManualNativeSignalKind.ACTIVITY_STARTED
            ):
                assert language_choice.accept_activity_started(
                    event=activity_event,
                    lifecycle=lifecycle,
                )
            else:
                assert language_choice.accept_activity_ended(
                    event=activity_event,
                    lifecycle=lifecycle,
                )
        sequence, canonical_at_ms = lifecycle.next_position(
            at_ms=canonical_at_ms
        )
        result = adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.INPUT_FINAL,
                _context(
                    sequence=sequence,
                    at_ms=canonical_at_ms,
                    turn_id=turn_id,
                    act_id=f"input_{turn_id}",
                ),
                payload=payload,
            )
        )
    else:
        result = adapter.handle(
            ChainedSignal(
                ChainedSignalKind.INPUT_FINAL,
                context,
                CandidateUsage(),
                payload,
            )
        )
    assert result.accepted
    event = result.events[0]
    assert lifecycle.ingest(event)
    pair = harness["receipts"].mint_language_recovery(
        adapter=adapter,
        lifecycle=lifecycle,
        language_choice=language_choice,
        result=result,
        event=event,
        content=content,
        detected_locale=detected_locale,
        now_ms=event.at_ms,
        ttl_ms=100,
    )
    return pair, event


def _prepare_language_window(harness):
    trigger = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            fields={
                "language": "fr",
                "intent": "service_request",
            }
        ),
        now_ms=11,
    )
    assert trigger.status is (
        CompositionStatus.LANGUAGE_CHOICE_REQUIRED
    )
    language_choice = OfflineLanguageChoiceLifecycle(
        binding=harness["lifecycle"].binding,
        speech=harness["speech"],
    )
    pending = harness[
        "transaction"
    ].prepare_language_choice(
        receipt=harness["receipt"],
        trigger=trigger,
        language_choice=language_choice,
        proposal=materialize_language_choice(
            state_version=trigger.state_version
        ),
    )
    assert pending.status is (
        CompositionStatus.LANGUAGE_CHOICE_PENDING
    )
    assert pending.act_kinds == (
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.LANGUAGE_CHOICE,
    )
    observed = pending
    for act_id in pending.act_ids:
        playback = _playback_after_transport(
            harness,
            act_id,
        )
        observed = harness["transaction"].observe_playback(
            event=playback,
            event_id=f"language_playback_{playback.sequence}",
            sequence=playback.sequence,
        )
        assert observed is not None
    assert observed.status is (
        CompositionStatus.LANGUAGE_CHOICE_WINDOW
    )
    assert language_choice.phase is (
        LanguageChoicePhase.RESPONSE_WINDOW
    )
    assert harness["state"].current_state().language == "fr"
    return language_choice, observed


def _disconnect_session(
    harness,
    *,
    at_ms: int,
) -> VoiceEvent:
    lifecycle = harness["lifecycle"]
    sequence, canonical_at_ms = lifecycle.next_position(
        at_ms=at_ms
    )
    context = _context(
        sequence=sequence,
        at_ms=canonical_at_ms,
        turn_id="disconnect_turn",
    )
    if type(harness["adapter"]) is NativeGeminiAdapter:
        result = harness["adapter"].handle(
            NativeSignal(
                NativeSignalKind.SESSION_DISCONNECTED,
                context,
            )
        )
    elif type(harness["adapter"]) is ConversationRelayAdapter:
        result = harness["adapter"].handle(
            RelaySignal(
                RelaySignalKind.SESSION_DISCONNECTED,
                context,
            )
        )
    elif type(harness["adapter"]) is ManualNativeAdapter:
        result = harness["adapter"].handle(
            ManualNativeSignal(
                ManualNativeSignalKind.SESSION_DISCONNECTED,
                context,
            )
        )
    else:
        result = harness["adapter"].handle(
            ChainedSignal(
                ChainedSignalKind.SESSION_DISCONNECTED,
                context,
            )
        )
    assert result.accepted
    event = result.events[0]
    assert lifecycle.ingest(event)
    return event


@pytest.mark.parametrize("arm", ["A", "B1", "B2", "C"])
def test_every_real_offline_adapter_can_prove_one_exact_final_turn(arm: str):
    content = f"synthetic {arm} caller content"
    digest = final_turn_content_digest(content)
    binding = _binding()
    lifecycle = VoiceLifecycle(binding=binding)

    if arm == "A":
        adapter = NativeGeminiAdapter(
            binding=binding,
            mode=NativeMode.MANUAL_GATED,
            limits=_limits(),
        )
        result = adapter.handle(
            NativeSignal(
                NativeSignalKind.INPUT_FINAL,
                _context(sequence=1, at_ms=10, turn_id="turn_1"),
                payload=VoicePayload(text_digest=digest),
            )
        )
    elif arm == "B1":
        adapter = ChainedStreamingAdapter(binding=binding, limits=_limits())
        result = adapter.handle(
            ChainedSignal(
                ChainedSignalKind.INPUT_FINAL,
                _context(sequence=1, at_ms=10, turn_id="turn_1"),
                payload=VoicePayload(text_digest=digest),
            )
        )
    elif arm == "B2":
        adapter = ConversationRelayAdapter(binding=binding, limits=_limits())
        result = adapter.handle(
            RelaySignal(
                RelaySignalKind.PROMPT_FINAL,
                _context(sequence=1, at_ms=10, turn_id="turn_1"),
                payload=VoicePayload(text_digest=digest),
            )
        )
    else:
        adapter = ManualNativeAdapter(
            binding=binding,
            limits=_limits(),
            generation_timeout_ms=100,
        )
        for sequence, signal_kind in (
            (1, ManualNativeSignalKind.ACTIVITY_STARTED),
            (2, ManualNativeSignalKind.ACTIVITY_ENDED),
        ):
            activity = adapter.handle(
                ManualNativeSignal(
                    signal_kind,
                    _context(
                        sequence=sequence,
                        at_ms=sequence,
                        turn_id="turn_1",
                    ),
                )
            )
            assert activity.accepted
            assert lifecycle.ingest(activity.events[0])
        result = adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.INPUT_FINAL,
                _context(sequence=3, at_ms=10, turn_id="turn_1"),
                payload=VoicePayload(text_digest=digest),
            )
        )

    authority = FinalTurnAdmissionAuthority(
        adapter=adapter,
        lifecycle=lifecycle,
        implementation_bindings=_implementation_bindings(),
        max_records=8,
        max_ttl_ms=100,
    )
    assert result.accepted
    final_event = result.events[0]
    assert authority.mint(
        adapter=adapter,
        lifecycle=lifecycle,
        result=result,
        event=final_event,
        content=content,
        now_ms=10,
        ttl_ms=50,
    ) is None
    assert lifecycle.ingest(final_event)
    receipt = authority.mint(
        adapter=adapter,
        lifecycle=lifecycle,
        result=result,
        event=final_event,
        content=content,
        now_ms=10,
        ttl_ms=50,
    )
    assert receipt is not None
    assert receipt.arm.value == arm
    assert (
        receipt.adapter_implementation_digest
        == _reviewed_implementation_digest(adapter.arm)
    )
    assert receipt.adapter_configuration_digest == adapter.configuration_digest
    assert content not in repr(receipt)


def test_public_accepted_api_cannot_forge_final_input_authority():
    content = "synthetic caller content"
    binding = _binding()
    adapter = ChainedStreamingAdapter(
        binding=binding,
        limits=_limits(),
    )
    context = _context(
        sequence=1,
        at_ms=10,
        turn_id="turn_1",
    )
    forged_event = context.event(
        VoiceEventKind.INPUT_TURN_FINAL,
        source=VoiceSource.PROVIDER_UNTRUSTED,
        payload=VoicePayload(
            text_digest=final_turn_content_digest(content)
        ),
    )
    with pytest.raises(ValueError, match="transition is invalid"):
        adapter._accept_final_transition(forged_event)
    result = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            context,
            payload=VoicePayload(
                text_digest=final_turn_content_digest(content)
            ),
        )
    )
    with pytest.raises(
        ValueError,
        match="adapter transition",
    ):
        adapter.accepted(result.events[0])


@pytest.mark.parametrize("arm", ["B1", "B2"])
def test_streaming_final_transition_is_atomic_with_terminalization(
    arm,
    monkeypatch,
):
    binding = _binding()
    context = _context(
        sequence=1,
        at_ms=1,
        turn_id=f"atomic_{arm}",
    )
    payload = VoicePayload(text_digest="e" * 64)
    if arm == "B1":
        adapter = ChainedStreamingAdapter(
            binding=binding,
            limits=_limits(),
        )
        signal = ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            context,
            payload=payload,
        )
    else:
        adapter = ConversationRelayAdapter(
            binding=binding,
            limits=_limits(),
        )
        signal = RelaySignal(
            RelaySignalKind.PROMPT_FINAL,
            context,
            payload=payload,
        )
    entered = threading.Event()
    release = threading.Event()
    original_accept = adapter._accept_final_transition

    def pause_before_final_acceptance(event):
        entered.set()
        assert release.wait(timeout=2)
        return original_accept(event)

    monkeypatch.setattr(
        adapter,
        "_accept_final_transition",
        pause_before_final_acceptance,
    )
    results = []
    errors: list[BaseException] = []

    def run_final():
        try:
            results.append(adapter.handle(signal))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    final_thread = threading.Thread(target=run_final)
    terminal_thread = threading.Thread(
        target=adapter.terminalize_permit_admission
    )
    final_thread.start()
    assert entered.wait(timeout=2)
    terminal_thread.start()
    terminal_thread.join(timeout=0.05)
    assert terminal_thread.is_alive()
    release.set()
    final_thread.join(timeout=2)
    terminal_thread.join(timeout=2)

    assert errors == []
    assert len(results) == 1
    result = results[0]
    assert result.accepted
    assert adapter.terminally_closed
    assert adapter.permit_admission_closed
    assert not adapter.consume_final_input_admission(
        result,
        result.events[0],
    )


def test_terminal_adapter_closure_cannot_be_resumed_in_same_instance_or_epoch():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=_limits(),
    )
    adapter.terminalize_permit_admission()
    revision = adapter.admission_revision
    assert adapter.terminally_closed
    resumed = adapter.handle(
        NativeSignal(
            NativeSignalKind.SESSION_RESUMED,
            _context(
                sequence=1,
                at_ms=10,
                turn_id="turn_resume",
            ),
        )
    )
    assert not resumed.accepted
    assert resumed.reason is AdapterRejectReason.STALE_EPOCH
    assert adapter.admission_revision == revision
    assert adapter.permit_admission_closed
    final = adapter.handle(
        NativeSignal(
            NativeSignalKind.INPUT_FINAL,
            _context(
                sequence=2,
                at_ms=11,
                turn_id="turn_after_terminal",
            ),
            payload=VoicePayload(
                text_digest=final_turn_content_digest(
                    "post terminal content"
                )
            ),
        )
    )
    assert not final.accepted
    assert final.final_input_admission is None
    assert adapter.permit_admission_closed


def test_final_input_capability_rejects_clone_subclass_and_replay():
    content = "synthetic caller content"
    binding = _binding()
    adapter = ChainedStreamingAdapter(
        binding=binding,
        limits=_limits(),
    )
    lifecycle = VoiceLifecycle(binding=binding)
    authority = FinalTurnAdmissionAuthority(
        adapter=adapter,
        lifecycle=lifecycle,
        implementation_bindings=_implementation_bindings(),
        max_records=8,
        max_ttl_ms=100,
    )
    result = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(sequence=1, at_ms=10, turn_id="turn_1"),
            payload=VoicePayload(
                text_digest=final_turn_content_digest(content)
            ),
        )
    )
    event = result.events[0]
    assert lifecycle.ingest(event)
    cloned = replace(event)
    assert authority.mint(
        adapter=adapter,
        lifecycle=lifecycle,
        result=result,
        event=cloned,
        content=content,
        now_ms=10,
        ttl_ms=50,
    ) is None
    assert authority.mint(
        adapter=adapter,
        lifecycle=lifecycle,
        result=result,
        event=event,
        content=content,
        now_ms=10,
        ttl_ms=50,
    ) is None

    class SpoofedChainedAdapter(ChainedStreamingAdapter):
        arm = CandidateArm.B1

    spoof = SpoofedChainedAdapter(
        binding=binding,
        limits=_limits(),
    )
    spoof_lifecycle = VoiceLifecycle(binding=binding)
    spoof_result = spoof.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(
                sequence=1,
                at_ms=10,
                turn_id="turn_spoof",
            ),
            payload=VoicePayload(
                text_digest=final_turn_content_digest(content)
            ),
        )
    )
    assert spoof_lifecycle.ingest(spoof_result.events[0])
    assert authority.mint(
        adapter=spoof,
        lifecycle=spoof_lifecycle,
        result=spoof_result,
        event=spoof_result.events[0],
        content=content,
        now_ms=10,
        ttl_ms=50,
    ) is None


def test_final_turn_authority_rejects_shadow_lifecycle_and_adapter():
    content = "synthetic caller content"
    binding = _binding()
    adapter = ChainedStreamingAdapter(
        binding=binding,
        limits=_limits(),
    )
    lifecycle = VoiceLifecycle(binding=binding)
    authority = FinalTurnAdmissionAuthority(
        adapter=adapter,
        lifecycle=lifecycle,
        implementation_bindings=_implementation_bindings(),
        max_records=8,
        max_ttl_ms=100,
    )
    assert lifecycle.ingest(
        _context(
            sequence=5,
            at_ms=5,
            turn_id="session_turn",
        ).event(
            VoiceEventKind.SESSION_DISCONNECTED,
            source=VoiceSource.PROVIDER_UNTRUSTED,
        )
    )
    context = _context(
        sequence=5,
        at_ms=5,
        turn_id="stale_final",
    )
    result = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            context,
            payload=VoicePayload(
                text_digest=final_turn_content_digest(content)
            ),
        )
    )
    event = result.events[0]
    assert not lifecycle.ingest(event)
    shadow_lifecycle = VoiceLifecycle(binding=binding)
    assert shadow_lifecycle.ingest(event)
    assert authority.mint(
        adapter=adapter,
        lifecycle=shadow_lifecycle,
        result=result,
        event=event,
        content=content,
        now_ms=5,
        ttl_ms=50,
    ) is None
    other_adapter = ChainedStreamingAdapter(
        binding=binding,
        limits=_limits(),
    )
    assert not authority.accepts_adapter(other_adapter)


def test_receipt_is_spent_by_first_attempt_and_binds_unicode_bytes_exactly():
    harness = _harness(content="🙂" * 4_000)
    receipt = harness["receipt"]
    authority = harness["receipts"]
    adapter = harness["adapter"]
    assert receipt.content_byte_length == 16_000
    assert not authority.consume(
        receipt,
        adapter=adapter,
        content="wrong",
        now_ms=11,
    )
    assert not authority.consume(
        receipt,
        adapter=adapter,
        content=harness["content"],
        now_ms=12,
    )

    binding = _binding()
    other = ChainedStreamingAdapter(binding=binding, limits=_limits())
    lifecycle = VoiceLifecycle(binding=binding)
    too_large = "🙂" * 4_001
    result = other.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(sequence=1, at_ms=10, turn_id="turn_large"),
            payload=VoicePayload(text_digest=final_turn_content_digest(too_large)),
        )
    )
    assert result.accepted and lifecycle.ingest(result.events[0])
    assert authority.mint(
        adapter=other,
        lifecycle=lifecycle,
        result=result,
        event=result.events[0],
        content=too_large,
        now_ms=10,
        ttl_ms=50,
    ) is None


@pytest.mark.parametrize(
    "mutation",
    ["arm", "configuration", "binding", "expiry"],
)
def test_receipt_rejects_arm_configuration_binding_and_expiry_drift(
    mutation: str,
):
    harness = _harness()
    adapter = harness["adapter"]
    now_ms = 11
    if mutation == "arm":
        adapter.arm = CandidateArm.A
    elif mutation == "configuration":
        adapter.limits = CandidateLimits(
            output_tokens=64,
            audio_ms=6_000,
            byte_count=1_000_000,
            wall_clock_ms=15_000,
            cost_minor_units=100,
            request_count=32,
        )
    elif mutation == "binding":
        adapter.binding = _binding(epoch=2)
    else:
        now_ms = harness["receipt"].expires_at_ms + 1
    assert not harness["receipts"].consume(
        harness["receipt"],
        adapter=adapter,
        content=harness["content"],
        now_ms=now_ms,
    )


def test_receipt_valid_consume_is_exactly_once():
    harness = _harness()
    assert harness["receipts"].consume(
        harness["receipt"],
        adapter=harness["adapter"],
        content=harness["content"],
        now_ms=11,
    )
    assert not harness["receipts"].consume(
        harness["receipt"],
        adapter=harness["adapter"],
        content=harness["content"],
        now_ms=12,
    )


def test_disconnect_revision_invalidates_an_already_minted_receipt():
    harness = _harness()
    revision = harness["receipt"].adapter_admission_revision
    harness["adapter"].revoke_permits_for_disconnect()
    assert harness["adapter"].admission_revision > revision
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert result.status is CompositionStatus.REJECTED
    assert harness["state"].version == 0


def test_accepted_facts_commit_then_question_waits_for_caller_playback():
    harness = _harness()
    transaction = harness["transaction"]
    result = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert result.status is CompositionStatus.RESPONSE_PENDING
    assert result.phase is CompositionPhase.RESPONSE_PENDING_PLAYBACK
    assert result.act_kinds == (VoiceSemanticActKind.QUESTION,)
    assert harness["state"].version == 1
    assert harness["state"].current_state().service_object == "furnace"
    assert harness["state"].current_state().asked_slots == set()
    assert harness["calls"].phase is SilencePhase.QUESTION_RESERVED

    act_id = result.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    lifecycle = harness["lifecycle"]
    confirmation = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
    )
    assert lifecycle.ingest(confirmation)
    assert transaction.accept_semantic_confirmation(
        event=confirmation,
        event_id="confirmation_1",
        sequence=confirmation.sequence,
    )
    pending = transaction._pending_by_act[act_id]
    reserved = next(
        item
        for item in pending.reserved
        if item.act_id == act_id
    )
    text_digest = hashlib.sha256(
        reserved.text.encode("utf-8")
    ).hexdigest()
    payload = VoicePayload(
        text_digest=text_digest,
        audio_id="audio_1",
    )
    tts = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=payload,
    )
    assert lifecycle.ingest(tts)
    assert transaction.accept_tts_binding(event=tts)
    playout_payload = VoicePayload(
        text_digest=text_digest,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    playout = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.PLAYOUT_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=playout_payload,
    )
    assert lifecycle.ingest(playout)
    assert transaction.accept_playout_binding(event=playout)
    transport = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.TRANSPORT_RESOLVED,
        source=VoiceSource.TWILIO_AUTHENTICATED,
        payload=playout_payload,
    )
    assert lifecycle.ingest(transport)
    assert transaction.accept_transport_resolution(
        event=transport,
        event_id="transport_1",
        sequence=transport.sequence,
    )
    assert harness["state"].current_state().asked_slots == set()
    playback = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=playout_payload,
    )
    assert lifecycle.ingest(playback)
    observed = transaction.observe_playback(
        event=playback,
        event_id="playback_1",
        sequence=playback.sequence,
    )
    assert observed is not None
    assert observed.status is CompositionStatus.RESPONSE_OBSERVED
    assert observed.state_version == 2
    assert harness["state"].current_state().asked_slots == {"job_complexity"}
    assert harness["calls"].phase is SilencePhase.FIRST_ARMED
    assert not harness["adapter"].has_permit(authorization)

    replay = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail("replay called extractor"),
        now_ms=12,
    )
    assert replay == observed


def test_repeat_then_slower_replay_exact_observed_question_without_state_mutation():
    harness = _harness()
    transaction = harness["transaction"]
    original = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    original_act_id = original.act_ids[0]
    original_playback = _playback_after_transport(
        harness,
        original_act_id,
    )
    original_observed = transaction.observe_playback(
        event=original_playback,
        event_id="playback_original",
        sequence=original_playback.sequence,
    )
    assert original_observed is not None
    assert (
        original_observed.status
        is CompositionStatus.RESPONSE_OBSERVED
    )
    state_before_replay = (
        harness["state"].current_state().to_dict()
    )
    version_before_replay = harness["state"].version
    original_digest = harness[
        "speech"
    ].authorized_text_digest(original_act_id)
    assert original_digest is not None
    assert harness["calls"].phase is SilencePhase.FIRST_ARMED

    repeat_content = "repeat"
    repeat_receipt = _mint_next_turn(
        harness,
        turn_id="turn_repeat",
        content=repeat_content,
        at_ms=original_playback.at_ms + 1,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    repeated = transaction.execute_replay(
        repeat_receipt,
        content=repeat_content,
        command=VoiceSemanticActKind.REPEAT,
        now_ms=repeat_receipt.at_ms,
    )
    assert repeated.status is CompositionStatus.REPLAY_PENDING
    assert repeated.replay_mode is ReplayMode.EXACT
    assert repeated.replay_source_act_id == original_act_id
    assert repeated.act_kinds == (VoiceSemanticActKind.QUESTION,)
    assert repeated.act_ids != original.act_ids
    assert harness["state"].version == version_before_replay
    assert (
        harness["state"].current_state().to_dict()
        == state_before_replay
    )
    assert harness["calls"].phase is SilencePhase.QUESTION_RESERVED
    repeated_act_id = repeated.act_ids[0]
    assert (
        harness["speech"].authorized_text_digest(repeated_act_id)
        == original_digest
    )
    replay_binding = harness["speech"].replay_binding(
        repeated_act_id
    )
    assert replay_binding is not None
    assert replay_binding.mode is ReplayMode.EXACT
    assert replay_binding.source_act_id == original_act_id
    assert replay_binding.request_id == repeat_receipt.receipt_id

    repeat_transport = _confirm_and_resolve_transport(
        harness,
        repeated_act_id,
    )
    assert (
        transaction.infer_playback(
            act_id=repeated_act_id,
            event_id="inferred_repeat",
            sequence=repeat_transport.sequence + 1,
            at_ms=repeat_transport.at_ms + 1,
            inference_id="inferred_repeat",
            transport_id=repeat_transport.payload.playout_id or "",
        )
        is None
    )
    assert harness["calls"].phase is SilencePhase.QUESTION_CONFIRMED
    repeat_playback = _event_after(
        harness["lifecycle"],
        transaction.authorization_receipt(repeated_act_id),
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=repeat_transport.payload,
    )
    assert harness["lifecycle"].ingest(repeat_playback)
    repeat_observed = transaction.observe_playback(
        event=repeat_playback,
        event_id="playback_repeat",
        sequence=repeat_playback.sequence,
    )
    assert repeat_observed is not None
    assert (
        repeat_observed.status
        is CompositionStatus.REPLAY_OBSERVED
    )
    assert harness["state"].version == version_before_replay
    assert (
        harness["state"].current_state().to_dict()
        == state_before_replay
    )
    assert harness["calls"].phase is SilencePhase.FIRST_ARMED
    assert transaction.execute_replay(
        repeat_receipt,
        content=repeat_content,
        command=VoiceSemanticActKind.REPEAT,
        now_ms=repeat_playback.at_ms + 1,
    ) == repeat_observed

    slower_content = "slower"
    slower_receipt = _mint_next_turn(
        harness,
        turn_id="turn_slower",
        content=slower_content,
        at_ms=repeat_playback.at_ms + 1,
        semantic_act_kind=VoiceSemanticActKind.SLOWER_SPEECH,
    )
    slower = transaction.execute_replay(
        slower_receipt,
        content=slower_content,
        command=VoiceSemanticActKind.SLOWER_SPEECH,
        now_ms=slower_receipt.at_ms,
    )
    assert slower.status is CompositionStatus.REPLAY_PENDING
    assert slower.replay_mode is ReplayMode.SLOWER
    assert slower.replay_source_act_id == repeated_act_id
    assert (
        harness["speech"].authorized_text_digest(
            slower.act_ids[0]
        )
        == original_digest
    )
    assert harness["state"].version == version_before_replay
    assert (
        harness["state"].current_state().to_dict()
        == state_before_replay
    )
    assert harness["calls"].phase is SilencePhase.QUESTION_RESERVED

    slower_playback = _playback_after_transport(
        harness,
        slower.act_ids[0],
    )
    slower_observed = transaction.observe_playback(
        event=slower_playback,
        event_id="playback_slower",
        sequence=slower_playback.sequence,
    )
    assert slower_observed is not None
    assert (
        slower_observed.status
        is CompositionStatus.REPLAY_OBSERVED
    )
    assert slower_observed.replay_mode is ReplayMode.SLOWER
    assert harness["state"].version == version_before_replay
    assert (
        harness["state"].current_state().to_dict()
        == state_before_replay
    )
    assert harness["calls"].phase is SilencePhase.FIRST_ARMED


def test_replay_requires_typed_command_receipt_and_an_observed_source():
    ordinary = _harness()
    ordinary_result = ordinary["transaction"].execute_replay(
        ordinary["receipt"],
        content=ordinary["content"],
        command=VoiceSemanticActKind.REPEAT,
        now_ms=11,
    )
    assert ordinary_result.status is CompositionStatus.TERMINAL_FAILURE
    assert ordinary_result.reason == "replay_command_mismatch"
    assert ordinary["transaction"].pending_response_count == 0
    assert ordinary["speech"].reservation_batch_count(
        ordinary["receipt"].binding
    ) == 0

    command = _harness()
    repeat_content = "repeat"
    repeat_receipt = _mint_next_turn(
        command,
        turn_id="turn_repeat_without_source",
        content=repeat_content,
        at_ms=11,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    result = command["transaction"].execute_replay(
        repeat_receipt,
        content=repeat_content,
        command=VoiceSemanticActKind.REPEAT,
        now_ms=repeat_receipt.at_ms,
    )
    assert result.status is CompositionStatus.SILENT
    assert result.reason == "no_caller_observed_replay_source"
    assert result.act_ids == ()
    assert command["transaction"].pending_response_count == 0
    assert command["calls"].is_quiescent

    normal_path = _harness()
    typed_receipt = _mint_next_turn(
        normal_path,
        turn_id="turn_repeat_on_normal_path",
        content=repeat_content,
        at_ms=11,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    normal_result = normal_path["transaction"].execute(
        typed_receipt,
        content=repeat_content,
        backend=lambda request: pytest.fail(
            "typed command reached extraction"
        ),
        now_ms=typed_receipt.at_ms,
    )
    assert normal_result.status is CompositionStatus.TERMINAL_FAILURE
    assert normal_result.reason == "input_semantic_kind"


def test_replay_command_mismatch_and_tts_text_drift_fail_closed():
    mismatch = _harness()
    mismatch_receipt = _mint_next_turn(
        mismatch,
        turn_id="turn_mismatch",
        content="repeat",
        at_ms=11,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    mismatch_result = mismatch["transaction"].execute_replay(
        mismatch_receipt,
        content="repeat",
        command=VoiceSemanticActKind.SLOWER_SPEECH,
        now_ms=mismatch_receipt.at_ms,
    )
    assert mismatch_result.status is CompositionStatus.TERMINAL_FAILURE
    assert mismatch_result.reason == "replay_command_mismatch"

    harness = _harness()
    original = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    original_playback = _playback_after_transport(
        harness,
        original.act_ids[0],
    )
    assert harness["transaction"].observe_playback(
        event=original_playback,
        event_id="playback_before_drift",
        sequence=original_playback.sequence,
    ) is not None
    replay_receipt = _mint_next_turn(
        harness,
        turn_id="turn_drift",
        content="repeat",
        at_ms=original_playback.at_ms + 1,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    replay = harness["transaction"].execute_replay(
        replay_receipt,
        content="repeat",
        command=VoiceSemanticActKind.REPEAT,
        now_ms=replay_receipt.at_ms,
    )
    replay_act_id = replay.act_ids[0]
    authorization = harness[
        "transaction"
    ].authorization_receipt(replay_act_id)
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
    )
    assert harness["lifecycle"].ingest(confirmation)
    assert harness["transaction"].accept_semantic_confirmation(
        event=confirmation,
        event_id="confirm_drift",
        sequence=confirmation.sequence,
    )
    tampered_tts = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(
            text_digest="f" * 64,
            audio_id="audio_tampered",
        ),
    )
    assert harness["lifecycle"].ingest(tampered_tts)
    assert not harness["transaction"].accept_tts_binding(
        event=tampered_tts
    )
    terminal = harness["transaction"]._outcomes[
        replay.receipt_id
    ]
    assert terminal.status is CompositionStatus.TERMINAL_FAILURE
    assert terminal.reason == "tts_binding_commit_failed"
    assert harness["adapter"].permit_admission_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert not harness["speech"].is_live(replay_act_id)
    assert harness["transaction"].pending_response_count == 0


def test_replay_call_cleanup_and_reservation_rollback_faults_seal_all_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    call_fault = _harness()
    original = call_fault["transaction"].execute(
        call_fault["receipt"],
        content=call_fault["content"],
        backend=_backend(),
        now_ms=11,
    )
    playback = _playback_after_transport(
        call_fault,
        original.act_ids[0],
    )
    assert call_fault["transaction"].observe_playback(
        event=playback,
        event_id="playback_before_call_fault",
        sequence=playback.sequence,
    ) is not None
    receipt = _mint_next_turn(
        call_fault,
        turn_id="turn_call_cleanup_fault",
        content="repeat",
        at_ms=playback.at_ms + 1,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    monkeypatch.setattr(
        call_fault["calls"],
        "cancel",
        lambda **kwargs: (),
    )
    failed = call_fault["transaction"].execute_replay(
        receipt,
        content="repeat",
        command=VoiceSemanticActKind.REPEAT,
        now_ms=receipt.at_ms,
    )
    assert failed.status is CompositionStatus.TERMINAL_FAILURE
    assert failed.reason == "replay_call_cleanup"
    assert call_fault["adapter"].terminally_closed
    assert call_fault["calls"].phase is SilencePhase.TERMINATED
    assert call_fault["calls"].is_quiescent
    assert all(
        not call_fault["speech"].is_live(act_id)
        for act_id in call_fault["speech"].act_ids_for_binding(
            call_fault["receipt"].binding
        )
    )

    rollback_fault = _harness()
    original = rollback_fault["transaction"].execute(
        rollback_fault["receipt"],
        content=rollback_fault["content"],
        backend=_backend(),
        now_ms=11,
    )
    playback = _playback_after_transport(
        rollback_fault,
        original.act_ids[0],
    )
    assert rollback_fault["transaction"].observe_playback(
        event=playback,
        event_id="playback_before_rollback_fault",
        sequence=playback.sequence,
    ) is not None
    receipt = _mint_next_turn(
        rollback_fault,
        turn_id="turn_rollback_fault",
        content="repeat",
        at_ms=playback.at_ms + 1,
        semantic_act_kind=VoiceSemanticActKind.REPEAT,
    )
    monkeypatch.setattr(
        rollback_fault["calls"],
        "reserve_question",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(
        rollback_fault["speech"],
        "rollback_reservation",
        lambda reserved: False,
    )
    failed = rollback_fault["transaction"].execute_replay(
        receipt,
        content="repeat",
        command=VoiceSemanticActKind.REPEAT,
        now_ms=receipt.at_ms,
    )
    assert failed.status is CompositionStatus.TERMINAL_FAILURE
    assert failed.reason == "replay_reservation_cleanup"
    assert rollback_fault["adapter"].terminally_closed
    assert rollback_fault["calls"].phase is SilencePhase.TERMINATED
    assert rollback_fault["calls"].is_quiescent
    assert rollback_fault["speech"].reservation_batch_count(
        rollback_fault["receipt"].binding
    ) == 0
    assert all(
        not rollback_fault["speech"].is_live(act_id)
        for act_id in rollback_fault[
            "speech"
        ].act_ids_for_binding(
            rollback_fault["receipt"].binding
        )
    )


def test_tts_receipt_must_match_current_canonical_lifecycle_binding():
    harness = _harness()
    pending = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = harness[
        "transaction"
    ].authorization_receipt(act_id)
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
    )
    assert harness["lifecycle"].ingest(confirmation)
    assert harness["transaction"].accept_semantic_confirmation(
        event=confirmation,
        event_id="confirm_canonical_tts",
        sequence=confirmation.sequence,
    )
    text_digest = harness[
        "speech"
    ].authorized_text_digest(act_id)
    assert text_digest is not None
    canonical_tts = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(
            text_digest=text_digest,
            audio_id="audio_canonical",
        ),
    )
    assert harness["lifecycle"].ingest(canonical_tts)
    forged = replace(
        canonical_tts,
        payload=VoicePayload(
            text_digest=text_digest,
            audio_id="audio_forged",
        ),
    )
    assert not harness["transaction"].accept_tts_binding(
        event=forged
    )
    terminal = harness["transaction"]._outcomes[
        pending.receipt_id
    ]
    assert terminal.status is CompositionStatus.TERMINAL_FAILURE
    assert terminal.reason == "tts_binding_validation_failed"
    assert harness["adapter"].terminally_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert not harness["speech"].is_live(act_id)


def test_transaction_abort_preserves_failed_batch_retirement_for_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    pending = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    queued = _mint_next_turn(
        harness,
        turn_id="turn_queued_before_abort",
        content="queued",
        at_ms=12,
    )
    assert queued is not None
    assert harness["receipts"].unconsumed_receipt_count == 1
    original_retire = harness[
        "transaction"
    ].coordinator.retire_batch
    attempts = 0

    def transient_retirement_failure(reserved):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return original_retire(reserved)

    monkeypatch.setattr(
        harness["transaction"].coordinator,
        "retire_batch",
        transient_retirement_failure,
    )

    assert not harness["transaction"].abort(at_ms=13)
    assert harness["transaction"].pending_response_count == 1
    assert harness["speech"].reservation_batch_count(
        harness["receipt"].binding
    ) == 1
    assert not harness["speech"].is_live(
        pending.act_ids[0]
    )
    assert harness["transaction"].abort(at_ms=14)
    assert attempts == 2
    terminal = harness["transaction"]._outcomes[
        pending.receipt_id
    ]
    assert terminal.status is CompositionStatus.TERMINAL_FAILURE
    assert terminal.reason == "transaction_aborted"
    assert harness["transaction"].pending_response_count == 0
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness["speech"].reservation_batch_count(
        harness["receipt"].binding
    ) == 0
    assert all(
        not harness["speech"].is_live(act_id)
        for act_id in harness["speech"].act_ids_for_binding(
            harness["receipt"].binding
        )
    )
    assert harness["adapter"].terminally_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["calls"].is_quiescent


def test_arm_c_timeout_terminalizes_owned_question_speech_and_call_authority():
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert pending.status is CompositionStatus.RESPONSE_PENDING
    assert pending.act_kinds == (VoiceSemanticActKind.QUESTION,)
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    deadline_ms = authorization.at_ms + 100
    timed_out = harness["adapter"].timer_fired(now_ms=deadline_ms)
    intent = timed_out.timeout_intents[0]
    other_adapter = ManualNativeAdapter(
        binding=_binding(),
        limits=_limits(),
        generation_timeout_ms=100,
    )
    assert (
        transaction.materialize_timeout(
            intent=intent,
            authority=other_adapter,
            at_ms=deadline_ms,
        )
        is None
    )
    assert (
        harness["adapter"].materialize_timeout(
            intent,
            lifecycle=harness["lifecycle"],
            at_ms=deadline_ms,
            owner=object(),
        )
        is None
    )
    assert harness["adapter"].authorizes_timeout(
        intent,
        now_ms=deadline_ms,
    )

    event = harness["adapter"].materialize_timeout(
        intent,
        lifecycle=harness["lifecycle"],
        at_ms=deadline_ms,
        owner=transaction,
    )

    assert event is not None
    result = transaction._outcomes[pending.receipt_id]
    assert event.kind is VoiceEventKind.ACT_TIMED_OUT
    assert harness["lifecycle"].accepts_act_timeout(event)
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.phase is CompositionPhase.TERMINAL
    assert result.reason == "act_timed_out"
    assert transaction.authorization_receipt(act_id) is None
    assert act_id not in transaction._pending_by_act
    assert harness["speech"].is_cancelled(act_id)
    assert not harness["speech"].is_live(act_id)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["calls"].is_quiescent
    assert transaction.coordinator._reserved_questions == {}
    assert harness["adapter"].terminally_closed
    assert harness["adapter"]._pending_timeout_receipts == {}

    replay = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail("replay called extractor"),
        now_ms=deadline_ms + 1,
    )
    assert replay is result


def test_arm_c_external_terminalization_immediately_compensates_owned_response():
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    deadline_ms = authorization.at_ms + 100
    intent = harness["adapter"].timer_fired(
        now_ms=deadline_ms
    ).timeout_intents[0]

    with pytest.raises(TypeError):
        harness["adapter"].terminalize_permit_admission(
            owner=transaction,
        )
    assert transaction.authorization_receipt(act_id) is authorization
    assert harness["speech"].is_live(act_id)
    assert harness["calls"].phase is SilencePhase.QUESTION_RESERVED

    harness["adapter"].terminalize_permit_admission()

    result = transaction._outcomes[pending.receipt_id]
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.reason == "timeout_authority_terminalized"
    assert transaction.authorization_receipt(act_id) is None
    assert transaction._pending_by_act == {}
    assert harness["speech"].is_cancelled(act_id)
    assert not harness["speech"].is_live(act_id)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["calls"].is_quiescent
    assert transaction.coordinator._reserved_questions == {}
    assert harness["adapter"].terminally_closed
    assert (
        transaction.materialize_timeout(
            intent=intent,
            authority=harness["adapter"],
            at_ms=deadline_ms,
        )
        is None
    )


def test_arm_c_private_timeout_commit_still_runs_mandatory_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    deadline_ms = authorization.at_ms + 100
    intent = harness["adapter"].timer_fired(
        now_ms=deadline_ms
    ).timeout_intents[0]
    monkeypatch.setattr(
        transaction,
        "_terminalize_owned_timeout_authority",
        lambda reason, at_ms: False,
    )

    event = harness["adapter"]._commit_timeout_from_owner(
        intent,
        lifecycle=harness["lifecycle"],
        at_ms=deadline_ms,
        owner=transaction,
    )

    assert event is not None
    assert harness["lifecycle"].accepts_act_timeout(event)
    result = transaction._outcomes[pending.receipt_id]
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.reason == "act_timed_out"
    assert transaction._pending_by_act == {}
    assert harness["speech"].is_cancelled(act_id)
    assert not harness["speech"].is_live(act_id)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["calls"].is_quiescent
    assert harness["adapter"].terminally_closed


def test_arm_c_foreign_adapter_cannot_bind_or_terminalize_transaction():
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    foreign_binding = _binding(epoch=2)
    foreign_adapter = ManualNativeAdapter(
        binding=foreign_binding,
        limits=_limits(),
        generation_timeout_ms=100,
    )
    foreign_lifecycle = VoiceLifecycle(binding=foreign_binding)
    assert foreign_adapter.bind_canonical_lifecycle(
        foreign_lifecycle
    )

    assert not foreign_adapter.bind_timeout_materializer(transaction)
    foreign_adapter.terminalize_permit_admission()

    assert foreign_adapter.terminally_closed
    assert transaction.authorization_receipt(act_id) is authorization
    assert act_id in transaction._pending_by_act
    assert harness["speech"].is_live(act_id)
    assert harness["calls"].phase is SilencePhase.QUESTION_RESERVED
    assert not harness["calls"].is_quiescent
    assert not harness["adapter"].terminally_closed


@pytest.mark.parametrize("reentry_point", ("authorization", "ingest"))
def test_arm_c_timeout_defers_same_thread_external_terminalization_without_orphan(
    monkeypatch: pytest.MonkeyPatch,
    reentry_point: str,
):
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    deadline_ms = authorization.at_ms + 100
    intent = harness["adapter"].timer_fired(
        now_ms=deadline_ms
    ).timeout_intents[0]
    terminal_state_during_callback: list[bool] = []

    if reentry_point == "authorization":
        original_authorizes = harness["adapter"].authorizes_timeout

        def authorizes(candidate, *, now_ms):
            harness["adapter"].terminalize_permit_admission()
            terminal_state_during_callback.append(
                harness["adapter"].terminally_closed
            )
            return original_authorizes(candidate, now_ms=now_ms)

        monkeypatch.setattr(
            harness["adapter"],
            "authorizes_timeout",
            authorizes,
        )
    else:
        original_ingest = harness["lifecycle"].ingest

        def ingest(event):
            if event.kind is VoiceEventKind.ACT_TIMED_OUT:
                harness["adapter"].terminalize_permit_admission()
                terminal_state_during_callback.append(
                    harness["adapter"].terminally_closed
                )
            return original_ingest(event)

        monkeypatch.setattr(harness["lifecycle"], "ingest", ingest)

    committed = transaction.materialize_timeout(
        intent=intent,
        authority=harness["adapter"],
        at_ms=deadline_ms,
    )

    assert committed is not None
    event, result = committed
    assert terminal_state_during_callback == [False]
    assert harness["lifecycle"].accepts_act_timeout(event)
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.reason == "timeout_authority_terminalized"
    assert transaction._pending_by_act == {}
    assert harness["speech"].is_cancelled(act_id)
    assert harness["calls"].is_quiescent
    assert harness["adapter"].terminally_closed
    assert harness["adapter"]._pending_timeouts == {}
    assert harness["adapter"]._pending_timeout_receipts == {}


def test_arm_c_timeout_serializes_cross_thread_terminalization_without_orphan(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    deadline_ms = authorization.at_ms + 100
    intent = harness["adapter"].timer_fired(
        now_ms=deadline_ms
    ).timeout_intents[0]
    ingest_entered = threading.Event()
    release_ingest = threading.Event()
    terminal_returned = threading.Event()
    original_ingest = harness["lifecycle"].ingest

    def pause_timeout_ingest(event):
        if event.kind is VoiceEventKind.ACT_TIMED_OUT:
            ingest_entered.set()
            assert release_ingest.wait(timeout=2)
        return original_ingest(event)

    monkeypatch.setattr(
        harness["lifecycle"],
        "ingest",
        pause_timeout_ingest,
    )
    committed: list[tuple[VoiceEvent, object] | None] = []
    failures: list[BaseException] = []

    def materialize() -> None:
        try:
            committed.append(
                transaction.materialize_timeout(
                    intent=intent,
                    authority=harness["adapter"],
                    at_ms=deadline_ms,
                )
            )
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    def terminalize() -> None:
        harness["adapter"].terminalize_permit_admission()
        terminal_returned.set()

    materialize_thread = threading.Thread(target=materialize)
    terminal_thread = threading.Thread(target=terminalize)
    materialize_thread.start()
    assert ingest_entered.wait(timeout=2)
    terminal_thread.start()
    assert not terminal_returned.wait(timeout=0.05)
    release_ingest.set()
    materialize_thread.join(timeout=2)
    terminal_thread.join(timeout=2)

    assert not failures
    assert not materialize_thread.is_alive()
    assert not terminal_thread.is_alive()
    assert len(committed) == 1
    assert committed[0] is not None
    event, result = committed[0]
    assert harness["lifecycle"].accepts_act_timeout(event)
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.reason in {
        "act_timed_out",
        "timeout_authority_terminalized",
    }
    assert terminal_returned.is_set()
    assert transaction._pending_by_act == {}
    assert harness["speech"].is_cancelled(act_id)
    assert harness["calls"].is_quiescent
    assert harness["adapter"].terminally_closed
    assert harness["adapter"]._pending_timeouts == {}
    assert harness["adapter"]._pending_timeout_receipts == {}


def test_arm_c_timeout_after_playout_retires_adapter_and_composition_state():
    harness = _harness(adapter_arm="C")
    transaction = harness["transaction"]
    pending = transaction.execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = pending.act_ids[0]
    authorization = transaction.authorization_receipt(act_id)
    assert authorization is not None
    adapter = harness["adapter"]
    lifecycle = harness["lifecycle"]
    started_context = _manual_context_after(
        lifecycle,
        authorization,
        at_ms=authorization.at_ms + 1,
    )
    started = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            started_context,
        )
    )
    assert started.accepted
    assert lifecycle.ingest(started.events[0])
    frame_context = _manual_context_after(
        lifecycle,
        authorization,
        at_ms=started_context.at_ms + 1,
    )
    frame = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.AUDIO_FRAME,
            frame_context,
            payload=VoicePayload(
                ordinal=0,
                duration_ms=20,
                audio_id="audio_1",
            ),
            frame_digest="0" * 64,
        )
    )
    assert frame.accepted
    assert lifecycle.ingest(frame.events[0])
    confirmation = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
    )
    assert lifecycle.ingest(confirmation)
    assert transaction.accept_semantic_confirmation(
        event=confirmation,
        event_id="confirmation_timeout_playout",
        sequence=confirmation.sequence,
    )
    tts_context = _manual_context_after(
        lifecycle,
        authorization,
        at_ms=confirmation.at_ms + 1,
    )
    tts = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.TTS_BOUND,
            tts_context,
            payload=VoicePayload(
                text_digest="a" * 64,
                audio_id="audio_1",
            ),
        )
    )
    assert tts.accepted
    assert lifecycle.ingest(tts.events[0])
    playout_payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    playout_context = _manual_context_after(
        lifecycle,
        authorization,
        at_ms=tts_context.at_ms + 1,
    )
    playout = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.PLAYOUT_BOUND,
            playout_context,
            payload=playout_payload,
        )
    )
    assert playout.accepted
    assert lifecycle.ingest(playout.events[0])
    deadline_ms = started_context.at_ms + 100
    intent = adapter.timer_fired(
        now_ms=deadline_ms
    ).timeout_intents[0]

    committed = transaction.materialize_timeout(
        intent=intent,
        authority=adapter,
        at_ms=deadline_ms,
    )

    assert committed is not None
    event, result = committed
    assert event.payload == playout_payload
    assert lifecycle.accepts_act_timeout(event)
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert transaction._pending_by_act == {}
    key = adapter.permit_key(playout_context)
    assert adapter.terminally_closed
    assert key not in adapter._failed
    assert key not in adapter._audio_seen
    assert key not in adapter._audio_ids
    assert key not in adapter._last_frame_ordinals
    assert key not in adapter._tts_bindings
    assert key not in adapter._playout_bindings
    assert adapter._pending_timeout_receipts == {}
    assert harness["speech"].is_cancelled(act_id)
    assert harness["calls"].is_quiescent


def test_newer_final_turn_retires_pending_permit_and_terminalizes_old_outcome():
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    old_act_id = first.act_ids[0]

    next_content = "synthetic corrected caller content"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=20,
    )
    second = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=_backend(
            fields={
                "service_action": "replace",
                "service_object": "furnace",
                "urgency": "routine",
            }
        ),
        now_ms=21,
    )
    assert second.status is CompositionStatus.RESPONSE_PENDING
    assert harness["speech"].is_cancelled(old_act_id)
    assert old_act_id in harness["lifecycle"]._act_terminals
    assert harness["state"].current_state().asked_slots == set()
    assert harness["calls"].phase is SilencePhase.QUESTION_RESERVED

    old_replay = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail("superseded replay called extractor"),
        now_ms=22,
    )
    assert old_replay.status is CompositionStatus.SUPERSEDED
    assert old_replay.reason == "newer_turn"


def test_canonical_disconnect_retires_pending_response_without_erasing_facts():
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    act_id = first.act_ids[0]
    authorization = harness[
        "transaction"
    ].authorization_receipt(act_id)
    assert authorization is not None
    before = harness["state"].current_state()
    sequence, at_ms = harness["lifecycle"].next_position(
        at_ms=12
    )
    disconnected = harness["adapter"].handle(
        ChainedSignal(
            ChainedSignalKind.SESSION_DISCONNECTED,
            _context(
                sequence=sequence,
                at_ms=at_ms,
                turn_id="disconnect_turn",
            ),
        )
    )
    assert disconnected.accepted
    event = disconnected.events[0]
    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["lifecycle"].ingest(event)
    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(
        event=replace(
            event,
            input_turn_id="cloned_disconnect",
        )
    )

    assert harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["transaction"].pending_response_count == 0
    assert harness["calls"].is_quiescent
    assert not harness["adapter"].has_permit(authorization)
    assert not harness["speech"].is_live(act_id)
    assert harness["state"].current_state() == before
    replay = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail(
            "disconnect replay called extractor"
        ),
        now_ms=13,
    )
    assert replay.status is CompositionStatus.TERMINAL_FAILURE
    assert replay.reason == "session_disconnected"


@pytest.mark.parametrize("arm", ["A", "B1", "B2", "C"])
def test_canonical_disconnect_tombstones_queued_receipt_before_release(
    arm: str,
):
    harness = _harness(adapter_arm=arm)
    queued_content = "second synthetic caller turn"
    queued_receipt = _mint_next_turn(
        harness,
        turn_id="turn_queued",
        content=queued_content,
        at_ms=11,
    )
    assert harness["receipts"].unconsumed_receipt_count == 2
    event = _disconnect_session(harness, at_ms=12)
    assert not harness["receipts"].retire_disconnected_session(
        event=replace(
            event,
            input_turn_id="cloned_disconnect",
        ),
        owner=harness["transaction"],
    )
    assert harness["receipts"].unconsumed_receipt_count == 2

    assert harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["receipts"].unconsumed_receipt_count == 0
    rejected = harness["transaction"].execute(
        queued_receipt,
        content=queued_content,
        backend=lambda request: pytest.fail(
            "retired queued receipt called extractor"
        ),
        now_ms=13,
    )
    assert rejected.status is CompositionStatus.REJECTED


@pytest.mark.parametrize("arm", ["A", "B1", "B2", "C"])
def test_disconnect_after_observed_question_retires_timer_and_speech(
    arm: str,
):
    harness = _harness(adapter_arm=arm)
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    assert first.act_kinds == (VoiceSemanticActKind.QUESTION,)
    act_id = first.act_ids[0]
    playback = _playback_after_transport(harness, act_id)
    observed = harness["transaction"].observe_playback(
        event=playback,
        event_id=f"observed_{arm}",
        sequence=playback.sequence,
    )
    assert observed is not None
    assert observed.status is CompositionStatus.RESPONSE_OBSERVED
    assert harness["transaction"].pending_response_count == 0
    assert harness["calls"].phase is SilencePhase.FIRST_ARMED
    assert harness["speech"].is_live(act_id)
    asked_slots = harness["state"].current_state().asked_slots
    timer_action_id = harness["calls"]._timer_action_id()
    timer_revision = harness["calls"].revision

    event = _disconnect_session(harness, at_ms=12)
    assert harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness["transaction"].pending_response_count == 0
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["calls"].is_quiescent
    assert not harness["speech"].is_live(act_id)
    assert harness["state"].current_state().asked_slots == asked_slots
    sequence, at_ms = harness["calls"].next_position(at_ms=100)
    assert harness["calls"].timer_fired(
        binding=harness["transaction"].binding,
        event_id=f"stale_timer_{arm}",
        sequence=sequence,
        action_id=timer_action_id,
        revision=timer_revision,
        now_ms=at_ms,
    ) == ()


def test_disconnect_receipt_retirement_fault_hard_terminalizes_live_receipts(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    sequence, at_ms = harness["lifecycle"].next_position(
        at_ms=12
    )
    disconnected = harness["adapter"].handle(
        ChainedSignal(
            ChainedSignalKind.SESSION_DISCONNECTED,
            _context(
                sequence=sequence,
                at_ms=at_ms,
                turn_id="disconnect_turn",
            ),
        )
    )
    assert disconnected.accepted
    event = disconnected.events[0]
    assert harness["lifecycle"].ingest(event)
    monkeypatch.setattr(
        harness["receipts"],
        "retire_disconnected_session",
        lambda **kwargs: False,
    )

    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness["adapter"].terminally_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED


def test_disconnect_cleanup_fault_hard_terminalizes_pending_response(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    sequence, at_ms = harness["lifecycle"].next_position(
        at_ms=12
    )
    disconnected = harness["adapter"].handle(
        ChainedSignal(
            ChainedSignalKind.SESSION_DISCONNECTED,
            _context(
                sequence=sequence,
                at_ms=at_ms,
                turn_id="disconnect_turn",
            ),
        )
    )
    assert disconnected.accepted
    event = disconnected.events[0]
    assert harness["lifecycle"].ingest(event)
    monkeypatch.setattr(
        harness["adapter"],
        "permit_was_revoked",
        lambda authorization: False,
    )

    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["adapter"].terminally_closed
    assert harness["transaction"].pending_response_count == 0
    assert harness["calls"].phase is SilencePhase.TERMINATED
    replay = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail(
            "failed disconnect replay called extractor"
        ),
        now_ms=13,
    )
    assert replay.status is CompositionStatus.TERMINAL_FAILURE
    assert replay.reason == "disconnect_compensation_failed"


def test_disconnect_cancel_fault_hard_terminalizes_speech_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    act_id = first.act_ids[0]
    sequence, at_ms = harness["lifecycle"].next_position(
        at_ms=12
    )
    disconnected = harness["adapter"].handle(
        ChainedSignal(
            ChainedSignalKind.SESSION_DISCONNECTED,
            _context(
                sequence=sequence,
                at_ms=at_ms,
                turn_id="disconnect_turn",
            ),
        )
    )
    assert disconnected.accepted
    event = disconnected.events[0]
    assert harness["lifecycle"].ingest(event)
    monkeypatch.setattr(
        harness["speech"],
        "cancel",
        lambda *args, **kwargs: False,
    )

    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["adapter"].terminally_closed
    assert harness["transaction"].pending_response_count == 0
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["speech"].is_cancelled(act_id)
    assert not harness["speech"].is_live(act_id)


def test_disconnect_binding_cleanup_fault_falls_back_to_owned_speech_acts(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    act_id = first.act_ids[0]
    playback = _playback_after_transport(harness, act_id)
    observed = harness["transaction"].observe_playback(
        event=playback,
        event_id="observed_before_fault",
        sequence=playback.sequence,
    )
    assert observed is not None
    assert observed.status is CompositionStatus.RESPONSE_OBSERVED
    assert harness["speech"].is_live(act_id)
    event = _disconnect_session(harness, at_ms=12)
    monkeypatch.setattr(
        harness["speech"],
        "hard_terminalize_binding",
        lambda binding: False,
    )

    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert not harness["speech"].is_live(act_id)


def test_disconnect_binding_cleanup_fault_survives_outcome_pruning(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    act_id = first.act_ids[0]
    playback = _playback_after_transport(harness, act_id)
    observed = harness["transaction"].observe_playback(
        event=playback,
        event_id="observed_before_prune",
        sequence=playback.sequence,
    )
    assert observed is not None
    assert observed.status is CompositionStatus.RESPONSE_OBSERVED
    assert harness["transaction"].retained_outcome_count == 1
    rejected = harness["transaction"].execute(
        None,
        content="invalid receipt",
        backend=lambda request: pytest.fail(
            "outcome prune called extractor"
        ),
        now_ms=1_111,
    )
    assert rejected.status is CompositionStatus.REJECTED
    assert harness["transaction"].retained_outcome_count == 0
    assert harness["speech"].is_live(act_id)
    event = _disconnect_session(harness, at_ms=1_112)
    monkeypatch.setattr(
        harness["speech"],
        "hard_terminalize_binding",
        lambda binding: False,
    )

    assert not harness[
        "transaction"
    ].terminalize_disconnected_session(event=event)
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["calls"].is_quiescent
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness["transaction"].pending_response_count == 0
    assert harness["speech"].is_cancelled(act_id)
    assert not harness["speech"].is_live(act_id)


def test_versioned_intake_store_preserves_explicit_handoff_version():
    state = IntakeState.new(call_sid="call_1")
    store = VersionedIntakeStore(
        binding=_binding(),
        initial_state=state,
        initial_version=7,
    )
    assert store.version == 7
    assert store.current_state() == state
    for invalid in (True, -1):
        with pytest.raises(
            ValueError,
            match="intake store input",
        ):
            VersionedIntakeStore(
                binding=_binding(),
                initial_state=state,
                initial_version=invalid,
            )


def test_preregistered_playback_inference_marks_question_only_after_transport_delay():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = result.act_ids[0]
    transport = _confirm_and_resolve_transport(harness, act_id)
    assert harness["state"].current_state().asked_slots == set()
    assert not harness["lifecycle"].pending_question_active

    assert (
        harness["transaction"].infer_playback(
            act_id=act_id,
            event_id="inference_early",
            sequence=transport.sequence + 1,
            at_ms=transport.at_ms,
            inference_id="inference_early",
            transport_id=transport.payload.playout_id or "",
        )
        is None
    )
    observed = harness["transaction"].infer_playback(
        act_id=act_id,
        event_id="inference_1",
        sequence=transport.sequence + 1,
        at_ms=transport.at_ms + 1,
        inference_id="inference_1",
        transport_id=transport.payload.playout_id or "",
    )
    assert observed is not None
    assert observed.status is CompositionStatus.RESPONSE_OBSERVED
    assert harness["state"].current_state().asked_slots == {"job_complexity"}
    assert harness["calls"].phase is SilencePhase.FIRST_ARMED
    assert not harness["lifecycle"].pending_question_active


def test_inferred_playback_rejects_after_transport_terminal_failure():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = result.act_ids[0]
    transport = _confirm_and_resolve_transport(harness, act_id)
    authorization = harness[
        "transaction"
    ].authorization_receipt(act_id)
    assert authorization is not None
    failed = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.ACT_FAILED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=transport.payload,
    )
    assert harness["lifecycle"].ingest(failed)

    assert (
        harness["transaction"].infer_playback(
            act_id=act_id,
            event_id="inference_after_failure",
            sequence=failed.sequence + 1,
            at_ms=failed.at_ms + 1,
            inference_id="inference_after_failure",
            transport_id=transport.payload.playout_id or "",
        )
        is None
    )
    assert harness["state"].current_state().asked_slots == set()
    assert harness["calls"].phase is SilencePhase.QUESTION_CONFIRMED


def test_recoverable_extraction_failure_uses_one_fixed_repair_without_state_mutation():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            confidences={
                "intent": 0.2,
                "service_action": 0.95,
                "service_object": 0.95,
            }
        ),
        now_ms=11,
    )
    assert result.status is CompositionStatus.REPAIR_PENDING
    assert result.act_kinds == (VoiceSemanticActKind.REPAIR,)
    assert harness["state"].version == 0
    assert harness["state"].current_state().known_facts == []
    assert harness["state"].current_state().asked_slots == set()


def test_repair_budget_is_global_to_call_epoch_and_requires_typed_closure():
    harness = _harness()
    low_confidence = _backend(
        confidences={
            "intent": 0.2,
            "service_action": 0.95,
            "service_object": 0.95,
        }
    )
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=low_confidence,
        now_ms=11,
    )
    assert first.status is CompositionStatus.REPAIR_PENDING

    next_content = "second unclear caller turn"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=20,
    )
    second = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=low_confidence,
        now_ms=21,
    )
    assert second.status is CompositionStatus.CLOSURE_REQUIRED
    assert second.reason == "call_repair_exhausted"
    assert second.act_ids == ()
    assert harness["calls"].phase is SilencePhase.IDLE
    assert harness["adapter"].retained_permit_count == 1


@pytest.mark.parametrize("locale", ["en", "es", "pt", "zh"])
def test_localized_emergency_assets_receive_locale_specific_safety_authority(
    locale: str,
):
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            fields={
                "language": locale,
                "intent": "emergency",
                "urgency": "emergency",
            }
        ),
        now_ms=11,
    )
    assert result.status is CompositionStatus.RESPONSE_PENDING
    assert result.act_kinds == (
        VoiceSemanticActKind.SAFETY,
        VoiceSemanticActKind.QUESTION,
    )
    assert harness["state"].current_state().language == locale


def test_unknown_reviewed_locale_fails_closed_after_facts_commit():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            fields={
                "language": "fr",
                "intent": "service_request",
            }
        ),
        now_ms=11,
    )
    assert result.status is (
        CompositionStatus.LANGUAGE_CHOICE_REQUIRED
    )
    assert result.reason == "unlisted_language"
    assert result.act_ids == ()


@pytest.mark.parametrize(
    ("locale", "supported"),
    (
        ("en", True),
        ("es-MX", True),
        ("pt-BR", True),
        ("zh-Hans", True),
        (None, False),
        ("", False),
        ("unknown", False),
        ("und", False),
        ("fr-FR", False),
    ),
)
def test_materializer_locale_authority_is_strict(
    locale: object,
    supported: bool,
):
    assert FixedProposalMaterializer.supports_locale(locale) is supported


@pytest.mark.parametrize(
    "fields",
    (
        {"intent": "service_request"},
        {"language": "unknown", "intent": "service_request"},
        {"language": "und", "intent": "service_request"},
    ),
)
def test_missing_unknown_and_und_language_require_choice(
    fields: dict[str, object],
):
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(fields=fields),
        now_ms=11,
    )

    assert result.status is CompositionStatus.LANGUAGE_CHOICE_REQUIRED
    assert result.reason == "unlisted_language"
    assert result.act_ids == ()
    assert harness["speech"].latest_replay_source(
        harness["lifecycle"].binding
    ) is None


@pytest.mark.parametrize(
    ("arm", "detected_locale", "content"),
    (
        (
            "A",
            "en",
            "Fixture caller says Spanish in English and needs a furnace repair.",
        ),
        (
            "B1",
            "es",
            "La persona dice español y necesita reparar la calefacción.",
        ),
        (
            "B2",
            "zh",
            "来电者用中文说普通话，并需要维修暖气。",
        ),
        (
            "C",
            "en",
            "Fixture caller says Mandarin in English and needs a furnace repair.",
        ),
    ),
)
def test_language_choice_recovers_through_purpose_sealed_pair_for_every_arm(
    arm: str,
    detected_locale: str,
    content: str,
):
    harness = _harness(adapter_arm=arm)
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None
    pair, _ = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="language_recovery_turn",
        content=content,
        detected_locale=detected_locale,
        at_ms=deadline,
    )
    assert pair is not None
    receipt, admission = pair
    assert type(receipt) is LanguageRecoveryFinalTurnReceipt
    assert receipt.purpose is AdmissionPurpose.LANGUAGE_RECOVERY
    assert harness["receipts"].unconsumed_receipt_count == 1

    ordinary_rejection = harness["transaction"].execute(
        receipt,
        content=content,
        backend=_backend(),
        now_ms=receipt.at_ms,
    )
    assert ordinary_rejection.status is CompositionStatus.REJECTED
    assert harness["receipts"].unconsumed_receipt_count == 1
    assert language_choice.phase is (
        LanguageChoicePhase.RECOVERY_PENDING
    )

    recovered = harness[
        "transaction"
    ].execute_language_recovery(
        receipt,
        admission,
        language_choice=language_choice,
        content=content,
        backend=_backend(
            fields={
                "language": detected_locale,
                "intent": "service_request",
                "service_action": "repair",
                "service_object": "furnace",
            }
        ),
        now_ms=receipt.at_ms,
    )

    assert recovered.status is CompositionStatus.RESPONSE_PENDING
    assert language_choice.phase is LanguageChoicePhase.RECOVERED
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert (
        harness["state"].current_state().language
        == detected_locale
    )
    assert harness["speech"].latest_replay_source(
        harness["lifecycle"].binding
    ) is None


def test_final_ingested_before_prompt_authorization_is_not_recovery_input():
    harness = _harness(adapter_arm="B1")
    trigger = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            fields={
                "language": "fr",
                "intent": "service_request",
            }
        ),
        now_ms=11,
    )
    assert trigger.status is CompositionStatus.LANGUAGE_CHOICE_REQUIRED
    stale_content = "Fixture final arrives before prompt authorization."
    sequence, at_ms = harness["lifecycle"].next_position(at_ms=12)
    context = _context(
        sequence=sequence,
        at_ms=at_ms,
        turn_id="pre_prompt_final",
        act_id="input_pre_prompt_final",
    )
    stale_result = harness["adapter"].handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            context,
            CandidateUsage(),
            VoicePayload(
                text_digest=final_turn_content_digest(
                    stale_content
                )
            ),
        )
    )
    assert stale_result.accepted
    stale_event = stale_result.events[0]
    assert harness["lifecycle"].ingest(stale_event)
    language_choice = OfflineLanguageChoiceLifecycle(
        binding=harness["lifecycle"].binding,
        speech=harness["speech"],
    )
    pending = harness["transaction"].prepare_language_choice(
        receipt=harness["receipt"],
        trigger=trigger,
        language_choice=language_choice,
        proposal=materialize_language_choice(
            state_version=trigger.state_version
        ),
    )
    assert pending.status is CompositionStatus.LANGUAGE_CHOICE_PENDING

    assert harness["receipts"].mint_language_recovery(
        adapter=harness["adapter"],
        lifecycle=harness["lifecycle"],
        language_choice=language_choice,
        result=stale_result,
        event=stale_event,
        content=stale_content,
        detected_locale="en",
        now_ms=stale_event.at_ms,
        ttl_ms=100,
    ) is None
    assert language_choice.phase is LanguageChoicePhase.PRESENTING
    assert harness["receipts"].unconsumed_receipt_count == 0


def test_language_playback_defers_to_earlier_retained_caller_activity():
    harness = _harness(adapter_arm="B1")
    trigger = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            fields={
                "language": "fr",
                "intent": "service_request",
            }
        ),
        now_ms=11,
    )
    language_choice = OfflineLanguageChoiceLifecycle(
        binding=harness["lifecycle"].binding,
        speech=harness["speech"],
    )
    pending_result = harness["transaction"].prepare_language_choice(
        receipt=harness["receipt"],
        trigger=trigger,
        language_choice=language_choice,
        proposal=materialize_language_choice(
            state_version=trigger.state_version
        ),
    )
    act_id = pending_result.act_ids[0]
    transport = _confirm_and_resolve_transport(harness, act_id)
    lifecycle = harness["lifecycle"]
    sequence, at_ms = lifecycle.next_position(
        at_ms=transport.at_ms + 1
    )
    caller_activity = VoiceEvent(
        schema_version=VOICE_SCHEMA_VERSION,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        sensitivity=VoiceSensitivity.OPERATIONAL,
        binding=lifecycle.binding,
        sequence=sequence,
        at_ms=at_ms,
        input_turn_id="interposed_language_turn",
        generation_id="interposed_language_generation",
        semantic_act_id="interposed_language_input",
        semantic_act_kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
        payload=VoicePayload(),
    )
    assert lifecycle.ingest(caller_activity)
    authorization = harness[
        "transaction"
    ].authorization_receipt(act_id)
    assert authorization is not None
    playback = _event_after(
        lifecycle,
        authorization,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=transport.payload,
    )
    assert lifecycle.ingest(playback)

    deferred = harness["transaction"].observe_playback(
        event=playback,
        event_id=f"deferred_playback_{playback.sequence}",
        sequence=playback.sequence,
    )

    assert deferred is pending_result
    assert deferred.status is CompositionStatus.LANGUAGE_CHOICE_PENDING
    assert language_choice.phase is LanguageChoicePhase.PRESENTING
    assert language_choice.observed_segment_count == 0
    assert harness["transaction"].adapter.has_permit(authorization)
    assert language_choice.accept_activity_started(
        event=caller_activity,
        lifecycle=lifecycle,
    )
    assert language_choice.phase is LanguageChoicePhase.ACTIVITY_OPEN
    assert all(
        not harness["speech"].is_live(item)
        for item in pending_result.act_ids
    )


def test_language_recovery_content_mismatch_tombstones_both_halves():
    harness = _harness()
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None
    content = "Fixture caller responds in English."
    pair, _ = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="content_bound_recovery",
        content=content,
        detected_locale="en",
        at_ms=deadline,
    )
    assert pair is not None
    receipt, admission = pair

    rejected = harness[
        "transaction"
    ].execute_language_recovery(
        receipt,
        admission,
        language_choice=language_choice,
        content=f"{content} drift",
        backend=_backend(),
        now_ms=receipt.at_ms,
    )

    assert rejected.status is CompositionStatus.REJECTED
    assert language_choice.phase is LanguageChoicePhase.TERMINAL
    assert language_choice.pending_pair_count == 0
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness[
        "transaction"
    ].execute_language_recovery(
        receipt,
        admission,
        language_choice=language_choice,
        content=content,
        backend=_backend(),
        now_ms=receipt.at_ms + 1,
    ).status is CompositionStatus.REJECTED


@pytest.mark.parametrize("extracted_locale", ("pt", "zh", None))
def test_recovery_extraction_must_match_candidate_final_locale(
    extracted_locale: str | None,
):
    harness = _harness()
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None
    content = "Fixture candidate final is classified as English."
    pair, _ = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="locale_bound_recovery",
        content=content,
        detected_locale="en",
        at_ms=deadline,
    )
    assert pair is not None
    receipt, admission = pair
    fields: dict[str, object] = {
        "intent": "service_request",
        "service_action": "repair",
        "service_object": "furnace",
    }
    if extracted_locale is not None:
        fields["language"] = extracted_locale

    rejected = harness[
        "transaction"
    ].execute_language_recovery(
        receipt,
        admission,
        language_choice=language_choice,
        content=content,
        backend=_backend(fields=fields),
        now_ms=receipt.at_ms,
    )

    assert rejected.status is CompositionStatus.TERMINAL_FAILURE
    assert rejected.reason == "language_recovery_locale_mismatch"
    assert rejected.phase is CompositionPhase.EXTRACTION_TERMINAL
    assert language_choice.phase is LanguageChoicePhase.TERMINAL
    assert language_choice.pending_pair_count == 0
    assert harness["state"].current_state().language == "fr"
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness["transaction"].pending_response_count == 0
    assert harness["adapter"].terminally_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED


def test_language_recovery_pair_is_consumed_once_under_concurrency():
    harness = _harness()
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None
    content = "Fixture caller responds in Spanish."
    pair, _ = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="concurrent_language_recovery",
        content=content,
        detected_locale="es",
        at_ms=deadline,
    )
    assert pair is not None
    receipt, admission = pair
    backend_calls = 0
    call_lock = threading.Lock()

    def count_backend():
        nonlocal backend_calls
        with call_lock:
            backend_calls += 1

    def execute():
        return harness[
            "transaction"
        ].execute_language_recovery(
            receipt,
            admission,
            language_choice=language_choice,
            content=content,
            backend=_backend(
                fields={
                    "language": "es",
                    "intent": "service_request",
                },
                effect=count_backend,
            ),
            now_ms=receipt.at_ms,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            future.result()
            for future in (
                executor.submit(execute),
                executor.submit(execute),
            )
        )

    assert results[0] is results[1]
    assert backend_calls == 1
    assert language_choice.phase is LanguageChoicePhase.RECOVERED
    assert harness["receipts"].unconsumed_receipt_count == 0


def test_language_recovery_binding_failure_leaves_zero_live_receipts(
    monkeypatch: pytest.MonkeyPatch,
):
    harness = _harness()
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None
    original = (
        OfflineLanguageChoiceLifecycle.bind_recovery_receipt
    )

    def fail_publication(self, receipt):
        assert original(self, receipt) is not None
        self.tombstone_pending_pair()

    monkeypatch.setattr(
        OfflineLanguageChoiceLifecycle,
        "bind_recovery_receipt",
        fail_publication,
    )

    pair, _ = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="failed_pair_publication",
        content="Fixture caller response cannot publish authority.",
        detected_locale="en",
        at_ms=deadline,
    )

    assert pair is None
    assert language_choice.phase is LanguageChoicePhase.TERMINAL
    assert language_choice.pending_pair_count == 0
    assert harness["receipts"].unconsumed_receipt_count == 0


@pytest.mark.parametrize(
    "detected_locale",
    ("pt", "fr_fr", "ar_msa", "ambiguous", None),
)
def test_language_choice_unqualified_final_has_no_recovery_authority(
    detected_locale,
):
    harness = _harness()
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None

    pair, _ = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="unqualified_language_turn",
        content="Fixture caller remains outside the qualified set.",
        detected_locale=detected_locale,
        at_ms=deadline,
    )

    assert pair is None
    assert language_choice.phase is LanguageChoicePhase.TERMINAL
    assert language_choice.pending_pair_count == 0
    assert harness["receipts"].unconsumed_receipt_count == 0
    assert harness["state"].current_state().language == "fr"


def test_language_choice_exhaustion_receipt_requires_exact_sealed_inventory():
    harness = _harness()
    language_choice, _ = _prepare_language_window(harness)
    deadline = language_choice.response_deadline_ms
    assert deadline is not None

    pair, final = _mint_language_recovery(
        harness,
        language_choice=language_choice,
        turn_id="exhausted_language_turn",
        content="Fixture caller remains outside the qualified set.",
        detected_locale="ar_msa",
        at_ms=deadline,
    )

    assert pair is None
    assert harness["transaction"].abort(at_ms=final.at_ms + 1)
    act_ids = harness["speech"].act_ids_for_binding(_binding())
    inventory = OfflineAuthorityInventory(
        transaction_pending=harness[
            "transaction"
        ].pending_response_count,
        admission_receipts=harness[
            "receipts"
        ].unconsumed_receipt_count,
        silence_pending=0,
        speech_batches=harness[
            "speech"
        ].reservation_batch_count(_binding()),
        live_speech_acts=sum(
            harness["speech"].is_live(act_id)
            for act_id in act_ids
        ),
        queued_outbound_frames=0,
        call_quiescent=harness["calls"].is_quiescent,
        call_terminated=(
            harness["calls"].phase is SilencePhase.TERMINATED
        ),
        adapter_terminally_closed=(
            harness["adapter"].terminally_closed
        ),
    )
    assert inventory.is_sealed
    terminal = language_choice.issue_terminal_receipt(
        inventory=inventory,
        at_ms=final.at_ms + 1,
    )
    assert terminal is not None
    assert terminal.trigger is ClosureTrigger.LANGUAGE_CHOICE_EXHAUSTED
    assert not terminal.satisfies_playback_observation
    assert not terminal.satisfies_disconnect_observation


def test_direct_answer_and_question_play_in_declared_order():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
                fields={
                    "language": "en",
                    "intent": "pricing_question",
                "service_action": "replace",
                "service_object": "faucet",
            }
        ),
        now_ms=11,
    )
    assert result.act_kinds == (
        VoiceSemanticActKind.ANSWER,
        VoiceSemanticActKind.QUESTION,
    )
    answer_id, question_id = result.act_ids
    pending = harness["transaction"]._pending_by_act[question_id]
    question_authorization = harness["transaction"]._authorization(
        pending,
        question_id,
    )
    assert question_authorization is not None
    assert (
        harness["transaction"].authorization_receipt(question_id)
        is None
    )
    assert not harness["adapter"].has_permit(
        question_authorization
    )
    answer_playback = _playback_after_transport(
        harness,
        answer_id,
    )
    assert (
        harness["transaction"].authorization_receipt(question_id)
        is None
    )
    first = harness["transaction"].observe_playback(
        event=answer_playback,
        event_id="answer_playback",
        sequence=answer_playback.sequence,
    )
    assert first is not None
    assert first.status is CompositionStatus.RESPONSE_PENDING
    assert (
        harness["transaction"].authorization_receipt(question_id)
        is question_authorization
    )
    assert harness["adapter"].has_permit(question_authorization)
    question_playback = _playback_after_transport(
        harness,
        question_id,
    )
    observed = harness["transaction"].observe_playback(
        event=question_playback,
        event_id="question_playback",
        sequence=question_playback.sequence,
    )
    assert observed is not None
    assert observed.status is CompositionStatus.RESPONSE_OBSERVED
    assert harness["state"].current_state().asked_slots == {
        "job_complexity"
    }


def test_out_of_scope_request_materializes_reviewed_non_answer_decline():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
                fields={
                    "language": "en",
                    "business_scope": "out_of_scope",
                "intent": "service_request",
            }
        ),
        now_ms=11,
    )
    assert result.status is CompositionStatus.RESPONSE_PENDING
    assert result.act_kinds == (
        VoiceSemanticActKind.ACKNOWLEDGEMENT,
    )


def test_superseding_turn_during_extraction_is_silent_and_cannot_commit():
    harness = _harness()
    state = harness["state"]
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
            effect=lambda: state.admit_turn(turn_id="turn_2", sequence=2)
        ),
        now_ms=11,
    )
    assert result.status is CompositionStatus.SILENT
    assert result.reason == "late"
    assert state.version == 0
    assert harness["adapter"].retained_permit_count == 0
    assert harness["calls"].phase is SilencePhase.IDLE


def test_adapter_permit_failure_preserves_facts_and_emits_only_authorized_act_failure(
    monkeypatch,
):
    harness = _harness()
    monkeypatch.setattr(
        harness["adapter"],
        "accept_permit",
        lambda event, *, lifecycle: False,
    )
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.reason == "adapter_permit"
    assert harness["state"].version == 1
    assert harness["state"].current_state().service_object == "furnace"
    assert harness["state"].current_state().asked_slots == set()
    assert harness["calls"].phase is SilencePhase.IDLE
    terminal_ids = tuple(harness["lifecycle"]._act_terminals)
    assert len(terminal_ids) == 1
    assert harness["speech"].is_cancelled(terminal_ids[0])


def test_failed_next_permit_closes_adapter_after_first_act_playback(
    monkeypatch,
):
    harness = _harness()
    original_accept = harness["adapter"].accept_permit
    accepted: list[VoiceEvent] = []

    def fail_second(event, *, lifecycle):
        if accepted:
            return False
        outcome = original_accept(event, lifecycle=lifecycle)
        if outcome:
            accepted.append(event)
        return outcome

    monkeypatch.setattr(
        harness["adapter"],
        "accept_permit",
        fail_second,
    )
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(
                fields={
                    "language": "en",
                    "intent": "pricing_question",
                "service_action": "replace",
                "service_object": "faucet",
            }
        ),
        now_ms=11,
    )
    assert result.status is CompositionStatus.RESPONSE_PENDING
    answer_id = result.act_ids[0]
    answer_playback = _playback_after_transport(
        harness,
        answer_id,
    )
    terminal = harness["transaction"].observe_playback(
        event=answer_playback,
        event_id="answer_playback",
        sequence=answer_playback.sequence,
    )
    assert terminal is not None
    assert terminal.status is CompositionStatus.TERMINAL_FAILURE
    assert terminal.reason == "next_act_permit_failed"
    assert harness["adapter"].permit_admission_closed
    assert accepted
    assert not harness["adapter"].has_permit(accepted[0])
    assert harness["calls"].phase is SilencePhase.TERMINATED


def test_failed_act_terminalization_closes_adapter_even_after_other_mutations(
    monkeypatch,
):
    harness = _harness()
    monkeypatch.setattr(
        harness["adapter"],
        "accept_permit",
        lambda event, *, lifecycle: False,
    )
    original_ingest = harness["lifecycle"].ingest

    def fail_act_terminal(event):
        if event.kind is VoiceEventKind.ACT_FAILED:
            return False
        return original_ingest(event)

    monkeypatch.setattr(
        harness["lifecycle"],
        "ingest",
        fail_act_terminal,
    )
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert result.status is CompositionStatus.TERMINAL_FAILURE
    assert result.reason.startswith("compensation_failed_")
    assert harness["adapter"].permit_admission_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED


def test_semantic_confirmation_second_phase_failure_hard_terminalizes(
    monkeypatch,
):
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = result.act_ids[0]
    authorization = harness[
        "transaction"
    ].authorization_receipt(act_id)
    assert authorization is not None
    confirmation = _event_after(
        harness["lifecycle"],
        authorization,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        payload=VoicePayload(),
    )
    assert harness["lifecycle"].ingest(confirmation)
    monkeypatch.setattr(
        harness["adapter"],
        "accept_semantic_confirmation",
        lambda event, *, lifecycle: False,
    )
    assert not harness[
        "transaction"
    ].accept_semantic_confirmation(
        event=confirmation,
        event_id="confirmation_fault",
        sequence=confirmation.sequence,
    )
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["adapter"].terminally_closed
    assert not harness["adapter"].has_permit(authorization)
    assert not harness["speech"].is_live(act_id)
    assert (
        harness["transaction"].authorization_receipt(act_id)
        is None
    )
    replay = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail(
            "confirmation failure replay called extractor"
        ),
        now_ms=12,
    )
    assert replay.status is CompositionStatus.TERMINAL_FAILURE
    assert replay.reason == "semantic_confirmation_commit_failed"


def test_supersession_retirement_fault_terminalizes_old_and_new_turns(
    monkeypatch,
):
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    monkeypatch.setattr(
        harness["adapter"],
        "retire_permit",
        lambda event: False,
    )
    next_content = "superseding caller content"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=20,
    )
    second = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=_backend(),
        now_ms=21,
    )
    assert second.status is CompositionStatus.TERMINAL_FAILURE
    assert second.reason == "supersede_compensation_failed"
    assert harness["adapter"].permit_admission_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED
    old = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail(
            "terminal old receipt called extractor"
        ),
        now_ms=22,
    )
    assert old.status is CompositionStatus.TERMINAL_FAILURE
    assert old.reason == "supersede_compensation_failed"


def test_resumed_session_reconciles_disconnect_revoked_pending_permit():
    harness = _harness(adapter_arm="A")
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    first_act_id = first.act_ids[0]
    pending = harness["transaction"]._pending_by_act[first_act_id]
    first_authorization = harness["transaction"]._authorization(
        pending,
        first_act_id,
    )
    assert first_authorization is not None

    lifecycle = harness["lifecycle"]
    sequence, at_ms = lifecycle.next_position(at_ms=12)
    disconnected = harness["adapter"].handle(
        NativeSignal(
            NativeSignalKind.SESSION_DISCONNECTED,
            _context(
                sequence=sequence,
                at_ms=at_ms,
                turn_id="disconnect_turn",
            ),
        )
    )
    assert disconnected.accepted
    assert lifecycle.ingest(disconnected.events[0])
    assert harness["adapter"].permit_was_revoked(
        first_authorization
    )

    sequence, at_ms = lifecycle.next_position(at_ms=13)
    resumed = harness["adapter"].handle(
        NativeSignal(
            NativeSignalKind.SESSION_RESUMED,
            _context(
                sequence=sequence,
                at_ms=at_ms,
                turn_id="resume_turn",
            ),
        )
    )
    resume_event = resumed.events[0]
    assert lifecycle.ingest(resume_event)
    assert harness["adapter"].resume_permit_admission(
        result=resumed,
        event=resume_event,
        lifecycle=lifecycle,
    )

    next_content = "caller continued after resume"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=20,
    )
    second = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=_backend(),
        now_ms=21,
    )
    assert second.status is CompositionStatus.RESPONSE_PENDING
    assert not harness["adapter"].terminally_closed
    assert not harness["speech"].is_live(first_act_id)
    replay = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail(
            "superseded reconnect replay called extractor"
        ),
        now_ms=22,
    )
    assert replay.status is CompositionStatus.SUPERSEDED


def test_ordinary_or_forged_permit_retirement_is_not_disconnect_proof():
    harness = _harness()
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    assert first.status is CompositionStatus.RESPONSE_PENDING
    act_id = first.act_ids[0]
    authorization = harness["transaction"].authorization_receipt(
        act_id
    )
    assert authorization is not None
    forged = replace(
        authorization,
        sequence=authorization.sequence + 100,
        at_ms=authorization.at_ms + 100,
    )
    assert not harness["adapter"].retire_permit(forged)
    assert not harness["adapter"].has_permit(forged)
    assert harness["adapter"].has_permit(authorization)
    assert harness["adapter"].retire_permit(authorization)
    assert not harness["adapter"].permit_was_revoked(
        authorization
    )

    next_content = "new turn after ambiguous retirement"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=120,
    )
    second = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=_backend(),
        now_ms=121,
    )
    assert second.status is CompositionStatus.TERMINAL_FAILURE
    assert second.reason == "supersede_compensation_failed"
    assert harness["adapter"].terminally_closed


def test_cleanup_failure_after_real_playback_hard_terminalizes_without_dead_timer(
    monkeypatch,
):
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    playback = _playback_after_transport(
        harness,
        result.act_ids[0],
    )
    monkeypatch.setattr(
        harness["transaction"].coordinator,
        "complete_batch",
        lambda reserved: False,
    )
    terminal = harness["transaction"].observe_playback(
        event=playback,
        event_id="playback_cleanup_failure",
        sequence=playback.sequence,
    )
    assert terminal is not None
    assert terminal.status is CompositionStatus.TERMINAL_FAILURE
    assert harness["adapter"].permit_admission_closed
    assert harness["calls"].phase is SilencePhase.TERMINATED
    assert harness["state"].current_state().asked_slots == {
        "job_complexity"
    }


def test_late_playback_after_speech_cancellation_cannot_mark_asked_or_arm_timer():
    harness = _harness()
    result = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    act_id = result.act_ids[0]
    playback = _playback_after_transport(harness, act_id)
    assert harness["speech"].cancel(
        act_id,
        reason=CancellationReason.INTERRUPTION,
    )
    assert harness["transaction"].observe_playback(
        event=playback,
        event_id="late_cancelled_playback",
        sequence=playback.sequence,
    ) is None
    assert harness["state"].current_state().asked_slots == set()
    assert harness["calls"].phase is SilencePhase.QUESTION_CONFIRMED


def test_outcome_capacity_consumes_receipt_without_growing_retained_map():
    harness = _harness(max_outcomes=1)
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(outcome=BackendOutcome.CANCELLED),
        now_ms=11,
    )
    assert first.status is CompositionStatus.SILENT
    assert harness["transaction"].retained_outcome_count == 1
    next_content = "capacity caller content"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=20,
    )
    capacity = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=lambda request: pytest.fail(
            "capacity called extractor"
        ),
        now_ms=21,
    )
    assert capacity.status is CompositionStatus.REJECTED
    assert capacity.reason == "outcome_capacity"
    assert harness["transaction"].retained_outcome_count == 1
    replay = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=lambda request: pytest.fail(
            "spent capacity receipt called extractor"
        ),
        now_ms=22,
    )
    assert replay.status is CompositionStatus.REJECTED
    third_content = "capacity after expiry"
    third_receipt = _mint_next_turn(
        harness,
        turn_id="turn_3",
        content=third_content,
        at_ms=1_200,
    )
    after_expiry = harness["transaction"].execute(
        third_receipt,
        content=third_content,
        backend=_backend(outcome=BackendOutcome.CANCELLED),
        now_ms=1_201,
    )
    assert after_expiry.status is CompositionStatus.SILENT
    assert harness["transaction"].retained_outcome_count == 1


def test_outcome_capacity_still_revokes_an_older_pending_response():
    harness = _harness(max_outcomes=1)
    first = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=_backend(),
        now_ms=11,
    )
    old_act_id = first.act_ids[0]
    old_authorization = harness[
        "transaction"
    ].authorization_receipt(old_act_id)
    assert old_authorization is not None
    assert harness["adapter"].has_permit(old_authorization)
    next_content = "capacity superseding caller content"
    next_receipt = _mint_next_turn(
        harness,
        turn_id="turn_2",
        content=next_content,
        at_ms=20,
    )
    capacity = harness["transaction"].execute(
        next_receipt,
        content=next_content,
        backend=lambda request: pytest.fail(
            "capacity pending called extractor"
        ),
        now_ms=21,
    )
    assert capacity.status is CompositionStatus.REJECTED
    assert capacity.reason == "outcome_capacity"
    assert not harness["adapter"].has_permit(
        old_authorization
    )
    assert harness["speech"].is_cancelled(old_act_id)
    assert harness["calls"].phase is SilencePhase.IDLE
    assert harness["state"].is_current(
        turn_id="turn_2",
        sequence=next_receipt.sequence,
    )
    old_replay = harness["transaction"].execute(
        harness["receipt"],
        content=harness["content"],
        backend=lambda request: pytest.fail(
            "capacity old replay called extractor"
        ),
        now_ms=22,
    )
    assert old_replay.status is CompositionStatus.SUPERSEDED


def test_state_delivery_lease_blocks_cas_change_until_permit_publication(
    monkeypatch,
):
    harness = _harness()
    entered = threading.Event()
    release = threading.Event()
    original_authorize = harness["speech"].authorize_text

    def block_authorization(act_id, text):
        entered.set()
        assert release.wait(timeout=2)
        return original_authorize(act_id, text)

    monkeypatch.setattr(
        harness["speech"],
        "authorize_text",
        block_authorization,
    )
    result_box: list[object] = []
    admission_box: list[bool] = []
    execute_thread = threading.Thread(
        target=lambda: result_box.append(
            harness["transaction"].execute(
                harness["receipt"],
                content=harness["content"],
                backend=_backend(),
                now_ms=11,
            )
        )
    )
    execute_thread.start()
    assert entered.wait(timeout=2)
    cas_thread = threading.Thread(
        target=lambda: admission_box.append(
            harness["state"].admit_turn(
                turn_id="turn_2",
                sequence=2,
            )
        )
    )
    cas_thread.start()
    cas_thread.join(timeout=0.05)
    assert cas_thread.is_alive()
    release.set()
    execute_thread.join(timeout=2)
    cas_thread.join(timeout=2)
    assert not execute_thread.is_alive()
    assert not cas_thread.is_alive()
    assert result_box[0].status is CompositionStatus.RESPONSE_PENDING
    assert admission_box == [True]


def test_materializer_is_structurally_non_authorizing_and_live_routes_are_unchanged():
    materializer_path = Path("app/services/voice_bakeoff_materializer.py")
    tree = ast.parse(materializer_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "SpeechAuthorization" not in imported_names
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"__import__", "eval", "exec"}
        for node in ast.walk(tree)
    )

    for path_string, expected in _BASELINE_HASHES.items():
        assert hashlib.sha256(Path(path_string).read_bytes()).hexdigest() == expected
    target = "voice_bakeoff_turn_composition"
    for path_string in (
        "app/experiments/voice_bakeoff_app.py",
        "app/main.py",
        "app/webhooks/media_stream.py",
    ):
        assert target not in Path(path_string).read_text(encoding="utf-8")


def test_offline_composition_transitive_import_closure_has_no_runtime_capabilities():
    forbidden_roots = {
        "aiohttp",
        "anthropic",
        "firebase_admin",
        "google",
        "httpx",
        "openai",
        "requests",
        "socket",
        "subprocess",
        "twilio",
        "urllib",
        "websockets",
    }
    queue = [
        Path("app/services/voice_bakeoff_turn_composition.py"),
        Path("app/services/voice_bakeoff_materializer.py"),
    ]
    seen: set[Path] = set()
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules = (node.module or "",)
            else:
                continue
            assert all(
                module.split(".", 1)[0] not in forbidden_roots
                for module in modules
            ), (path, modules)
            for module in modules:
                if not module.startswith("app.services."):
                    continue
                dependency = Path(
                    module.replace(".", "/") + ".py"
                )
                if not dependency.exists():
                    dependency = Path(
                        module.replace(".", "/")
                    ) / "__init__.py"
                if dependency.exists():
                    queue.append(dependency)
        assert not any(
            isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id
                    in {"__import__", "eval", "exec"}
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {
                        "getenv",
                        "open",
                        "Popen",
                        "run",
                    }
                )
            )
            for node in ast.walk(tree)
        ), path
