"""Offline Arm A interface and lifecycle tests."""

import ast
import hashlib
from pathlib import Path
from threading import Event, Thread

import pytest

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


def _commit_resume(
    adapter: NativeGeminiAdapter,
    lifecycle: VoiceLifecycle,
    context: EventContext,
):
    assert adapter.bind_canonical_lifecycle(lifecycle)
    result = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, context)
    )
    assert result.accepted
    event = result.events[0]
    assert lifecycle.ingest(event)
    assert adapter.resume_permit_admission(
        result=result,
        event=event,
        lifecycle=lifecycle,
    )
    return result


def _audio_binding(payload: VoicePayload) -> VoicePayload:
    assert payload.text_digest is not None
    assert payload.audio_id is not None
    return VoicePayload(
        text_digest=payload.text_digest,
        audio_id=payload.audio_id,
    )


def _frame_signal(
    context: EventContext,
    *,
    ordinal: int = 0,
    audio_id: str = "audio_1",
    duration_ms: int = 20,
    usage: CandidateUsage | None = None,
    frame_digest: str | None = None,
) -> NativeSignal:
    if usage is None:
        usage = CandidateUsage()
    if frame_digest is None:
        material = "|".join(
            (
                context.input_turn_id,
                context.generation_id,
                context.semantic_act_id,
                str(ordinal),
                audio_id,
            )
        ).encode()
        frame_digest = hashlib.sha256(material).hexdigest()
    return NativeSignal(
        NativeSignalKind.AUDIO_FRAME,
        context,
        usage=usage,
        payload=VoicePayload(
            ordinal=ordinal,
            duration_ms=duration_ms,
            audio_id=audio_id,
        ),
        frame_digest=frame_digest,
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
    for signal in (
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(1)),
        _frame_signal(_context(1)),
    ):
        result = adapter.handle(signal)
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
            _frame_signal(_context(3)),
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
            _frame_signal(
                _context(sequence),
                ordinal=sequence - 3,
                duration_ms=1,
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
            frame_digest="0" * 64,
        )
    )
    assert invalid.reason is AdapterRejectReason.INVALID_SIGNAL
    retry = adapter.handle(
        _frame_signal(
            _context(4),
            usage=CandidateUsage(),
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
        _frame_signal(
            _context(3),
            usage=CandidateUsage(output_tokens=9),
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
    lifecycle = VoiceLifecycle(binding=_binding())
    go_away = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_GO_AWAY, _context(1))
    )
    assert go_away.accepted
    assert lifecycle.ingest(go_away.events[0])
    _commit_resume(adapter, lifecycle, _context(2))


def test_native_resume_requires_exact_canonical_two_phase_commit():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    lifecycle = VoiceLifecycle(binding=_binding())
    assert adapter.bind_canonical_lifecycle(lifecycle)
    disconnected = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_DISCONNECTED, _context(5))
    )
    assert disconnected.accepted
    assert lifecycle.ingest(disconnected.events[0])

    stale = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, _context(5))
    )
    assert stale.accepted
    stale_event = stale.events[0]
    assert not lifecycle.ingest(stale_event)
    shadow_lifecycle = VoiceLifecycle(binding=_binding())
    assert shadow_lifecycle.ingest(
        _context(4).event(
            VoiceEventKind.SESSION_DISCONNECTED,
            source=VoiceSource.PROVIDER_UNTRUSTED,
        )
    )
    assert shadow_lifecycle.ingest(stale_event)
    assert not adapter.resume_permit_admission(
        result=stale,
        event=stale_event,
        lifecycle=shadow_lifecycle,
    )
    assert adapter.permit_admission_closed
    rejected_final = adapter.handle(
        NativeSignal(
            NativeSignalKind.INPUT_FINAL,
            _context(6, input_turn_id="closed_turn"),
            payload=VoicePayload(text_digest="a" * 64),
        )
    )
    assert not rejected_final.accepted
    assert rejected_final.final_input_admission is None

    _commit_resume(adapter, lifecycle, _context(6))
    accepted_final = adapter.handle(
        NativeSignal(
            NativeSignalKind.INPUT_FINAL,
            _context(7, input_turn_id="resumed_turn"),
            payload=VoicePayload(text_digest="b" * 64),
        )
    )
    assert accepted_final.accepted
    assert accepted_final.final_input_admission is not None


