"""Offline Arm C manual-turn feasibility tests."""

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_call_lifecycle import CallLifecycle
from app.services.voice_candidates import (
    AdapterRejectReason,
    CandidateLimits,
    CandidateUsage,
    EventContext,
)
from app.services.voice_candidates.manual_native import (
    ManualNativeAdapter,
    ManualNativeSignal,
    ManualNativeSignalKind,
)
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSessionBinding,
    VoiceSource,
)
from app.services.voice_speech_control import SpeechControl, SpeechPolicy

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


def _frame_signal(
    context: EventContext,
    *,
    ordinal: int = 0,
    audio_id: str = "audio_1",
    duration_ms: int = 20,
    usage: CandidateUsage | None = None,
    frame_digest: str | None = None,
) -> ManualNativeSignal:
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
    return ManualNativeSignal(
        ManualNativeSignalKind.AUDIO_FRAME,
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
    adapter: ManualNativeAdapter,
    context: EventContext,
) -> VoiceLifecycle:
    lifecycle = VoiceLifecycle(binding=context.binding)
    permit = _permit(context)
    assert lifecycle.ingest(permit)
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    return lifecycle


def _adapter() -> ManualNativeAdapter:
    return ManualNativeAdapter(
        binding=_binding(),
        limits=LIMITS,
        generation_timeout_ms=10,
    )


def _coordinator(lifecycle: VoiceLifecycle) -> VoiceBakeoffCoordinator:
    return VoiceBakeoffCoordinator(
        speech=SpeechControl(SpeechPolicy(12, 20, ("safe",), ("goodbye",))),
        calls=CallLifecycle(
            binding=_binding(),
            voice_lifecycle=lifecycle,
            first_silence_ms=10,
            second_silence_ms=20,
        ),
    )


def _finalize(adapter: ManualNativeAdapter) -> None:
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_STARTED, _context(1))
    ).accepted
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_ENDED, _context(2))
    ).accepted
    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.INPUT_FINAL,
            _context(3),
            payload=VoicePayload(text_digest="a" * 64),
        )
    ).accepted


def test_manual_activity_handles_long_pauses_and_finality_deterministically():
    adapter = _adapter()
    lifecycle = VoiceLifecycle(binding=_binding())
    signals = (
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_STARTED, _context(1)),
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_ENDED, _context(2)),
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_STARTED, _context(3)),
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_ENDED, _context(4)),
        ManualNativeSignal(
            ManualNativeSignalKind.INPUT_FINAL,
            _context(5),
            payload=VoicePayload(text_digest="a" * 64),
        ),
    )
    results = [adapter.handle(signal) for signal in signals]
    assert [result.events[0].kind for result in results] == [
        VoiceEventKind.INPUT_ACTIVITY_STARTED,
        VoiceEventKind.INPUT_ACTIVITY_ENDED,
        VoiceEventKind.INPUT_ACTIVITY_STARTED,
        VoiceEventKind.INPUT_ACTIVITY_ENDED,
        VoiceEventKind.INPUT_TURN_FINAL,
    ]
    assert all(lifecycle.ingest(result.events[0]) for result in results)


def test_manual_final_requires_a_completed_activity_cycle():
    adapter = _adapter()
    final = ManualNativeSignal(
        ManualNativeSignalKind.INPUT_FINAL,
        _context(3),
        payload=VoicePayload(text_digest="a" * 64),
    )
    assert adapter.handle(final).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_STARTED, _context(1))
    ).accepted
    assert adapter.handle(final).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.ACTIVITY_ENDED, _context(2))
    ).accepted
    assert adapter.handle(final).accepted


def test_permit_precedes_every_native_audio_frame_and_generation_maps_totally():
    adapter = _adapter()
    _finalize(adapter)
    before = adapter.handle(_frame_signal(_context(4)))
    assert before.reason is AdapterRejectReason.PERMIT_REQUIRED
    _accept_permit(adapter, _context(5))
    started = adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.GENERATION_STARTED, _context(6))
    )
    audio = adapter.handle(_frame_signal(_context(7)))
    completed = adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.GENERATION_COMPLETED, _context(8))
    )
    assert [result.events[0].kind for result in (started, audio, completed)] == [
        VoiceEventKind.GENERATION_STARTED,
        VoiceEventKind.AUDIO_FRAME_GENERATED,
        VoiceEventKind.GENERATION_COMPLETED,
    ]


