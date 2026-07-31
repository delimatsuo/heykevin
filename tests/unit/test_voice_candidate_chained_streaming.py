"""Offline Arm B1 streamed-chain adapter tests."""

import ast
from pathlib import Path

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
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSessionBinding,
    VoiceSource,
)
from app.services.voice_speech_control import (
    SemanticAct,
    SpeechAuthorization,
    SpeechControl,
    SpeechPolicy,
    SpokenPlan,
)

LIMITS = CandidateLimits(1_024, 15_000, 2_000_000, 30_000, 200, 200)


def _binding(epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", epoch)


def _context(sequence: int, **changes: object) -> EventContext:
    values = {
        "binding": _binding(),
        "sequence": sequence,
        "at_ms": sequence,
        "input_turn_id": "turn_1",
        "generation_id": "generation_1",
        "semantic_act_id": "act_1",
        "semantic_act_kind": VoiceSemanticActKind.ANSWER,
    }
    values.update(changes)
    return EventContext(**values)


def _permit(context: EventContext):
    return context.event(
        VoiceEventKind.RESPONSE_AUTHORIZED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )


def _accept_permit(
    adapter: ChainedStreamingAdapter,
    context: EventContext,
) -> VoiceLifecycle:
    lifecycle = VoiceLifecycle(binding=context.binding)
    permit = _permit(context)
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    return lifecycle


def _adapter_after_final() -> ChainedStreamingAdapter:
    adapter = ChainedStreamingAdapter(binding=_binding(), limits=LIMITS)
    final = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(1),
            payload=VoicePayload(text_digest="a" * 64),
        )
    )
    assert final.accepted
    _accept_permit(adapter, _context(2))
    return adapter


def _adapter_after_playout() -> ChainedStreamingAdapter:
    adapter = _adapter_after_final()
    lifecycle = VoiceLifecycle(binding=_binding())
    permit = _permit(_context(2))
    assert lifecycle.ingest(permit)
    generation = (
        ChainedSignal(ChainedSignalKind.GENERATION_STARTED, _context(3)),
        ChainedSignal(
            ChainedSignalKind.TEXT_SEGMENT,
            _context(4),
            payload=VoicePayload(text_digest="a" * 64),
        ),
        ChainedSignal(ChainedSignalKind.GENERATION_COMPLETED, _context(5)),
    )
    for signal in generation:
        result = adapter.handle(signal)
        assert result.accepted
        assert lifecycle.ingest(result.events[0])
    confirmation = _context(6).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)
    assert adapter.accept_semantic_confirmation(
        confirmation,
        lifecycle=lifecycle,
    )
    tts = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.TTS_BOUND,
            _context(7),
            payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1"),
        )
    )
    assert tts.accepted
    assert lifecycle.ingest(tts.events[0])
    playout = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.PLAYOUT_BOUND,
            _context(8),
            payload=VoicePayload(
                text_digest="a" * 64,
                audio_id="audio_1",
                playout_id="playout_1",
            ),
        )
    )
    assert playout.accepted
    assert lifecycle.ingest(playout.events[0])
    return adapter


def test_candidate_final_precedes_permit_and_output():
    adapter = ChainedStreamingAdapter(binding=_binding(), limits=LIMITS)
    lifecycle = VoiceLifecycle(binding=_binding())
    permit = _permit(_context(1))
    assert lifecycle.ingest(permit)
    assert not adapter.accept_permit(permit, lifecycle=lifecycle)
    before_final = adapter.handle(
        ChainedSignal(ChainedSignalKind.GENERATION_STARTED, _context(2))
    )
    assert before_final.reason is AdapterRejectReason.PERMIT_REQUIRED
    partial = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_PARTIAL,
            _context(3),
            payload=VoicePayload(text_digest="a" * 64),
        )
    )
    final = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(4),
            payload=VoicePayload(text_digest="b" * 64),
        )
    )
    assert partial.events[0].kind is VoiceEventKind.INPUT_TURN_PARTIAL
    assert final.events[0].kind is VoiceEventKind.INPUT_TURN_FINAL
    _accept_permit(adapter, _context(5))