def test_terminalization_is_atomic_against_pending_resume(monkeypatch):
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    lifecycle = VoiceLifecycle(binding=_binding())
    assert adapter.bind_canonical_lifecycle(lifecycle)
    disconnected = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_DISCONNECTED, _context(1))
    )
    assert lifecycle.ingest(disconnected.events[0])
    resume = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, _context(2))
    )
    resume_event = resume.events[0]
    assert lifecycle.ingest(resume_event)

    retired = Event()
    release = Event()
    original_retire = adapter._retire_permit_admission_for_fresh_epoch

    def pause_after_retirement():
        original_retire()
        retired.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(
        adapter,
        "_retire_permit_admission_for_fresh_epoch",
        pause_after_retirement,
    )
    terminal_thread = Thread(
        target=adapter.terminalize_permit_admission
    )
    resume_results: list[bool] = []
    resume_thread = Thread(
        target=lambda: resume_results.append(
            adapter.resume_permit_admission(
                result=resume,
                event=resume_event,
                lifecycle=lifecycle,
            )
        )
    )
    terminal_thread.start()
    assert retired.wait(timeout=2)
    resume_thread.start()
    resume_thread.join(timeout=0.05)
    assert resume_thread.is_alive()
    release.set()
    terminal_thread.join(timeout=2)
    resume_thread.join(timeout=2)

    assert resume_results == [False]
    assert adapter.terminally_closed
    assert adapter.permit_admission_closed
    final = adapter.handle(
        NativeSignal(
            NativeSignalKind.INPUT_FINAL,
            _context(3, input_turn_id="post_terminal_turn"),
            payload=VoicePayload(text_digest="c" * 64),
        )
    )
    assert not final.accepted
    assert final.final_input_admission is None
    permit_lifecycle = VoiceLifecycle(binding=_binding())
    permit = _permit(
        _context(3, input_turn_id="post_terminal_turn")
    )
    assert permit_lifecycle.ingest(permit)
    assert not adapter.accept_permit(
        permit,
        lifecycle=permit_lifecycle,
    )


def test_native_final_transition_is_atomic_with_terminalization(
    monkeypatch,
):
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    entered = Event()
    release = Event()
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
            results.append(
                adapter.handle(
                    NativeSignal(
                        NativeSignalKind.INPUT_FINAL,
                        _context(1, input_turn_id="atomic_final"),
                        payload=VoicePayload(text_digest="d" * 64),
                    )
                )
            )
        except BaseException as error:
            errors.append(error)

    final_thread = Thread(target=run_final)
    terminal_thread = Thread(
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
    assert adapter.handle(
        _frame_signal(_context(3))
    ).accepted
    payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_BOUND,
            _context(4),
            payload=_audio_binding(payload),
        )
    ).accepted
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_BOUND,
            _context(5),
            payload=payload,
        )
    ).accepted
    cancelled = adapter.handle(
        NativeSignal(NativeSignalKind.PROVIDER_INTERRUPTED, _context(6))
    )
    transport = adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(7),
            payload=payload,
        )
    )
    assert cancelled.events[0].kind is VoiceEventKind.GENERATION_CANCELLED
    assert transport.events[0].kind is VoiceEventKind.TRANSPORT_RESOLVED
    assert transport.events[0].kind is not VoiceEventKind.CALLER_PLAYBACK_OBSERVED


def test_native_playout_must_bind_to_generated_audio_and_exact_transport():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    unseen = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_unseen",
        playout_id="playout_1",
    )
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(3),
            payload=unseen,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER

    assert adapter.handle(
        _frame_signal(_context(4))
    ).accepted
    bound = VoicePayload(
        text_digest="b" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_BOUND,
            _context(5),
            payload=_audio_binding(bound),
        )
    ).accepted
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_BOUND,
            _context(6),
            payload=bound,
        )
    ).accepted
    resolved = adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(7),
            payload=bound,
        )
    )
    assert resolved.accepted
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(8),
            payload=bound,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER

    mismatched = VoicePayload(
        text_digest="b" * 64,
        audio_id="audio_1",
        playout_id="playout_other",
    )
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_CLEARED,
            _context(9),
            payload=mismatched,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    cleared_after_resolution = adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_CLEARED,
            _context(10),
            payload=bound,
        )
    )
    assert cleared_after_resolution.reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_CLEARED,
            _context(11),
            payload=bound,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(12),
            payload=bound,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER


