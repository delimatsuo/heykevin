from __future__ import annotations

import hashlib
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.voice_bakeoff_closure import OfflineAuthorityInventory
from app.services.voice_bakeoff_language_choice import (
    LANGUAGE_CHOICE_DESCRIPTOR,
    LANGUAGE_CHOICE_DESCRIPTOR_BYTES,
    LANGUAGE_CHOICE_DESCRIPTOR_DIGEST,
    LANGUAGE_CHOICE_FINALIZATION_MS,
    LANGUAGE_CHOICE_MAX_SPEECH_MS,
    LANGUAGE_CHOICE_PAUSES_MS,
    LANGUAGE_CHOICE_RESPONSE_MS,
    LANGUAGE_CHOICE_TEXT_DIGESTS,
    UNLISTED_CHALLENGE_LOCALES,
    AdmissionPurpose,
    LanguageChoiceDescriptor,
    LanguageChoicePhase,
    LanguageChoiceSegment,
    LanguageChoiceTerminalOutcome,
    LanguageFinalDisposition,
    LanguageRecoveryFinalTurnReceipt,
    OfflineLanguageChoiceLifecycle,
    materialize_language_choice,
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
    SpeechAuthorization,
    SpeechControl,
    SpeechPolicy,
)

_EXPECTED_DESCRIPTOR_HEX = (
    "6865792d6b6576696e2f6f66666c696e652d6c616e67756167652d63686f696365"
    "2d61737365742f76310000010020756e737570706f727465645f6c616e6775616765"
    "5f726573706f6e73655f7631000f6c616e67756167655f63686f69636500036d756c"
    "03000002656e000000675468697320746573742063616e20636f6e74696e7565206f"
    "6e6c7920696e20456e676c6973682c205370616e6973682c206f72204d616e646172"
    "696e2e20506c6561736520726573706f6e6420696e206f6e65206f662074686f7365"
    "206c616e6775616765732efa2ef0f88a82bb845ae31a47a7f8a6092fc374973c2d92"
    "44b1c6cfa403eebf10010002657300000063457374612070727565626120736f6c6f"
    "20707565646520636f6e74696e75617220656e20696e676cc3a9732c2065737061c3"
    "b16f6c206f206d616e646172c3ad6e2e20526573706f6e646120656e20756e6f2064"
    "652065736f73206964696f6d61732e7d045720baf2dad66a05813c1f971f02cd1667"
    "877007a878b510e62ae033078d0200027a6800000060e69cace6aca1e6b58be8af95"
    "e58faae883bde4bdbfe794a8e88bb1e8afade38081e8a5bfe78fade78999e8afade6"
    "8896e699aee9809ae8af9de38082e8afb7e4bdbfe794a8e585b6e4b8ade4b880e7a7"
    "8de8afade8a880e59b9ee7ad94e38082f6373e32485d9ac35138e52c860de716567a"
    "158ec9fbf7509236d20b76589d6e0200fa00fa"
)


def _binding() -> VoiceSessionBinding:
    return VoiceSessionBinding(
        environment="offline",
        contractor_binding="contractor_fixture",
        call_binding="call_fixture",
        stream_binding="stream_fixture",
        epoch=1,
    )


def _speech() -> SpeechControl:
    return SpeechControl(
        SpeechPolicy(
            normal_word_budget=24,
            safety_word_budget=24,
            required_safety_fragments=("emergency",),
            terminal_fragments=("goodbye",),
        )
    )


def _sealed_inventory() -> OfflineAuthorityInventory:
    return OfflineAuthorityInventory(
        transaction_pending=0,
        admission_receipts=0,
        silence_pending=0,
        speech_batches=0,
        live_speech_acts=0,
        queued_outbound_frames=0,
        call_quiescent=True,
        call_terminated=True,
        adapter_terminally_closed=True,
    )


