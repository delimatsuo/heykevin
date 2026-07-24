"""Offline Arm A interface and lifecycle tests."""

import ast
from pathlib import Path

from app.services.voice_candidates import (
    AdapterRejectReason,
    CandidateLimits,
    CandidateUsage,
    EventContext,
)
from app.services.voice_candidates.native_gemini import (
    BASELINE_128_LIMITS,
    RUNAWAY_ONLY_LIMITS,
    NativeGeminiAdapter,
    NativeMode,
    NativeSignal,
    NativeSignalKind,
)
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSessionBinding,
    VoiceSource,
)


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
    adapter: NativeGeminiAdapter,
    context: EventContext,
) -> VoiceLifecycle:
    lifecycle = VoiceLifecycle(binding=context.binding)
    permit = _permit(context)
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    return lifecycle


def test_manual_variant_emits_no_audio_or_generation_before_permit():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    for kind in (
        NativeSignalKind.GENERATION_STARTED,
        NativeSignalKind.AUDIO_FRAME,
    ):
        result = adapter.handle(
            NativeSignal(
                kind,
                _context(1),
                payload=(
                    VoicePayload(audio_id="audio_1")
                    if kind is NativeSignalKind.AUDIO_FRAME
                    else VoicePayload()
                ),
            )
        )
        assert not result.accepted
        assert result.reason is AdapterRejectReason.PERMIT_REQUIRED
        assert result.events == ()


def test_both_preregistered_configs_map_total_generation_lifecycle_offline():
    for limits in (BASELINE_128_LIMITS, RUNAWAY_ONLY_LIMITS):
        adapter = NativeGeminiAdapter(
            binding=_binding(),
            mode=NativeMode.MANUAL_GATED,
            limits=limits,
        )
        lifecycle = VoiceLifecycle(binding=_binding())
        permit = _permit(_context(1))
        assert lifecycle.ingest(permit)
        assert adapter.accept_permit(permit, lifecycle=lifecycle)
        signals = (
            NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2)),
            NativeSignal(
                NativeSignalKind.AUDIO_FRAME,
                _context(3),
                payload=VoicePayload(audio_id="audio_1", duration_ms=20),
            ),
            NativeSignal(NativeSignalKind.GENERATION_COMPLETED, _context(4)),
            NativeSignal(NativeSignalKind.TURN_COMPLETED, _context(5)),
        )
        mapped = [adapter.handle(signal) for signal in signals]
        assert all(result.accepted for result in mapped)
        assert [result.events[0].kind for result in mapped] == [
            VoiceEventKind.GENERATION_STARTED,
            VoiceEventKind.AUDIO_FRAME_GENERATED,
            VoiceEventKind.GENERATION_COMPLETED,
            VoiceEventKind.PROVIDER_TURN_COMPLETED,
        ]
        assert all(lifecycle.ingest(result.events[0]) for result in mapped)


def test_automatic_control_is_explicitly_nonselectable_and_bounds_fail_closed():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.AUTOMATIC_CONTROL,
        limits=BASELINE_128_LIMITS,
    )
    assert not adapter.selectable_offline
    before_permit = adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(1))
    )
    assert before_permit.reason is AdapterRejectReason.PERMIT_REQUIRED
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted

    gated = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=BASELINE_128_LIMITS,
    )
    _accept_permit(gated, _context(1))
    exceeded = gated.handle(
        NativeSignal(
            NativeSignalKind.GENERATION_STARTED,
            _context(2),
            usage=CandidateUsage(output_tokens=128),
        )
    )
    assert not exceeded.accepted
    assert exceeded.reason is AdapterRejectReason.LIMIT_EXCEEDED
    assert exceeded.events[0].kind is VoiceEventKind.ACT_FAILED
    assert gated.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(3))
    ).reason is AdapterRejectReason.OUT_OF_ORDER


def test_every_independent_runaway_bound_fails_on_contact():
    usages = (
        CandidateUsage(output_tokens=RUNAWAY_ONLY_LIMITS.output_tokens),
        CandidateUsage(audio_ms=RUNAWAY_ONLY_LIMITS.audio_ms),
        CandidateUsage(byte_count=RUNAWAY_ONLY_LIMITS.byte_count),
        CandidateUsage(wall_clock_ms=RUNAWAY_ONLY_LIMITS.wall_clock_ms),
        CandidateUsage(cost_minor_units=RUNAWAY_ONLY_LIMITS.cost_minor_units),
    )
    for usage in usages:
        adapter = NativeGeminiAdapter(
            binding=_binding(),
            mode=NativeMode.MANUAL_GATED,
            limits=RUNAWAY_ONLY_LIMITS,
        )
        _accept_permit(adapter, _context(1))
        result = adapter.handle(
            NativeSignal(
                NativeSignalKind.GENERATION_STARTED,
                _context(2),
                usage=usage,
            )
        )
        assert result.reason is AdapterRejectReason.LIMIT_EXCEEDED
        assert result.events[0].kind is VoiceEventKind.ACT_FAILED