def test_native_clear_before_transport_resolution_stops_bound_playout():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert adapter.handle(
        _frame_signal(_context(3))
    ).accepted
    bound = VoicePayload(
        text_digest="c" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_BOUND,
            _context(4),
            payload=_audio_binding(bound),
        )
    ).accepted
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_BOUND,
            _context(5),
            payload=bound,
        )
    ).accepted
    cleared = adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_CLEARED,
            _context(6),
            payload=bound,
        )
    )
    assert cleared.accepted
    assert cleared.events[0].kind is VoiceEventKind.PLAYOUT_CLEARED
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_CLEARED,
            _context(7),
            payload=bound,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(8),
            payload=bound,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER


def test_native_audio_identity_is_one_per_request_and_state_is_bounded():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 15_000, 2_000_000, 30_000, 200, 2),
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert adapter.handle(
        _frame_signal(_context(3), duration_ms=1)
    ).accepted
    for sequence in range(4, 100):
        result = adapter.handle(
            _frame_signal(
                _context(sequence),
                ordinal=sequence - 3,
                duration_ms=1,
            )
        )
        assert result.accepted
    assert adapter.handle(
        _frame_signal(
            _context(100),
            ordinal=97,
            audio_id="audio_2",
            duration_ms=1,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert len(adapter._audio_ids) == 1
    assert set(adapter._audio_ids.values()) == {"audio_1"}
    second_key = _context(
        101,
        input_turn_id="turn_2",
        generation_id="generation_2",
        semantic_act_id="act_2",
    )
    second_permit = _permit(second_key)
    lifecycle = VoiceLifecycle(binding=_binding())
    assert lifecycle.ingest(second_permit)
    assert adapter.accept_permit(second_permit, lifecycle=lifecycle)
    capped = adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, second_key)
    )
    assert capped.reason is AdapterRejectReason.LIMIT_EXCEEDED
    assert len(adapter._audio_ids) == 1


def test_native_frames_require_contiguous_ordinals_and_unique_digests():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    first_digest = "1" * 64
    assert adapter.handle(
        _frame_signal(
            _context(3),
            duration_ms=1,
            frame_digest=first_digest,
        )
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(4),
            ordinal=0,
            duration_ms=1,
            frame_digest="2" * 64,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(5),
            ordinal=1,
            duration_ms=1,
            frame_digest=first_digest,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(6),
            ordinal=2,
            duration_ms=1,
            frame_digest="3" * 64,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(7),
            ordinal=1,
            duration_ms=1,
            frame_digest="4" * 64,
        )
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(8),
            ordinal=2,
            duration_ms=0,
            frame_digest="5" * 64,
        )
    ).reason is AdapterRejectReason.INVALID_SIGNAL
    with pytest.raises(ValueError, match="audio frame digest"):
        NativeSignal(
            NativeSignalKind.AUDIO_FRAME,
            _context(9),
            payload=VoicePayload(
                ordinal=2,
                duration_ms=1,
                audio_id="audio_1",
            ),
        )


def test_native_frame_audio_cap_is_internal_and_fail_on_contact():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 3, 2_000_000, 30_000, 200, 2),
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert adapter.handle(
        _frame_signal(_context(3), duration_ms=1, frame_digest="1" * 64)
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(4),
            ordinal=1,
            duration_ms=1,
            frame_digest="2" * 64,
        )
    ).accepted
    capped = adapter.handle(
        _frame_signal(
            _context(5),
            ordinal=2,
            duration_ms=1,
            frame_digest="3" * 64,
        )
    )
    assert capped.reason is AdapterRejectReason.LIMIT_EXCEEDED
    assert capped.events[0].kind is VoiceEventKind.ACT_FAILED
    assert adapter._internal_audio_ms == 2
    assert set(adapter._last_frame_ordinals.values()) == {1}
    assert adapter._seen_frame_digests == {"1" * 64, "2" * 64}