def test_text_tts_and_playout_preserve_speech_control_identity():
    speech = SpeechControl(
        SpeechPolicy(20, 30, ("call emergency services",), ("goodbye",))
    )
    authorization = SpeechAuthorization(
        binding=_binding(),
        turn_id="turn_1",
        authorized_kinds=(VoiceSemanticActKind.ANSWER,),
        terminal_allowed=False,
    )
    reserved = speech.reserve(
        SpokenPlan(
            "plan_1",
            (SemanticAct(VoiceSemanticActKind.ANSWER, "Yes, we can help."),),
        ),
        authorization,
    )[0]
    assert speech.authorize_text(reserved.act_id, reserved.text)
    assert speech.bind_tts(reserved.act_id, audio_id="audio_1")
    assert speech.bind_playout(reserved.act_id, playout_id="playout_1")
    audio = speech.audio_binding(reserved.act_id)
    playout = speech.playout_binding(reserved.act_id)
    assert audio is not None and playout is not None

    adapter = ChainedStreamingAdapter(binding=_binding(), limits=LIMITS)
    context = _context(1, semantic_act_id=reserved.act_id)
    final = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            context,
            payload=VoicePayload(text_digest="b" * 64),
        )
    )
    assert final.accepted
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(final.events[0])
    permit = _permit(_context(2, semantic_act_id=reserved.act_id))
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    generation_started = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.GENERATION_STARTED,
            _context(3, semantic_act_id=reserved.act_id),
        )
    )
    assert generation_started.accepted
    assert lifecycle.ingest(generation_started.events[0])
    text = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.TEXT_SEGMENT,
            _context(4, semantic_act_id=reserved.act_id),
            payload=VoicePayload(text_digest=audio.text_digest),
        )
    )
    assert lifecycle.ingest(text.events[0])
    generation_completed = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.GENERATION_COMPLETED,
            _context(5, semantic_act_id=reserved.act_id),
        )
    )
    assert generation_completed.accepted
    assert lifecycle.ingest(generation_completed.events[0])
    confirmation = _context(6, semantic_act_id=reserved.act_id).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)
    assert adapter.accept_semantic_confirmation(
        confirmation,
        lifecycle=lifecycle,
    )
    tts = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.TTS_BOUND,
            _context(7, semantic_act_id=reserved.act_id),
            payload=VoicePayload(
                text_digest=audio.text_digest,
                audio_id=audio.audio_id,
            ),
        )
    )
    bound = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.PLAYOUT_BOUND,
            _context(8, semantic_act_id=reserved.act_id),
            payload=VoicePayload(
                text_digest=playout.text_digest,
                audio_id=playout.audio_id,
                playout_id=playout.playout_id,
            ),
        )
    )
    assert text.events[0].kind is VoiceEventKind.TEXT_SEGMENT_EMITTED
    assert tts.events[0].payload.text_digest == audio.text_digest
    assert bound.events[0].payload == VoicePayload(
        text_digest=playout.text_digest,
        audio_id=playout.audio_id,
        playout_id=playout.playout_id,
    )


def test_high_risk_output_tools_and_terminal_requests_never_bypass_permit():
    adapter = ChainedStreamingAdapter(binding=_binding(), limits=LIMITS)
    tts = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.TTS_BOUND,
            _context(1, semantic_act_kind=VoiceSemanticActKind.SAFETY),
            payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1"),
        )
    )
    assert tts.reason is AdapterRejectReason.PERMIT_REQUIRED
    adapter = _adapter_after_final()
    assert adapter.handle(
        ChainedSignal(ChainedSignalKind.UNEXPECTED_TOOL_CALL, _context(3))
    ).reason is AdapterRejectReason.TOOL_DENIED
    adapter = _adapter_after_final()
    assert adapter.handle(
        ChainedSignal(ChainedSignalKind.TERMINAL_REQUESTED, _context(4))
    ).reason is AdapterRejectReason.TERMINAL_DENIED


