"""Qualification for the sealed, synthetic-only offline session driver."""

from __future__ import annotations

import ast
import hashlib
import re
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import app.services.voice_bakeoff_closure as closure_module
from app.services.voice_bakeoff_closure import (
    generic_failure_text_digest,
    opt_out_text_digest,
)
from app.services.voice_bakeoff_language_choice import (
    LANGUAGE_CHOICE_DESCRIPTOR_DIGEST,
)
from app.services.voice_bakeoff_materializer import (
    FixedProposalMaterializer,
)
from app.services.voice_bakeoff_session_driver import (
    _ADAPTER_CODE_DIGESTS,
    _ADAPTER_TYPES,
    _ASSEMBLY_CODE_DIGESTS,
    _DRIVER_SOURCE_DIGEST,
    _FACADE_CODE_DIGEST,
    _FIXTURES,
    _LANGUAGE_EXHAUSTION_LOCALES,
    DriverFailure,
    OfflineClosureOutcome,
    OfflineFailureInjection,
    OfflineSessionDriver,
    OfflineSessionFacade,
    OfflineSessionLimits,
    OfflineSessionState,
    OfflineStopPosture,
    SyntheticJourney,
    TraceKind,
    _MutableFrame,
    _SyntheticObservationBackend,
)
from app.services.voice_bakeoff_turn_composition import (
    CompositionStatus,
    TurnCompositionTransaction,
)
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoiceLifecycle,
    VoiceSemanticActKind,
    VoiceSessionBinding,
)
from app.services.voice_session_auth import CandidateArm
from app.services.voice_speech_control import (
    ReplayMode,
    SpeechControl,
)

_DRIVER_PATH = Path("app/services/voice_bakeoff_session_driver.py")
_LIVE_ROUTE_PATHS = (
    Path("app/main.py"),
    Path("app/webhooks/media_stream.py"),
    Path("app/experiments/voice_bakeoff_app.py"),
)
_EXPECTED_LIVE_HASHES = {
    # Re-pinned 2026-08-20 for estimate_worker_loop registration.
    # Keep in sync with the pin in test_voice_bakeoff_turn_composition.py
    # and update only with a reviewed main.py change.
    Path("app/main.py"):
        "e8d3cfa4384ec97e7dc4f3172588ace0fb7d8bf610cd76751d160198a462da80",
    # Re-pinned 2026-08-12: authenticated media streams now load the shared,
    # default-off customer-memory and service-request context.
    Path("app/webhooks/media_stream.py"):
        "d614e95370aa91af90a754ac0f213fa312b017eceb73c72ff6958d6636455fe7",
    Path("app/experiments/voice_bakeoff_app.py"):
        "082d82e73deff2db331ba120513327f6911f41f1c9f0e9e7279e8f711df13127",
}
_REVIEWED_ASSEMBLY_SOURCE_PATHS = {
    "caller_observation_extractor":
        Path("app/services/caller_observation_extractor.py"),
    "composition":
        Path("app/services/voice_bakeoff_turn_composition.py"),
    "dialogue_planner":
        Path("app/services/dialogue_planner.py"),
    "materializer":
        Path("app/services/voice_bakeoff_materializer.py"),
    "receptionist_state":
        Path("app/services/receptionist_state.py"),
    "voice_bakeoff_coordinator":
        Path("app/services/voice_bakeoff_coordinator.py"),
    "voice_bakeoff_closure":
        Path("app/services/voice_bakeoff_closure.py"),
    "voice_bakeoff_language_choice":
        Path("app/services/voice_bakeoff_language_choice.py"),
    "voice_bakeoff_silence":
        Path("app/services/voice_bakeoff_silence.py"),
    "voice_call_lifecycle":
        Path("app/services/voice_call_lifecycle.py"),
    "voice_candidates_base":
        Path("app/services/voice_candidates/__init__.py"),
    "voice_lifecycle":
        Path("app/services/voice_lifecycle.py"),
    "voice_session_auth":
        Path("app/services/voice_session_auth.py"),
    "voice_speech_control":
        Path("app/services/voice_speech_control.py"),
}
_REVIEWED_DIRECT_SERVICE_MODULES = {
    "app.services.caller_observation_extractor",
    "app.services.receptionist_state",
    "app.services.voice_bakeoff_coordinator",
    "app.services.voice_bakeoff_closure",
    "app.services.voice_bakeoff_language_choice",
    "app.services.voice_bakeoff_materializer",
    "app.services.voice_bakeoff_silence",
    "app.services.voice_bakeoff_turn_composition",
    "app.services.voice_call_lifecycle",
    "app.services.voice_candidates",
    "app.services.voice_lifecycle",
    "app.services.voice_session_auth",
    "app.services.voice_speech_control",
}
_REVIEWED_ADAPTER_MODULES = {
    "app.services.voice_candidates.chained_streaming",
    "app.services.voice_candidates.conversation_relay",
    "app.services.voice_candidates.manual_native",
    "app.services.voice_candidates.native_gemini",
}


def _lease(
    *,
    arm: CandidateArm = CandidateArm.B1,
    journey: SyntheticJourney = SyntheticJourney.QUESTION_ONLY,
    limits: OfflineSessionLimits = OfflineSessionLimits(),  # noqa: B008
    now_ms: int = 10,
    stop_posture: OfflineStopPosture = (
        OfflineStopPosture.RESPONSE_PENDING
    ),
):
    driver = OfflineSessionDriver(limits)
    facade = driver.lease(
        arm=arm,
        journey=journey,
        now_ms=now_ms,
        stop_posture=stop_posture,
    )
    assert facade is not None
    return driver, facade


def _trace_projection(result):
    return tuple(
        (
            event.kind,
            event.semantic_act_kind,
            event.composition_status,
            event.locale,
            event.replay_mode,
            event.content_digest,
        )
        for event in result.trace
    )


@pytest.mark.parametrize("journey", tuple(SyntheticJourney))
def test_every_arm_produces_the_same_content_free_trace(
    journey: SyntheticJourney,
):
    traces = {}
    for arm in CandidateArm:
        driver, facade = _lease(arm=arm, journey=journey)
        result = driver.run(facade, now_ms=10)
        assert result is not None
        assert result.state is OfflineSessionState.CLOSED
        assert result.failure is None
        assert result.buffers_scrubbed
        traces[arm] = _trace_projection(result)

    assert len(set(traces.values())) == 1


def test_unconfirmed_or_ambiguous_opt_out_is_silent_withdrawal():
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.outbound_bytes == 0
    assert result.outbound_audio_ms == 0
    assert result.buffers_scrubbed
    kinds = tuple(event.kind for event in result.trace)
    assert TraceKind.WITHDRAWAL_CLASSIFIED in kinds
    assert TraceKind.GENERAL_AUTHORITY_CANCELLED in kinds
    assert TraceKind.CLOSURE_TEARDOWN_COMPLETE in kinds
    forbidden = {
        TraceKind.SCRIPTED_OPT_OUT_AUTHORIZED,
        TraceKind.CLOSURE_CAPABILITY_RETAINED,
        TraceKind.CLOSURE_STAGED,
        TraceKind.OFFLINE_OUTBOUND_COMMITTED,
        TraceKind.SYNTHETIC_PLAYBACK_OBSERVED,
        TraceKind.ACT_CONFIRMED,
        TraceKind.TRANSPORT_RESOLVED,
        TraceKind.PLAYBACK_OBSERVED,
    }
    assert forbidden.isdisjoint(kinds)