def test_cap_touching_frame_must_be_ordered_before_it_can_fail_the_act():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 4, 2_000_000, 30_000, 200, 10),
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert adapter.handle(
        _frame_signal(_context(3), duration_ms=2, frame_digest="1" * 64)
    ).accepted

    second = _context(
        4,
        input_turn_id="turn_2",
        generation_id="generation_2",
        semantic_act_id="act_2",
    )
    _accept_permit(adapter, second)
    premature = adapter.handle(
        _frame_signal(
            second,
            duration_ms=2,
            audio_id="audio_2",
            frame_digest="2" * 64,
        )
    )
    assert premature.reason is AdapterRejectReason.OUT_OF_ORDER
    assert premature.events == ()
    assert adapter.permit_key(second) not in adapter._failed

    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, second)
    ).accepted
    corrected = adapter.handle(
        _frame_signal(
            _context(
                5,
                input_turn_id="turn_2",
                generation_id="generation_2",
                semantic_act_id="act_2",
            ),
            duration_ms=1,
            audio_id="audio_2",
            frame_digest="3" * 64,
        )
    )
    assert corrected.accepted


def test_cap_touching_frame_must_not_hide_usage_regression_or_poison_retry():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 5, 2_000_000, 30_000, 200, 10),
    )
    context = _context(1)
    _accept_permit(adapter, context)
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.GENERATION_STARTED,
            _context(2),
            usage=CandidateUsage(audio_ms=1),
        )
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(3),
            duration_ms=2,
            usage=CandidateUsage(audio_ms=2),
            frame_digest="1" * 64,
        )
    ).accepted

    regressed = adapter.handle(
        _frame_signal(
            _context(4),
            ordinal=1,
            duration_ms=3,
            usage=CandidateUsage(audio_ms=1),
            frame_digest="2" * 64,
        )
    )
    assert regressed.reason is AdapterRejectReason.OUT_OF_ORDER
    assert regressed.events == ()
    assert adapter.permit_key(context) not in adapter._failed

    corrected = adapter.handle(
        _frame_signal(
            _context(5),
            ordinal=1,
            duration_ms=1,
            usage=CandidateUsage(audio_ms=3),
            frame_digest="3" * 64,
        )
    )
    assert corrected.accepted


def test_native_rejects_repeated_opaque_frame_identity_across_canonical_keys():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    digest = "a" * 64
    assert adapter.handle(
        _frame_signal(_context(3), duration_ms=1, frame_digest=digest)
    ).accepted

    second = _context(
        4,
        input_turn_id="turn_2",
        generation_id="generation_2",
        semantic_act_id="act_2",
    )
    lifecycle = VoiceLifecycle(binding=_binding())
    permit = _permit(second)
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, second)
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(
                5,
                input_turn_id="turn_2",
                generation_id="generation_2",
                semantic_act_id="act_2",
            ),
            duration_ms=1,
            audio_id="audio_2",
            frame_digest=digest,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER


def test_native_cross_key_transport_and_terminal_unbound_state_fail_closed():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    first = _context(1)
    _accept_permit(adapter, first)
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(3),
            audio_id="audio_shared",
        )
    ).accepted

    second = _context(
        4,
        input_turn_id="turn_2",
        generation_id="generation_2",
        semantic_act_id="act_2",
    )
    lifecycle = VoiceLifecycle(binding=_binding())
    second_permit = _permit(second)
    assert lifecycle.ingest(second_permit)
    assert adapter.accept_permit(second_permit, lifecycle=lifecycle)
    injected = VoicePayload(
        text_digest="e" * 64,
        audio_id="audio_shared",
        playout_id="playout_2",
    )
    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_BOUND,
            second,
            payload=injected,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER

    interrupted = adapter.handle(
        NativeSignal(NativeSignalKind.PROVIDER_INTERRUPTED, _context(5))
    )
    assert interrupted.accepted
    assert adapter._audio_ids == {}
    assert adapter._playout_records == {}

    third_adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(third_adapter, _context(1))
    assert third_adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert third_adapter.handle(
        _frame_signal(_context(3))
    ).accepted
    assert third_adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_COMPLETED, _context(4))
    ).accepted
    assert third_adapter.handle(
        NativeSignal(NativeSignalKind.TURN_COMPLETED, _context(5))
    ).accepted
    assert third_adapter._audio_ids == {}
    assert third_adapter._playout_records == {}