def _setup_choice(*, begin_presentation: bool = True):
    binding = _binding()
    voice = VoiceLifecycle(binding=binding)
    speech = _speech()
    choice = OfflineLanguageChoiceLifecycle(
        binding=binding,
        speech=speech,
    )
    proposal = materialize_language_choice(state_version=2)
    reserved = speech.reserve(
        proposal.plan,
        SpeechAuthorization(
            binding=binding,
            turn_id="unlisted_turn",
            authorized_kinds=(
                VoiceSemanticActKind.LANGUAGE_CHOICE,
            ),
            terminal_allowed=False,
            locale="mul",
        ),
    )
    assert all(
        speech.authorize_text(item.act_id, item.text)
        for item in reserved
    )
    assert choice.reserve(
        proposal=proposal,
        reserved=reserved,
    )
    if begin_presentation:
        assert choice.begin_presentation(lifecycle=voice)
    return binding, voice, speech, choice, reserved


def _event(
    voice: VoiceLifecycle,
    *,
    kind: VoiceEventKind,
    source: VoiceSource,
    at_ms: int,
    turn_id: str,
    act_id: str,
    act_kind: VoiceSemanticActKind,
    payload: VoicePayload | None = None,
    generation_id: str = "generation_fixture",
) -> VoiceEvent:
    sequence, canonical_at_ms = voice.next_position(at_ms=at_ms)
    event = VoiceEvent(
        schema_version=VOICE_SCHEMA_VERSION,
        kind=kind,
        source=source,
        sensitivity=VoiceSensitivity.OPERATIONAL,
        binding=voice.binding,
        sequence=sequence,
        at_ms=canonical_at_ms,
        input_turn_id=turn_id,
        generation_id=generation_id,
        semantic_act_id=act_id,
        semantic_act_kind=act_kind,
        payload=payload if payload is not None else VoicePayload(),
    )
    assert voice.ingest(event)
    return event


def _observe_segment(
    *,
    voice: VoiceLifecycle,
    speech: SpeechControl,
    choice: OfflineLanguageChoiceLifecycle,
    reserved,
    ordinal: int,
    at_ms: int,
) -> VoiceEvent:
    item = reserved[ordinal]
    authorization = _event(
        voice,
        kind=VoiceEventKind.RESPONSE_AUTHORIZED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=at_ms,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
    )
    _event(
        voice,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 1,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
    )
    audio_id = f"audio_{ordinal}"
    playout_id = f"playout_{ordinal}"
    assert speech.bind_tts(item.act_id, audio_id=audio_id)
    text_digest = LANGUAGE_CHOICE_TEXT_DIGESTS[ordinal]
    _event(
        voice,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 2,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=VoicePayload(
            text_digest=text_digest,
            audio_id=audio_id,
        ),
    )
    assert speech.bind_playout(
        item.act_id,
        playout_id=playout_id,
    )
    payload = VoicePayload(
        text_digest=text_digest,
        audio_id=audio_id,
        playout_id=playout_id,
    )
    _event(
        voice,
        kind=VoiceEventKind.PLAYOUT_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 3,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=payload,
    )
    _event(
        voice,
        kind=VoiceEventKind.TRANSPORT_RESOLVED,
        source=VoiceSource.TWILIO_AUTHENTICATED,
        at_ms=authorization.at_ms + 4,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=payload,
    )
    playback = _event(
        voice,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 5,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=payload,
    )
    assert speech.record_caller_playback_observed(
        item.act_id,
        playout_id=playout_id,
    )
    assert choice.observe_segment(
        event=playback,
        lifecycle=voice,
    )
    return playback


def _open_response_window():
    binding, voice, speech, choice, reserved = _setup_choice()
    playback = None
    for ordinal in range(3):
        playback = _observe_segment(
            voice=voice,
            speech=speech,
            choice=choice,
            reserved=reserved,
            ordinal=ordinal,
            at_ms=100 + ordinal * 10,
        )
    assert playback is not None
    assert choice.phase is LanguageChoicePhase.CLEANUP_PENDING
    assert speech.complete_reservation(reserved)
    assert choice.complete_prompt_cleanup()
    assert choice.phase is LanguageChoicePhase.RESPONSE_WINDOW
    assert choice.response_deadline_ms == (
        playback.at_ms + LANGUAGE_CHOICE_RESPONSE_MS
    )
    return binding, voice, speech, choice, reserved


