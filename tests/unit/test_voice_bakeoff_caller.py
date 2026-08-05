"""Offline caller harness security, timing, cap, and teardown tests."""

import ast
import hashlib
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.voice_bakeoff_caller import (
    BakeoffExecutionPlan,
    CallerSchedule,
    CommonEvidenceClock,
    DeterministicOfflineSessionRunner,
    EphemeralEvidenceStore,
    ExecutionCase,
    HarnessCaps,
    OfflineCallerHarness,
    OfflineSessionBudget,
    OfflineSessionController,
    OfflineSessionResult,
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


def _runner() -> DeterministicOfflineSessionRunner:
    return DeterministicOfflineSessionRunner()


def _observe_full_input(observe, schedule: CallerSchedule) -> None:
    observe("caller_speech_onset", at_ms=0)
    for segment in schedule.segments:
        observe(
            "input_speech_sample",
            at_ms=segment.at_ms + segment.duration_ms,
        )


def test_every_arm_uses_the_identical_deterministic_pcmu_schedule(tmp_path: Path):
    schedule = development_schedule()
    harness = OfflineCallerHarness(
        caps=_CAPS,
        plan=_plan(),
        session_runner=_runner(),
    )
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
    clock.observe("session_closed", at_ms=41)
    assert clock.is_closed
    with pytest.raises(ValueError, match="clock"):
        clock.observe("assistant_sample_received", at_ms=41)


def test_harness_requires_an_injected_session_runner():
    with pytest.raises(TypeError, match="configuration"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=None,  # type: ignore[arg-type]
        )


def test_harness_consumes_injected_audio_and_zeroes_runner_buffer(tmp_path: Path):
    class InjectedRunner:
        def __init__(self) -> None:
            self.returned_audio = bytearray(b"\xfd" * 80)
            self.run_count = 0

        def budget(self, schedule: CallerSchedule) -> OfflineSessionBudget:
            assert schedule == development_schedule()
            return OfflineSessionBudget(
                output_byte_count=80,
                output_audio_ms=10,
                duration_ms=161,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            self.run_count += 1
            assert arm == "A"
            assert not stop_requested()
            observe("caller_speech_onset", at_ms=0)
            for segment in schedule.segments:
                observe(
                    "input_speech_sample",
                    at_ms=segment.at_ms + segment.duration_ms,
                )
            observe("assistant_sample_received", at_ms=140)
            observe("playback_sample_received", at_ms=150)
            observe("session_closed", at_ms=161)
            return OfflineSessionResult(
                input_byte_count=320,
                input_audio_ms=40,
                output_audio_ms=10,
                duration_ms=161,
                returned_audio=self.returned_audio,
            )

    runner = InjectedRunner()
    summary = OfflineCallerHarness(
        caps=_CAPS,
        plan=_plan(),
        session_runner=runner,
    ).run(
        arm="A",
        scenario_id=development_schedule().scenario_id,
        evidence=_store(tmp_path),
    )
    assert runner.run_count == 1
    assert summary.byte_count == 400
    assert summary.audio_ms == 50
    assert summary.clock.first_playback_sample_ms == 160
    assert runner.returned_audio == bytearray(80)
    assert 'b"\\xfe"' not in inspect.getsource(OfflineCallerHarness)


def test_harness_rejects_over_budget_audio_and_zeroes_it(tmp_path: Path):
    class OversizedRunner:
        def __init__(self) -> None:
            self.returned_audio = bytearray(b"\xfc" * 160)

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=80,
                output_audio_ms=10,
                duration_ms=161,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            _observe_full_input(observe, schedule)
            observe("assistant_sample_received", at_ms=140)
            observe("playback_sample_received", at_ms=160)
            observe("session_closed", at_ms=161)
            return OfflineSessionResult(
                input_byte_count=320,
                input_audio_ms=40,
                output_audio_ms=20,
                duration_ms=161,
                returned_audio=self.returned_audio,
            )

    runner = OversizedRunner()
    root = tmp_path / "oversized_runner"
    with pytest.raises(RuntimeError, match="reserved budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=EphemeralEvidenceStore(
                root=root,
                repo_root=Path.cwd(),
                key=hashlib.sha256(b"oversized-runner-key").digest(),
            ),
        )
    assert runner.returned_audio == bytearray(160)
    assert not root.exists()


def test_harness_rejects_unobserved_stop_claim(tmp_path: Path):
    class ForgedStopRunner:
        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=0,
                output_audio_ms=0,
                duration_ms=120,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            observe("session_closed", at_ms=120)
            return OfflineSessionResult(
                input_byte_count=0,
                input_audio_ms=0,
                output_audio_ms=0,
                duration_ms=120,
                returned_audio=bytearray(),
                stopped=True,
            )

    with pytest.raises(RuntimeError, match="reserved budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=ForgedStopRunner(),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path),
        )


def test_harness_rejects_duration_budget_below_schedule_floor(tmp_path: Path):
    class ZeroTimeRunner:
        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=8,
                output_audio_ms=1,
                duration_ms=0,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            raise AssertionError("an under-reserved runner must not execute")

    root = tmp_path / "zero_time"
    with pytest.raises(TypeError, match="budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=ZeroTimeRunner(),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=EphemeralEvidenceStore(
                root=root,
                repo_root=Path.cwd(),
                key=hashlib.sha256(b"zero-time-key").digest(),
            ),
        )
    assert not root.exists()