def test_generation_must_begin_before_the_permit_deadline_exactly_once():
    adapter = _adapter()
    _finalize(adapter)
    lifecycle = _accept_permit(adapter, _context(4, at_ms=100))
    assert lifecycle.ingest(
        _context(10, at_ms=105).event(
            VoiceEventKind.SESSION_GO_AWAY,
            source=VoiceSource.PROVIDER_UNTRUSTED,
        )
    )
    assert adapter.timer_fired(now_ms=109).reason is AdapterRejectReason.OUT_OF_ORDER
    timed_out = adapter.timer_fired(now_ms=110)
    assert timed_out.reason is AdapterRejectReason.TIMEOUT
    assert timed_out.events == ()
    assert len(timed_out.timeout_intents) == 1
    intent = timed_out.timeout_intents[0]
    coordinator = _coordinator(lifecycle)
    assert (
        coordinator.materialize_timeout(
            intent=replace(intent),
            authority=adapter,
            at_ms=110,
        )
        is None
    )
    assert (
        coordinator.materialize_timeout(
            intent=intent,
            authority=_adapter(),
            at_ms=110,
        )
        is None
    )
    assert (
        coordinator.materialize_timeout(
            intent=intent,
            authority=adapter,
            at_ms=109,
        )
        is None
    )
    event = coordinator.materialize_timeout(
        intent=intent,
        authority=adapter,
        at_ms=110,
    )
    assert event is not None
    assert event.kind is VoiceEventKind.ACT_TIMED_OUT
    assert event.sequence == 11
    assert not adapter.accept_timeout(event, lifecycle=lifecycle)
    assert (
        coordinator.materialize_timeout(
            intent=intent,
            authority=adapter,
            at_ms=111,
        )
        is None
    )
    assert adapter.timer_fired(now_ms=111).reason is AdapterRejectReason.OUT_OF_ORDER


def test_generation_completion_timeout_is_bounded_once_and_fails_the_act():
    adapter = _adapter()
    _finalize(adapter)
    lifecycle = _accept_permit(adapter, _context(4, at_ms=99))
    started = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5, at_ms=100),
        )
    )
    assert started.accepted
    assert lifecycle.ingest(started.events[0])
    assert lifecycle.ingest(
        _context(20, at_ms=105).event(
            VoiceEventKind.SESSION_GO_AWAY,
            source=VoiceSource.PROVIDER_UNTRUSTED,
        )
    )
    assert adapter.timer_fired(now_ms=109).reason is AdapterRejectReason.OUT_OF_ORDER
    timed_out = adapter.timer_fired(now_ms=110)
    assert timed_out.reason is AdapterRejectReason.TIMEOUT
    event = _coordinator(lifecycle).materialize_timeout(
        intent=timed_out.timeout_intents[0],
        authority=adapter,
        at_ms=110,
    )
    assert event is not None and event.sequence == 21
    assert not adapter.accept_timeout(event, lifecycle=lifecycle)
    assert adapter.timer_fired(now_ms=111).reason is AdapterRejectReason.OUT_OF_ORDER


def test_invalid_audio_does_not_consume_usage_or_completion_state():
    adapter = _adapter()
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5),
        )
    ).accepted
    invalid = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.AUDIO_FRAME,
            _context(6),
            usage=CandidateUsage(output_tokens=50),
            frame_digest="a" * 64,
        )
    )
    assert invalid.reason is AdapterRejectReason.INVALID_SIGNAL
    retry = adapter.handle(_frame_signal(_context(7)))
    assert retry.accepted


def test_manual_audio_frame_identity_is_strictly_ordered_and_single_audio():
    adapter = _adapter()
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5),
        )
    ).accepted

    assert adapter.handle(
        _frame_signal(
            _context(6),
            ordinal=1,
            frame_digest="1" * 64,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(7),
            frame_digest="0" * 64,
        )
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(8),
            frame_digest="1" * 64,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(9),
            ordinal=1,
            frame_digest="0" * 64,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(10),
            ordinal=1,
            audio_id="audio_2",
            frame_digest="1" * 64,
        )
    ).reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter.handle(
        _frame_signal(
            _context(11),
            ordinal=1,
            frame_digest="1" * 64,
        )
    ).accepted

    assert set(adapter._last_frame_ordinals.values()) == {1}
    assert adapter._seen_frame_digests == {"0" * 64, "1" * 64}
    assert adapter._internal_audio_ms == 40