def test_stop_boundary_never_enters_extraction_or_input_admission(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )
    captured_content = []
    original = _SyntheticObservationBackend.__call__

    def capture(backend, request):
        captured_content.append(request.content)
        return original(backend, request)

    monkeypatch.setattr(
        _SyntheticObservationBackend,
        "__call__",
        capture,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert captured_content == [
        "Fixture caller requests a furnace repair."
    ]
    assert all(
        token not in captured_content[0].casefold()
        for token in ("stop", "opt out", "withdraw")
    )
    assert sum(
        event.kind is TraceKind.INPUT_FINAL
        for event in result.trace
    ) == 1
    assert result.outbound_frame_count == 0


@pytest.mark.parametrize(
    ("locale", "expected_digest"),
    (
        (
            "en",
            "bd79a37b7abe7e5311eb00e96fb5e2bc14b81e9e78933e930ded109e70787bff",
        ),
        (
            "es",
            "6580c58f06b53ef873c3a000c37f3a2d839df332c56b5f9e3d457896aabcc976",
        ),
        (
            "zh",
            "07a670fe45b7415e3cc6d474d9a9300e9ed670cc6b4cec322ca04636f64f2bb6",
        ),
    ),
)
def test_scripted_opt_out_commits_one_reviewed_local_frame_per_arm(
    locale: str,
    expected_digest: str,
):
    traces = {}
    for arm in CandidateArm:
        driver = OfflineSessionDriver()
        facade = driver.lease(
            arm=arm,
            journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
            now_ms=10,
            scripted_locale=locale,
        )
        assert facade is not None
        confirmation = driver.confirm_scripted_opt_out(
            facade,
            now_ms=10,
        )
        assert confirmation is not None

        result = driver.run(facade, now_ms=10)

        assert result is not None
        assert result.state is OfflineSessionState.CLOSED
        assert result.failure is None
        assert result.outbound_frame_count == 1
        assert result.outbound_bytes == 160
        assert result.outbound_audio_ms == 20
        assert result.buffers_scrubbed
        commits = tuple(
            event
            for event in result.trace
            if event.kind is TraceKind.OFFLINE_OUTBOUND_COMMITTED
        )
        assert len(commits) == 1
        assert commits[0].locale == locale
        assert commits[0].content_digest == expected_digest
        assert commits[0].semantic_act_kind is VoiceSemanticActKind.OPT_OUT
        assert opt_out_text_digest(locale) == expected_digest
        assert sum(
            event.kind is TraceKind.CLOSURE_CAPABILITY_RETAINED
            for event in result.trace
        ) == 1
        assert sum(
            event.kind is TraceKind.SYNTHETIC_PLAYBACK_OBSERVED
            for event in result.trace
        ) == 1
        assert not any(
            event.kind is TraceKind.TRANSPORT_RESOLVED
            for event in result.trace
        )
        traces[arm] = _trace_projection(result)

    assert len(set(traces.values())) == 1


@pytest.mark.parametrize("posture", tuple(OfflineStopPosture))
@pytest.mark.parametrize("scripted", (False, True))
def test_stop_postures_cancel_stale_authority_before_exact_delta(
    posture: OfflineStopPosture,
    scripted: bool,
):
    driver = OfflineSessionDriver()
    facade = driver.lease(
        arm=CandidateArm.B1,
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        now_ms=10,
        scripted_locale="en",
        stop_posture=posture,
    )
    assert facade is not None
    if scripted:
        assert driver.confirm_scripted_opt_out(
            facade,
            now_ms=10,
        ) is not None

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    expected_baseline = int(
        posture is not OfflineStopPosture.RESPONSE_PENDING
    )
    expected_delta = int(scripted)
    assert (
        result.pre_stop_outbound_frame_count
        == expected_baseline
    )
    assert result.post_stop_outbound_frame_delta == expected_delta
    assert result.post_stop_outbound_ordinals == (
        (0,) if scripted else ()
    )
    assert result.outbound_frame_count == expected_delta
    assert result.outbound_bytes == expected_delta * 160
    assert result.outbound_audio_ms == expected_delta * 20
    assert result.buffers_scrubbed
    kinds = tuple(event.kind for event in result.trace)
    activity_index = max(
        index
        for index, kind in enumerate(kinds)
        if kind is TraceKind.PARTICIPANT_ACTIVITY
    )
    cancelled_index = kinds.index(
        TraceKind.GENERAL_AUTHORITY_CANCELLED,
        activity_index,
    )
    cleared_index = kinds.index(
        TraceKind.GENERAL_OUTBOUND_CLEARED,
        activity_index,
    )
    assert activity_index < cancelled_index < cleared_index
    assert {
        TraceKind.REPLAY_OBSERVED,
        TraceKind.SILENCE_TIMER_ARMED,
        TraceKind.TRANSPORT_RESOLVED,
        TraceKind.PLAYBACK_OBSERVED,
    }.isdisjoint(kinds[activity_index + 1:])
    if scripted:
        assert TraceKind.WITHDRAWAL_CLASSIFIED not in kinds
        assert sum(
            kind is TraceKind.OFFLINE_OUTBOUND_COMMITTED
            for kind in kinds
        ) == 1
    else:
        assert kinds.count(TraceKind.WITHDRAWAL_CLASSIFIED) == 1
        assert TraceKind.OFFLINE_OUTBOUND_COMMITTED not in kinds


@pytest.mark.parametrize(
    "posture",
    tuple(
        posture
        for posture in OfflineStopPosture
        if posture is not OfflineStopPosture.RESPONSE_PENDING
    ),
)
def test_nonzero_general_queue_is_zeroized_and_cleared_not_hidden(
    posture: OfflineStopPosture,
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        stop_posture=posture,
    )
    retained_payloads: list[bytearray] = []
    original = driver._clear_general_outbound

    def capture_and_clear(*, checkpoint):
        retained_payloads.extend(
            frame.payload for frame in driver._outbound_frames
        )
        return original(checkpoint=checkpoint)

    monkeypatch.setattr(
        driver,
        "_clear_general_outbound",
        capture_and_clear,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.pre_stop_outbound_frame_count == 1
    assert result.post_stop_outbound_frame_delta == 0
    assert result.outbound_frame_count == 0
    assert len(retained_payloads) == 1
    assert all(not any(payload) for payload in retained_payloads)
    assert not driver._outbound_frames


def test_portuguese_scripted_closure_is_unapproved_and_fails_silent():
    driver = OfflineSessionDriver()
    facade = driver.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        now_ms=10,
        scripted_locale="pt",
    )
    assert facade is not None

    assert driver.confirm_scripted_opt_out(
        facade,
        now_ms=10,
    ) is None
    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.buffers_scrubbed
    assert opt_out_text_digest("pt") is None
    assert TraceKind.WITHDRAWAL_CLASSIFIED in {
        event.kind for event in result.trace
    }
    assert not any(
        event.kind is TraceKind.OFFLINE_OUTBOUND_COMMITTED
        for event in result.trace
    )


def test_independent_withdrawal_overrides_live_scripted_confirmation():
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )
    assert driver.confirm_scripted_opt_out(
        facade,
        now_ms=10,
    ) is not None
    assert driver.withdraw(facade)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.buffers_scrubbed
    kinds = tuple(event.kind for event in result.trace)
    assert TraceKind.WITHDRAWAL_CLASSIFIED in kinds
    assert TraceKind.OFFLINE_OUTBOUND_COMMITTED not in kinds


def test_withdrawal_is_reachable_while_run_holds_driver_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )
    assert driver.confirm_scripted_opt_out(
        facade,
        now_ms=10,
    ) is not None
    commit_build_started = threading.Event()
    allow_commit_build = threading.Event()
    original_token = closure_module._token

    def blocked_token(domain: bytes, *parts: str) -> str:
        if domain == closure_module._COMMIT_DOMAIN:
            commit_build_started.set()
            assert allow_commit_build.wait(timeout=2)
        return original_token(domain, *parts)

    monkeypatch.setattr(closure_module, "_token", blocked_token)
    results = []
    run_thread = threading.Thread(
        target=lambda: results.append(
            driver.run(facade, now_ms=10)
        )
    )
    run_thread.start()
    assert commit_build_started.wait(timeout=2)

    assert driver.withdraw(facade)
    allow_commit_build.set()
    run_thread.join(timeout=2)

    assert not run_thread.is_alive()
    assert len(results) == 1
    result = results[0]
    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.buffers_scrubbed
    final_activity = max(
        index
        for index, event in enumerate(result.trace)
        if event.kind is TraceKind.PARTICIPANT_ACTIVITY
    )
    forbidden_after_withdrawal = {
        TraceKind.CLOSURE_STAGED,
        TraceKind.OFFLINE_OUTBOUND_COMMITTED,
        TraceKind.SYNTHETIC_PLAYBACK_OBSERVED,
        TraceKind.TRANSPORT_RESOLVED,
        TraceKind.PLAYBACK_OBSERVED,
        TraceKind.SILENCE_TIMER_ARMED,
    }
    assert forbidden_after_withdrawal.isdisjoint(
        event.kind
        for event in result.trace[final_activity + 1:]
    )


def test_copied_confirmation_cannot_authorize_scripted_closure():
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )
    confirmation = driver.confirm_scripted_opt_out(
        facade,
        now_ms=10,
    )
    assert confirmation is not None
    driver._scripted_confirmation = replace(confirmation)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.buffers_scrubbed
    assert TraceKind.CLOSURE_PREDICATE_REJECTED in {
        event.kind for event in result.trace
    }
    assert not any(
        event.kind is TraceKind.OFFLINE_OUTBOUND_COMMITTED
        for event in result.trace
    )


@pytest.mark.parametrize(
    "boundary",
    ("mint_capability", "stage", "commit"),
)
def test_rejected_closure_predicate_adds_zero_frames(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )
    assert driver.confirm_scripted_opt_out(
        facade,
        now_ms=10,
    ) is not None
    monkeypatch.setattr(
        driver._closure_authority,
        boundary,
        lambda **_: None,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.outbound_bytes == 0
    assert result.outbound_audio_ms == 0
    assert result.buffers_scrubbed
    assert TraceKind.CLOSURE_PREDICATE_REJECTED in {
        event.kind for event in result.trace
    }
    assert not any(
        event.kind is TraceKind.OFFLINE_OUTBOUND_COMMITTED
        for event in result.trace
    )


def test_ambiguous_general_cleanup_never_mints_or_commits_closure(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
    )
    assert driver.confirm_scripted_opt_out(
        facade,
        now_ms=10,
    ) is not None
    original = driver._seal_composition_assembly
    attempts = 0

    def fail_first(*, assembly, at_ms):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return original(assembly=assembly, at_ms=at_ms)

    monkeypatch.setattr(
        driver,
        "_seal_composition_assembly",
        fail_first,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.DELIVERY
    assert result.outbound_frame_count == 0
    assert result.buffers_scrubbed
    assert TraceKind.CLOSURE_CAPABILITY_RETAINED not in {
        event.kind for event in result.trace
    }
    assert TraceKind.OFFLINE_OUTBOUND_COMMITTED not in {
        event.kind for event in result.trace
    }


def test_scripted_locale_is_bound_into_the_lease_identity():
    first = OfflineSessionDriver()
    first_facade = first.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        now_ms=10,
        scripted_locale="en",
    )
    second = OfflineSessionDriver()
    second_facade = second.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        now_ms=10,
        scripted_locale="es",
    )

    assert first_facade is not None
    assert second_facade is not None
    assert first_facade._lease_id != second_facade._lease_id


def test_stop_posture_is_bound_into_the_lease_identity_and_scope():
    first, first_facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        stop_posture=OfflineStopPosture.RESPONSE_PENDING,
    )
    second, second_facade = _lease(
        journey=SyntheticJourney.OPT_OUT_WITHDRAWAL,
        stop_posture=OfflineStopPosture.REPLAY_PENDING,
    )

    assert first_facade._lease_id != second_facade._lease_id
    assert first_facade._stop_posture is (
        OfflineStopPosture.RESPONSE_PENDING
    )
    assert second_facade._stop_posture is (
        OfflineStopPosture.REPLAY_PENDING
    )
    assert first is not second
    assert OfflineSessionDriver().lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.DIRECT_ANSWER,
        now_ms=10,
        stop_posture=OfflineStopPosture.REPLAY_PENDING,
    ) is None