def test_harness_checks_stop_without_runner_cooperation(tmp_path: Path):
    class IgnoringRunner:
        run_count = 0

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=160,
                output_audio_ms=20,
                duration_ms=161,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            self.run_count += 1
            raise AssertionError("runner must not execute after stop")

    runner = IgnoringRunner()
    sessions = OfflineSessionController()
    root = tmp_path / "stop_before_runner"
    with pytest.raises(RuntimeError, match="stop requested"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
            stop_requested=lambda: True,
            session_controller=sessions,
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=EphemeralEvidenceStore(
                root=root,
                repo_root=Path.cwd(),
                key=hashlib.sha256(b"stop-before-runner-key").digest(),
            ),
        )
    assert runner.run_count == 0
    assert sessions.active_count == 0
    assert sessions.abort_count == 1
    assert not root.exists()


def test_harness_rejects_output_after_runner_observes_stop(tmp_path: Path):
    class ContinuingRunner:
        def __init__(self) -> None:
            self.returned_audio = bytearray(b"\xfb" * 8)

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=8,
                output_audio_ms=1,
                duration_ms=121,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            assert stop_requested()
            observe("session_closed", at_ms=121)
            return OfflineSessionResult(
                input_byte_count=0,
                input_audio_ms=0,
                output_audio_ms=1,
                duration_ms=121,
                returned_audio=self.returned_audio,
                stopped=True,
            )

    stop_checks = iter((False, True, True))
    runner = ContinuingRunner()
    with pytest.raises(RuntimeError, match="reserved budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
            stop_requested=lambda: next(stop_checks),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path),
        )
    assert runner.returned_audio == bytearray(8)


def test_post_run_stop_failure_still_zeroes_runner_audio(tmp_path: Path):
    class TrackingRunner:
        def __init__(self) -> None:
            self.returned_audio = bytearray(b"\xf8" * 80)

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=80,
                output_audio_ms=10,
                duration_ms=161,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            _observe_full_input(observe, schedule)
            observe("assistant_sample_received", at_ms=140)
            observe("playback_sample_received", at_ms=150)
            observe("session_closed", at_ms=161)
            return OfflineSessionResult(
                input_byte_count=320,
                input_audio_ms=40,
                output_audio_ms=10,
                duration_ms=161,
                returned_audio=self.returned_audio,
            )

    stop_checks = iter((False, RuntimeError("post-run stop poll failed")))

    def stop_requested() -> bool:
        result = next(stop_checks)
        if isinstance(result, BaseException):
            raise result
        return result

    runner = TrackingRunner()
    root = tmp_path / "post_run_stop_failure"
    with pytest.raises(RuntimeError, match="post-run stop poll"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
            stop_requested=stop_requested,
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=EphemeralEvidenceStore(
                root=root,
                repo_root=Path.cwd(),
                key=hashlib.sha256(b"post-run-stop-key").digest(),
            ),
        )
    assert runner.returned_audio == bytearray(80)
    assert not root.exists()