def test_manual_audio_frame_requires_started_generation_and_live_deadline():
    adapter = _adapter()
    _finalize(adapter)
    _accept_permit(adapter, _context(4))

    before_start = adapter.handle(
        _frame_signal(
            _context(5),
            frame_digest="0" * 64,
        )
    )
    assert before_start.reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter._audio_ids == {}
    assert adapter._last_frame_ordinals == {}
    assert adapter._seen_frame_digests == set()
    assert adapter._internal_audio_ms == 0

    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(6),
        )
    ).accepted
    at_deadline = adapter.handle(
        _frame_signal(
            _context(16),
            frame_digest="0" * 64,
        )
    )
    assert at_deadline.reason is AdapterRejectReason.TIMEOUT
    assert at_deadline.events == ()
    assert len(at_deadline.timeout_intents) == 1
    assert adapter._audio_ids == {}
    assert adapter._last_frame_ordinals == {}
    assert adapter._seen_frame_digests == set()
    assert adapter._internal_audio_ms == 0


def test_manual_tts_binding_requires_exact_generated_audio_identity():
    adapter = _adapter()
    _finalize(adapter)
    lifecycle = _accept_permit(adapter, _context(4))
    started = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5),
        )
    )
    frame = adapter.handle(
        _frame_signal(
            _context(6),
            frame_digest="0" * 64,
        )
    )
    assert lifecycle.ingest(started.events[0])
    assert lifecycle.ingest(frame.events[0])
    confirmation = _context(7).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)
    assert adapter.accept_semantic_confirmation(
        confirmation,
        lifecycle=lifecycle,
    )
    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_COMPLETED,
            _context(8),
        )
    ).accepted
    late_frame = adapter.handle(
        _frame_signal(
            _context(9),
            ordinal=1,
            audio_id="audio_2",
            frame_digest="1" * 64,
        )
    )
    assert late_frame.reason is AdapterRejectReason.OUT_OF_ORDER
    assert set(adapter._audio_ids.values()) == {"audio_1"}
    assert set(adapter._last_frame_ordinals.values()) == {0}
    assert adapter._seen_frame_digests == {"0" * 64}
    assert adapter._internal_audio_ms == 20

    mismatch = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.TTS_BOUND,
            _context(10),
            payload=VoicePayload(
                text_digest="a" * 64,
                audio_id="audio_2",
            ),
        )
    )
    assert mismatch.reason is AdapterRejectReason.OUT_OF_ORDER
    assert adapter._tts_bindings == {}
    assert set(adapter._audio_ids.values()) == {"audio_1"}

    corrected = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.TTS_BOUND,
            _context(11),
            payload=VoicePayload(
                text_digest="a" * 64,
                audio_id="audio_1",
            ),
        )
    )
    assert corrected.accepted
    assert adapter._tts_bindings
    assert set(adapter._audio_ids.values()) == {"audio_1"}


def test_manual_streaming_audio_binding_cannot_release_identity_for_switch():
    adapter = _adapter()
    _finalize(adapter)
    lifecycle = _accept_permit(adapter, _context(4))
    started = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5),
        )
    )
    first = adapter.handle(
        _frame_signal(
            _context(6),
            frame_digest="0" * 64,
        )
    )
    assert lifecycle.ingest(started.events[0])
    assert lifecycle.ingest(first.events[0])
    confirmation = _context(7).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)
    assert adapter.accept_semantic_confirmation(
        confirmation,
        lifecycle=lifecycle,
    )
    bound = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.TTS_BOUND,
            _context(8),
            payload=VoicePayload(
                text_digest="a" * 64,
                audio_id="audio_1",
            ),
        )
    )
    assert bound.accepted
    assert lifecycle.ingest(bound.events[0])
    assert set(adapter._audio_ids.values()) == {"audio_1"}

    switched = adapter.handle(
        _frame_signal(
            _context(9),
            ordinal=1,
            audio_id="audio_2",
            frame_digest="1" * 64,
        )
    )
    assert switched.reason is AdapterRejectReason.OUT_OF_ORDER
    assert set(adapter._audio_ids.values()) == {"audio_1"}
    assert set(adapter._last_frame_ordinals.values()) == {0}
    assert adapter._seen_frame_digests == {"0" * 64}
    assert adapter._internal_audio_ms == 20

    continued = adapter.handle(
        _frame_signal(
            _context(10),
            ordinal=1,
            frame_digest="1" * 64,
        )
    )
    assert continued.accepted
    assert set(adapter._audio_ids.values()) == {"audio_1"}