def test_direct_answer_precedes_at_most_one_question():
    driver, facade = _lease(
        journey=SyntheticJourney.DIRECT_ANSWER,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    confirmed = tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    )
    assert confirmed == (
        VoiceSemanticActKind.ANSWER,
        VoiceSemanticActKind.QUESTION,
    )
    assert confirmed.count(VoiceSemanticActKind.QUESTION) == 1


def test_question_repair_safety_and_unsupported_locale_are_typed():
    expected = {
        SyntheticJourney.QUESTION_ONLY: (
            VoiceSemanticActKind.QUESTION,
        ),
        SyntheticJourney.LOW_CONFIDENCE_REPAIR: (
            VoiceSemanticActKind.REPAIR,
        ),
        SyntheticJourney.SAFETY_GUIDANCE: (
            VoiceSemanticActKind.SAFETY,
            VoiceSemanticActKind.QUESTION,
        ),
        SyntheticJourney.UNSUPPORTED_LANGUAGE: (
            VoiceSemanticActKind.LANGUAGE_CHOICE,
            VoiceSemanticActKind.LANGUAGE_CHOICE,
            VoiceSemanticActKind.LANGUAGE_CHOICE,
            VoiceSemanticActKind.QUESTION,
        ),
    }
    for journey, expected_acts in expected.items():
        driver, facade = _lease(journey=journey)
        result = driver.run(facade, now_ms=10)
        assert result is not None
        confirmed = tuple(
            event.semantic_act_kind
            for event in result.trace
            if event.kind is TraceKind.ACT_CONFIRMED
        )
        assert confirmed == expected_acts
        if journey is SyntheticJourney.LOW_CONFIDENCE_REPAIR:
            repairs = tuple(
                event
                for event in result.trace
                if event.kind is TraceKind.REPAIR_PENDING
            )
            assert len(repairs) == 1
            assert repairs[0].locale == "es"
        if journey is SyntheticJourney.UNSUPPORTED_LANGUAGE:
            kinds = tuple(event.kind for event in result.trace)
            assert kinds.count(
                TraceKind.LANGUAGE_CHOICE_REQUIRED
            ) == 1
            assert kinds.count(
                TraceKind.LANGUAGE_CHOICE_PENDING
            ) == 1
            assert kinds.count(
                TraceKind.LANGUAGE_CHOICE_WINDOW
            ) >= 1
            assert kinds.count(
                TraceKind.LANGUAGE_RECOVERY_ADMITTED
            ) == 1
            assert (
                kinds.index(TraceKind.LANGUAGE_CHOICE_REQUIRED)
                < kinds.index(TraceKind.LANGUAGE_CHOICE_PENDING)
                < kinds.index(TraceKind.LANGUAGE_CHOICE_WINDOW)
                < kinds.index(
                    TraceKind.LANGUAGE_RECOVERY_ADMITTED
                )
            )
            assert TraceKind.NO_AUDIO_TEARDOWN not in kinds
            assert result.outbound_frame_count == 4


@pytest.mark.parametrize(
    ("journey", "recovered_locale"),
    (
        (SyntheticJourney.UNSUPPORTED_LANGUAGE, "en"),
        (
            SyntheticJourney.UNSUPPORTED_LANGUAGE_RECOVERY_ES,
            "es",
        ),
        (
            SyntheticJourney.UNSUPPORTED_LANGUAGE_RECOVERY_ZH,
            "zh",
        ),
    ),
)
def test_integrated_language_recovery_journeys_are_exact_and_same_call(
    journey: SyntheticJourney,
    recovered_locale: str,
):
    driver, facade = _lease(journey=journey)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    confirmed = tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    )
    assert confirmed == (
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.QUESTION,
    )
    admitted = tuple(
        event
        for event in result.trace
        if event.kind is TraceKind.LANGUAGE_RECOVERY_ADMITTED
    )
    assert len(admitted) == 1
    assert admitted[0].locale == recovered_locale
    assert result.outbound_frame_count == 4
    assert TraceKind.LANGUAGE_CHOICE_EXHAUSTED not in {
        event.kind for event in result.trace
    }
    assert TraceKind.NO_AUDIO_TEARDOWN not in {
        event.kind for event in result.trace
    }


@pytest.mark.parametrize(
    "journey",
    (
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_PT,
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AMBIGUOUS,
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_TIMEOUT,
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_FR,
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AR,
    ),
)
def test_integrated_language_exhaustion_is_distinct_sealed_and_no_audio(
    journey: SyntheticJourney,
):
    driver, facade = _lease(journey=journey)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    confirmed = tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    )
    assert confirmed == (
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.LANGUAGE_CHOICE,
        VoiceSemanticActKind.LANGUAGE_CHOICE,
    )
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.LANGUAGE_CHOICE_EXHAUSTED) == 1
    assert kinds.count(TraceKind.NO_AUDIO_TEARDOWN) == 1
    assert result.outbound_frame_count == 3
    assert (
        kinds.count(TraceKind.INPUT_FINAL)
        == (
            1
            if journey
            is SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_TIMEOUT
            else 2
        )
    )
    exhausted = next(
        event
        for event in result.trace
        if event.kind is TraceKind.LANGUAGE_CHOICE_EXHAUSTED
    )
    assert exhausted.content_digest == LANGUAGE_CHOICE_DESCRIPTOR_DIGEST
    assert not any(
        event.semantic_act_kind is VoiceSemanticActKind.CLOSING
        for event in result.trace
    )
    assert {
        TraceKind.GENERIC_FAILURE_PROOF_RETAINED,
        TraceKind.CLOSURE_CAPABILITY_RETAINED,
        TraceKind.CLOSURE_STAGED,
        TraceKind.OFFLINE_OUTBOUND_COMMITTED,
        TraceKind.SYNTHETIC_PLAYBACK_OBSERVED,
        TraceKind.ATOMIC_SYNTHETIC_FRAME_CONSUMED,
        TraceKind.CLOSURE_TEARDOWN_COMPLETE,
    }.isdisjoint(kinds)


def test_language_journeys_cover_both_exact_unlisted_trigger_locales():
    assert {
        journey: dict(_FIXTURES[journey].fields)["language"]
        for journey in (
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_FR,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AR,
        )
    } == {
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_FR:
            "fr-FR",
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AR:
            "ar-MSA",
    }
    assert {
        journey: _LANGUAGE_EXHAUSTION_LOCALES[journey]
        for journey in (
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_FR,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AR,
        )
    } == {
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_FR:
            "fr_fr",
        SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AR:
            "ar_msa",
    }
    assert {
        dict(_FIXTURES[journey].fields)["language"]
        for journey in (
            SyntheticJourney.UNSUPPORTED_LANGUAGE,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_RECOVERY_ES,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_RECOVERY_ZH,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_PT,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AMBIGUOUS,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_TIMEOUT,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_FR,
            SyntheticJourney.UNSUPPORTED_LANGUAGE_EXHAUSTION_AR,
        )
    } == {"fr-FR", "ar-MSA"}