def _cleanup_pending_choice():
    binding, voice, speech, choice, reserved = _setup_choice()
    playback = None
    for ordinal in range(3):
        playback = _observe_segment(
            voice=voice,
            speech=speech,
            choice=choice,
            reserved=reserved,
            ordinal=ordinal,
            at_ms=100 + ordinal * 10,
        )
    assert playback is not None
    assert choice.phase is LanguageChoicePhase.CLEANUP_PENDING
    assert choice.response_deadline_ms == (
        playback.at_ms + LANGUAGE_CHOICE_RESPONSE_MS
    )
    return binding, voice, speech, choice, reserved


def _input_event(
    voice: VoiceLifecycle,
    *,
    kind: VoiceEventKind,
    at_ms: int,
    turn_id: str = "language_response_turn",
) -> VoiceEvent:
    return _event(
        voice,
        kind=kind,
        source=(
            VoiceSource.PROVIDER_UNTRUSTED
            if kind is VoiceEventKind.INPUT_TURN_FINAL
            else VoiceSource.LOCAL_AUTHORITATIVE
        ),
        at_ms=at_ms,
        turn_id=turn_id,
        act_id="language_response_input",
        act_kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
        payload=(
            VoicePayload(text_digest="a" * 64)
            if kind is VoiceEventKind.INPUT_TURN_FINAL
            else VoicePayload()
        ),
        generation_id="language_response_generation",
    )


def _recovery_receipt(
    choice: OfflineLanguageChoiceLifecycle,
    event: VoiceEvent,
    *,
    generation: int | None = None,
    locale: str = "en",
) -> LanguageRecoveryFinalTurnReceipt:
    return LanguageRecoveryFinalTurnReceipt(
        receipt_id="language_receipt_fixture",
        purpose=AdmissionPurpose.LANGUAGE_RECOVERY,
        arm=CandidateArm.A,
        adapter_implementation_digest="1" * 64,
        adapter_configuration_digest="2" * 64,
        canonical_event_digest="3" * 64,
        content_digest="4" * 64,
        content_byte_length=17,
        adapter_admission_revision=1,
        binding=event.binding,
        input_turn_id=event.input_turn_id,
        input_semantic_act_kind=VoiceSemanticActKind.ACKNOWLEDGEMENT,
        sequence=event.sequence,
        at_ms=event.at_ms,
        expires_at_ms=event.at_ms + 1_000,
        language_generation=(
            choice.generation
            if generation is None
            else generation
        ),
        detected_locale=locale,
    )


def test_descriptor_pins_exact_canonical_utf8_vector():
    canonical = LANGUAGE_CHOICE_DESCRIPTOR.canonical_bytes()

    assert len(canonical) == LANGUAGE_CHOICE_DESCRIPTOR_BYTES
    assert canonical.hex() == _EXPECTED_DESCRIPTOR_HEX
    assert hashlib.sha256(canonical).hexdigest() == (
        LANGUAGE_CHOICE_DESCRIPTOR_DIGEST
    )
    assert tuple(
        hashlib.sha256(segment.text.encode("utf-8")).hexdigest()
        for segment in LANGUAGE_CHOICE_DESCRIPTOR.segments
    ) == LANGUAGE_CHOICE_TEXT_DIGESTS
    assert LANGUAGE_CHOICE_DESCRIPTOR.pauses_ms == (
        LANGUAGE_CHOICE_PAUSES_MS
    )
    assert UNLISTED_CHALLENGE_LOCALES == frozenset(
        {"fr_fr", "ar_msa"}
    )


