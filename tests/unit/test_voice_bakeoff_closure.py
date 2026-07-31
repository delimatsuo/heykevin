"""Authority and race qualification for local offline opt-out closure."""

from __future__ import annotations

import ast
import hashlib
import threading
from dataclasses import fields, replace
from pathlib import Path

import pytest

import app.services.voice_bakeoff_closure as closure_module
from app.services.voice_bakeoff_closure import (
    GenericFailureProofReceipt,
    OfflineAuthorityInventory,
    OfflineClosureDestination,
    OfflineClosurePhase,
    OfflineClosurePrivacy,
    OfflineClosureStep,
    OfflineClosureTransport,
    OfflineLocalClosureAuthority,
    generic_failure_text_digest,
    opt_out_text_digest,
)
from app.services.voice_bakeoff_turn_composition import (
    CompositionPhase,
    CompositionResult,
    CompositionStatus,
)
from app.services.voice_lifecycle import VoiceSessionBinding

_CONTRACT_DIGEST = "a" * 64
_SOURCE_PATH = Path("app/services/voice_bakeoff_closure.py")
_LIVE_ROUTE_PATHS = (
    Path("app/main.py"),
    Path("app/webhooks/media_stream.py"),
    Path("app/experiments/voice_bakeoff_app.py"),
)


def _binding(*, epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding(
        environment="bakeoff_offline",
        contractor_binding="synthetic_tenant",
        call_binding="synthetic_call",
        stream_binding="synthetic_stream",
        epoch=epoch,
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


def _failure_record() -> CompositionResult:
    return CompositionResult(
        status=CompositionStatus.CLOSURE_REQUIRED,
        phase=CompositionPhase.EXTRACTION_TERMINAL,
        receipt_id="receipt_generic_failure",
        input_turn_id="turn_generic_failure",
        state_version=2,
    )


def _failure_snapshot(
    *, locale: str = "en",
) -> dict[str, object]:
    return {
        "call_sid": "synthetic_call",
        "language": locale,
        "side_effects_allowed": False,
    }


def _registered(*, locale: str = "en"):
    authority = OfflineLocalClosureAuthority()
    facade = object()
    leased = object()
    active = object()
    driver = object()
    participant = object()
    assert authority.register_leased(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=participant,
        lease_revision=0,
        expires_at_ms=1_000,
        arm="b1",
        journey="opt_out_withdrawal",
        contract_digest=_CONTRACT_DIGEST,
        binding=_binding(),
        locale=locale,
    )
    return authority, facade, leased, active, driver, participant


def _active(*, locale: str = "en"):
    (
        authority,
        facade,
        leased,
        active,
        driver,
        participant,
    ) = _registered(locale=locale)
    confirmation = authority.confirm_scripted_step(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=participant,
        now_ms=1,
    )
    assert confirmation is not None
    assert authority.activate(
        facade=facade,
        leased_record=leased,
        active_record=active,
        driver_identity=driver,
        active_revision=1,
    )
    return authority, facade, active, confirmation


def _capable(*, locale: str = "en"):
    authority, facade, active, confirmation = _active(
        locale=locale
    )
    capability = authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    )
    assert capability is not None
    return authority, facade, active, capability


def _staged(*, locale: str = "en"):
    authority, facade, active, capability = _capable(
        locale=locale
    )
    stage = authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=3,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    )
    assert stage is not None
    return authority, facade, active, capability, stage


def _owned_stage_payload(
    authority: OfflineLocalClosureAuthority,
) -> bytearray:
    entry = authority._entry
    assert entry is not None
    stage = entry.state.stage
    assert stage is not None
    return stage.frame.payload


def _generic_active(*, locale: str = "en"):
    authority = OfflineLocalClosureAuthority(
        generic_failure_record_type=CompositionResult,
    )
    facade = object()
    leased = object()
    active = object()
    driver = object()
    assert authority.register_leased(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=object(),
        lease_revision=0,
        expires_at_ms=1_000,
        arm="b1",
        journey="generic_failure_closure",
        contract_digest=_CONTRACT_DIGEST,
        binding=_binding(),
        locale=locale,
        step=OfflineClosureStep.GENERIC_FAILURE,
    )
    assert authority.activate(
        facade=facade,
        leased_record=leased,
        active_record=active,
        driver_identity=driver,
        active_revision=1,
    )
    return authority, facade, active, driver


def _generic_capable(*, locale: str = "en"):
    authority, facade, active, driver = _generic_active(
        locale=locale
    )
    failure_record = _failure_record()
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )
    proof = authority.admit_generic_failure(
        facade=facade,
        active_record=active,
        driver_identity=driver,
        failure_record=failure_record,
        state_version=2,
        state_snapshot=_failure_snapshot(locale=locale),
        latest_locale=locale,
        destination=OfflineClosureDestination.SYNTHETIC_LOOPBACK,
        privacy=OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB,
        transport=OfflineClosureTransport.LOCAL_READY,
        inventory=_sealed_inventory(),
        now_ms=2,
    )
    assert proof is not None
    capability = authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=proof,
        inventory=_sealed_inventory(),
        now_ms=3,
    )
    assert capability is not None
    return authority, facade, active, proof, capability


def _generic_committed(*, locale: str = "en"):
    authority, facade, active, proof, capability = (
        _generic_capable(locale=locale)
    )
    stage = authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    )
    assert stage is not None
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=5,
    )
    assert commit is not None
    return authority, facade, active, proof, capability, stage, commit


def test_generic_registration_requires_an_exact_record_type():
    authority = OfflineLocalClosureAuthority()

    assert not authority.register_leased(
        facade=object(),
        leased_record=object(),
        driver_identity=object(),
        participant_surrogate=object(),
        lease_revision=0,
        expires_at_ms=1_000,
        arm="b1",
        journey="generic_failure_closure",
        contract_digest=_CONTRACT_DIGEST,
        binding=_binding(),
        locale="en",
        step=OfflineClosureStep.GENERIC_FAILURE,
    )