def test_native_resumable_events_revoke_active_state_and_retain_tombstones():
    for resumable in (
        NativeSignalKind.SESSION_DISCONNECTED,
        NativeSignalKind.SESSION_GO_AWAY,
    ):
        adapter = NativeGeminiAdapter(
            binding=_binding(),
            mode=NativeMode.MANUAL_GATED,
            limits=RUNAWAY_ONLY_LIMITS,
        )
        _accept_permit(adapter, _context(1))
        assert adapter.handle(
            NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
        ).accepted
        assert adapter.handle(
            _frame_signal(_context(3))
        ).accepted
        bound = VoicePayload(
            text_digest="d" * 64,
            audio_id="audio_1",
            playout_id="playout_1",
        )
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.AUDIO_BOUND,
                _context(4),
                payload=_audio_binding(bound),
            )
        ).accepted
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.PLAYOUT_BOUND,
                _context(5),
                payload=bound,
            )
        ).accepted
        assert adapter.handle(NativeSignal(resumable, _context(6))).accepted
        assert adapter._audio_ids == {}
        assert adapter._audio_bindings == {}
        assert adapter._playout_records == {}
        assert set(adapter._last_frame_ordinals.values()) == {0}
        assert len(adapter._seen_frame_digests) == 1
        assert adapter._internal_audio_ms == 20
        assert adapter._generation_state == {}
        assert adapter._permitted == set()
        assert adapter.permit_key(_context(1)) in adapter._retired_permit_keys


def test_native_reestablished_event_evicts_terminal_adapter_tombstones():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    assert adapter.handle(_frame_signal(_context(3))).accepted
    assert adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_REESTABLISHED, _context(4))
    ).accepted
    assert adapter._last_frame_ordinals == {}
    assert adapter._seen_frame_digests == set()
    assert adapter._internal_audio_ms == 0
    assert adapter._retired_permit_keys == set()
    assert adapter._generation_state == {}
    assert adapter._permitted == set()
    assert adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, _context(5))
    ).reason is AdapterRejectReason.STALE_EPOCH


def test_native_resume_cannot_reuse_permit_frame_identity_or_audio_budget():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 3, 2_000_000, 30_000, 200, 10),
    )
    first = _context(1)
    lifecycle = _accept_permit(adapter, first)
    first_permit = _permit(first)
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(2))
    ).accepted
    identity = "a" * 64
    assert adapter.handle(
        _frame_signal(
            _context(3),
            duration_ms=2,
            frame_digest=identity,
        )
    ).accepted
    go_away = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_GO_AWAY, _context(4))
    )
    assert go_away.accepted
    assert lifecycle.ingest(go_away.events[0])
    assert not adapter.accept_permit(first_permit, lifecycle=lifecycle)

    second = _context(
        6,
        input_turn_id="turn_2",
        generation_id="generation_2",
        semantic_act_id="act_2",
    )
    second_lifecycle = VoiceLifecycle(binding=_binding())
    second_permit = _permit(second)
    assert second_lifecycle.ingest(second_permit)
    assert not adapter.accept_permit(
        second_permit,
        lifecycle=second_lifecycle,
    )
    invalid_resume = adapter.handle(
        NativeSignal(
            NativeSignalKind.SESSION_RESUMED,
            _context(5),
            payload=VoicePayload(audio_id="forbidden"),
        )
    )
    assert invalid_resume.reason is AdapterRejectReason.INVALID_SIGNAL
    assert adapter._resume_required
    assert not adapter.accept_permit(
        second_permit,
        lifecycle=second_lifecycle,
    )
    _commit_resume(adapter, lifecycle, _context(5))
    assert not adapter._resume_required
    assert not adapter.accept_permit(
        second_permit,
        lifecycle=second_lifecycle,
    )
    assert lifecycle.ingest(second_permit)
    assert adapter.accept_permit(
        second_permit,
        lifecycle=lifecycle,
    )
    assert adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, second)
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(
                7,
                input_turn_id="turn_2",
                generation_id="generation_2",
                semantic_act_id="act_2",
            ),
            duration_ms=1,
            audio_id="audio_2",
            frame_digest=identity,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    capped = adapter.handle(
        _frame_signal(
            _context(
                8,
                input_turn_id="turn_2",
                generation_id="generation_2",
                semantic_act_id="act_2",
            ),
            duration_ms=1,
            audio_id="audio_2",
            frame_digest="b" * 64,
        )
    )
    assert capped.reason is AdapterRejectReason.LIMIT_EXCEEDED
    assert capped.events[0].kind is VoiceEventKind.ACT_FAILED