def test_offline_result_rejects_bytearray_subclasses():
    class LyingBytearray(bytearray):
        def __len__(self):
            return 8

    with pytest.raises(ValueError, match="result"):
        OfflineSessionResult(
            input_byte_count=320,
            input_audio_ms=40,
            output_audio_ms=1,
            duration_ms=161,
            returned_audio=LyingBytearray(b"\xfa" * 100),
        )


def test_exact_integer_accounting_rejects_hostile_subclasses(tmp_path: Path):
    class EvilInt(int):
        def __lt__(self, other):
            return False

        def __gt__(self, other):
            return False

        def __add__(self, other):
            return 0

        def __radd__(self, other):
            return 0

    with pytest.raises(ValueError, match="caps"):
        HarnessCaps(
            requests=1,
            calls=1,
            duration_ms=EvilInt(1),
            byte_count=1,
            audio_ms=1,
            retries=1,
            concurrency=1,
            cost_minor_units=1,
        )
    with pytest.raises(ValueError, match="budget"):
        OfflineSessionBudget(
            output_byte_count=0,
            output_audio_ms=0,
            duration_ms=EvilInt(0),
        )
    with pytest.raises(ValueError, match="result"):
        OfflineSessionResult(
            input_byte_count=EvilInt(0),
            input_audio_ms=0,
            output_audio_ms=0,
            duration_ms=0,
            returned_audio=bytearray(),
        )

    class MutatedBudgetRunner:
        def budget(self, schedule):
            budget = OfflineSessionBudget(
                output_byte_count=160,
                output_audio_ms=20,
                duration_ms=161,
            )
            object.__setattr__(budget, "duration_ms", EvilInt(0))
            return budget

        def run(self, *, arm, schedule, stop_requested, observe):
            raise AssertionError("hostile accounting must reject before execution")

    root = tmp_path / "evil_integer"
    with pytest.raises(TypeError, match="budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=MutatedBudgetRunner(),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=EphemeralEvidenceStore(
                root=root,
                repo_root=Path.cwd(),
                key=hashlib.sha256(b"evil-integer-key").digest(),
            ),
        )
    assert not root.exists()


def test_runner_cannot_expand_budget_after_reservation(tmp_path: Path):
    class MutatingBudgetRunner:
        def __init__(self) -> None:
            self.budget_value = None
            self.returned_audio = bytearray(b"\xf7" * 160)

        def budget(self, schedule):
            self.budget_value = OfflineSessionBudget(
                output_byte_count=8,
                output_audio_ms=1,
                duration_ms=161,
            )
            return self.budget_value

        def run(self, *, arm, schedule, stop_requested, observe):
            object.__setattr__(
                self.budget_value,
                "output_byte_count",
                160,
            )
            object.__setattr__(self.budget_value, "output_audio_ms", 20)
            _observe_full_input(observe, schedule)
            observe("assistant_sample_received", at_ms=140)
            observe("playback_sample_received", at_ms=160)
            observe("session_closed", at_ms=161)
            return OfflineSessionResult(
                input_byte_count=320,
                input_audio_ms=40,
                output_audio_ms=20,
                duration_ms=161,
                returned_audio=self.returned_audio,
            )

    runner = MutatingBudgetRunner()
    with pytest.raises(RuntimeError, match="reserved budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path),
        )
    assert runner.returned_audio == bytearray(160)