def test_generic_proof_rejects_a_duck_typed_failure_record():
    class DuckFailureRecord:
        status = CompositionStatus.CLOSURE_REQUIRED
        phase = CompositionPhase.EXTRACTION_TERMINAL
        state_version = 2
        act_ids = ()
        act_kinds = ()

    authority, facade, active, driver = _generic_active()
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )

    assert authority.admit_generic_failure(
        facade=facade,
        active_record=active,
        driver_identity=driver,
        failure_record=DuckFailureRecord(),
        state_version=2,
        state_snapshot=_failure_snapshot(),
        latest_locale="en",
        destination=OfflineClosureDestination.SYNTHETIC_LOOPBACK,
        privacy=OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB,
        transport=OfflineClosureTransport.LOCAL_READY,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None


@pytest.mark.parametrize(
    "snapshot",
    (
        {
            "call_sid": "other_call",
            "language": "en",
            "side_effects_allowed": False,
        },
        {
            "call_sid": "synthetic_call",
            "language": "es",
            "side_effects_allowed": False,
        },
        {
            "call_sid": "synthetic_call",
            "language": "en",
            "side_effects_allowed": True,
        },
        {
            "call_sid": "synthetic_call",
            "language": "en",
        },
        {
            "call_sid": "synthetic_call",
            "language": "en",
            "side_effects_allowed": False,
            "caller_identity": "must_not_be_retained",
        },
    ),
)
def test_generic_proof_requires_semantically_bound_snapshot(
    snapshot: dict[str, object],
):
    authority, facade, active, driver = _generic_active()
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )

    assert authority.admit_generic_failure(
        facade=facade,
        active_record=active,
        driver_identity=driver,
        failure_record=_failure_record(),
        state_version=2,
        state_snapshot=snapshot,
        latest_locale="en",
        destination=OfflineClosureDestination.SYNTHETIC_LOOPBACK,
        privacy=OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB,
        transport=OfflineClosureTransport.LOCAL_READY,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None


def test_generic_proof_validates_only_its_private_snapshot_copy(
    monkeypatch: pytest.MonkeyPatch,
):
    authority, facade, active, driver = _generic_active()
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )
    caller_snapshot = _failure_snapshot()
    copy_started = threading.Event()
    allow_copy = threading.Event()
    original_deepcopy = closure_module.deepcopy

    def blocked_deepcopy(value):
        if value is caller_snapshot:
            copy_started.set()
            assert allow_copy.wait(timeout=2)
        return original_deepcopy(value)

    monkeypatch.setattr(
        closure_module,
        "deepcopy",
        blocked_deepcopy,
    )
    proofs = []
    worker = threading.Thread(
        target=lambda: proofs.append(
            authority.admit_generic_failure(
                facade=facade,
                active_record=active,
                driver_identity=driver,
                failure_record=_failure_record(),
                state_version=2,
                state_snapshot=caller_snapshot,
                latest_locale="en",
                destination=(
                    OfflineClosureDestination.SYNTHETIC_LOOPBACK
                ),
                privacy=(
                    OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB
                ),
                transport=OfflineClosureTransport.LOCAL_READY,
                inventory=_sealed_inventory(),
                now_ms=2,
            )
        )
    )
    worker.start()
    assert copy_started.wait(timeout=2)
    caller_snapshot["side_effects_allowed"] = True
    allow_copy.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert proofs == [None]
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is (
        OfflineClosurePhase.GENERAL_AUTHORITY_SEALED
    )
    assert not snapshot.confirmation_live


@pytest.mark.parametrize(
    ("locale", "expected_text", "expected_digest"),
    (
        (
            "en",
            "Okay. I’ll stop this test call now. Goodbye.",
            "bd79a37b7abe7e5311eb00e96fb5e2bc14b81e9e78933e930ded109e70787bff",
        ),
        (
            "es",
            (
                "De acuerdo. Finalizaré esta llamada de prueba "
                "ahora. Adiós."
            ),
            "6580c58f06b53ef873c3a000c37f3a2d839df332c56b5f9e3d457896aabcc976",
        ),
        (
            "zh",
            "好的。我现在结束这次测试通话。再见。",
            "07a670fe45b7415e3cc6d474d9a9300e9ed670cc6b4cec322ca04636f64f2bb6",
        ),
    ),
)
def test_exact_reviewed_assets_stage_and_commit_once(
    locale: str,
    expected_text: str,
    expected_digest: str,
):
    authority, facade, active, capability, stage = _staged(
        locale=locale
    )

    assert stage.text == expected_text
    assert stage.text_digest == expected_digest
    assert stage.frame_ordinal == 0
    assert stage.frame_duration_ms == 20
    assert stage.frame_byte_count == 160
    assert stage.audio_digest == hashlib.sha256(
        _owned_stage_payload(authority)
    ).hexdigest()
    assert opt_out_text_digest(locale) == expected_digest
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    assert commit.text_digest == expected_digest
    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=5,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is OfflineClosurePhase.COMMITTED
    assert snapshot.committed_frame_count == 1
    assert snapshot.confirmation_tombstoned
    assert snapshot.capability_tombstoned
    assert not snapshot.capability_live