def test_native_active_and_retired_permit_keys_share_a_fixed_request_cap():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=CandidateLimits(1_024, 100, 2_000_000, 30_000, 200, 2),
    )
    accepted = 0
    for index in range(100):
        context = _context(
            index + 1,
            input_turn_id=f"turn_{index}",
            generation_id=f"generation_{index}",
            semantic_act_id=f"act_{index}",
        )
        lifecycle = VoiceLifecycle(binding=_binding())
        permit = _permit(context)
        assert lifecycle.ingest(permit)
        accepted += adapter.accept_permit(permit, lifecycle=lifecycle)

    assert accepted == adapter.limits.request_count
    assert len(adapter._permitted) == adapter.limits.request_count
    assert adapter.request_count == 0
    terminal = _context(
        101,
        input_turn_id="terminal_turn",
        generation_id="terminal_generation",
        semantic_act_id="terminal_act",
    )
    session_lifecycle = VoiceLifecycle(binding=_binding())
    go_away = adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_GO_AWAY, terminal)
    )
    assert go_away.accepted
    assert session_lifecycle.ingest(go_away.events[0])
    assert adapter._permitted == set()
    assert len(adapter._retired_permit_keys) == adapter.limits.request_count

    resumed = _context(
        102,
        input_turn_id="resume_turn",
        generation_id="resume_generation",
        semantic_act_id="resume_act",
    )
    _commit_resume(adapter, session_lifecycle, resumed)
    new_context = _context(
        103,
        input_turn_id="new_turn",
        generation_id="new_generation",
        semantic_act_id="new_act",
    )
    new_permit = _permit(new_context)
    assert session_lifecycle.ingest(new_permit)
    assert not adapter.accept_permit(
        new_permit,
        lifecycle=session_lifecycle,
    )
    assert len(adapter._retired_permit_keys) == adapter.limits.request_count

    assert adapter.handle(
        NativeSignal(
            NativeSignalKind.SESSION_REESTABLISHED,
            _context(
                104,
                input_turn_id="reestablished_turn",
                generation_id="reestablished_generation",
                semantic_act_id="reestablished_act",
            ),
        )
    ).accepted
    assert adapter._retired_permit_keys == set()
    assert not adapter.accept_permit(
        new_permit,
        lifecycle=session_lifecycle,
    )


def test_native_audio_and_playout_bindings_follow_shared_lifecycle():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    context = _context(1)
    lifecycle = _accept_permit(adapter, context)
    confirmation = _context(2).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)

    started = adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(3))
    )
    assert started.accepted
    assert lifecycle.ingest(started.events[0])
    frame = adapter.handle(
        _frame_signal(_context(4))
    )
    assert frame.accepted
    assert lifecycle.ingest(frame.events[0])

    playout = VoicePayload(
        text_digest="f" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    audio_bound = adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_BOUND,
            _context(5),
            payload=_audio_binding(playout),
        )
    )
    assert audio_bound.accepted
    assert audio_bound.events[0].kind is VoiceEventKind.TTS_BOUND
    assert lifecycle.ingest(audio_bound.events[0])
    playout_bound = adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_BOUND,
            _context(6),
            payload=playout,
        )
    )
    assert playout_bound.accepted
    assert lifecycle.ingest(playout_bound.events[0])
    cleared = adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_CLEARED,
            _context(7),
            payload=playout,
        )
    )
    assert cleared.accepted
    assert lifecycle.ingest(cleared.events[0])


