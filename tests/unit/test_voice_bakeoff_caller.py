"""Offline caller harness security, timing, cap, and teardown tests."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import threading

import pytest

from scripts.voice_bakeoff_caller import (
    BakeoffExecutionPlan,
    CallerSchedule,
    CommonEvidenceClock,
    EphemeralEvidenceStore,
    ExecutionCase,
    HarnessCaps,
    OfflineCallerHarness,
    OfflineSessionController,
    PcmuSegment,
    candidate_order,
    development_harness_manifest,
    development_schedule,
    run_offline_self_check,
)


_CAPS = HarnessCaps(
    requests=10,
    calls=10,
    duration_ms=2_000,
    byte_count=10_000,
    audio_ms=1_000,
    retries=2,
    concurrency=2,
    cost_minor_units=10,
)


def _plan(
    *,
    seed: str | None = None,
    first_arm: str = "A",
) -> BakeoffExecutionPlan:
    arms = (first_arm, *(arm for arm in ("A", "B1", "B2", "C") if arm != first_arm))
    schedule = development_schedule()
    return BakeoffExecutionPlan.create(
        schedules=(schedule,),
        manifest_schedule_digests={schedule.scenario_id: schedule.digest},
        arms=arms,
        sealed_seed_digest=seed,
    )


def _store(tmp_path: Path) -> EphemeralEvidenceStore:
    return EphemeralEvidenceStore(
        root=tmp_path / "evidence",
        repo_root=Path.cwd(),
        key=hashlib.sha256(b"test-bakeoff-key").digest(),
    )


def test_every_arm_uses_the_identical_deterministic_pcmu_schedule(tmp_path: Path):
    schedule = development_schedule()
    harness = OfflineCallerHarness(caps=_CAPS, plan=_plan())
    summaries = []
    for arm in ("A", "B1", "B2", "C"):
        root = tmp_path / arm
        store = EphemeralEvidenceStore(
            root=root,
            repo_root=Path.cwd(),
            key=hashlib.sha256(f"key-{arm}".encode()).digest(),
        )
        summaries.append(
            harness.run(
                arm=arm,
                scenario_id=schedule.scenario_id,
                evidence=store,
            )
        )
        assert not root.exists()
    assert {summary.schedule_digest for summary in summaries} == {schedule.digest}
    assert {
        (
            summary.clock.caller_speech_onset_ms,
            summary.clock.last_input_speech_sample_ms,
            summary.clock.last_assistant_sample_ms,
            summary.clock.first_playback_sample_ms,
        )
        for summary in summaries
    } == {(0, 120, 140, 160)}
    assert all(summary.byte_count == 480 for summary in summaries)
    assert all(summary.audio_ms == 60 for summary in summaries)


def test_common_clock_rejects_regression_and_uses_one_timeline():
    clock = CommonEvidenceClock()
    clock.observe("caller_speech_onset", at_ms=10)
    clock.observe("input_speech_sample", at_ms=20)
    clock.observe("assistant_sample_received", at_ms=30)
    clock.observe("playback_sample_received", at_ms=40)
    assert clock.snapshot().first_playback_sample_ms == 40
    with pytest.raises(ValueError, match="clock"):
        clock.observe("input_speech_sample", at_ms=39)
    with pytest.raises(ValueError, match="clock"):
        clock.observe("raw_transcript", at_ms=41)


def test_pcmu_evidence_is_encrypted_round_trip_and_teardown_removes_residue(
    tmp_path: Path,
):
    store = _store(tmp_path)
    raw = development_schedule().segments[0].audio
    path, digest = store.capture(artifact_id="pcmu_round_trip", audio=raw)
    assert path.read_bytes() != raw
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert store.read_for_verification(
        artifact_id="pcmu_round_trip",
        path=path,
    ) == raw
    store.teardown()
    assert not path.exists()
    assert not store.root.exists()


@pytest.mark.parametrize(
    ("changes", "run_changes"),
    (
        ({"requests": 1}, {}),
        ({"calls": 1}, {}),
        ({"duration_ms": 161}, {}),
        ({"byte_count": 480}, {}),
        ({"audio_ms": 60}, {}),
        ({"retries": 1}, {"retry_count": 1}),
        ({"concurrency": 1}, {}),
        ({"cost_minor_units": 1}, {}),
    ),
)
def test_every_cap_fails_on_contact_and_tears_down(
    tmp_path: Path,
    changes: dict[str, int],
    run_changes: dict[str, int],
):
    caps = replace(_CAPS, **changes)
    root = tmp_path / next(iter(changes))
    store = EphemeralEvidenceStore(
        root=root,
        repo_root=Path.cwd(),
        key=hashlib.sha256(b"cap-key").digest(),
    )
    with pytest.raises(RuntimeError, match="cap"):
        OfflineCallerHarness(caps=caps, plan=_plan()).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=store,
            **run_changes,
        )
    assert not root.exists()


def test_stop_trigger_cancels_and_tears_down_without_raw_retention(tmp_path: Path):
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    root = tmp_path / "stopped"
    store = EphemeralEvidenceStore(
        root=root,
        repo_root=Path.cwd(),
        key=hashlib.sha256(b"stop-key").digest(),
    )
    sessions = OfflineSessionController()
    harness = OfflineCallerHarness(
        caps=_CAPS,
        plan=_plan(first_arm="C"),
        stop_requested=stop,
        session_controller=sessions,
    )
    summary = harness.run(
        arm="C",
        scenario_id=development_schedule().scenario_id,
        evidence=store,
    )
    assert summary.stopped
    assert summary.byte_count == 160
    assert summary.audio_ms == 20
    assert summary.duration_ms == 21
    assert sessions.active_count == 0
    assert sessions.abort_count == 1
    assert not root.exists()
    assert "ff" * 40 not in repr(summary)
    assert "audio" not in harness.__dict__


def test_window_caps_accumulate_across_candidate_runs_and_include_playback(
    tmp_path: Path,
):
    caps = replace(_CAPS, calls=3, requests=3, duration_ms=400)
    harness = OfflineCallerHarness(caps=caps, plan=_plan())
    for arm in ("A", "B1"):
        harness.run(
            arm=arm,
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path / arm),
        )
    assert harness.usage.calls == 2
    assert harness.usage.duration_ms == 322
    with pytest.raises(RuntimeError, match="cap"):
        harness.run(
            arm="B2",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path / "B2"),
        )


def test_schedule_is_canonical_pcmu_and_sealed_order_is_enforced(tmp_path: Path):
    with pytest.raises(ValueError, match="PCMU"):
        PcmuSegment(at_ms=0, duration_ms=20, audio=b"\xff" * 159)
    harness = OfflineCallerHarness(caps=_CAPS, plan=_plan())
    with pytest.raises(RuntimeError, match="sealed"):
        harness.run(
            arm="B1",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path / "wrong_order"),
        )


def test_two_normal_overlapping_runs_do_not_abort_each_other(tmp_path: Path):
    barrier = threading.Barrier(2)
    first_started = threading.Event()

    def synchronize_segments() -> bool:
        first_started.set()
        barrier.wait(timeout=2)
        return False

    sessions = OfflineSessionController()
    harness = OfflineCallerHarness(
        caps=replace(_CAPS, concurrency=3),
        plan=_plan(),
        stop_requested=synchronize_segments,
        session_controller=sessions,
    )
    scenario_id = development_schedule().scenario_id
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            harness.run,
            arm="A",
            scenario_id=scenario_id,
            evidence=_store(tmp_path / "A"),
        )
        assert first_started.wait(timeout=2)
        second = executor.submit(
            harness.run,
            arm="B1",
            scenario_id=scenario_id,
            evidence=_store(tmp_path / "B1"),
        )
        summaries = (first.result(timeout=3), second.result(timeout=3))
    assert {summary.arm for summary in summaries} == {"A", "B1"}
    assert sessions.active_count == 0
    assert sessions.abort_count == 0


def test_manifest_binds_every_scenario_and_seals_case_order(tmp_path: Path):
    first = development_schedule()
    second = CallerSchedule(
        scenario_id="synthetic_interrupt",
        segments=(
            PcmuSegment(at_ms=0, duration_ms=20, audio=b"\x01" * 160),
        ),
    )
    seed = hashlib.sha256(b"sealed-case-order").hexdigest()
    manifest_digests = {
        first.scenario_id: first.digest,
        second.scenario_id: second.digest,
    }
    plan = BakeoffExecutionPlan.create(
        schedules=(first, second),
        manifest_schedule_digests=manifest_digests,
        sealed_seed_digest=seed,
    )
    assert len(plan.sealed_case_order) == 8
    assert plan.sealed_case_order != tuple(
        ExecutionCase(
            arm=arm,
            scenario_id=schedule.scenario_id,
        )
        for arm in ("A", "B1", "B2", "C")
        for schedule in (first, second)
    )
    assert plan.case_order_digest == BakeoffExecutionPlan.create(
        schedules=(first, second),
        manifest_schedule_digests=manifest_digests,
        sealed_seed_digest=seed,
    ).case_order_digest
    with pytest.raises(ValueError, match="manifest-bound"):
        BakeoffExecutionPlan.create(
            schedules=(first, second),
            manifest_schedule_digests={
                first.scenario_id: "0" * 64,
                second.scenario_id: second.digest,
            },
            sealed_seed_digest=seed,
        )

    first_case = plan.sealed_case_order[0]
    wrong_scenario = (
        first.scenario_id
        if first_case.scenario_id != first.scenario_id
        else second.scenario_id
    )
    harness = OfflineCallerHarness(
        caps=replace(
            _CAPS,
            requests=20,
            calls=20,
            duration_ms=4_000,
            byte_count=20_000,
            audio_ms=2_000,
            cost_minor_units=20,
        ),
        plan=plan,
    )
    with pytest.raises(RuntimeError, match="sealed"):
        harness.run(
            arm=first_case.arm,
            scenario_id=wrong_scenario,
            evidence=_store(tmp_path / "wrong_scenario"),
        )

    execution = OfflineCallerHarness(
        caps=replace(
            _CAPS,
            requests=20,
            calls=20,
            duration_ms=4_000,
            byte_count=20_000,
            audio_ms=2_000,
            cost_minor_units=20,
        ),
        plan=plan,
    )
    expected_digests = {
        first.scenario_id: first.digest,
        second.scenario_id: second.digest,
    }
    summaries = [
        execution.run(
            arm=case.arm,
            scenario_id=case.scenario_id,
            evidence=_store(tmp_path / f"case_{index}"),
        )
        for index, case in enumerate(plan.sealed_case_order)
    ]
    assert [
        (summary.arm, summary.scenario_id)
        for summary in summaries
    ] == [
        (case.arm, case.scenario_id)
        for case in plan.sealed_case_order
    ]
    assert all(
        summary.schedule_digest == expected_digests[summary.scenario_id]
        for summary in summaries
    )


def test_partial_encrypted_write_is_tracked_for_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)

    def partial_write(path: Path, value: bytes) -> int:
        with path.open("wb") as handle:
            handle.write(value[:8])
        raise OSError("simulated partial write")

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    with pytest.raises(OSError, match="partial"):
        store.capture(artifact_id="partial", audio=b"\xff" * 160)
    store.teardown()
    assert not store.root.exists()


def test_sealed_candidate_order_is_seeded_and_complete():
    arms = ("A", "B1", "B2", "C")
    seed = hashlib.sha256(b"sealed-order").hexdigest()
    first = candidate_order(arms, sealed_seed_digest=seed)
    assert first == candidate_order(arms, sealed_seed_digest=seed)
    assert set(first) == set(arms)
    assert candidate_order(arms, sealed_seed_digest=None) == arms
    with pytest.raises(ValueError, match="every arm"):
        candidate_order(("A", "B1"), sealed_seed_digest=seed)


def test_runner_self_check_uses_temp_encrypted_fixture_only():
    seed = hashlib.sha256(b"self-check-order").hexdigest()
    caller_harness = development_harness_manifest(sealed_seed_digest=seed)
    assert run_offline_self_check(
        arm="B1",
        manifest={
            "candidate": {"arm": "B1"},
            "caller_harness": caller_harness,
        },
    )
    assert not run_offline_self_check(
        arm="A",
        manifest={
            "candidate": {"arm": "B1"},
            "caller_harness": caller_harness,
        },
    )


def test_harness_has_no_network_provider_or_logging_imports():
    path = Path("scripts/voice_bakeoff_caller.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in imported
        for forbidden in (
            "socket",
            "requests",
            "httpx",
            "websockets",
            "twilio",
            "google",
            "deepgram",
            "elevenlabs",
            "logging",
        )
    )