def test_runner_receives_detached_schedule_and_plan_snapshot(tmp_path: Path):
    class MutatingScheduleRunner:
        def __init__(self) -> None:
            self.budget_schedule = None

        def budget(self, schedule):
            self.budget_schedule = schedule
            object.__setattr__(
                schedule,
                "segments",
                (PcmuSegment(at_ms=0, duration_ms=1_000, audio=b"\xf6" * 8_000),),
            )
            return OfflineSessionBudget(
                output_byte_count=160,
                output_audio_ms=20,
                duration_ms=161,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            assert schedule is not self.budget_schedule
            assert schedule.digest == development_schedule().digest
            return DeterministicOfflineSessionRunner().run(
                arm=arm,
                schedule=schedule,
                stop_requested=stop_requested,
                observe=observe,
            )

    original_plan = _plan()
    runner = MutatingScheduleRunner()
    harness = OfflineCallerHarness(
        caps=_CAPS,
        plan=original_plan,
        session_runner=runner,
    )
    object.__setattr__(original_plan, "schedules", ())
    summary = harness.run(
        arm="A",
        scenario_id=development_schedule().scenario_id,
        evidence=_store(tmp_path),
    )
    assert summary.schedule_digest == development_schedule().digest
    assert summary.byte_count == 480
    assert summary.audio_ms == 60


def test_runner_timestamps_cannot_manufacture_zero_latency(tmp_path: Path):
    class ForgedTimestampRunner:
        def __init__(self) -> None:
            self.returned_audio = bytearray(b"\xf5" * 8)

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=8,
                output_audio_ms=1,
                duration_ms=120,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            observe("caller_speech_onset", at_ms=120)
            for _ in schedule.segments:
                observe("input_speech_sample", at_ms=120)
            observe("assistant_sample_received", at_ms=120)
            observe("playback_sample_received", at_ms=120)
            observe("session_closed", at_ms=120)
            return OfflineSessionResult(
                input_byte_count=320,
                input_audio_ms=40,
                output_audio_ms=1,
                duration_ms=120,
                returned_audio=self.returned_audio,
            )

    runner = ForgedTimestampRunner()
    with pytest.raises(RuntimeError, match="reserved budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path),
        )
    assert runner.returned_audio == bytearray(8)


def test_mutated_result_audio_is_rejected_and_best_effort_erased(tmp_path: Path):
    class HiddenAudio(bytearray):
        def __len__(self):
            return 0

        def __setitem__(self, key, value):
            raise RuntimeError("subclass assignment must be bypassed")

    class MutatedResultRunner:
        def __init__(self) -> None:
            self.hidden_audio = HiddenAudio(b"secret!!")

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=0,
                output_audio_ms=0,
                duration_ms=120,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            assert stop_requested()
            observe("session_closed", at_ms=120)
            result = OfflineSessionResult(
                input_byte_count=0,
                input_audio_ms=0,
                output_audio_ms=0,
                duration_ms=120,
                returned_audio=bytearray(),
                stopped=True,
            )
            object.__setattr__(result, "returned_audio", self.hidden_audio)
            return result

    stop_checks = iter((False, True, True))
    runner = MutatedResultRunner()
    with pytest.raises(RuntimeError, match="reserved budget"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
            stop_requested=lambda: next(stop_checks),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path),
        )
    assert bytes(runner.hidden_audio) == b"\x00" * 8