def test_descriptor_rejects_non_nfc_order_and_pause_mutations():
    first = LANGUAGE_CHOICE_DESCRIPTOR.segments[0]
    non_nfc = unicodedata.normalize(
        "NFD",
        "Esta prueba solo puede continuar en inglés.",
    )
    with pytest.raises(ValueError, match="already be NFC"):
        LanguageChoiceSegment(
            ordinal=0,
            locale="en",
            text=non_nfc,
            text_digest=hashlib.sha256(
                non_nfc.encode("utf-8")
            ).hexdigest(),
        )
    with pytest.raises(ValueError, match="descriptor is invalid"):
        LanguageChoiceDescriptor(
            schema_version=1,
            asset_id=LANGUAGE_CHOICE_DESCRIPTOR.asset_id,
            semantic_kind=VoiceSemanticActKind.LANGUAGE_CHOICE,
            asset_locale="mul",
            segments=(
                LANGUAGE_CHOICE_DESCRIPTOR.segments[1],
                first,
                LANGUAGE_CHOICE_DESCRIPTOR.segments[2],
            ),
            pauses_ms=LANGUAGE_CHOICE_PAUSES_MS,
        )
    with pytest.raises(ValueError, match="descriptor is invalid"):
        LanguageChoiceDescriptor(
            schema_version=1,
            asset_id=LANGUAGE_CHOICE_DESCRIPTOR.asset_id,
            semantic_kind=VoiceSemanticActKind.LANGUAGE_CHOICE,
            asset_locale="mul",
            segments=LANGUAGE_CHOICE_DESCRIPTOR.segments,
            pauses_ms=(250, 251),
        )
    mutated_text = f"{first.text} Drift."
    with pytest.raises(ValueError, match="descriptor is invalid"):
        LanguageChoiceDescriptor(
            schema_version=1,
            asset_id=LANGUAGE_CHOICE_DESCRIPTOR.asset_id,
            semantic_kind=VoiceSemanticActKind.LANGUAGE_CHOICE,
            asset_locale="mul",
            segments=(
                LanguageChoiceSegment(
                    ordinal=0,
                    locale="en",
                    text=mutated_text,
                    text_digest=hashlib.sha256(
                        mutated_text.encode("utf-8")
                    ).hexdigest(),
                ),
                LANGUAGE_CHOICE_DESCRIPTOR.segments[1],
                LANGUAGE_CHOICE_DESCRIPTOR.segments[2],
            ),
            pauses_ms=LANGUAGE_CHOICE_PAUSES_MS,
        )


def test_language_choice_is_categorically_non_replayable():
    _, voice, speech, choice, reserved = _setup_choice()
    _observe_segment(
        voice=voice,
        speech=speech,
        choice=choice,
        reserved=reserved,
        ordinal=0,
        at_ms=100,
    )

    assert speech.latest_replay_source(voice.binding) is None


def test_ordered_observation_opens_window_only_after_cleanup():
    _, _, speech, choice, reserved = _open_response_window()

    assert choice.observed_segment_count == 3
    assert not speech.tracks_reservation_batch(reserved)
    assert all(not speech.is_live(item.act_id) for item in reserved)


def test_segment_barge_in_cancels_every_stale_choice_act_once():
    _, voice, speech, choice, reserved = _setup_choice()
    first = _observe_segment(
        voice=voice,
        speech=speech,
        choice=choice,
        reserved=reserved,
        ordinal=0,
        at_ms=100,
    )
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=first.at_ms + 1,
    )

    assert choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.ACTIVITY_OPEN
    assert not speech.tracks_reservation_batch(reserved)
    assert all(not speech.is_live(item.act_id) for item in reserved)
    assert choice.activity_deadline_ms == (
        onset.at_ms + LANGUAGE_CHOICE_MAX_SPEECH_MS
    )