def test_native_transport_resolution_follows_shared_lifecycle_without_playback_claim():
    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    lifecycle = _accept_permit(adapter, _context(1))
    assert lifecycle.ingest(
        _context(2).event(
            VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
            source=VoiceSource.LOCAL_AUTHORITATIVE,
        )
    )
    started = adapter.handle(
        NativeSignal(NativeSignalKind.GENERATION_STARTED, _context(3))
    )
    frame = adapter.handle(
        _frame_signal(_context(4))
    )
    assert lifecycle.ingest(started.events[0])
    assert lifecycle.ingest(frame.events[0])
    playout = VoicePayload(
        text_digest="e" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    audio_bound = adapter.handle(
        NativeSignal(
            NativeSignalKind.AUDIO_BOUND,
            _context(5),
            payload=_audio_binding(playout),
        )
    )
    playout_bound = adapter.handle(
        NativeSignal(
            NativeSignalKind.PLAYOUT_BOUND,
            _context(6),
            payload=playout,
        )
    )
    resolved = adapter.handle(
        NativeSignal(
            NativeSignalKind.TRANSPORT_RESOLVED,
            _context(7),
            payload=playout,
        )
    )
    assert lifecycle.ingest(audio_bound.events[0])
    assert lifecycle.ingest(playout_bound.events[0])
    assert lifecycle.ingest(resolved.events[0])
    assert resolved.events[0].kind is VoiceEventKind.TRANSPORT_RESOLVED
    assert resolved.events[0].kind is not VoiceEventKind.CALLER_PLAYBACK_OBSERVED


def test_rejected_session_terminal_still_revokes_permit_and_identity_state():
    for terminal in (
        NativeSignalKind.SESSION_DISCONNECTED,
        NativeSignalKind.SESSION_GO_AWAY,
    ):
        adapter = NativeGeminiAdapter(
            binding=_binding(),
            mode=NativeMode.MANUAL_GATED,
            limits=RUNAWAY_ONLY_LIMITS,
        )
        _accept_permit(adapter, _context(1))
        used = CandidateUsage(output_tokens=1)
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.GENERATION_STARTED,
                _context(2),
                usage=used,
            )
        ).accepted
        assert adapter.handle(
            _frame_signal(
                _context(3),
                usage=used,
            )
        ).accepted
        playout = VoicePayload(
            text_digest="a" * 64,
            audio_id="audio_1",
            playout_id="playout_1",
        )
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.AUDIO_BOUND,
                _context(4),
                usage=used,
                payload=_audio_binding(playout),
            )
        ).accepted
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.PLAYOUT_BOUND,
                _context(5),
                usage=used,
                payload=playout,
            )
        ).accepted

        rejected = adapter.handle(NativeSignal(terminal, _context(6)))
        assert rejected.reason is AdapterRejectReason.OUT_OF_ORDER
        assert adapter._permitted == set()
        assert adapter._generation_state == {}
        assert adapter._audio_ids == {}
        assert adapter._audio_bindings == {}
        assert adapter._playout_records == {}
        assert set(adapter._last_frame_ordinals.values()) == {0}
        assert len(adapter._seen_frame_digests) == 1
        assert adapter._internal_audio_ms == 20
        assert adapter.permit_key(_context(1)) in adapter._retired_permit_keys
        assert adapter.handle(
            NativeSignal(
                NativeSignalKind.TRANSPORT_RESOLVED,
                _context(7),
                payload=playout,
            )
        ).reason is AdapterRejectReason.PERMIT_REQUIRED

    adapter = NativeGeminiAdapter(
        binding=_binding(),
        mode=NativeMode.MANUAL_GATED,
        limits=RUNAWAY_ONLY_LIMITS,
    )
    _accept_permit(adapter, _context(1))
    invalid = adapter.handle(
        NativeSignal(
            NativeSignalKind.SESSION_REESTABLISHED,
            _context(2),
            payload=VoicePayload(audio_id="forbidden"),
        )
    )
    assert invalid.reason is AdapterRejectReason.INVALID_SIGNAL
    assert adapter._permitted == set()
    assert adapter.handle(
        NativeSignal(NativeSignalKind.SESSION_RESUMED, _context(3))
    ).reason is AdapterRejectReason.STALE_EPOCH


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