def test_manual_audio_cap_is_adapter_owned_and_not_usage_claimed():
    adapter = ManualNativeAdapter(
        binding=_binding(),
        limits=CandidateLimits(1_024, 3, 2_000_000, 30_000, 200, 200),
        generation_timeout_ms=10,
    )
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5),
        )
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(6),
            duration_ms=2,
            frame_digest="0" * 64,
        )
    ).accepted

    capped = adapter.handle(
        _frame_signal(
            _context(7),
            ordinal=1,
            duration_ms=1,
            frame_digest="1" * 64,
        )
    )
    assert capped.reason is AdapterRejectReason.LIMIT_EXCEEDED
    assert capped.events[0].kind is VoiceEventKind.ACT_FAILED
    assert adapter._internal_audio_ms == 2
    assert set(adapter._last_frame_ordinals.values()) == {0}
    assert adapter._seen_frame_digests == {"0" * 64}


def test_manual_frame_tombstones_survive_disconnect_until_terminal_reestablish():
    adapter = _adapter()
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5),
        )
    ).accepted
    assert adapter.handle(
        _frame_signal(
            _context(6),
            duration_ms=2,
            frame_digest="0" * 64,
        )
    ).accepted

    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.SESSION_DISCONNECTED,
            _context(7),
        )
    ).accepted
    assert adapter._audio_ids == {}
    assert adapter._audio_seen == set()
    assert set(adapter._last_frame_ordinals.values()) == {0}
    assert adapter._seen_frame_digests == {"0" * 64}
    assert adapter._internal_audio_ms == 2
    assert adapter.handle(
        _frame_signal(
            _context(8),
            ordinal=1,
            duration_ms=1,
            frame_digest="1" * 64,
        )
    ).reason is AdapterRejectReason.STALE_EPOCH

    assert adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.SESSION_REESTABLISHED,
            _context(9),
        )
    ).accepted
    assert adapter._last_frame_ordinals == {}
    assert adapter._seen_frame_digests == set()
    assert adapter._internal_audio_ms == 0


def test_manual_frame_digest_shape_and_signal_scope_are_closed():
    with pytest.raises(ValueError, match="audio frame digest"):
        ManualNativeSignal(
            ManualNativeSignalKind.AUDIO_FRAME,
            _context(1),
        )
    with pytest.raises(ValueError, match="only valid for audio frames"):
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(1),
            frame_digest="0" * 64,
        )


@pytest.mark.parametrize(
    ("terminal_signal", "terminal_event"),
    (
        (
            ManualNativeSignalKind.GENERATION_COMPLETED,
            VoiceEventKind.GENERATION_COMPLETED,
        ),
        (
            ManualNativeSignalKind.PROVIDER_INTERRUPTED,
            VoiceEventKind.GENERATION_CANCELLED,
        ),
    ),
)
def test_generation_terminal_then_exact_playout_clear_is_canonical_once(
    terminal_signal: ManualNativeSignalKind,
    terminal_event: VoiceEventKind,
):
    adapter = _adapter()
    _finalize(adapter)
    lifecycle = _accept_permit(adapter, _context(4))
    started = adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.GENERATION_STARTED, _context(5))
    )
    audio = adapter.handle(_frame_signal(_context(6)))
    assert lifecycle.ingest(started.events[0])
    assert lifecycle.ingest(audio.events[0])
    confirmation = _context(7).event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(confirmation)
    assert adapter.accept_semantic_confirmation(
        confirmation,
        lifecycle=lifecycle,
    )
    tts = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.TTS_BOUND,
            _context(8),
            payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1"),
        )
    )
    playout_payload = VoicePayload(
        text_digest="a" * 64,
        audio_id="audio_1",
        playout_id="playout_1",
    )
    playout = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.PLAYOUT_BOUND,
            _context(9),
            payload=playout_payload,
        )
    )
    assert lifecycle.ingest(tts.events[0])
    assert lifecycle.ingest(playout.events[0])
    terminal = adapter.handle(
        ManualNativeSignal(terminal_signal, _context(10))
    )
    assert terminal.events[0].kind is terminal_event
    assert lifecycle.ingest(terminal.events[0])
    cleared = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.PLAYOUT_CLEARED,
            _context(11),
            payload=playout_payload,
        )
    )
    assert cleared.accepted
    assert lifecycle.ingest(cleared.events[0])
    duplicate = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.PLAYOUT_CLEARED,
            _context(12),
            payload=playout_payload,
        )
    )
    assert duplicate.reason is AdapterRejectReason.OUT_OF_ORDER