def test_playback_callback_cannot_skip_earlier_retained_activity():
    _, voice, speech, choice, reserved = _setup_choice()
    item = reserved[0]
    authorization = _event(
        voice,
        kind=VoiceEventKind.RESPONSE_AUTHORIZED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=100,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
    )
    _event(
        voice,
        kind=VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 1,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
    )
    audio_id = "audio_deferred"
    playout_id = "playout_deferred"
    assert speech.bind_tts(item.act_id, audio_id=audio_id)
    text_digest = LANGUAGE_CHOICE_TEXT_DIGESTS[0]
    _event(
        voice,
        kind=VoiceEventKind.TTS_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 2,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=VoicePayload(
            text_digest=text_digest,
            audio_id=audio_id,
        ),
    )
    assert speech.bind_playout(item.act_id, playout_id=playout_id)
    payload = VoicePayload(
        text_digest=text_digest,
        audio_id=audio_id,
        playout_id=playout_id,
    )
    _event(
        voice,
        kind=VoiceEventKind.PLAYOUT_BOUND,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=authorization.at_ms + 3,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=payload,
    )
    transport = _event(
        voice,
        kind=VoiceEventKind.TRANSPORT_RESOLVED,
        source=VoiceSource.TWILIO_AUTHENTICATED,
        at_ms=authorization.at_ms + 4,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=payload,
    )
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=transport.at_ms + 1,
    )
    playback = _event(
        voice,
        kind=VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
        at_ms=onset.at_ms + 1,
        turn_id=item.turn_id,
        act_id=item.act_id,
        act_kind=item.kind,
        payload=payload,
    )

    assert choice.defers_playback(event=playback, lifecycle=voice)
    assert not choice.observe_segment(event=playback, lifecycle=voice)
    assert choice.phase is LanguageChoicePhase.PRESENTING
    assert choice.observed_segment_count == 0
    assert choice.accept_activity_started(event=onset, lifecycle=voice)
    assert choice.phase is LanguageChoicePhase.ACTIVITY_OPEN
    assert not speech.tracks_reservation_batch(reserved)
    assert all(not speech.is_live(candidate.act_id) for candidate in reserved)


@pytest.mark.parametrize("observed_segment_count", (0, 1, 2, 3))
def test_final_only_barge_in_seals_prompt_at_every_segment_boundary(
    observed_segment_count: int,
):
    _, voice, speech, choice, reserved = _setup_choice()
    for ordinal in range(observed_segment_count):
        _observe_segment(
            voice=voice,
            speech=speech,
            choice=choice,
            reserved=reserved,
            ordinal=ordinal,
            at_ms=100 + ordinal * 10,
        )
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=200,
    )

    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.QUALIFIED
    assert choice.phase is LanguageChoicePhase.FINALIZING
    assert not speech.tracks_reservation_batch(reserved)
    assert all(not speech.is_live(item.act_id) for item in reserved)


@pytest.mark.parametrize(
    ("offset", "disposition"),
    (
        (0, LanguageFinalDisposition.QUALIFIED),
        (1, LanguageFinalDisposition.UNQUALIFIED),
    ),
)
def test_cleanup_boundary_final_uses_latched_final_marker_deadline(
    offset: int,
    disposition: LanguageFinalDisposition,
):
    _, voice, _, choice, _ = _cleanup_pending_choice()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline + offset,
    )

    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="en",
    ) is disposition
    assert choice.phase is (
        LanguageChoicePhase.FINALIZING
        if disposition is LanguageFinalDisposition.QUALIFIED
        else LanguageChoicePhase.TERMINAL
    )


@pytest.mark.parametrize("offset", (0, 1))
def test_cleanup_boundary_onset_is_inclusive_then_fails_closed(
    offset: int,
):
    _, voice, _, choice, _ = _cleanup_pending_choice()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline + offset,
    )

    accepted = choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    assert accepted is (offset == 0)
    assert choice.phase is (
        LanguageChoicePhase.ACTIVITY_OPEN
        if accepted
        else LanguageChoicePhase.TERMINAL
    )