def test_low_confidence_repair_reserves_the_reviewed_spanish_asset(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = []
    original = FixedProposalMaterializer.input_repair

    def record(self, **kwargs):
        proposal = original(self, **kwargs)
        captured.append(proposal)
        return proposal

    monkeypatch.setattr(
        FixedProposalMaterializer,
        "input_repair",
        record,
    )
    driver, facade = _lease(
        journey=SyntheticJourney.LOW_CONFIDENCE_REPAIR,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert len(captured) == 1
    assert captured[0].locale == "es"
    assert tuple(act.text for act in captured[0].plan.acts) == (
        "Perdón, no entendí. Puede repetirlo?",
    )


def test_one_repair_success_then_second_failure_requires_silent_closure():
    driver, facade = _lease(
        journey=SyntheticJourney.REPAIR_EXHAUSTION,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 1
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.INPUT_FINAL) == 2
    assert kinds.count(TraceKind.REPAIR_PENDING) == 1
    assert kinds.count(TraceKind.ACT_CONFIRMED) == 1
    assert kinds.count(TraceKind.PLAYBACK_OBSERVED) == 1
    assert kinds.count(TraceKind.RESPONSE_OBSERVED) == 1
    assert kinds.count(TraceKind.TERMINAL) == 1
    second_input = kinds.index(
        TraceKind.INPUT_FINAL,
        kinds.index(TraceKind.INPUT_FINAL) + 1,
    )
    assert (
        kinds.index(TraceKind.RESPONSE_OBSERVED)
        < second_input
        < kinds.index(TraceKind.TERMINAL)
    )
    terminal = next(
        event
        for event in result.trace
        if event.kind is TraceKind.TERMINAL
    )
    assert terminal.composition_status is (
        CompositionStatus.CLOSURE_REQUIRED
    )
    assert terminal.locale == "es"
    assert tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    ) == (VoiceSemanticActKind.REPAIR,)


@pytest.mark.parametrize(
    ("locale", "expected_digest"),
    (
        (
            "en",
            "3875698272528850f8a6097bbd9e2076b2c14441badb77574516427201bba303",
        ),
        (
            "es",
            "9cc751340092ffbdefc119d416c4b5e1ad59e939af864442ea95a2eb72f8a8be",
        ),
        (
            "zh",
            "e3c6136a7cbf0868713c392d464497af2cefc95e76802bc1eb61c1605e861fec",
        ),
    ),
)
def test_generic_failure_closure_consumes_one_tier_a_marker_per_arm(
    locale: str,
    expected_digest: str,
):
    traces = {}
    for arm in CandidateArm:
        driver = OfflineSessionDriver()
        facade = driver.lease(
            arm=arm,
            journey=SyntheticJourney.GENERIC_FAILURE_CLOSURE,
            now_ms=10,
            scripted_locale=locale,
        )
        assert facade is not None

        result = driver.run(facade, now_ms=10)

        assert result is not None
        assert result.state is OfflineSessionState.CLOSED
        assert result.failure is None
        assert result.pre_stop_outbound_frame_count == 1
        assert result.post_stop_outbound_frame_delta == 1
        assert result.post_stop_outbound_ordinals == (0,)
        assert result.outbound_frame_count == 1
        assert result.outbound_bytes == 160
        assert result.outbound_audio_ms == 20
        assert result.buffers_scrubbed
        assert result.closure_evidence is not None
        assert result.closure_evidence.outcome is (
            OfflineClosureOutcome.SYNTHETIC_PLAYBACK_MARKER
        )
        assert result.closure_evidence.provisional_locale == locale
        assert result.closure_evidence.content_digest == expected_digest
        assert result.closure_evidence.injection is (
            OfflineFailureInjection.NONE
        )
        assert generic_failure_text_digest(locale) == expected_digest
        kinds = tuple(event.kind for event in result.trace)
        seal = kinds.index(TraceKind.GENERAL_AUTHORITY_SEALED)
        assert kinds.count(
            TraceKind.GENERIC_FAILURE_PROOF_RETAINED
        ) == 1
        assert kinds.count(TraceKind.CLOSURE_STAGED) == 1
        assert kinds.count(
            TraceKind.OFFLINE_OUTBOUND_COMMITTED
        ) == 1
        assert kinds.count(
            TraceKind.ATOMIC_SYNTHETIC_FRAME_CONSUMED
        ) == 1
        assert {
            TraceKind.ACT_CONFIRMED,
            TraceKind.TRANSPORT_RESOLVED,
            TraceKind.PLAYBACK_OBSERVED,
            TraceKind.RESPONSE_OBSERVED,
        }.isdisjoint(kinds[seal + 1:])
        lane = result.trace[seal + 1:]
        assert all(
            event.semantic_act_kind
            in {None, VoiceSemanticActKind.CLOSING}
            for event in lane
        )
        traces[arm] = _trace_projection(result)

    assert len(set(traces.values())) == 1


@pytest.mark.parametrize(
    "injection",
    tuple(
        item
        for item in OfflineFailureInjection
        if item is not OfflineFailureInjection.NONE
    ),
)
def test_generic_failure_uncertainty_matrix_is_exactly_no_audio(
    injection: OfflineFailureInjection,
):
    driver = OfflineSessionDriver()
    facade = driver.lease(
        arm=CandidateArm.B1,
        journey=SyntheticJourney.GENERIC_FAILURE_CLOSURE,
        now_ms=10,
        failure_injection=injection,
    )
    assert facade is not None

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.outbound_bytes == 0
    assert result.outbound_audio_ms == 0
    assert result.post_stop_outbound_frame_delta == 0
    assert result.post_stop_outbound_ordinals == ()
    assert result.buffers_scrubbed
    assert result.closure_evidence is not None
    assert result.closure_evidence.outcome is (
        OfflineClosureOutcome.NO_AUDIO_TEARDOWN
    )
    assert result.closure_evidence.content_digest is None
    assert result.closure_evidence.injection is injection
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.NO_AUDIO_TEARDOWN) == 1
    assert TraceKind.ATOMIC_SYNTHETIC_FRAME_CONSUMED not in kinds
    assert TraceKind.SYNTHETIC_PLAYBACK_OBSERVED not in kinds


def test_generic_failure_portuguese_asset_is_unapproved_and_silent():
    driver = OfflineSessionDriver()
    facade = driver.lease(
        arm=CandidateArm.C,
        journey=SyntheticJourney.GENERIC_FAILURE_CLOSURE,
        now_ms=10,
        scripted_locale="pt",
    )
    assert facade is not None

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 0
    assert result.closure_evidence is not None
    assert result.closure_evidence.outcome is (
        OfflineClosureOutcome.NO_AUDIO_TEARDOWN
    )
    assert generic_failure_text_digest("pt") is None
    kinds = {event.kind for event in result.trace}
    assert TraceKind.GENERIC_FAILURE_PROOF_RETAINED not in kinds
    assert TraceKind.CLOSURE_CAPABILITY_RETAINED not in kinds
    assert TraceKind.CLOSURE_STAGED not in kinds
    assert TraceKind.OFFLINE_OUTBOUND_COMMITTED not in kinds


def test_failure_injection_is_lease_bound_and_generic_only():
    first = OfflineSessionDriver()
    first_facade = first.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.GENERIC_FAILURE_CLOSURE,
        now_ms=10,
        failure_injection=OfflineFailureInjection.CALL_UNCERTAIN,
    )
    second = OfflineSessionDriver()
    second_facade = second.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.GENERIC_FAILURE_CLOSURE,
        now_ms=10,
        failure_injection=OfflineFailureInjection.SESSION_UNCERTAIN,
    )
    assert first_facade is not None
    assert second_facade is not None
    assert first_facade._lease_id != second_facade._lease_id
    assert OfflineSessionDriver().lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.DIRECT_ANSWER,
        now_ms=10,
        failure_injection=OfflineFailureInjection.CALL_UNCERTAIN,
    ) is None


def test_repeat_and_slower_replay_exact_localized_question_and_seal_every_assembly(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.REPEAT_SLOWER,
    )
    assemblies = []
    replay_requests = []
    original_assembly = driver._assembly
    original_reserve_replay = SpeechControl.reserve_replay

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    def capture_replay(self, **kwargs):
        source = kwargs["source"]
        replay_requests.append(
            (
                source.locale,
                source.text,
                source.text_digest,
                kwargs["mode"],
            )
        )
        return original_reserve_replay(self, **kwargs)

    monkeypatch.setattr(driver, "_assembly", capture_assembly)
    monkeypatch.setattr(
        SpeechControl,
        "reserve_replay",
        capture_replay,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.buffers_scrubbed
    assert result.outbound_frame_count == 12
    assert result.outbound_audio_ms == 320
    expected_text = {
        "en": "What details would help us understand the job?",
        "es": "Qué detalles ayudan a entender el trabajo?",
        "pt": "Quais detalhes ajudam a entender o serviço?",
        "zh": "还有哪些细节可以帮助我们了解这项服务？",
    }
    assert len(replay_requests) == 8
    for locale_index, locale in enumerate(
        ("en", "es", "pt", "zh")
    ):
        first, second = replay_requests[
            locale_index * 2:locale_index * 2 + 2
        ]
        assert first[:2] == (locale, expected_text[locale])
        assert second[:2] == (locale, expected_text[locale])
        assert first[2] == second[2] == hashlib.sha256(
            expected_text[locale].encode("utf-8")
        ).hexdigest()
        assert first[3] is ReplayMode.EXACT
        assert second[3] is ReplayMode.SLOWER
    requested = tuple(
        (
            event.locale,
            event.semantic_act_kind,
            event.replay_mode,
        )
        for event in result.trace
        if event.kind is TraceKind.REPLAY_REQUESTED
    )
    assert requested == tuple(
        pair
        for locale in ("en", "es", "pt", "zh")
        for pair in (
            (
                locale,
                VoiceSemanticActKind.REPEAT,
                ReplayMode.EXACT,
            ),
            (
                locale,
                VoiceSemanticActKind.SLOWER_SPEECH,
                ReplayMode.SLOWER,
            ),
        )
    )
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.REPLAY_PENDING) == 8
    assert kinds.count(TraceKind.REPLAY_OBSERVED) == 8
    assert kinds.count(TraceKind.RESPONSE_OBSERVED) == 4
    assert kinds.count(TraceKind.PLAYBACK_OBSERVED) == 12
    assert len(assemblies) == 4
    for assembly in assemblies:
        assert assembly.transaction.pending_response_count == 0
        assert assembly.receipts.unconsumed_receipt_count == 0
        assert assembly.calls.is_quiescent
        assert assembly.adapter.terminally_closed
        assert assembly.speech.reservation_batch_count(
            assembly.binding
        ) == 0
        assert all(
            not assembly.speech.is_live(act_id)
            for act_id in assembly.speech.act_ids_for_binding(
                assembly.binding
            )
        )