def test_request_count_is_an_independent_fail_on_contact_bound():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 15_000, 2_000_000, 30_000, 200, 1),
    )
    _accept_permit(adapter, _context(1))
    result = adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    )
    assert result.reason is AdapterRejectReason.LIMIT_EXCEEDED
    assert result.events[0].kind is VoiceEventKind.ACT_FAILED


def test_request_count_tracks_generation_requests_not_response_frames():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 15_000, 2_000_000, 30_000, 200, 2),
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    for sequence in range(3, 8):
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.AUDIO_FRAME,
                _context(sequence),
                payload=VoicePayload(audio_id=f"audio_{sequence}"),
            )
        ).accepted
    assert adapter.request_count == 1


def test_invalid_signal_does_not_poison_usage_or_generation_retry():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    invalid = adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_FRAME,
            _context(3),
            usage=CandidateUsage(output_tokens=50),
        )
    )
    assert invalid.reason is AdapterRejectReason.INVALID_SIGNAL
    retry = adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_FRAME,
            _context(4),
            usage=CandidateUsage(),
            payload=VoicePayload(audio_id="audio_1"),
        )
    )
    assert retry.accepted


def test_canonical_permit_identity_rejects_turn_and_answer_kind_substitution():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    lifecycle = _accept_permit(adapter, _context(1))
    for context in (
        _context(2, input_turn_id="turn_2"),
        _context(3, semantic_act_kind=VoiceSemanticActKind.CLOSING),
    ):
        result = adapter.handle(
            NativeSignal(NativeSignalKind.GENERATION_STARTED, context)
        )
        assert result.reason is AdapterRejectReason.PERMIT_REQUIRED
        assert not lifecycle.ingest(
            context.event(
                VoiceEventKind.GENERATION_STARTED,
                source=VoiceSource.PROVIDER_UNTRUSTED,
            )
        )


def test_cumulative_usage_must_be_monotonic():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.GENERATION_STARTED,
            _context(2),
            usage=CandidateUsage(output_tokens=10),
        )
    ).accepted
    regressed = adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_FRAME,
            _context(3),
            usage=CandidateUsage(output_tokens=9),
            payload=VoicePayload(audio_id="audio_1"),
        )
    )
    assert regressed.reason is AdapterRejectReason.OUT_OF_ORDER


def test_tools_terminal_actions_and_unbound_raw_inputs_are_denied():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.UNEXPECTED_TOOL_CALL, _context(2))
    ).reason is AdapterRejectReason.TOOL_DENIED
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.TERMINAL_REQUESTED, _context(3))
    ).reason is AdapterRejectReason.TERMINAL_DENIED
    assert adapter.handle({"provider": "raw"}).reason is AdapterRejectReason.INVALID_SIGNAL
    config = adapter.provider_configuration()
    assert "tools" not in config
    assert "terminal_actions" not in config


def test_native_session_mapping_requires_a_fresh_adapter_after_reestablishment():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    disconnected = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_DISCONNECTED, _context(1))
    )
    restored = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_REESTABLISHED, _context(2))
    )
    assert disconnected.events[0].kind is VoiceEventKind.SESSION_DISCONNECTED
    assert restored.events[0].kind is VoiceEventKind.SESSION_REESTABLISHED
    assert adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, _context(3))
    ).reason is AdapterRejectReason.STALE_EPOCH
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    assert adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_GO_AWAY, _context(1))
    ).accepted
    assert adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, _context(2))
    ).accepted


def test_native_transport_and_interruption_facts_remain_separate():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    cancelled = adapter.handle(
        NativeSignal(NativeSignalKind.PROVIDER_INTERRUPTED, _context(3))
    )
    payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    transport = adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(4),
            payload=payload,
        )
    )
    assert cancelled.events[0].kind is VoiceEventKind.GENERATION_CANCELLED
    assert transport.events[0].kind is VoiceEventKind.TRANSPORT_RESOLVED
    assert transport.events[0].kind is not VoiceEventKind.CALLER_PLAYBACK_OBSERVED


def test_native_adapter_has_no_sdk_network_or_terminal_executor_imports():
    path = Path("app/services/voice_candidates/native_gemini.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not roots & {
        "google",
        "socket",
        "requests",
        "httpx",
        "subprocess",
        "app.webhooks",
    }