def test_cleanup_steps_are_independent_and_preserve_cleanup_error(tmp_path: Path):
    class RaisingAbortController:
        def begin(self, session_id):
            return None

        def close(self, session_id):
            return None

        def abort(self, session_id):
            raise RuntimeError("simulated abort cleanup failure")

        def abort_all(self):
            raise RuntimeError("simulated abort-all cleanup failure")

    class TrackingRunner:
        def __init__(self) -> None:
            self.returned_audio = bytearray(b"\xf9" * 80)

        def budget(self, schedule):
            return OfflineSessionBudget(
                output_byte_count=80,
                output_audio_ms=10,
                duration_ms=161,
            )

        def run(self, *, arm, schedule, stop_requested, observe):
            _observe_full_input(observe, schedule)
            observe("assistant_sample_received", at_ms=140)
            observe("playback_sample_received", at_ms=150)
            observe("session_closed", at_ms=161)
            return OfflineSessionResult(
                input_byte_count=320,
                input_audio_ms=40,
                output_audio_ms=10,
                duration_ms=161,
                returned_audio=self.returned_audio,
            )

    runner = TrackingRunner()
    root = tmp_path / "independent_cleanup"
    with pytest.raises(RuntimeError, match="abort cleanup"):
        OfflineCallerHarness(
            caps=_CAPS,
            plan=_plan(),
            session_runner=runner,
            session_controller=RaisingAbortController(),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=EphemeralEvidenceStore(
                root=root,
                repo_root=Path.cwd(),
                key=hashlib.sha256(b"independent-cleanup-key").digest(),
            ),
        )
    assert runner.returned_audio == bytearray(80)
    assert not root.exists()


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
        ({"duration_ms": 160}, {}),
        ({"byte_count": 479}, {}),
        ({"audio_ms": 59}, {}),
        ({"retries": 1}, {"retry_count": 2}),
        ({"cost_minor_units": 1}, {"cost_minor_units": 2}),
    ),
)
def test_oversized_usage_fails_before_contact_and_tears_down(
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
        OfflineCallerHarness(
            caps=caps,
            plan=_plan(),
            session_runner=_runner(),
        ).run(
            arm="A",
            scenario_id=development_schedule().scenario_id,
            evidence=store,
            **run_changes,
        )
    assert not root.exists()


def test_usage_exactly_at_every_cap_is_allowed(tmp_path: Path):
    caps = HarnessCaps(
        requests=1,
        calls=1,
        duration_ms=161,
        byte_count=480,
        audio_ms=60,
        retries=1,
        concurrency=1,
        cost_minor_units=1,
    )
    summary = OfflineCallerHarness(
        caps=caps,
        plan=_plan(),
        session_runner=_runner(),
    ).run(
        arm="A",
        scenario_id=development_schedule().scenario_id,
        evidence=_store(tmp_path),
        retry_count=1,
    )
    assert summary.duration_ms == caps.duration_ms
    assert summary.byte_count == caps.byte_count
    assert summary.audio_ms == caps.audio_ms


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
        session_runner=_runner(),
        stop_requested=stop,
        session_controller=sessions,
    )
    summary = harness.run(
        arm="C",
        scenario_id=development_schedule().scenario_id,
        evidence=store,
    )
    assert summary.stopped
    assert summary.byte_count == 0
    assert summary.audio_ms == 0
    assert summary.duration_ms == 1
    assert sessions.active_count == 0
    assert sessions.abort_count == 1
    assert not root.exists()
    assert "ff" * 40 not in repr(summary)
    assert "audio" not in harness.__dict__


def test_window_caps_accumulate_across_candidate_runs_and_include_playback(
    tmp_path: Path,
):
    caps = replace(_CAPS, calls=2, requests=2, duration_ms=322)
    harness = OfflineCallerHarness(
        caps=caps,
        plan=_plan(),
        session_runner=_runner(),
    )
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


def test_usage_snapshot_cannot_reset_internal_caps(tmp_path: Path):
    caps = HarnessCaps(
        requests=1,
        calls=1,
        duration_ms=161,
        byte_count=480,
        audio_ms=60,
        retries=1,
        concurrency=1,
        cost_minor_units=1,
    )
    harness = OfflineCallerHarness(
        caps=caps,
        plan=_plan(),
        session_runner=_runner(),
    )
    harness.run(
        arm="A",
        scenario_id=development_schedule().scenario_id,
        evidence=_store(tmp_path / "A"),
    )
    leaked_snapshot = harness.usage
    for field_name in (
        "requests",
        "calls",
        "duration_ms",
        "byte_count",
        "audio_ms",
        "retries",
        "cost_minor_units",
    ):
        object.__setattr__(leaked_snapshot, field_name, 0)
    assert harness.usage.calls == 1
    with pytest.raises(RuntimeError, match="cap"):
        harness.run(
            arm="B1",
            scenario_id=development_schedule().scenario_id,
            evidence=_store(tmp_path / "B1"),
        )


def test_schedule_is_canonical_pcmu_and_sealed_order_is_enforced(tmp_path: Path):
    with pytest.raises(ValueError, match="PCMU"):
        PcmuSegment(at_ms=0, duration_ms=20, audio=b"\xff" * 159)
    harness = OfflineCallerHarness(
        caps=_CAPS,
        plan=_plan(),
        session_runner=_runner(),
    )
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
        caps=replace(_CAPS, concurrency=2),
        plan=_plan(),
        session_runner=_runner(),
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
        session_runner=_runner(),
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
        session_runner=_runner(),
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