def test_repeat_slower_reservation_fault_aborts_and_seals_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.REPEAT_SLOWER,
    )
    assemblies = []
    original_assembly = driver._assembly
    original_reserve_replay = SpeechControl.reserve_replay

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    def fail_slower(self, **kwargs):
        if kwargs["mode"] is ReplayMode.SLOWER:
            return ()
        return original_reserve_replay(self, **kwargs)

    monkeypatch.setattr(driver, "_assembly", capture_assembly)
    monkeypatch.setattr(
        SpeechControl,
        "reserve_replay",
        fail_slower,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.COMPOSITION
    assert result.buffers_scrubbed
    assert len(assemblies) == 1
    assembly = assemblies[0]
    assert assembly.transaction.pending_response_count == 0
    assert assembly.receipts.unconsumed_receipt_count == 0
    assert assembly.calls.is_quiescent
    assert assembly.adapter.terminally_closed
    assert assembly.speech.reservation_batch_count(
        assembly.binding
    ) == 0
    assert all(
        not assembly.speech.is_live(act_id)
        for act_id in assembly.speech.act_ids_for_binding(
            assembly.binding
        )
    )


def test_repeat_slower_exact_audio_limit_and_one_under_rejection():
    driver, facade = _lease(
        journey=SyntheticJourney.REPEAT_SLOWER,
        limits=OfflineSessionLimits(
            max_outbound_audio_ms=320,
        ),
    )
    exact = driver.run(facade, now_ms=10)
    assert exact is not None
    assert exact.state is OfflineSessionState.CLOSED
    assert exact.failure is None
    assert exact.outbound_audio_ms == 320

    driver, facade = _lease(
        journey=SyntheticJourney.REPEAT_SLOWER,
        limits=OfflineSessionLimits(
            max_outbound_audio_ms=319,
        ),
    )
    rejected = driver.run(facade, now_ms=10)
    assert rejected is not None
    assert rejected.state is OfflineSessionState.ABORTED
    assert rejected.failure is DriverFailure.RESOURCE_LIMIT
    assert rejected.buffers_scrubbed


def test_repeat_slower_localized_start_fault_seals_both_assemblies(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.REPEAT_SLOWER,
    )
    assemblies = []
    original_assembly = driver._assembly

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    def fail_localized_start(**kwargs):
        raise RuntimeError("synthetic localized start fault")

    monkeypatch.setattr(driver, "_assembly", capture_assembly)
    monkeypatch.setattr(
        driver,
        "_start_silence_question",
        fail_localized_start,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.INTERNAL
    assert result.buffers_scrubbed
    assert len(assemblies) == 2
    for assembly in assemblies:
        assert assembly.transaction.pending_response_count == 0
        assert assembly.receipts.unconsumed_receipt_count == 0
        assert assembly.calls.is_quiescent
        assert assembly.adapter.terminally_closed
        assert assembly.speech.reservation_batch_count(
            assembly.binding
        ) == 0
        assert all(
            not assembly.speech.is_live(act_id)
            for act_id in assembly.speech.act_ids_for_binding(
                assembly.binding
            )
        )


@pytest.mark.parametrize(
    "fault_method",
    (
        "accept_tts_binding",
        "accept_playout_binding",
        "accept_transport_resolution",
    ),
)
def test_replay_binding_fault_commits_no_additional_outbound_audio(
    monkeypatch: pytest.MonkeyPatch,
    fault_method: str,
):
    driver, facade = _lease(
        journey=SyntheticJourney.REPEAT_SLOWER,
    )
    original = getattr(
        TurnCompositionTransaction,
        fault_method,
    )

    def reject_replay(self, *args, **kwargs):
        event = kwargs["event"]
        if (
            self.coordinator.speech.replay_binding(
                event.semantic_act_id
            )
            is not None
        ):
            return False
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        TurnCompositionTransaction,
        fault_method,
        reject_replay,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.DELIVERY
    assert result.buffers_scrubbed
    assert result.outbound_frame_count == 1
    assert result.outbound_audio_ms == 20


def test_superseding_turn_cancels_old_response_before_new_playback():
    driver, facade = _lease(
        journey=SyntheticJourney.SUPERSEDING_TURN,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.INPUT_FINAL) == 2
    first_pending = kinds.index(TraceKind.RESPONSE_PENDING)
    second_input = kinds.index(
        TraceKind.INPUT_FINAL,
        kinds.index(TraceKind.INPUT_FINAL) + 1,
    )
    assert first_pending < second_input < kinds.index(
        TraceKind.SUPERSEDED
    )
    assert kinds.index(TraceKind.SUPERSEDED) < kinds.index(
        TraceKind.ACT_CONFIRMED
    )
    assert kinds.count(TraceKind.PLAYBACK_OBSERVED) == 1
    assert kinds[-2:] == (
        TraceKind.RESPONSE_OBSERVED,
        TraceKind.BUFFERS_SCRUBBED,
    )


def test_bidirectional_code_switch_supersedes_stale_speech_without_repeated_facts(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.BIDIRECTIONAL_CODE_SWITCH,
    )
    assemblies = []
    original_assembly = driver._assembly

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    monkeypatch.setattr(
        driver,
        "_assembly",
        capture_assembly,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert len(assemblies) == 1
    state = assemblies[0].state.current_state()
    assert state.language == "es"
    assert set(state.known_facts) == {
        "service_action:repair",
        "service_object:furnace",
    }
    assert len(state.known_facts) == len(set(state.known_facts))
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.INPUT_FINAL) == 3
    assert kinds.count(TraceKind.SUPERSEDED) == 2
    assert kinds.count(TraceKind.PLAYBACK_OBSERVED) == 1
    assert tuple(
        event.locale
        for event in result.trace
        if event.kind is TraceKind.RESPONSE_PENDING
    ) == ("es", "zh", "es")


def test_unobserved_question_outcomes_never_mark_a_slot_asked(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.UNOBSERVED_QUESTION_OUTCOMES,
    )
    assemblies = []
    accepted_lifecycle_kinds = []
    original_assembly = driver._assembly
    original_ingest = VoiceLifecycle.ingest

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    def capture_ingest(lifecycle, event):
        accepted = original_ingest(lifecycle, event)
        if accepted:
            accepted_lifecycle_kinds.append(event.kind)
        return accepted

    monkeypatch.setattr(
        driver,
        "_assembly",
        capture_assembly,
    )
    monkeypatch.setattr(
        VoiceLifecycle,
        "ingest",
        capture_ingest,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 4
    assert len(assemblies) == 1
    assembly = assemblies[0]
    assert assembly.state.current_state().asked_slots == set()
    assert assembly.transaction.pending_response_count == 0
    assert assembly.receipts.unconsumed_receipt_count == 0
    assert assembly.calls.is_quiescent
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.INPUT_FINAL) == 5
    assert kinds.count(TraceKind.ACT_CONFIRMED) == 4
    assert kinds.count(TraceKind.SUPERSEDED) == 4
    assert kinds.count(TraceKind.PLAYOUT_PARTIAL) == 1
    assert kinds.count(TraceKind.PLAYOUT_CLEARED) == 1
    assert kinds.count(TraceKind.ACT_FAILED) == 1
    assert kinds.count(TraceKind.PLAYOUT_INTERRUPTED) == 1
    assert kinds.count(TraceKind.TRANSPORT_RESOLVED) == 1
    assert kinds.count(TraceKind.PLAYBACK_OBSERVED) == 0
    assert kinds.count(TraceKind.RESPONSE_OBSERVED) == 0
    assert accepted_lifecycle_kinds.count(
        VoiceEventKind.PLAYOUT_PARTIAL
    ) == 1
    assert accepted_lifecycle_kinds.count(
        VoiceEventKind.PLAYOUT_CLEARED
    ) == 1
    assert accepted_lifecycle_kinds.count(
        VoiceEventKind.PLAYOUT_INTERRUPTED
    ) == 1
    assert accepted_lifecycle_kinds.count(
        VoiceEventKind.ACT_FAILED
    ) == 1
    assert VoiceEventKind.CALLER_PLAYBACK_OBSERVED not in (
        accepted_lifecycle_kinds
    )
    final = next(
        event
        for event in reversed(result.trace)
        if event.kind is TraceKind.TERMINAL
    )
    assert final.composition_status is CompositionStatus.SILENT


def test_interruption_reconnect_uses_fresh_epoch_without_stale_speech(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.INTERRUPTION_RECONNECT,
    )
    assemblies = []
    repairs = []
    original_assembly = driver._assembly
    original_repair = FixedProposalMaterializer.input_repair

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    def capture_repair(materializer, **kwargs):
        proposal = original_repair(materializer, **kwargs)
        repairs.append(proposal)
        return proposal

    monkeypatch.setattr(
        driver,
        "_assembly",
        capture_assembly,
    )
    monkeypatch.setattr(
        FixedProposalMaterializer,
        "input_repair",
        capture_repair,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 2
    assert len(assemblies) == 2
    stale, fresh = assemblies
    assert stale.binding.call_binding == fresh.binding.call_binding
    assert (
        stale.binding.contractor_binding
        == fresh.binding.contractor_binding
    )
    assert stale.binding.environment == fresh.binding.environment
    assert stale.binding.stream_binding != fresh.binding.stream_binding
    assert fresh.binding.epoch == stale.binding.epoch + 1
    assert stale.adapter.terminally_closed
    assert stale.transaction.pending_response_count == 0
    assert stale.receipts.unconsumed_receipt_count == 0
    assert stale.calls.is_quiescent
    assert fresh.transaction.pending_response_count == 0
    assert fresh.receipts.unconsumed_receipt_count == 0
    assert fresh.calls.is_quiescent
    assert stale.state.current_state() == fresh.state.current_state()
    assert stale.state.version == fresh.state.version
    assert fresh.state.current_state().asked_slots == set()
    assert len(repairs) == 1
    assert repairs[0].locale == "en"
    assert tuple(act.text for act in repairs[0].plan.acts) == (
        "Sorry, I did not understand. Please say that again.",
    )
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.INPUT_FINAL) == 2
    assert kinds.count(TraceKind.PLAYOUT_INTERRUPTED) == 1
    assert kinds.count(TraceKind.SESSION_DISCONNECTED) == 1
    assert kinds.count(TraceKind.SESSION_REESTABLISHED) == 1
    assert kinds.count(TraceKind.REPAIR_PENDING) == 1
    assert kinds.count(TraceKind.PLAYBACK_OBSERVED) == 1
    assert kinds.count(TraceKind.RESPONSE_OBSERVED) == 1
    assert tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    ) == (
        VoiceSemanticActKind.QUESTION,
        VoiceSemanticActKind.REPAIR,
    )


def test_silence_boundary_and_more_time_use_fixed_canonical_speech(
    monkeypatch: pytest.MonkeyPatch,
):
    proposals = []
    original = FixedProposalMaterializer.lifecycle_act

    def capture(materializer, **kwargs):
        proposal = original(materializer, **kwargs)
        proposals.append(proposal)
        return proposal

    monkeypatch.setattr(
        FixedProposalMaterializer,
        "lifecycle_act",
        capture,
    )
    driver, facade = _lease(
        journey=SyntheticJourney.SILENCE_BOUNDARY_MORE_TIME,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 9
    assert result.outbound_audio_ms == 180
    assert result.session_duration_ms == 70_053
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.CALLER_ACTIVITY_AT_BOUNDARY) == 1
    assert kinds.count(TraceKind.MORE_TIME_ACCEPTED) == 2
    assert kinds.count(TraceKind.MORE_TIME_IGNORED) == 1
    assert kinds.count(TraceKind.LOCAL_TERMINAL_ELIGIBLE) == 2
    assert kinds.count(TraceKind.LOCAL_TERMINATED) == 2
    assert tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    ) == (
        VoiceSemanticActKind.QUESTION,
        VoiceSemanticActKind.QUESTION,
        VoiceSemanticActKind.ACKNOWLEDGEMENT,
        VoiceSemanticActKind.PRESENCE_CHECK,
        VoiceSemanticActKind.CLOSING,
        VoiceSemanticActKind.QUESTION,
        VoiceSemanticActKind.PRESENCE_CHECK,
        VoiceSemanticActKind.ACKNOWLEDGEMENT,
        VoiceSemanticActKind.CLOSING,
    )
    assert tuple(
        proposal.plan.acts[0].text
        for proposal in proposals
    ) == (
        "Take your time. I’ll wait twenty more seconds.",
        "Are you still there?",
        (
            "I can’t hear a response, so I’ll end this "
            "test call now. Goodbye."
        ),
        "Are you still there?",
        "Take your time. I’ll wait twenty more seconds.",
        (
            "I can’t hear a response, so I’ll end this "
            "test call now. Goodbye."
        ),
    )