@pytest.mark.parametrize(
    ("locale", "expected_text", "expected_digest"),
    (
        (
            "en",
            "I’m sorry, I can’t continue this test call. Goodbye.",
            "3875698272528850f8a6097bbd9e2076b2c14441badb77574516427201bba303",
        ),
        (
            "es",
            (
                "Lo siento, no puedo continuar con esta llamada "
                "de prueba. Adiós."
            ),
            "9cc751340092ffbdefc119d416c4b5e1ad59e939af864442ea95a2eb72f8a8be",
        ),
        (
            "zh",
            "抱歉，我无法继续这次测试通话。再见。",
            "e3c6136a7cbf0868713c392d464497af2cefc95e76802bc1eb61c1605e861fec",
        ),
    ),
)
def test_generic_failure_assets_are_kind_bound_and_consumed_atomically(
    locale: str,
    expected_text: str,
    expected_digest: str,
):
    (
        authority,
        facade,
        active,
        proof,
        capability,
        stage,
        commit,
    ) = _generic_committed(locale=locale)
    entry = authority._entry
    assert entry is not None
    owned_payload = entry.state.commit.frame.payload

    assert type(proof) is GenericFailureProofReceipt
    assert proof.step is OfflineClosureStep.GENERIC_FAILURE
    assert proof.binding is not entry.state.binding
    assert proof.state_snapshot_digest == (
        closure_module._failure_snapshot_digest(
            _failure_snapshot(locale=locale)
        )
    )
    assert capability.step is OfflineClosureStep.GENERIC_FAILURE
    assert stage.step is OfflineClosureStep.GENERIC_FAILURE
    assert stage.text == expected_text
    assert stage.text_digest == expected_digest
    assert commit.step is OfflineClosureStep.GENERIC_FAILURE
    assert generic_failure_text_digest(locale) == expected_digest
    assert authority.committed_frame(
        facade=facade,
        active_record=active,
    ) is None
    assert not authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )

    frame = authority.consume_for_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
        invalidation_generation=0,
        now_ms=6,
    )

    assert frame is not None
    assert frame.ordinal == 0
    assert frame.duration_ms == 20
    assert len(frame.payload) == 160
    assert not any(owned_payload)
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is (
        OfflineClosurePhase.FRAME_CONSUMED_FOR_SYNTHETIC_PLAYBACK
    )
    assert snapshot.frame_consumed
    assert snapshot.synthetic_playback_observed
    assert snapshot.committed_frame_count == 0
    assert authority.committed_frame(
        facade=facade,
        active_record=active,
    ) is None
    assert authority.consume_for_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
        invalidation_generation=0,
        now_ms=6,
    ) is None


def test_generic_invalidation_before_atomic_consume_is_silent_and_scrubs():
    (
        authority,
        facade,
        active,
        _proof,
        _capability,
        _stage,
        commit,
    ) = _generic_committed()
    entry = authority._entry
    assert entry is not None
    owned_payload = entry.state.commit.frame.payload

    assert authority.invalidate(
        facade=facade,
        active_record=active,
    )
    assert not any(owned_payload)
    assert authority.consume_for_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
        invalidation_generation=0,
        now_ms=6,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is OfflineClosurePhase.NO_AUDIO_TEARDOWN
    assert snapshot.invalidated
    assert snapshot.invalidation_generation == 1
    assert not snapshot.synthetic_playback_observed


def test_generic_expiry_before_atomic_consume_selects_no_audio():
    (
        authority,
        facade,
        active,
        _proof,
        _capability,
        _stage,
        commit,
    ) = _generic_committed()
    entry = authority._entry
    assert entry is not None
    owned_payload = entry.state.commit.frame.payload

    assert authority.consume_for_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
        invalidation_generation=0,
        now_ms=1_001,
    ) is None

    assert not any(owned_payload)
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is OfflineClosurePhase.NO_AUDIO_TEARDOWN
    assert snapshot.invalidated
    assert snapshot.invalidation_generation == 1
    assert not snapshot.frame_consumed
    assert not snapshot.synthetic_playback_observed


@pytest.mark.parametrize(
    ("invalidation_generation", "now_ms"),
    (
        (-1, 6),
        (0, -1),
        (True, 6),
        (0, True),
    ),
)
def test_malformed_consume_input_irreversibly_selects_no_audio(
    invalidation_generation: object,
    now_ms: object,
):
    (
        authority,
        facade,
        active,
        _proof,
        _capability,
        _stage,
        commit,
    ) = _generic_committed()
    entry = authority._entry
    assert entry is not None
    owned_payload = entry.state.commit.frame.payload

    assert authority.consume_for_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
        invalidation_generation=invalidation_generation,
        now_ms=now_ms,
    ) is None
    assert not any(owned_payload)
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is OfflineClosurePhase.NO_AUDIO_TEARDOWN
    assert snapshot.invalidated
    assert snapshot.invalidation_generation == 1
    assert not snapshot.frame_consumed
    assert not snapshot.synthetic_playback_observed
    assert authority.consume_for_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
        invalidation_generation=1,
        now_ms=7,
    ) is None


def test_generic_invalidation_during_commit_construction_wins_silently(
    monkeypatch: pytest.MonkeyPatch,
):
    authority, facade, active, _proof, capability = (
        _generic_capable()
    )
    stage = authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    )
    assert stage is not None
    owned_payload = _owned_stage_payload(authority)
    commit_build_started = threading.Event()
    allow_commit_build = threading.Event()
    original_token = closure_module._token

    def blocked_token(domain: bytes, *parts: str) -> str:
        if domain == closure_module._COMMIT_DOMAIN:
            commit_build_started.set()
            assert allow_commit_build.wait(timeout=2)
        return original_token(domain, *parts)

    monkeypatch.setattr(closure_module, "_token", blocked_token)
    commits = []
    worker = threading.Thread(
        target=lambda: commits.append(
            authority.commit(
                facade=facade,
                active_record=active,
                capability=capability,
                stage=stage,
                now_ms=5,
            )
        )
    )
    worker.start()
    assert commit_build_started.wait(timeout=2)

    assert authority.invalidate(
        facade=facade,
        active_record=active,
    )
    allow_commit_build.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert commits == [None]
    assert not any(owned_payload)
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.phase is OfflineClosurePhase.NO_AUDIO_TEARDOWN
    assert snapshot.invalidated
    assert snapshot.committed_frame_count == 0
    assert not snapshot.synthetic_playback_observed


