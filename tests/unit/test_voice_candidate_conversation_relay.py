"""Offline Arm B2 ConversationRelay adapter tests."""

import ast
from pathlib import Path

import pytest

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
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSessionBinding,
    VoiceSource,
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
    adapter: ConversationRelayAdapter,
    context: EventContext,
) -> VoiceLifecycle:
    lifecycle = VoiceLifecycle(binding=context.binding)
    permit = _permit(context)
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    return lifecycle


def _adapter_after_final() -> ConversationRelayAdapter:
    adapter = ConversationRelayAdapter(binding=_binding(), limits=LIMITS)
    assert adapter.handle(
        RelaySignal(
            RelaySignalKind.PROMPT_FINAL,
            _context(1),
            payload=VoicePayload(text_digest="a" * 64),
        )
    ).accepted
    _accept_permit(adapter, _context(2))
    return adapter


def test_b1_and_b2_candidate_final_events_have_the_same_shared_shape():
    context = _context(1)
    payload = VoicePayload(text_digest="a" * 64)
    b1 = ChainedStreamingAdapter(binding=_binding(), limits=LIMITS).handle(
        ChainedSignal(ChainedSignalKind.INPUT_FINAL, context, payload=payload)
    )
    b2 = ConversationRelayAdapter(binding=_binding(), limits=LIMITS).handle(
        RelaySignal(RelaySignalKind.PROMPT_FINAL, context, payload=payload)
    )
    assert b1.events == b2.events
    assert b1.events[0].kind is VoiceEventKind.INPUT_TURN_FINAL


def test_text_tokens_map_to_generation_not_playback():
    adapter = _adapter_after_final()
    lifecycle = VoiceLifecycle(binding=_binding())
    final = RelaySignal(
        RelaySignalKind.PROMPT_FINAL,
        _context(1),
        payload=VoicePayload(text_digest="a" * 64),
    )
    lifecycle_adapter = ConversationRelayAdapter(binding=_binding(), limits=LIMITS)
    final_result = lifecycle_adapter.handle(final)
    permit = _permit(_context(2))
    assert lifecycle.ingest(final_result.events[0])
    assert lifecycle.ingest(permit)
    assert lifecycle_adapter.accept_permit(permit, lifecycle=lifecycle)
    signals = (
        RelaySignal(RelaySignalKind.GENERATION_STARTED, _context(3)),
        RelaySignal(
            RelaySignalKind.TEXT_TOKEN,
            _context(4),
            payload=VoicePayload(text_digest="b" * 64, ordinal=1),
        ),
        RelaySignal(RelaySignalKind.LAST_TEXT_TOKEN, _context(5)),
    )
    results = [lifecycle_adapter.handle(signal) for signal in signals]
    assert [result.events[0].kind for result in results] == [
        VoiceEventKind.GENERATION_STARTED,
        VoiceEventKind.TEXT_SEGMENT_EMITTED,
        VoiceEventKind.GENERATION_COMPLETED,
    ]
    assert all(lifecycle.ingest(result.events[0]) for result in results)
    assert all(
        result.events[0].kind
        not in {
            VoiceEventKind.TRANSPORT_RESOLVED,
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        }
        for result in results
    )
    assert not adapter.supports_normal_playback_receipt


def test_interruption_and_preemption_are_transport_facts_only():
    payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    adapter = _adapter_after_final()
    interrupted = adapter.handle(
        RelaySignal(RelaySignalKind.INTERRUPTION, _context(3), payload=payload)
    )
    adapter = _adapter_after_final()
    preempted = adapter.handle(
        RelaySignal(RelaySignalKind.PLAYOUT_PREEMPTED, _context(4), payload=payload)
    )
    adapter = _adapter_after_final()
    unavailable = adapter.handle(
        RelaySignal(
            RelaySignalKind.NORMAL_PLAYBACK_RECEIPT,
            _context(5),
            payload=payload,
        )
    )
    assert interrupted.events[0].kind is VoiceEventKind.PLAYOUT_INTERRUPTED
    assert preempted.events[0].kind is VoiceEventKind.PLAYOUT_CLEARED
    assert unavailable.reason is AdapterRejectReason.UNSUPPORTED_CAPABILITY