def test_silence_journey_closes_manual_timeout_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        arm=CandidateArm.C,
        journey=SyntheticJourney.SILENCE_BOUNDARY_MORE_TIME,
    )
    assemblies = []
    original = driver._assembly

    def capture(*args, **kwargs):
        assembly = original(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    monkeypatch.setattr(driver, "_assembly", capture)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert len(assemblies) == 3
    assert all(
        assembly.adapter.terminally_closed
        and not assembly.adapter._begin_deadlines
        and not assembly.adapter._completion_deadlines
        and not assembly.adapter._pending_timeouts
        and not assembly.adapter._pending_timeout_receipts
        and assembly.calls.is_quiescent
        for assembly in assemblies
    )


def test_fixed_nonterminal_fallbacks_are_exact_isolated_and_sealed(
    monkeypatch: pytest.MonkeyPatch,
):
    proposals = []
    original_materialize = FixedProposalMaterializer.lifecycle_act

    def capture_proposal(materializer, **kwargs):
        proposal = original_materialize(
            materializer,
            **kwargs,
        )
        proposals.append(proposal)
        return proposal

    monkeypatch.setattr(
        FixedProposalMaterializer,
        "lifecycle_act",
        capture_proposal,
    )
    driver, facade = _lease(
        journey=SyntheticJourney.FIXED_NONTERMINAL_FALLBACKS,
    )
    assemblies = []
    original_assembly = driver._assembly

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        return assembly

    monkeypatch.setattr(driver, "_assembly", capture_assembly)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None
    assert result.outbound_frame_count == 4
    assert result.outbound_audio_ms == 80
    assert result.session_duration_ms == 10_030
    kinds = tuple(event.kind for event in result.trace)
    assert kinds.count(TraceKind.SILENCE_TIMER_ARMED) == 2
    assert kinds.count(TraceKind.FIXED_FALLBACK_REQUESTED) == 2
    assert kinds.count(TraceKind.FIXED_FALLBACK_OBSERVED) == 2
    assert TraceKind.LOCAL_TERMINAL_ELIGIBLE not in kinds
    assert TraceKind.LOCAL_TERMINATED not in kinds
    assert tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    ) == (
        VoiceSemanticActKind.QUESTION,
        VoiceSemanticActKind.ACKNOWLEDGEMENT,
        VoiceSemanticActKind.QUESTION,
        VoiceSemanticActKind.ACKNOWLEDGEMENT,
    )
    assert tuple(
        proposal.plan.acts[0].text
        for proposal in proposals
    ) == (
        (
            "Este teste de voz não permite usar o teclado nem "
            "chamadas por texto. Fale ou encerre a chamada."
        ),
        (
            "Este teste não pode gravar uma mensagem. "
            "Você pode encerrar a chamada agora."
        ),
    )
    assert len(assemblies) == 2
    assert all(
        assembly.adapter.terminally_closed
        and assembly.calls.is_quiescent
        and assembly.silence.pending_count == 0
        and assembly.silence.is_terminal
        and assembly.transaction.pending_response_count == 0
        and assembly.receipts.unconsumed_receipt_count == 0
        and all(
            not assembly.speech.is_live(act_id)
            for act_id
            in assembly.speech.act_ids_for_binding(
                assembly.binding
            )
        )
        for assembly in assemblies
    )


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_fixed_fallback_failure_seals_all_capabilities_with_per_act_fallback(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    driver, facade = _lease(
        journey=SyntheticJourney.FIXED_NONTERMINAL_FALLBACKS,
    )
    assemblies = []
    original_assembly = driver._assembly

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        original_timer_fired = assembly.calls.timer_fired

        def fail_stale_timer(*, event_id, **timer_kwargs):
            if event_id.startswith("stale_"):
                return (object(),)
            return original_timer_fired(
                event_id=event_id,
                **timer_kwargs,
            )

        def fail_binding_cleanup(binding):
            if failure_mode == "raise":
                raise RuntimeError(
                    "synthetic binding cleanup failure"
                )
            return False

        monkeypatch.setattr(
            assembly.calls,
            "timer_fired",
            fail_stale_timer,
        )
        monkeypatch.setattr(
            assembly.speech,
            "hard_terminalize_binding",
            fail_binding_cleanup,
        )
        return assembly

    monkeypatch.setattr(driver, "_assembly", capture_assembly)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.DELIVERY
    assert len(assemblies) == 1
    assembly = assemblies[0]
    assert assembly.silence.is_terminal
    assert assembly.silence.pending_count == 0
    assert assembly.calls.is_quiescent
    assert assembly.adapter.terminally_closed
    assert all(
        not assembly.speech.is_live(act_id)
        for act_id
        in assembly.speech.act_ids_for_binding(
            assembly.binding
        )
    )


@pytest.mark.parametrize("failure_mode", ("false", "raise"))
def test_fixed_fallback_failure_forces_pending_batch_retirement(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    driver, facade = _lease(
        journey=SyntheticJourney.FIXED_NONTERMINAL_FALLBACKS,
    )
    assemblies = []
    original_assembly = driver._assembly

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        coordinator = assembly.silence.coordinator

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
        original_prepare = assembly.silence.prepare

        def prepare_then_fail_confirmation(*args, **kwargs):
            pending = original_prepare(*args, **kwargs)
            if pending is not None:
                monkeypatch.setattr(
                    assembly.adapter,
                    "accept_semantic_confirmation",
                    lambda event, *, lifecycle: False,
                )
            return pending

        monkeypatch.setattr(
            assembly.silence,
            "prepare",
            prepare_then_fail_confirmation,
        )
        return assembly

    monkeypatch.setattr(driver, "_assembly", capture_assembly)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.DELIVERY
    assert len(assemblies) == 1
    assembly = assemblies[0]
    assert (
        assembly.silence.coordinator.reservation_batch_count(
            assembly.binding
        )
        == 0
    )
    assert assembly.silence.pending_count == 0
    assert assembly.silence.is_terminal
    assert assembly.calls.is_quiescent
    assert assembly.adapter.terminally_closed
    assert all(
        not assembly.speech.is_live(act_id)
        for act_id
        in assembly.speech.act_ids_for_binding(
            assembly.binding
        )
    )


def test_driver_retains_prepermit_cleanup_authority_until_explicit_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        journey=SyntheticJourney.FIXED_NONTERMINAL_FALLBACKS,
    )
    assemblies = []
    original_assembly = driver._assembly
    original_force_retirements = []

    def capture_assembly(*args, **kwargs):
        assembly = original_assembly(*args, **kwargs)
        assemblies.append(assembly)
        coordinator = assembly.silence.coordinator
        original_force_retirements.append(
            coordinator.force_retire_batch
        )
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
        original_prepare = assembly.silence.prepare

        def reject_prepermit(*args, **kwargs):
            monkeypatch.setattr(
                assembly.adapter,
                "accept_permit",
                lambda event, *, lifecycle: False,
            )
            return original_prepare(*args, **kwargs)

        monkeypatch.setattr(
            assembly.silence,
            "prepare",
            reject_prepermit,
        )
        return assembly

    monkeypatch.setattr(driver, "_assembly", capture_assembly)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.DELIVERY
    assert len(assemblies) == 1
    assembly = assemblies[0]
    coordinator = assembly.silence.coordinator
    assert coordinator.reservation_batch_count(
        assembly.binding
    ) == 1
    assert assembly.silence.pending_count == 1
    assert not assembly.silence.is_terminal
    assert assembly.adapter.terminally_closed
    assert all(
        not assembly.speech.is_live(act_id)
        for act_id
        in assembly.speech.act_ids_for_binding(
            assembly.binding
        )
    )
    monkeypatch.setattr(
        coordinator,
        "force_retire_batch",
        original_force_retirements[0],
    )

    assert assembly.silence.abort(
        at_ms=result.session_duration_ms + 10
    )
    assert coordinator.reservation_batch_count(
        assembly.binding
    ) == 0
    assert assembly.silence.pending_count == 0
    assert assembly.silence.is_terminal


def test_facade_is_single_use_and_driver_allows_only_one_live_lease():
    driver, facade = _lease()
    assert facade.state is OfflineSessionState.LEASED
    assert (
        driver.lease(
            arm=CandidateArm.A,
            journey=SyntheticJourney.DIRECT_ANSWER,
            now_ms=10,
        )
        is None
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert facade.state is OfflineSessionState.CLOSED
    assert facade.revoked
    assert driver.run(facade, now_ms=11) is None
    replacement = driver.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.DIRECT_ANSWER,
        now_ms=11,
    )
    assert replacement is not None
    assert replacement is not facade
    assert driver.run(facade, now_ms=11) is None


def test_revoke_and_expiry_scrub_buffers_and_reject_use_after_return():
    driver, revoked = _lease()
    revoked_payloads = tuple(
        frame.payload for frame in revoked._frames
    )
    assert driver.revoke(revoked)
    assert revoked.state is OfflineSessionState.ABORTED
    assert revoked.revoked
    assert revoked._frames == []
    assert all(not any(payload) for payload in revoked_payloads)
    assert not driver.revoke(revoked)
    assert driver.run(revoked, now_ms=10) is None

    expiring_driver, expired = _lease(now_ms=10)
    expired_payloads = tuple(
        frame.payload for frame in expired._frames
    )
    result = expiring_driver.run(
        expired,
        now_ms=10 + expiring_driver.limits.lease_ttl_ms + 1,
    )
    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.EXPIRED_LEASE
    assert result.buffers_scrubbed
    assert all(not any(payload) for payload in expired_payloads)


def test_success_scrubs_retained_mutable_buffers_and_drops_facade_references(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease()
    frames = tuple(facade._frames)
    payloads = tuple(frame.payload for frame in frames)
    outbound_payloads = []
    original_append = driver._append_outbound_frame

    def capture(**kwargs):
        original_append(**kwargs)
        outbound_payloads.append(
            driver._outbound_frames[-1].payload
        )

    monkeypatch.setattr(driver, "_append_outbound_frame", capture)
    assert all(any(payload) for payload in payloads)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.frame_count == len(frames)
    assert result.inbound_bytes == sum(len(payload) for payload in payloads)
    assert result.inbound_audio_ms == sum(
        frame.duration_ms for frame in frames
    )
    assert result.outbound_frame_count == len(outbound_payloads)
    assert result.outbound_bytes == sum(
        len(payload) for payload in outbound_payloads
    )
    assert result.buffers_scrubbed
    assert facade._frames == []
    assert facade._outbound_frames == []
    assert all(not any(payload) for payload in payloads)
    assert all(not any(payload) for payload in outbound_payloads)


@pytest.mark.parametrize(
    "mutation",
    (
        "frame_bytes",
        "frame_count",
        "inbound_bytes",
        "inbound_audio_ms",
        "queue_depth",
    ),
)
def test_exactly_one_over_each_resource_limit_aborts_and_scrubs(
    mutation: str,
):
    limits = OfflineSessionLimits(
        max_inbound_frame_bytes=8,
        max_outbound_frame_bytes=8,
        max_inbound_frames=2,
        max_outbound_frames=2,
        max_inbound_bytes=16,
        max_outbound_bytes=16,
        max_inbound_audio_ms=40,
        max_outbound_audio_ms=40,
        max_session_ms=100,
        max_queue_depth=2,
        lease_ttl_ms=50,
    )
    driver, facade = _lease(limits=limits)
    facade._frames = [
        _MutableFrame(ordinal=0, duration_ms=20, payload=bytearray(b"12345678"))
    ]
    if mutation == "frame_bytes":
        facade._frames[0].payload.append(1)
    elif mutation == "frame_count":
        facade._frames.extend(
            (
                _MutableFrame(1, 10, bytearray(b"1")),
                _MutableFrame(2, 10, bytearray(b"1")),
            )
        )
    elif mutation == "inbound_bytes":
        facade._frames.append(
            _MutableFrame(1, 10, bytearray(b"12345678"))
        )
        facade._frames[0].payload.append(1)
    elif mutation == "inbound_audio_ms":
        facade._frames[0].duration_ms = 41
    else:
        facade._frames.extend(
            (
                _MutableFrame(1, 10, bytearray(b"1")),
                _MutableFrame(2, 10, bytearray(b"1")),
            )
        )
    retained = tuple(frame.payload for frame in facade._frames)

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.RESOURCE_LIMIT
    assert result.buffers_scrubbed
    assert all(not any(payload) for payload in retained)


def test_exact_resource_limits_are_accepted():
    limits = OfflineSessionLimits(
        max_inbound_frame_bytes=8,
        max_outbound_frame_bytes=8,
        max_inbound_frames=2,
        max_outbound_frames=2,
        max_inbound_bytes=16,
        max_outbound_bytes=16,
        max_inbound_audio_ms=40,
        max_outbound_audio_ms=40,
        max_session_ms=100,
        max_queue_depth=2,
        lease_ttl_ms=50,
    )
    driver, facade = _lease(limits=limits)
    facade._frames = [
        _MutableFrame(0, 20, bytearray(b"12345678")),
        _MutableFrame(1, 20, bytearray(b"12345678")),
    ]

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.failure is None


@pytest.mark.parametrize(
    ("limit_changes", "journey"),
    (
        (
            {
                "max_outbound_frames": 1,
            },
            SyntheticJourney.DIRECT_ANSWER,
        ),
        (
            {
                "max_outbound_bytes": 8,
                "max_outbound_frame_bytes": 8,
            },
            SyntheticJourney.DIRECT_ANSWER,
        ),
        (
            {
                "max_outbound_audio_ms": 20,
            },
            SyntheticJourney.DIRECT_ANSWER,
        ),
        (
            {
                "max_session_ms": 1,
            },
            SyntheticJourney.QUESTION_ONLY,
        ),
    ),
)
def test_outbound_and_session_one_over_limits_abort_and_scrub(
    limit_changes: dict[str, int],
    journey: SyntheticJourney,
):
    values = {
        "max_inbound_frame_bytes": 8,
        "max_outbound_frame_bytes": 8,
        "max_inbound_frames": 2,
        "max_outbound_frames": 2,
        "max_inbound_bytes": 16,
        "max_outbound_bytes": 16,
        "max_inbound_audio_ms": 40,
        "max_outbound_audio_ms": 40,
        "max_session_ms": 100,
        "max_queue_depth": 2,
        "lease_ttl_ms": 50,
    }
    values.update(limit_changes)
    driver, facade = _lease(
        limits=OfflineSessionLimits(**values),
        journey=journey,
    )

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.RESOURCE_LIMIT
    assert result.buffers_scrubbed


def test_internal_failure_does_not_echo_raw_fixture_content(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease()
    synthetic_secret = "fixture raw content must not escape"

    def fail(**kwargs):
        raise RuntimeError(synthetic_secret)

    monkeypatch.setattr(driver, "_execute_fixture", fail)
    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure is DriverFailure.INTERNAL
    assert synthetic_secret not in repr(result)
    assert synthetic_secret not in repr(facade)
    assert result.buffers_scrubbed


def test_foreign_driver_and_binding_tampering_cannot_consume_facade():
    owner, facade = _lease()
    foreign = OfflineSessionDriver()
    assert foreign.run(facade, now_ms=10) is None
    original = facade._binding
    object.__setattr__(
        facade,
        "_binding",
        VoiceSessionBinding(
            original.environment,
            original.contractor_binding,
            "synthetic_call_999",
            original.stream_binding,
            original.epoch,
        ),
    )
    assert owner.run(facade, now_ms=10) is None
    object.__setattr__(facade, "_binding", original)
    assert owner.run(facade, now_ms=10) is not None


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("_lease_id", "f" * 64),
        ("_arm", CandidateArm.C),
        ("_journey", SyntheticJourney.SAFETY_GUIDANCE),
        ("_contract_digest", "e" * 64),
    ),
)
def test_mutable_facade_cannot_rewrite_driver_owned_grant(
    attribute: str,
    replacement: object,
):
    driver, facade = _lease()
    original = getattr(facade, attribute)

    setattr(facade, attribute, replacement)

    assert driver.run(facade, now_ms=10) is None
    assert driver._lease_grant is not None
    assert driver._lease_grant.state is OfflineSessionState.LEASED
    setattr(facade, attribute, original)
    result = driver.run(facade, now_ms=10)
    assert result is not None
    assert result.state is OfflineSessionState.CLOSED


def test_facade_cannot_extend_expiry_or_rebind_environment_and_tenant():
    expiry_driver, expiry_facade = _lease(now_ms=10)
    issued_expiry = expiry_facade._expires_at_ms
    expiry_facade._expires_at_ms = 10_000

    assert expiry_driver.run(expiry_facade, now_ms=2_000) is None

    expiry_facade._expires_at_ms = issued_expiry
    expired = expiry_driver.run(expiry_facade, now_ms=2_000)
    assert expired is not None
    assert expired.failure is DriverFailure.EXPIRED_LEASE

    driver, facade = _lease()
    original = facade._binding
    facade._binding = VoiceSessionBinding(
        environment="production",
        contractor_binding="foreign_tenant",
        call_binding=original.call_binding,
        stream_binding=original.stream_binding,
        epoch=original.epoch,
    )

    assert driver.run(facade, now_ms=10) is None

    facade._binding = original
    result = driver.run(facade, now_ms=10)
    assert result is not None
    assert result.state is OfflineSessionState.CLOSED


def test_equal_binding_clone_cannot_replace_exact_grant_binding():
    driver, facade = _lease()
    original = facade._binding
    facade._binding = VoiceSessionBinding(
        environment=original.environment,
        contractor_binding=original.contractor_binding,
        call_binding=original.call_binding,
        stream_binding=original.stream_binding,
        epoch=original.epoch,
    )

    assert facade._binding == original
    assert facade._binding is not original
    assert driver.run(facade, now_ms=10) is None

    facade._binding = original
    result = driver.run(facade, now_ms=10)
    assert result is not None
    assert result.state is OfflineSessionState.CLOSED


def test_consumed_facade_reset_cannot_restore_one_use_authority():
    driver, facade = _lease()
    assert driver.run(facade, now_ms=10) is not None
    facade._state = OfflineSessionState.LEASED
    facade._revoked = False
    facade._frames = [
        _MutableFrame(0, 20, bytearray(b"synthetic"))
    ]

    assert driver.run(facade, now_ms=11) is None
    assert driver.revoke(facade) is False
    replacement = driver.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.DIRECT_ANSWER,
        now_ms=11,
    )
    assert replacement is not None