def test_cleanup_pending_clock_expires_at_one_over_not_at_boundary():
    _, voice, _, choice, _ = _cleanup_pending_choice()
    deadline = choice.response_deadline_ms
    assert deadline is not None

    assert not choice.advance_time(
        lifecycle=voice,
        at_ms=deadline,
    )
    assert choice.phase is LanguageChoicePhase.CLEANUP_PENDING
    assert choice.advance_time(
        lifecycle=voice,
        at_ms=deadline + 1,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


def test_cleanup_completion_and_boundary_onset_arbitrate_atomically():
    _, voice, speech, choice, reserved = _cleanup_pending_choice()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    assert speech.complete_reservation(reserved)
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup = executor.submit(choice.complete_prompt_cleanup)
        activity = executor.submit(
            choice.accept_activity_started,
            event=onset,
            lifecycle=voice,
        )

    assert activity.result()
    assert cleanup.result() in {True, False}
    assert choice.phase is LanguageChoicePhase.ACTIVITY_OPEN
    assert not speech.tracks_reservation_batch(reserved)
    assert all(not speech.is_live(item.act_id) for item in reserved)


def test_pre_prompt_activity_cannot_open_the_post_prompt_window():
    _, voice, speech, choice, reserved = _setup_choice(
        begin_presentation=False
    )
    stale = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=1,
    )
    assert choice.begin_presentation(lifecycle=voice)
    for ordinal in range(3):
        _observe_segment(
            voice=voice,
            speech=speech,
            choice=choice,
            reserved=reserved,
            ordinal=ordinal,
            at_ms=100 + ordinal * 10,
        )
    assert speech.complete_reservation(reserved)
    assert choice.complete_prompt_cleanup()
    deadline = choice.response_deadline_ms
    assert deadline is not None

    assert not choice.accept_activity_started(
        event=stale,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.RESPONSE_WINDOW
    assert choice.advance_time(
        lifecycle=voice,
        at_ms=deadline + 1,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


def test_wrong_turn_end_cannot_stall_activity_timeout():
    _, voice, _, choice, _ = _open_response_window()
    response_deadline = choice.response_deadline_ms
    assert response_deadline is not None
    accepted_onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=response_deadline,
        turn_id="accepted_language_turn",
    )
    assert choice.accept_activity_started(
        event=accepted_onset,
        lifecycle=voice,
    )
    wrong_onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=accepted_onset.at_ms + 1,
        turn_id="wrong_language_turn",
    )
    wrong_end = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_ENDED,
        at_ms=wrong_onset.at_ms + 1,
        turn_id="wrong_language_turn",
    )

    assert not choice.accept_activity_ended(
        event=wrong_end,
        lifecycle=voice,
    )
    activity_deadline = choice.activity_deadline_ms
    assert activity_deadline is not None
    assert choice.advance_time(
        lifecycle=voice,
        at_ms=activity_deadline + 1,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


def test_retained_intermediate_activity_is_not_hidden_by_newer_end_event():
    _, voice, _, choice, _ = _open_response_window()
    response_deadline = choice.response_deadline_ms
    assert response_deadline is not None
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=response_deadline,
    )
    ended = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_ENDED,
        at_ms=onset.at_ms + 1,
    )

    assert not choice.advance_time(
        lifecycle=voice,
        at_ms=ended.at_ms + 1,
    )
    assert choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    assert choice.accept_activity_ended(
        event=ended,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.FINALIZING


def test_later_onset_callback_cannot_skip_earlier_canonical_onset():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    earlier = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline,
        turn_id="earlier_onset_turn",
    )
    later = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline + 1,
        turn_id="later_onset_turn",
    )

    assert not choice.accept_activity_started(
        event=later,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.RESPONSE_WINDOW
    assert choice.accept_activity_started(
        event=earlier,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.ACTIVITY_OPEN


def test_later_end_callback_cannot_skip_earlier_accepted_turn_end():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline,
        turn_id="accepted_end_turn",
    )
    assert choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    earlier_end = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_ENDED,
        at_ms=onset.at_ms + 1,
        turn_id="accepted_end_turn",
    )
    wrong_onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=earlier_end.at_ms + 1,
        turn_id="later_wrong_end_turn",
    )
    later_wrong_end = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_ENDED,
        at_ms=wrong_onset.at_ms + 1,
        turn_id="later_wrong_end_turn",
    )

    assert not choice.accept_activity_ended(
        event=later_wrong_end,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.ACTIVITY_OPEN
    assert choice.accept_activity_ended(
        event=earlier_end,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.FINALIZING


def test_later_wrong_final_cannot_skip_earlier_eligible_final():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline,
        turn_id="accepted_final_turn",
    )
    assert choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    eligible = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=onset.at_ms + 1,
        turn_id="accepted_final_turn",
    )
    later_wrong = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=eligible.at_ms + 1,
        turn_id="later_wrong_final_turn",
    )

    assert choice.stage_final(
        event=later_wrong,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.REJECTED
    assert choice.phase is LanguageChoicePhase.ACTIVITY_OPEN
    assert choice.stage_final(
        event=eligible,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.QUALIFIED
    assert choice.phase is LanguageChoicePhase.FINALIZING


def test_input_history_gap_fails_closed_instead_of_guessing_order():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    for index in range(257):
        _input_event(
            voice,
            kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
            at_ms=deadline,
            turn_id=f"bounded_history_turn_{index}",
        )

    assert choice.advance_time(
        lifecycle=voice,
        at_ms=deadline,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


@pytest.mark.parametrize("offset", (9_999, 10_000))
def test_final_at_response_boundary_is_eligible(offset: int):
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline - 10_000 + offset,
    )

    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.QUALIFIED
    assert choice.phase is LanguageChoicePhase.FINALIZING


def test_final_at_10001_without_onset_times_out():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline + 1,
    )

    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.UNQUALIFIED
    assert choice.phase is LanguageChoicePhase.TERMINAL


@pytest.mark.parametrize("offset", (9_999, 10_000))
def test_eligible_onset_suspends_response_timeout(offset: int):
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline - 10_000 + offset,
    )
    assert choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    ended = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_ENDED,
        at_ms=deadline + 1,
    )
    assert choice.accept_activity_ended(
        event=ended,
        lifecycle=voice,
    )
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=ended.at_ms + LANGUAGE_CHOICE_FINALIZATION_MS,
    )

    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="zh",
    ) is LanguageFinalDisposition.QUALIFIED