def test_atomic_consume_linearizes_before_concurrent_invalidation(
    monkeypatch: pytest.MonkeyPatch,
):
    (
        authority,
        facade,
        active,
        _proof,
        _capability,
        _stage,
        commit,
    ) = _generic_committed()
    copy_started = threading.Event()
    allow_copy = threading.Event()
    original_frame = closure_module.OfflineClosureCommittedFrame

    def blocked_frame(**kwargs):
        copy_started.set()
        assert allow_copy.wait(timeout=2)
        return original_frame(**kwargs)

    monkeypatch.setattr(
        closure_module,
        "OfflineClosureCommittedFrame",
        blocked_frame,
    )
    consumed = []
    invalidated = []
    consume_thread = threading.Thread(
        target=lambda: consumed.append(
            authority.consume_for_synthetic_playback(
                facade=facade,
                active_record=active,
                commit=commit,
                invalidation_generation=0,
                now_ms=6,
            )
        )
    )
    consume_thread.start()
    assert copy_started.wait(timeout=2)
    invalidate_thread = threading.Thread(
        target=lambda: invalidated.append(
            authority.invalidate(
                facade=facade,
                active_record=active,
            )
        )
    )
    invalidate_thread.start()
    allow_copy.set()
    consume_thread.join(timeout=2)
    invalidate_thread.join(timeout=2)

    assert not consume_thread.is_alive()
    assert not invalidate_thread.is_alive()
    assert len(consumed) == 1
    assert consumed[0] is not None
    assert invalidated == [True]
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.frame_consumed
    assert snapshot.synthetic_playback_observed
    assert not snapshot.invalidated


def test_generic_failure_rejects_unapproved_locale_and_cross_kind_copy():
    authority, facade, active, driver = _generic_active(locale="pt")
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )
    assert authority.admit_generic_failure(
        facade=facade,
        active_record=active,
        driver_identity=driver,
        failure_record=object(),
        state_version=2,
        state_snapshot=_failure_snapshot(locale="pt"),
        latest_locale="pt",
        destination=OfflineClosureDestination.SYNTHETIC_LOOPBACK,
        privacy=OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB,
        transport=OfflineClosureTransport.LOCAL_READY,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None
    assert generic_failure_text_digest("pt") is None

    authority, facade, active, _proof, capability = (
        _generic_capable()
    )
    copied = replace(
        capability,
        step=OfflineClosureStep.SCRIPTED_OPT_OUT,
    )
    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=copied,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is None


def test_generic_proof_derives_and_revalidates_record_and_snapshot():
    authority, facade, active, driver = _generic_active()
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )
    assert authority.admit_generic_failure(
        facade=facade,
        active_record=active,
        driver_identity=driver,
        failure_record=object(),
        state_version=2,
        state_snapshot=_failure_snapshot(),
        latest_locale="en",
        destination=OfflineClosureDestination.SYNTHETIC_LOOPBACK,
        privacy=OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB,
        transport=OfflineClosureTransport.LOCAL_READY,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None

    authority, facade, active, driver = _generic_active()
    assert authority.seal_general_authority(
        facade=facade,
        active_record=active,
        inventory=_sealed_inventory(),
    )
    caller_snapshot = _failure_snapshot()
    proof = authority.admit_generic_failure(
        facade=facade,
        active_record=active,
        driver_identity=driver,
        failure_record=_failure_record(),
        state_version=2,
        state_snapshot=caller_snapshot,
        latest_locale="en",
        destination=OfflineClosureDestination.SYNTHETIC_LOOPBACK,
        privacy=OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB,
        transport=OfflineClosureTransport.LOCAL_READY,
        inventory=_sealed_inventory(),
        now_ms=2,
    )
    assert proof is not None
    caller_snapshot["language"] = "zh"
    capability = authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=proof,
        inventory=_sealed_inventory(),
        now_ms=3,
    )
    assert capability is not None

    authority, facade, active, proof, _capability = (
        _generic_capable()
    )
    entry = authority._entry
    assert entry is not None
    entry.state.failure_state_snapshot["language"] = "zh"
    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=entry.state.capability,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is None

    authority, facade, active, _proof, _capability = (
        _generic_capable()
    )
    entry = authority._entry
    assert entry is not None
    entry.state.failure_state_snapshot[
        "side_effects_allowed"
    ] = True
    entry.state = replace(
        entry.state,
        failure_snapshot_digest=(
            closure_module._failure_snapshot_digest(
                entry.state.failure_state_snapshot
            )
        ),
    )
    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=entry.state.capability,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is None


def test_closure_asset_catalogs_are_immutable():
    with pytest.raises(TypeError):
        closure_module._GENERIC_FAILURE_TEXT["en"] = "changed"
    with pytest.raises(TypeError):
        closure_module._GENERIC_FAILURE_TEXT_DIGESTS["en"] = "0" * 64
    with pytest.raises(TypeError):
        closure_module._OPT_OUT_TEXT["en"] = "changed"


def test_confirmation_copy_cannot_mint_and_cleanup_failure_retains_original():
    authority, facade, active, confirmation = _active()
    copied = replace(confirmation)
    failed_inventory = replace(
        _sealed_inventory(),
        live_speech_acts=1,
    )

    assert authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=copied,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None
    assert authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=failed_inventory,
        now_ms=2,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.confirmation_live
    assert not snapshot.confirmation_tombstoned
    capability = authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=3,
    )
    assert capability is not None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert not snapshot.confirmation_live
    assert snapshot.confirmation_tombstoned
    assert snapshot.capability_live
    assert authority.tombstone_count == 1


def test_registry_binding_is_private_and_public_bindings_are_distinct():
    authority = OfflineLocalClosureAuthority()
    facade = object()
    leased = object()
    active = object()
    driver = object()
    participant = object()
    supplied_binding = _binding()
    assert authority.register_leased(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=participant,
        lease_revision=0,
        expires_at_ms=1_000,
        arm="b1",
        journey="opt_out_withdrawal",
        contract_digest=_CONTRACT_DIGEST,
        binding=supplied_binding,
        locale="en",
    )
    entry = authority._entry
    assert entry is not None
    private_binding = entry.state.binding
    assert private_binding == supplied_binding
    assert private_binding is not supplied_binding
    object.__setattr__(
        supplied_binding,
        "call_binding",
        "mutated_call",
    )
    assert private_binding.call_binding == "synthetic_call"

    confirmation = authority.confirm_scripted_step(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=participant,
        now_ms=1,
    )
    assert confirmation is not None
    assert confirmation.binding == private_binding
    assert confirmation.binding is not private_binding
    assert authority.activate(
        facade=facade,
        leased_record=leased,
        active_record=active,
        driver_identity=driver,
        active_revision=1,
    )
    capability = authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    )
    assert capability is not None
    assert capability.binding == private_binding
    assert capability.binding is not private_binding
    assert capability.binding is not confirmation.binding