def test_concurrent_facade_scope_mutation_cannot_redirect_active_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease(
        arm=CandidateArm.B1,
        journey=SyntheticJourney.QUESTION_ONLY,
    )
    original_binding = facade._binding
    original_contract = facade._contract_digest
    entered = threading.Event()
    release = threading.Event()
    original_execute = driver._execute_fixture

    def pause(**kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_execute(**kwargs)

    monkeypatch.setattr(driver, "_execute_fixture", pause)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            driver.run(facade, now_ms=10)
        )
    )
    worker.start()
    assert entered.wait(timeout=2)
    facade._arm = CandidateArm.C
    facade._journey = SyntheticJourney.SAFETY_GUIDANCE
    facade._binding = VoiceSessionBinding(
        environment="production",
        contractor_binding="foreign_tenant",
        call_binding=original_binding.call_binding,
        stream_binding=original_binding.stream_binding,
        epoch=original_binding.epoch,
    )
    facade._lease_id = "f" * 64
    facade._expires_at_ms = 10_000
    facade._contract_digest = "e" * 64
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(results) == 1
    result = results[0]
    assert result is not None
    assert result.state is OfflineSessionState.CLOSED
    assert result.arm is CandidateArm.B1
    assert result.journey is SyntheticJourney.QUESTION_ONLY
    assert result.contract_digest == original_contract
    assert tuple(
        event.semantic_act_kind
        for event in result.trace
        if event.kind is TraceKind.ACT_CONFIRMED
    ) == (VoiceSemanticActKind.QUESTION,)
    assert facade.arm is CandidateArm.B1
    assert facade.journey is SyntheticJourney.QUESTION_ONLY
    assert facade._binding is original_binding