def test_simultaneous_timeouts_receive_unique_canonical_sequences():
    adapter = _adapter()
    contexts = (
        _context(1, input_turn_id="turn_1", semantic_act_id="act_1"),
        _context(2, input_turn_id="turn_2", semantic_act_id="act_2"),
    )
    for context in contexts:
        assert adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.ACTIVITY_STARTED,
                context,
            )
        ).accepted
        assert adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.ACTIVITY_ENDED,
                context,
            )
        ).accepted
        assert adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.INPUT_FINAL,
                context,
                payload=VoicePayload(text_digest="a" * 64),
            )
        ).accepted
    lifecycle = VoiceLifecycle(binding=_binding())
    for sequence, context in enumerate(contexts, start=3):
        permit_context = _context(
            sequence,
            at_ms=100,
            input_turn_id=context.input_turn_id,
            semantic_act_id=context.semantic_act_id,
        )
        permit = _permit(permit_context)
        assert lifecycle.ingest(permit)
        assert adapter.accept_permit(permit, lifecycle=lifecycle)
    timed_out = adapter.timer_fired(now_ms=110)
    assert len(timed_out.timeout_intents) == 2
    coordinator = _coordinator(lifecycle)
    events = tuple(
        coordinator.materialize_timeout(
            intent=intent,
            authority=adapter,
            at_ms=110,
        )
        for intent in timed_out.timeout_intents
    )
    assert all(event is not None for event in events)
    assert [event.sequence for event in events if event is not None] == [5, 6]
    assert all(
        not adapter.accept_timeout(event, lifecycle=lifecycle)
        for event in events
        if event is not None
    )
    assert adapter.timer_fired(now_ms=111).reason is AdapterRejectReason.OUT_OF_ORDER


def test_late_cross_turn_and_reconnected_events_fail_closed():
    adapter = _adapter()
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    cross_turn = adapter.handle(
        ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            _context(5, input_turn_id="turn_2"),
        )
    )
    assert cross_turn.reason is AdapterRejectReason.PERMIT_REQUIRED
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.SESSION_DISCONNECTED, _context(6))
    ).accepted
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.SESSION_REESTABLISHED, _context(7))
    ).accepted
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.GENERATION_STARTED, _context(8))
    ).reason is AdapterRejectReason.STALE_EPOCH


def test_manual_config_disables_automatic_vad_tools_and_terminal_actions():
    adapter = _adapter()
    config = adapter.provider_configuration()
    assert config["automatic_activity_detection"] is False
    assert "tools" not in config
    assert "terminal_actions" not in config
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.UNEXPECTED_TOOL_CALL, _context(5))
    ).reason is AdapterRejectReason.TOOL_DENIED
    adapter = _adapter()
    _finalize(adapter)
    _accept_permit(adapter, _context(4))
    assert adapter.handle(
        ManualNativeSignal(ManualNativeSignalKind.TERMINAL_REQUESTED, _context(6))
    ).reason is AdapterRejectReason.TERMINAL_DENIED


def test_manual_native_module_has_no_sdk_network_or_live_route_imports():
    path = Path("app/services/voice_candidates/manual_native.py")
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
            "google",
            "gemini",
            "socket",
            "requests",
            "app.main",
            "media_stream",
        )
    )