@pytest.mark.parametrize(
    "field",
    (
        "environment",
        "contractor_binding",
        "call_binding",
        "stream_binding",
        "epoch",
    ),
)
def test_register_rejects_malformed_nested_binding_without_raising(
    field: str,
):
    authority = OfflineLocalClosureAuthority()
    binding = _binding()
    object.__setattr__(binding, field, object())

    assert not authority.register_leased(
        facade=object(),
        leased_record=object(),
        driver_identity=object(),
        participant_surrogate=object(),
        lease_revision=0,
        expires_at_ms=1_000,
        arm="b1",
        journey="opt_out_withdrawal",
        contract_digest=_CONTRACT_DIGEST,
        binding=binding,
        locale="en",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("environment", "other_environment"),
        ("contractor_binding", "other_tenant"),
        ("call_binding", "other_call"),
        ("stream_binding", "other_stream"),
        ("epoch", 2),
    ),
)
def test_nested_confirmation_binding_mutation_cannot_change_registry(
    field: str,
    value: object,
):
    authority, facade, active, confirmation = _active()
    entry = authority._entry
    assert entry is not None
    private_binding = entry.state.binding
    original = getattr(confirmation.binding, field)
    object.__setattr__(confirmation.binding, field, value)

    assert getattr(private_binding, field) == original
    assert authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None
    object.__setattr__(confirmation.binding, field, original)
    capability = authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=3,
    )
    assert capability is not None
    assert capability.binding == private_binding
    assert capability.confirmation_id == confirmation.confirmation_id


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("environment", "other_environment"),
        ("contractor_binding", "other_tenant"),
        ("call_binding", "other_call"),
        ("stream_binding", "other_stream"),
        ("epoch", 2),
    ),
)
def test_nested_capability_binding_mutation_cannot_cross_session(
    field: str,
    value: object,
):
    authority, facade, active, capability = _capable()
    entry = authority._entry
    assert entry is not None
    private_binding = entry.state.binding
    canonical_capability_id = capability.capability_id
    original = getattr(capability.binding, field)
    object.__setattr__(capability.binding, field, value)

    assert getattr(private_binding, field) == original
    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=3,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is None
    object.__setattr__(capability.binding, field, original)
    stage = authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    )
    assert stage is not None
    assert stage.capability_id == canonical_capability_id
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=5,
    )
    assert commit is not None
    assert authority.committed_frame(
        facade=facade,
        active_record=active,
    ) is not None
    assert authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )


@pytest.mark.parametrize(
    "field",
    (
        "environment",
        "contractor_binding",
        "call_binding",
        "stream_binding",
        "epoch",
    ),
)
@pytest.mark.parametrize(
    "receipt_kind",
    ("confirmation", "capability"),
)
def test_malformed_nested_public_binding_fails_without_raising(
    field: str,
    receipt_kind: str,
):
    if receipt_kind == "confirmation":
        authority, facade, active, confirmation = _active()
        original = getattr(confirmation.binding, field)
        object.__setattr__(confirmation.binding, field, object())
        assert authority.mint_capability(
            facade=facade,
            active_record=active,
            confirmation=confirmation,
            inventory=_sealed_inventory(),
            now_ms=2,
        ) is None
        object.__setattr__(confirmation.binding, field, original)
        assert authority.mint_capability(
            facade=facade,
            active_record=active,
            confirmation=confirmation,
            inventory=_sealed_inventory(),
            now_ms=3,
        ) is not None
        return

    authority, facade, active, capability = _capable()
    original = getattr(capability.binding, field)
    object.__setattr__(capability.binding, field, object())
    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=3,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is None
    object.__setattr__(capability.binding, field, original)
    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=4,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is not None


@pytest.mark.parametrize(
    "receipt_kind",
    ("confirmation", "capability"),
)
@pytest.mark.parametrize(
    "raises",
    (False, True),
)
def test_public_binding_never_invokes_nested_equality(
    receipt_kind: str,
    raises: bool,
):
    class AdversarialEquality:
        def __init__(self) -> None:
            self.calls = 0

        def __eq__(self, other: object) -> bool:
            del other
            self.calls += 1
            if raises:
                raise RuntimeError("must not execute")
            return True

    adversarial = AdversarialEquality()
    if receipt_kind == "confirmation":
        authority, facade, active, confirmation = _active()
        object.__setattr__(
            confirmation.binding,
            "call_binding",
            adversarial,
        )
        assert authority.mint_capability(
            facade=facade,
            active_record=active,
            confirmation=confirmation,
            inventory=_sealed_inventory(),
            now_ms=2,
        ) is None
    else:
        authority, facade, active, capability = _capable()
        object.__setattr__(
            capability.binding,
            "stream_binding",
            adversarial,
        )
        assert authority.stage(
            facade=facade,
            active_record=active,
            capability=capability,
            now_ms=3,
            max_frame_bytes=320,
            max_outbound_frames=1,
            max_outbound_bytes=320,
            max_outbound_audio_ms=20,
        ) is None

    assert adversarial.calls == 0
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert not snapshot.withdrawn


@pytest.mark.parametrize(
    "operation",
    ("mint", "stage"),
)
def test_reentrant_state_change_cannot_be_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    if operation == "mint":
        authority, facade, active, public_record = _active()
    else:
        authority, facade, active, public_record = _capable()

    def reentrant_match(
        public_binding: object,
        private_binding: object,
    ) -> bool:
        del public_binding, private_binding
        assert authority.withdraw(facade=facade)
        return True

    monkeypatch.setattr(
        closure_module,
        "_bindings_match",
        reentrant_match,
    )
    if operation == "mint":
        assert authority.mint_capability(
            facade=facade,
            active_record=active,
            confirmation=public_record,
            inventory=_sealed_inventory(),
            now_ms=2,
        ) is None
    else:
        assert authority.stage(
            facade=facade,
            active_record=active,
            capability=public_record,
            now_ms=3,
            max_frame_bytes=320,
            max_outbound_frames=1,
            max_outbound_bytes=320,
            max_outbound_audio_ms=20,
        ) is None

    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.withdrawn
    assert snapshot.capability_live is (operation == "stage")
    assert snapshot.committed_frame_count == 0