def test_high_risk_text_waits_for_canonical_semantic_confirmation():
    adapter = ConversationRelayAdapter(binding=_binding(), limits=LIMITS)
    question_context = _context(
        1,
        semantic_act_kind=VoiceSemanticActKind.QUESTION,
    )
    final = adapter.handle(
        RelaySignal(
            RelaySignalKind.PROMPT_FINAL,
            question_context,
            payload=VoicePayload(text_digest="a" * 64),
        )
    )
    assert final.accepted
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(final.events[0])
    permit = _permit(
        _context(2, semantic_act_kind=VoiceSemanticActKind.QUESTION)
    )
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    started = adapter.handle(
        RelaySignal(
            RelaySignalKind.GENERATION_STARTED,
            _context(3, semantic_act_kind=VoiceSemanticActKind.QUESTION),
        )
    )
    assert started.accepted
    assert lifecycle.ingest(started.events[0])
    token = RelaySignal(
        RelaySignalKind.TEXT_TOKEN,
        _context(4, semantic_act_kind=VoiceSemanticActKind.QUESTION),
        payload=VoicePayload(text_digest="b" * 64),
    )
    assert adapter.handle(token).reason is AdapterRejectReason.PERMIT_REQUIRED
    confirmation = _context(
        4,
        semantic_act_kind=VoiceSemanticActKind.QUESTION,
    ).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)
    assert adapter.accept_semantic_confirmation(
        confirmation,
        lifecycle=lifecycle,
    )
    assert adapter.handle(token).accepted


def test_invalid_token_does_not_consume_usage_or_advance_generation():
    adapter = _adapter_after_final()
    assert adapter.handle(
        RelaySignal(RelaySignalKind.GENERATION_STARTED, _context(3))
    ).accepted
    invalid = adapter.handle(
        RelaySignal(
            RelaySignalKind.TEXT_TOKEN,
            _context(4),
            usage=CandidateUsage(output_tokens=50),
        )
    )
    assert invalid.reason is AdapterRejectReason.INVALID_SIGNAL
    retry = adapter.handle(
        RelaySignal(
            RelaySignalKind.TEXT_TOKEN,
            _context(5),
            payload=VoicePayload(text_digest="b" * 64),
        )
    )
    assert retry.accepted


def test_disconnect_reestablishment_requires_a_fresh_epoch_and_no_duplicate_act():
    adapter = _adapter_after_final()
    assert adapter.handle(
        RelaySignal(RelaySignalKind.SESSION_DISCONNECTED, _context(3))
    ).accepted
    assert adapter.handle(
        RelaySignal(RelaySignalKind.SESSION_REESTABLISHED, _context(4))
    ).accepted
    assert adapter.handle(
        RelaySignal(
            RelaySignalKind.TEXT_TOKEN,
            _context(5),
            payload=VoicePayload(text_digest="a" * 64),
        )
    ).reason is AdapterRejectReason.STALE_EPOCH
    lifecycle = VoiceLifecycle(binding=_binding())
    permit = _permit(_context(6))
    assert lifecycle.ingest(permit)
    assert not adapter.accept_permit(permit, lifecycle=lifecycle)


def test_tools_terminal_raw_shapes_and_provider_specific_fields_are_rejected():
    adapter = _adapter_after_final()
    assert adapter.handle(
        RelaySignal(RelaySignalKind.UNEXPECTED_TOOL_CALL, _context(3))
    ).reason is AdapterRejectReason.TOOL_DENIED
    adapter = _adapter_after_final()
    assert adapter.handle(
        RelaySignal(RelaySignalKind.TERMINAL_REQUESTED, _context(4))
    ).reason is AdapterRejectReason.TERMINAL_DENIED
    assert adapter.handle({"prompt": "raw"}).reason is AdapterRejectReason.INVALID_SIGNAL
    with pytest.raises(TypeError):
        RelaySignal(
            RelaySignalKind.PROMPT_FINAL,
            _context(5),
            provider_message={"prompt": "raw"},
        )


def test_b2_module_has_no_route_sdk_or_network_imports():
    path = Path("app/services/voice_candidates/conversation_relay.py")
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
            "fastapi",
            "twilio",
            "websocket",
            "socket",
            "requests",
            "app.main",
        )
    )