@pytest.mark.parametrize(
    "mutation",
    (
        "duration_type",
        "arbitrary_entry",
        "payload_type",
        "inbound_list_replaced",
        "outbound_entry",
        "outbound_list_replaced",
    ),
)
def test_malformed_frame_state_is_terminal_scrubbed_and_reusable(
    mutation: str,
):
    class MalformedFrame:
        def __init__(self) -> None:
            self.payload = bytearray(b"malformed")

    driver, facade = _lease()
    issued_payloads = tuple(driver._issued_inbound_payloads)
    injected = MalformedFrame()
    if mutation == "duration_type":
        facade._frames[0].duration_ms = "20"
    elif mutation == "arbitrary_entry":
        facade._frames.append(injected)
    elif mutation == "payload_type":
        facade._frames[0].payload = b"immutable"
    elif mutation == "inbound_list_replaced":
        facade._frames = [injected]
    elif mutation == "outbound_entry":
        facade._outbound_frames.append(injected)
    else:
        facade._outbound_frames = [injected]

    result = driver.run(facade, now_ms=10)

    assert result is not None
    assert result.state is OfflineSessionState.ABORTED
    assert result.failure in {
        DriverFailure.RESOURCE_LIMIT,
        DriverFailure.INTERNAL,
    }
    assert result.buffers_scrubbed
    assert all(not any(payload) for payload in issued_payloads)
    if mutation in {
        "arbitrary_entry",
        "inbound_list_replaced",
        "outbound_entry",
        "outbound_list_replaced",
    }:
        assert not any(injected.payload)
    replacement = driver.lease(
        arm=CandidateArm.A,
        journey=SyntheticJourney.DIRECT_ANSWER,
        now_ms=11,
    )
    assert replacement is not None
    replacement_result = driver.run(replacement, now_ms=11)
    assert replacement_result is not None
    assert replacement_result.state is OfflineSessionState.CLOSED


def test_facade_constructor_and_limits_are_closed_exact_types():
    with pytest.raises(ValueError, match="sealed"):
        OfflineSessionFacade(
            seal=object(),
            driver=OfflineSessionDriver(),
            arm=CandidateArm.A,
            journey=SyntheticJourney.DIRECT_ANSWER,
            binding=VoiceSessionBinding("x", "x", "x", "x", 1),
            lease_id="a" * 64,
            expires_at_ms=1,
            contract_digest="b" * 64,
            scripted_locale="en",
            stop_posture=OfflineStopPosture.RESPONSE_PENDING,
            frames=[],
        )
    with pytest.raises(ValueError, match="exact integers"):
        OfflineSessionLimits(max_inbound_frames=True)
    with pytest.raises(ValueError, match="concurrency"):
        OfflineSessionLimits(max_concurrency=2)
    with pytest.raises(ValueError, match="smaller"):
        OfflineSessionLimits(
            max_inbound_frame_bytes=2,
            max_inbound_bytes=1,
        )


def test_contract_digest_binds_limits_and_all_assembly_identities():
    first = OfflineSessionDriver()
    second = OfflineSessionDriver(
        OfflineSessionLimits(max_inbound_frames=15)
    )
    assert first.contract_digest != second.contract_digest
    assert len(first.contract_digest) == 64
    source = _DRIVER_PATH.read_text(encoding="utf-8")
    for identity in (
        "_DRIVER_SOURCE_DIGEST",
        "_FACADE_CODE_DIGEST",
        "_ASSEMBLY_CODE_DIGESTS",
        "_ADAPTER_CODE_DIGESTS",
        "_CODEC",
        "_FRAME_SCHEMA",
        "limits_digest",
    ):
        assert identity in source


def test_pinned_digests_match_exact_reviewed_source_bytes():
    adapter_paths = {
        CandidateArm.A:
            Path("app/services/voice_candidates/native_gemini.py"),
        CandidateArm.B1:
            Path("app/services/voice_candidates/chained_streaming.py"),
        CandidateArm.B2:
            Path("app/services/voice_candidates/conversation_relay.py"),
        CandidateArm.C:
            Path("app/services/voice_candidates/manual_native.py"),
    }
    assert {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in _REVIEWED_ASSEMBLY_SOURCE_PATHS.items()
    } == _ASSEMBLY_CODE_DIGESTS
    assert {
        arm: hashlib.sha256(path.read_bytes()).hexdigest()
        for arm, path in adapter_paths.items()
    } == _ADAPTER_CODE_DIGESTS

    source = _DRIVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    facade = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "OfflineSessionFacade"
    )
    facade_source = ast.get_source_segment(source, facade)
    assert facade_source is not None
    assert hashlib.sha256(
        facade_source.encode("utf-8")
    ).hexdigest() == _FACADE_CODE_DIGEST
    normalized = re.sub(
        (
            r'_DRIVER_SOURCE_DIGEST = "[0-9a-f]{64}"'
        ),
        '_DRIVER_SOURCE_DIGEST = "<reviewed-source-digest>"',
        source,
        count=1,
    )
    assert hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest() == _DRIVER_SOURCE_DIGEST


def test_source_identity_set_covers_every_reviewed_direct_service_import():
    source = _DRIVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct_service_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("app.services.")
    }
    assert direct_service_modules == (
        _REVIEWED_DIRECT_SERVICE_MODULES
        | _REVIEWED_ADAPTER_MODULES
    )
    assert set(_ASSEMBLY_CODE_DIGESTS) == set(
        _REVIEWED_ASSEMBLY_SOURCE_PATHS
    )


def test_reviewed_catalog_and_identity_maps_are_immutable():
    with pytest.raises(TypeError):
        _FIXTURES[SyntheticJourney.DIRECT_ANSWER] = object()
    with pytest.raises(TypeError):
        _ASSEMBLY_CODE_DIGESTS["composition"] = "f" * 64
    with pytest.raises(TypeError):
        _ADAPTER_CODE_DIGESTS[CandidateArm.A] = "f" * 64
    with pytest.raises(TypeError):
        _ADAPTER_TYPES[CandidateArm.A] = object


def test_driver_has_no_async_dynamic_runtime_or_external_import_capability():
    source = _DRIVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden_imports = {
        "asyncio",
        "builtins",
        "fastapi",
        "google",
        "httpx",
        "importlib",
        "inspect",
        "os",
        "pathlib",
        "requests",
        "socket",
        "subprocess",
        "twilio",
        "websockets",
        "app.main",
        "app.webhooks",
        "app.services.voice_pipeline",
        "app.services.gemini_pipeline",
    }
    assert all(
        not any(
            module == forbidden
            or module.startswith(f"{forbidden}.")
            for forbidden in forbidden_imports
        )
        for module in imported
    )
    assert not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.Await))
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id in {"__import__", "eval", "exec"}
            or isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"import_module", "create_task", "run_in_executor"}
        )
        for node in ast.walk(tree)
    )


def test_live_routes_do_not_import_driver_and_hashes_remain_sealed():
    for path in _LIVE_ROUTE_PATHS:
        source = path.read_text(encoding="utf-8")
        assert "voice_bakeoff_session_driver" not in source
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            _EXPECTED_LIVE_HASHES[path]
        )


def test_facade_exposes_only_payload_safe_public_values():
    _driver, facade = _lease()
    public = {
        name
        for name in dir(facade)
        if not name.startswith("_")
    }
    assert public == {
        "arm",
        "contract_digest",
        "journey",
        "revoked",
        "state",
    }
    for name in public:
        value = getattr(facade, name)
        assert isinstance(
            value,
            (
                bool,
                str,
                CandidateArm,
                OfflineSessionState,
                SyntheticJourney,
            ),
        )
    assert "payload" not in repr(facade).lower()
    assert "content" not in repr(facade).lower()


def test_run_holds_one_synchronous_invocation_lease(
    monkeypatch: pytest.MonkeyPatch,
):
    driver, facade = _lease()
    entered = threading.Event()
    release = threading.Event()
    original = driver._execute_fixture

    def pause(**kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original(**kwargs)

    monkeypatch.setattr(driver, "_execute_fixture", pause)
    results = []
    worker = threading.Thread(
        target=lambda: results.append(driver.run(facade, now_ms=10))
    )
    worker.start()
    assert entered.wait(timeout=2)
    assert (
        driver.lease(
            arm=CandidateArm.C,
            journey=SyntheticJourney.QUESTION_ONLY,
            now_ms=10,
        )
        is None
    )
    assert driver.run(facade, now_ms=10) is None
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0] is not None
    assert results[0].state is OfflineSessionState.CLOSED