def test_cross_registry_stale_expiry_and_wrong_active_identity_are_rejected():
    first, facade, active, confirmation = _active()
    second, second_facade, second_active, _ = _active()

    assert second.mint_capability(
        facade=second_facade,
        active_record=second_active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None
    assert first.mint_capability(
        facade=facade,
        active_record=object(),
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None
    assert first.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=1_001,
    ) is None


def test_capability_copy_wrong_binding_and_wrong_posture_fail_closed():
    authority, facade, active, capability = _capable()
    copied = replace(capability)
    wrong_binding = replace(
        capability,
        binding=_binding(epoch=2),
    )
    wrong_destination = replace(
        capability,
        destination=OfflineClosureDestination(
            "synthetic_loopback"
        ),
    )
    wrong_privacy = replace(
        capability,
        privacy=OfflineClosurePrivacy("local_buffer_scrub"),
    )
    wrong_transport = replace(
        capability,
        transport=OfflineClosureTransport("offline_local_ready"),
    )

    for forged in (
        copied,
        wrong_binding,
        wrong_destination,
        wrong_privacy,
        wrong_transport,
    ):
        assert authority.stage(
            facade=facade,
            active_record=active,
            capability=forged,
            now_ms=3,
            max_frame_bytes=320,
            max_outbound_frames=1,
            max_outbound_bytes=320,
            max_outbound_audio_ms=20,
        ) is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("confirmation_id", "c" * 64),
        ("capability_id", "d" * 64),
        ("active_revision", 2),
        ("expires_at_ms", 1),
        ("arm", "c"),
        ("journey", "other_journey"),
        ("contract_digest", "b" * 64),
        ("binding", _binding(epoch=2)),
        ("locale", "pt"),
        ("destination", object()),
        ("privacy", object()),
        ("transport", object()),
    ),
)
def test_tampered_live_capability_dimension_fails_closed(
    field: str,
    value: object,
):
    authority, facade, active, capability = _capable()
    object.__setattr__(capability, field, value)

    assert authority.stage(
        facade=facade,
        active_record=active,
        capability=capability,
        now_ms=3,
        max_frame_bytes=320,
        max_outbound_frames=1,
        max_outbound_bytes=320,
        max_outbound_audio_ms=20,
    ) is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("confirmation_id", "c" * 64),
        ("lease_revision", 1),
        ("expires_at_ms", 1),
        ("arm", "c"),
        ("journey", "other_journey"),
        ("contract_digest", "b" * 64),
        ("binding", _binding(epoch=2)),
        ("locale", "pt"),
        ("step", object()),
    ),
)
def test_tampered_live_confirmation_dimension_fails_closed(
    field: str,
    value: object,
):
    authority, facade, active, confirmation = _active()
    object.__setattr__(confirmation, field, value)

    assert authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("stage_id", "e" * 64),
        ("capability_id", "f" * 64),
        ("locale", "pt"),
        ("text", "tampered"),
        ("text_digest", "b" * 64),
        ("audio_id", "tampered_audio"),
        ("playout_id", "tampered_playout"),
        ("transport", object()),
        ("frame_ordinal", 1),
        ("frame_duration_ms", 21),
        ("frame_byte_count", 159),
        ("audio_digest", "b" * 64),
    ),
)
def test_tampered_live_stage_binding_cannot_commit(
    field: str,
    value: object,
):
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    object.__setattr__(stage, field, value)

    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.committed_frame_count == 0
    assert not snapshot.capability_live
    assert snapshot.capability_tombstoned
    assert not any(owned_payload)


def test_tampered_private_staged_payload_cannot_commit():
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    owned_payload[0] ^= 0xFF

    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    ) is None
    assert authority.committed_frame(
        facade=facade,
        active_record=active,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert not snapshot.capability_live
    assert snapshot.capability_tombstoned
    assert not any(owned_payload)


def test_public_stage_cannot_detach_private_audio_owner():
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    approved = bytes(owned_payload)

    with pytest.raises(AttributeError):
        object.__setattr__(stage, "frame", object())
    assert not hasattr(stage, "frame")
    assert not hasattr(stage, "payload")
    assert bytes(owned_payload) == approved
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    frame = authority.committed_frame(
        facade=facade,
        active_record=active,
    )
    assert frame is not None
    assert frame.payload == approved


@pytest.mark.parametrize("failure", ("copied", "expired"))
def test_uncertain_consume_tombstones_and_exact_retry_fails(
    failure: str,
):
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    rejected_stage = replace(stage) if failure == "copied" else stage
    rejected_at_ms = 4 if failure == "copied" else 1_001

    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=rejected_stage,
        now_ms=rejected_at_ms,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert not snapshot.capability_live
    assert snapshot.capability_tombstoned
    assert not any(owned_payload)
    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    ) is None


def test_commit_owns_private_audio_and_exposes_only_immutable_copy():
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    approved_payload = bytes(owned_payload)
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    assert commit.audio_digest == hashlib.sha256(
        approved_payload
    ).hexdigest()
    assert bytes(owned_payload) == approved_payload
    object.__setattr__(stage, "audio_digest", "b" * 64)

    first = authority.committed_frame(
        facade=facade,
        active_record=active,
    )
    second = authority.committed_frame(
        facade=facade,
        active_record=active,
    )
    assert first is not None
    assert second is not None
    assert first is not second
    assert first.payload == approved_payload
    assert second.payload == approved_payload
    assert first.audio_digest == commit.audio_digest
    with pytest.raises(TypeError):
        first.payload[0] = 0  # type: ignore[index]
    assert authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )


def test_transient_public_stage_metadata_cannot_cross_commit_boundary(
    monkeypatch: pytest.MonkeyPatch,
):
    authority, facade, active, capability, stage = _staged()
    canonical_stage_id = stage.stage_id
    first_validation_complete = threading.Event()
    allow_capture = threading.Event()
    commit_token_started = threading.Event()
    allow_second_validation = threading.Event()
    original_can_commit = authority._can_commit
    original_token = closure_module._token
    validation_count = 0
    commit_token_parts = []

    def gated_can_commit(state, **kwargs):
        nonlocal validation_count
        result = original_can_commit(state, **kwargs)
        validation_count += 1
        if validation_count == 1:
            first_validation_complete.set()
            assert allow_capture.wait(timeout=2)
        return result

    def gated_token(domain: bytes, *parts: str) -> str:
        if domain == closure_module._COMMIT_DOMAIN:
            commit_token_parts.append(parts)
            commit_token_started.set()
            assert allow_second_validation.wait(timeout=2)
        return original_token(domain, *parts)

    monkeypatch.setattr(
        authority,
        "_can_commit",
        gated_can_commit,
    )
    monkeypatch.setattr(closure_module, "_token", gated_token)
    outcomes = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            authority.commit(
                facade=facade,
                active_record=active,
                capability=capability,
                stage=stage,
                now_ms=4,
            )
        )
    )
    worker.start()
    assert first_validation_complete.wait(timeout=2)
    object.__setattr__(stage, "stage_id", "e" * 64)
    allow_capture.set()
    assert commit_token_started.wait(timeout=2)
    assert commit_token_parts == [
        (
            OfflineClosureStep.SCRIPTED_OPT_OUT.value,
            canonical_stage_id,
            "4",
        )
    ]
    object.__setattr__(stage, "stage_id", canonical_stage_id)
    allow_second_validation.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    commit = outcomes[0]
    assert commit is not None
    assert commit.stage_id == canonical_stage_id
    assert authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )


def test_playback_revalidates_private_committed_audio_digest():
    authority, facade, active, capability, stage = _staged()
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    retained = authority._entry
    assert retained is not None
    owned_commit = retained.state.commit
    assert owned_commit is not None
    frame = owned_commit.frame
    frame.payload[0] ^= 0xFF

    assert not authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )
    assert authority.committed_frame(
        facade=facade,
        active_record=active,
    ) is None


@pytest.mark.parametrize(
    "field",
    (
        "commit_id",
        "stage_id",
        "locale",
        "text_digest",
        "audio_digest",
        "frame_count",
        "byte_count",
        "audio_ms",
        "committed_at_ms",
    ),
)
def test_tampered_commit_receipt_cannot_change_authoritative_snapshot(
    field: str,
):
    authority, facade, active, capability, stage = _staged()
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    before = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert before is not None
    object.__setattr__(commit, field, object())

    after = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert after == before
    assert authority.committed_frame(
        facade=facade,
        active_record=active,
    ) is None
    assert not authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )
    terminated = authority.terminate(
        facade=facade,
        active_record=active,
    )
    assert terminated is not None
    assert all(not any(payload) for payload in terminated[1])


@pytest.mark.parametrize(
    "field",
    (
        "capability_id",
        "confirmation_id",
        "active_revision",
        "expires_at_ms",
        "arm",
        "journey",
        "contract_digest",
        "binding",
        "locale",
        "destination",
        "privacy",
        "transport",
    ),
)
def test_malformed_staged_capability_tombstones_without_raising(
    field: str,
):
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    original = getattr(capability, field)
    object.__setattr__(capability, field, object())

    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    ) is None
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert not snapshot.capability_live
    assert snapshot.capability_tombstoned
    assert not any(owned_payload)
    object.__setattr__(capability, field, original)
    assert authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=5,
    ) is None


def test_unapproved_portuguese_cannot_mint_confirmation_or_audio():
    (
        authority,
        facade,
        leased,
        active,
        driver,
        participant,
    ) = _registered(locale="pt")

    assert authority.confirm_scripted_step(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=participant,
        now_ms=1,
    ) is None
    assert authority.activate(
        facade=facade,
        leased_record=leased,
        active_record=active,
        driver_identity=driver,
        active_revision=1,
    )
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert not snapshot.confirmation_live
    assert snapshot.committed_frame_count == 0
    assert opt_out_text_digest("pt") is None


def test_withdrawal_before_activation_is_irreversible_and_dominates():
    (
        authority,
        facade,
        leased,
        active,
        driver,
        participant,
    ) = _registered()
    confirmation = authority.confirm_scripted_step(
        facade=facade,
        leased_record=leased,
        driver_identity=driver,
        participant_surrogate=participant,
        now_ms=1,
    )
    assert confirmation is not None

    thread = threading.Thread(
        target=lambda: authority.withdraw(facade=facade)
    )
    thread.start()
    thread.join()
    assert authority.activate(
        facade=facade,
        leased_record=leased,
        active_record=active,
        driver_identity=driver,
        active_revision=1,
    )
    assert authority.is_withdrawn(
        facade=facade,
        active_record=active,
    )
    assert authority.mint_capability(
        facade=facade,
        active_record=active,
        confirmation=confirmation,
        inventory=_sealed_inventory(),
        now_ms=2,
    ) is None


def test_concurrent_withdrawal_wins_before_commit_assignment(
    monkeypatch: pytest.MonkeyPatch,
):
    authority, facade, active, capability, stage = _staged()
    commit_build_started = threading.Event()
    allow_commit_build = threading.Event()
    original_token = closure_module._token

    def blocked_token(domain: bytes, *parts: str) -> str:
        if domain == closure_module._COMMIT_DOMAIN:
            commit_build_started.set()
            assert allow_commit_build.wait(timeout=2)
        return original_token(domain, *parts)

    monkeypatch.setattr(closure_module, "_token", blocked_token)
    outcome = []
    commit_thread = threading.Thread(
        target=lambda: outcome.append(
            authority.commit(
                facade=facade,
                active_record=active,
                capability=capability,
                stage=stage,
                now_ms=4,
            )
        )
    )
    commit_thread.start()
    assert commit_build_started.wait(timeout=2)
    assert authority.withdraw(facade=facade)
    allow_commit_build.set()
    commit_thread.join()

    assert outcome == [None]
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.withdrawn
    assert snapshot.committed_frame_count == 0