def test_onset_at_10001_is_timeout():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    onset = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
        at_ms=deadline + 1,
    )

    assert not choice.accept_activity_started(
        event=onset,
        lifecycle=voice,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


def test_speech_and_finalization_boundaries_are_inclusive_then_fail_closed():
    for end_delta, final_delta, accepted in (
        (15_000, 2_000, True),
        (15_001, 0, False),
        (1_000, 2_001, False),
    ):
        _, voice, _, choice, _ = _open_response_window()
        deadline = choice.response_deadline_ms
        assert deadline is not None
        onset = _input_event(
            voice,
            kind=VoiceEventKind.INPUT_ACTIVITY_STARTED,
            at_ms=deadline,
        )
        assert choice.accept_activity_started(
            event=onset,
            lifecycle=voice,
        )
        ended = _input_event(
            voice,
            kind=VoiceEventKind.INPUT_ACTIVITY_ENDED,
            at_ms=onset.at_ms + end_delta,
        )
        ended_accepted = choice.accept_activity_ended(
            event=ended,
            lifecycle=voice,
        )
        if end_delta > LANGUAGE_CHOICE_MAX_SPEECH_MS:
            assert not ended_accepted
            assert choice.phase is LanguageChoicePhase.TERMINAL
            continue
        assert ended_accepted
        final = _input_event(
            voice,
            kind=VoiceEventKind.INPUT_TURN_FINAL,
            at_ms=ended.at_ms + final_delta,
        )
        disposition = choice.stage_final(
            event=final,
            lifecycle=voice,
            detected_locale="es",
        )
        assert (
            disposition is LanguageFinalDisposition.QUALIFIED
        ) is accepted
        assert (
            choice.phase is LanguageChoicePhase.TERMINAL
        ) is (not accepted)


def test_fake_clock_expires_only_after_exact_deadline():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None

    assert not choice.advance_time(
        lifecycle=voice,
        at_ms=deadline,
    )
    assert choice.phase is LanguageChoicePhase.RESPONSE_WINDOW
    assert choice.advance_time(
        lifecycle=voice,
        at_ms=deadline + 1,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


def test_accepted_boundary_final_wins_concurrent_timeout_arbitration():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        timeout = executor.submit(
            choice.advance_time,
            lifecycle=voice,
            at_ms=deadline + 1,
        )
        staged = executor.submit(
            choice.stage_final,
            event=final,
            lifecycle=voice,
            detected_locale="en",
        )

    assert not timeout.result()
    assert staged.result() is LanguageFinalDisposition.QUALIFIED
    assert choice.phase is LanguageChoicePhase.FINALIZING


@pytest.mark.parametrize(
    "detected_locale",
    ("pt", "fr_fr", "ar_msa", "ambiguous", None),
)
def test_unqualified_missing_and_ambiguous_final_are_no_audio_terminal(
    detected_locale,
):
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline,
    )

    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale=detected_locale,
    ) is LanguageFinalDisposition.UNQUALIFIED
    assert choice.phase is LanguageChoicePhase.TERMINAL
    assert choice.issue_terminal_receipt(
        inventory=OfflineAuthorityInventory(
            transaction_pending=1,
            admission_receipts=0,
            silence_pending=0,
            speech_batches=0,
            live_speech_acts=0,
            queued_outbound_frames=0,
            call_quiescent=True,
            call_terminated=True,
            adapter_terminally_closed=True,
        ),
        at_ms=final.at_ms + 1,
    ) is None
    receipt = choice.issue_terminal_receipt(
        inventory=_sealed_inventory(),
        at_ms=final.at_ms + 1,
    )
    assert receipt is not None
    assert receipt.outcome is (
        LanguageChoiceTerminalOutcome.NO_AUDIO_TEARDOWN
    )
    assert not receipt.satisfies_playback_observation
    assert not receipt.satisfies_disconnect_observation