def test_invalid_final_and_text_do_not_mutate_finality_usage_or_generation():
    adapter = ChainedStreamingAdapter(binding=_binding(), limits=LIMITS)
    invalid_final = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(1),
            usage=CandidateUsage(output_tokens=50),
        )
    )
    assert invalid_final.reason is AdapterRejectReason.INVALID_SIGNAL
    final = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.INPUT_FINAL,
            _context(2),
            payload=VoicePayload(text_digest="a" * 64),
        )
    )
    assert final.accepted
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(final.events[0])
    permit = _permit(_context(3))
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    assert adapter.handle(
        ChainedSignal(ChainedSignalKind.GENERATION_STARTED, _context(4))
    ).accepted
    invalid_text = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.TEXT_SEGMENT,
            _context(5),
            usage=CandidateUsage(output_tokens=50),
        )
    )
    assert invalid_text.reason is AdapterRejectReason.INVALID_SIGNAL
    retry = adapter.handle(
        ChainedSignal(
            ChainedSignalKind.TEXT_SEGMENT,
            _context(6),
            payload=VoicePayload(text_digest="b" * 64),
        )
    )
    assert retry.accepted


def test_transport_terminals_and_reconnect_are_typed_and_epoch_safe():
    payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    for sequence, signal_kind, event_kind in (
        (9, ChainedSignalKind.TRANSPORT_RESOLVED, VoiceEventKind.TRANSPORT_RESOLVED),
        (9, ChainedSignalKind.PLAYOUT_PARTIAL, VoiceEventKind.PLAYOUT_PARTIAL),
        (9, ChainedSignalKind.PLAYOUT_CLEARED, VoiceEventKind.PLAYOUT_CLEARED),
        (9, ChainedSignalKind.PLAYOUT_INTERRUPTED, VoiceEventKind.PLAYOUT_INTERRUPTED),
    ):
        adapter = _adapter_after_playout()
        result = adapter.handle(
            ChainedSignal(signal_kind, _context(sequence), payload=payload)
        )
        assert result.events[0].kind is event_kind
        if signal_kind is ChainedSignalKind.TRANSPORT_RESOLVED:
            assert adapter.handle(
                ChainedSignal(
                    signal_kind,
                    _context(sequence + 1),
                    payload=payload,
                )
            ).reason is AdapterRejectReason.OUT_OF_ORDER
            for offset, contradictory in enumerate(
                (
                    ChainedSignalKind.PLAYOUT_PARTIAL,
                    ChainedSignalKind.PLAYOUT_CLEARED,
                    ChainedSignalKind.PLAYOUT_INTERRUPTED,
                ),
                start=2,
            ):
                assert adapter.handle(
                    ChainedSignal(
                        contradictory,
                        _context(sequence + offset),
                        payload=payload,
                    )
                ).reason is AdapterRejectReason.OUT_OF_ORDER
        if signal_kind in {
            ChainedSignalKind.PLAYOUT_PARTIAL,
            ChainedSignalKind.PLAYOUT_CLEARED,
            ChainedSignalKind.PLAYOUT_INTERRUPTED,
        }:
            assert adapter.handle(
                ChainedSignal(
                    ChainedSignalKind.TEXT_SEGMENT,
                    _context(sequence + 1),
                    payload=VoicePayload(text_digest="b" * 64),
                )
            ).reason is AdapterRejectReason.OUT_OF_ORDER
    adapter = _adapter_after_final()
    assert adapter.handle(
        ChainedSignal(ChainedSignalKind.SESSION_DISCONNECTED, _context(7))
    ).accepted
    assert adapter.handle(
        ChainedSignal(ChainedSignalKind.SESSION_REESTABLISHED, _context(8))
    ).accepted
    assert adapter.handle(
        ChainedSignal(ChainedSignalKind.TEXT_SEGMENT, _context(9), payload=payload)
    ).reason is AdapterRejectReason.STALE_EPOCH


def test_b1_has_no_legacy_pipeline_provider_or_network_imports():
    path = Path("app/services/voice_candidates/chained_streaming.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        forbidden in imported
        for imported in imports
        for forbidden in (
            "voice_pipeline",
            "gemini_pipeline",
            "deepgram",
            "elevenlabs",
            "twilio",
            "socket",
            "requests",
        )
    )