@pytest.mark.parametrize("copied_stage", (False, True))
def test_commit_or_failed_consume_racing_termination_scrubs_once(
    copied_stage: bool,
):
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    consume_stage = replace(stage) if copied_stage else stage
    barrier = threading.Barrier(3)
    commits = []
    terminations = []
    errors = []

    def consume() -> None:
        try:
            barrier.wait()
            commits.append(
                authority.commit(
                    facade=facade,
                    active_record=active,
                    capability=capability,
                    stage=consume_stage,
                    now_ms=4,
                )
            )
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    def terminate() -> None:
        try:
            barrier.wait()
            terminations.append(
                authority.terminate(
                    facade=facade,
                    active_record=active,
                )
            )
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    consume_thread = threading.Thread(target=consume)
    terminate_thread = threading.Thread(target=terminate)
    consume_thread.start()
    terminate_thread.start()
    barrier.wait()
    consume_thread.join()
    terminate_thread.join()

    assert not errors
    assert len(commits) == 1
    assert len(terminations) == 1
    assert terminations[0] is not None
    if copied_stage:
        assert commits == [None]
    assert not any(owned_payload)
    assert authority.snapshot(
        facade=facade,
        active_record=active,
    ) is None


def test_preassignment_fault_commits_zero_and_preserves_exact_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    authority, facade, active, capability, stage = _staged()
    owned_payload = _owned_stage_payload(authority)
    approved_payload = bytes(owned_payload)
    original_token = closure_module._token
    faulted = False

    def fault_once(domain: bytes, *parts: str) -> str:
        nonlocal faulted
        if (
            domain == closure_module._COMMIT_DOMAIN
            and not faulted
        ):
            faulted = True
            raise RuntimeError("preassignment fault")
        return original_token(domain, *parts)

    monkeypatch.setattr(closure_module, "_token", fault_once)
    with pytest.raises(RuntimeError, match="preassignment"):
        authority.commit(
            facade=facade,
            active_record=active,
            capability=capability,
            stage=stage,
            now_ms=4,
        )
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.committed_frame_count == 0
    assert snapshot.capability_live
    retained = authority._entry
    assert retained is not None
    assert authority._payloads(retained.state) == (owned_payload,)
    assert bytes(owned_payload) == approved_payload
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=5,
    )
    assert commit is not None


def test_commit_wins_before_later_withdrawal_and_delta_is_zero():
    authority, facade, active, capability, stage = _staged()
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    before_withdraw = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert before_withdraw is not None
    assert authority.withdraw(facade=facade)
    after_withdraw = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert after_withdraw is not None
    assert before_withdraw.committed_frame_count == 1
    assert after_withdraw.committed_frame_count == 1


def test_teardown_zeroizes_without_synthetic_playback_and_drops_authority():
    authority, facade, active, capability, stage = _staged()
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    frame = authority.committed_frame(
        facade=facade,
        active_record=active,
    )
    assert frame is not None
    payload = frame.payload
    assert any(payload)

    terminated = authority.terminate(
        facade=facade,
        active_record=active,
    )

    assert terminated is not None
    snapshot, payloads = terminated
    assert snapshot.phase is OfflineClosurePhase.TERMINATED
    assert not snapshot.synthetic_playback_observed
    assert snapshot.committed_frame_count == 0
    assert all(not any(item) for item in payloads)
    assert payload
    assert authority.snapshot(
        facade=facade,
        active_record=active,
    ) is None
    assert authority.tombstone_count <= 2
    retained = authority._entry
    assert retained is not None
    assert {
        field.name for field in fields(retained.state)
    } == {
        "phase",
        "lease_revision",
        "active_revision",
        "withdrawn",
        "confirmation_tombstoned",
        "capability_tombstoned",
        "text_digest",
        "synthetic_playback_observed",
        "step",
        "invalidated",
        "invalidation_generation",
        "frame_consumed",
    }


def test_teardown_zeroizes_uncommitted_staging_payload():
    authority, facade, active, _, _stage = _staged()
    payload = _owned_stage_payload(authority)
    assert any(payload)

    terminated = authority.terminate(
        facade=facade,
        active_record=active,
    )

    assert terminated is not None
    snapshot, payloads = terminated
    assert snapshot.phase is OfflineClosurePhase.TERMINATED
    assert snapshot.committed_frame_count == 0
    assert payload in payloads
    assert not any(payload)
    assert authority.tombstone_count <= 2


def test_synthetic_playback_is_optional_and_not_commit_authority():
    authority, facade, active, capability, stage = _staged()
    commit = authority.commit(
        facade=facade,
        active_record=active,
        capability=capability,
        stage=stage,
        now_ms=4,
    )
    assert commit is not None
    assert authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )
    assert not authority.mark_synthetic_playback(
        facade=facade,
        active_record=active,
        commit=commit,
    )
    snapshot = authority.snapshot(
        facade=facade,
        active_record=active,
    )
    assert snapshot is not None
    assert snapshot.synthetic_playback_observed


def test_closure_authority_has_no_candidate_provider_or_live_route_imports():
    tree = ast.parse(_SOURCE_PATH.read_text(encoding="utf-8"))
    direct_services = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("app.services.")
    }
    assert direct_services == {"app.services.voice_lifecycle"}
    source = _SOURCE_PATH.read_text(encoding="utf-8").casefold()
    assert "offlinefailureinjection" not in source
    forbidden = {
        "twilio",
        "gemini",
        "deepgram",
        "elevenlabs",
        "requests",
        "httpx",
        "websocket",
        "voice_candidates",
        "media_stream",
        "task 4.8",
    }
    assert not any(token in source for token in forbidden)
    for path in _LIVE_ROUTE_PATHS:
        assert "voice_bakeoff_closure" not in path.read_text(
            encoding="utf-8"
        )