def test_recovery_pair_is_exact_one_use_and_content_bound():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline,
    )
    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.QUALIFIED
    receipt = _recovery_receipt(choice, final)
    admission = choice.bind_recovery_receipt(receipt)
    assert admission is not None
    assert choice.pending_pair_count == 1

    assert choice.consume_recovery_pair(
        receipt=receipt,
        admission=admission,
        now_ms=final.at_ms + 1,
    )
    assert choice.phase is LanguageChoicePhase.RECOVERY_CONSUMED
    assert choice.pending_pair_count == 0
    assert choice.validate_recovery_locale("en")
    assert choice.phase is LanguageChoicePhase.RECOVERY_VALIDATED
    assert choice.commit_recovery()
    assert choice.phase is LanguageChoicePhase.RECOVERED
    assert not choice.consume_recovery_pair(
        receipt=receipt,
        admission=admission,
        now_ms=final.at_ms + 2,
    )
    assert choice.phase is LanguageChoicePhase.TERMINAL


def test_failed_pair_publication_tombstones_without_orphan_authority():
    _, voice, _, choice, _ = _open_response_window()
    deadline = choice.response_deadline_ms
    assert deadline is not None
    final = _input_event(
        voice,
        kind=VoiceEventKind.INPUT_TURN_FINAL,
        at_ms=deadline,
    )
    assert choice.stage_final(
        event=final,
        lifecycle=voice,
        detected_locale="en",
    ) is LanguageFinalDisposition.QUALIFIED
    wrong_generation = _recovery_receipt(
        choice,
        final,
        generation=choice.generation + 1,
    )

    assert choice.bind_recovery_receipt(wrong_generation) is None
    assert choice.pending_pair_count == 0
    assert choice.phase is LanguageChoicePhase.TERMINAL
