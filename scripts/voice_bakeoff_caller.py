#!/usr/bin/env python3
"""Offline caller-side harness primitives for the isolated voice bakeoff."""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_ARMS = ("A", "B1", "B2", "C")
_ALLOWED_EVENTS = frozenset(
    {
        "input_speech_sample",
        "caller_speech_onset",
        "assistant_sample_received",
        "playback_sample_received",
        "session_closed",
    }
)


@dataclass(frozen=True, slots=True)
class HarnessCaps:
    requests: int
    calls: int
    duration_ms: int
    byte_count: int
    audio_ms: int
    retries: int
    concurrency: int
    cost_minor_units: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (
                self.requests,
                self.calls,
                self.duration_ms,
                self.byte_count,
                self.audio_ms,
                self.retries,
                self.concurrency,
                self.cost_minor_units,
            )
        ):
            raise ValueError("harness caps must be positive integers")


@dataclass(frozen=True, slots=True)
class PcmuSegment:
    at_ms: int
    duration_ms: int
    audio: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.at_ms, bool)
            or not isinstance(self.at_ms, int)
            or self.at_ms < 0
            or isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 1
            or not isinstance(self.audio, bytes)
            or not self.audio
            or len(self.audio) != self.duration_ms * 8
        ):
            raise ValueError("PCMU segment is invalid")


@dataclass(frozen=True, slots=True)
class CallerSchedule:
    scenario_id: str
    segments: tuple[PcmuSegment, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scenario_id, str)
            or not self.scenario_id
            or not self.segments
            or any(
                current.at_ms < prior.at_ms + prior.duration_ms
                for prior, current in zip(self.segments, self.segments[1:])
            )
        ):
            raise ValueError("caller schedule is invalid")

    @property
    def digest(self) -> str:
        material = b"|".join(
            (
                self.scenario_id.encode("utf-8"),
                *(
                    b":".join(
                        (
                            str(segment.at_ms).encode("ascii"),
                            str(segment.duration_ms).encode("ascii"),
                            hashlib.sha256(segment.audio).hexdigest().encode("ascii"),
                        )
                    )
                    for segment in self.segments
                ),
            )
        )
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class ClockEvidence:
    last_input_speech_sample_ms: int | None
    caller_speech_onset_ms: int | None
    last_assistant_sample_ms: int | None
    first_playback_sample_ms: int | None


class CommonEvidenceClock:
    """One monotonic clock for all caller-side latency landmarks."""

    def __init__(self) -> None:
        self._now_ms = 0
        self._last_input: int | None = None
        self._onset: int | None = None
        self._last_assistant: int | None = None
        self._first_playback: int | None = None

    def observe(self, event: str, *, at_ms: int) -> None:
        if (
            event not in _ALLOWED_EVENTS
            or isinstance(at_ms, bool)
            or not isinstance(at_ms, int)
            or at_ms < self._now_ms
        ):
            raise ValueError("clock observation is invalid")
        self._now_ms = at_ms
        if event == "input_speech_sample":
            self._last_input = at_ms
        elif event == "caller_speech_onset" and self._onset is None:
            self._onset = at_ms
        elif event == "assistant_sample_received":
            self._last_assistant = at_ms
        elif event == "playback_sample_received" and self._first_playback is None:
            self._first_playback = at_ms

    def snapshot(self) -> ClockEvidence:
        return ClockEvidence(
            last_input_speech_sample_ms=self._last_input,
            caller_speech_onset_ms=self._onset,
            last_assistant_sample_ms=self._last_assistant,
            first_playback_sample_ms=self._first_playback,
        )


class EphemeralEvidenceStore:
    """AES-GCM evidence files outside the repository, with explicit teardown."""

    def __init__(self, *, root: Path, repo_root: Path, key: bytes) -> None:
        resolved_root = root.resolve()
        resolved_repo = repo_root.resolve()
        if (
            not isinstance(key, bytes)
            or len(key) != 32
            or resolved_root == resolved_repo
            or resolved_repo in resolved_root.parents
        ):
            raise ValueError("ephemeral evidence location or key is invalid")
        resolved_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.root = resolved_root
        self._cipher = AESGCM(key)
        self._paths: list[Path] = []

    def capture(self, *, artifact_id: str, audio: bytes) -> tuple[Path, str]:
        if (
            not artifact_id.replace("_", "").isalnum()
            or not isinstance(audio, bytes)
        ):
            raise ValueError("evidence artifact is invalid")
        nonce = secrets.token_bytes(12)
        encrypted = nonce + self._cipher.encrypt(
            nonce,
            audio,
            artifact_id.encode("utf-8"),
        )
        path = self.root / f"{artifact_id}.aesgcm"
        if path.exists():
            raise ValueError("evidence artifact already exists")
        self._paths.append(path)
        path.write_bytes(encrypted)
        return path, hashlib.sha256(encrypted).hexdigest()

    def read_for_verification(self, *, artifact_id: str, path: Path) -> bytes:
        encrypted = path.read_bytes()
        return self._cipher.decrypt(
            encrypted[:12],
            encrypted[12:],
            artifact_id.encode("utf-8"),
        )

    def teardown(self) -> None:
        for path in tuple(self._paths):
            if path.exists():
                path.unlink()
        self._paths.clear()
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()


@dataclass(frozen=True, slots=True)
class HarnessSummary:
    arm: str
    scenario_id: str
    schedule_digest: str
    encrypted_artifact_digest: str
    byte_count: int
    audio_ms: int
    duration_ms: int
    clock: ClockEvidence
    stopped: bool


@dataclass(frozen=True, slots=True)
class ExecutionCase:
    arm: str
    scenario_id: str

    def __post_init__(self) -> None:
        if (
            self.arm not in _ARMS
            or not isinstance(self.scenario_id, str)
            or not self.scenario_id
        ):
            raise ValueError("execution case is invalid")


def _case_order_digest(cases: tuple[ExecutionCase, ...]) -> str:
    encoded = json.dumps(
        [
            {"arm": case.arm, "scenario_id": case.scenario_id}
            for case in cases
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sealed_case_order(
    cases: tuple[ExecutionCase, ...],
    *,
    sealed_seed_digest: str | None,
) -> tuple[ExecutionCase, ...]:
    if sealed_seed_digest is None:
        return cases
    _validate_seed_digest(sealed_seed_digest)
    shuffled = list(cases)
    random.Random(int(sealed_seed_digest, 16)).shuffle(shuffled)
    return tuple(shuffled)


@dataclass(frozen=True, slots=True)
class BakeoffExecutionPlan:
    """Manifest-bound schedules and sealed candidate/scenario ordering."""

    schedules: tuple[CallerSchedule, ...]
    manifest_schedule_digests: tuple[tuple[str, str], ...]
    source_arms: tuple[str, ...]
    sealed_seed_digest: str | None
    sealed_case_order: tuple[ExecutionCase, ...]

    def __post_init__(self) -> None:
        schedule_ids = tuple(schedule.scenario_id for schedule in self.schedules)
        actual_digests = tuple(
            sorted((schedule.scenario_id, schedule.digest) for schedule in self.schedules)
        )
        source_cases = tuple(
            ExecutionCase(arm=arm, scenario_id=scenario_id)
            for arm in self.source_arms
            for scenario_id in schedule_ids
        )
        if (
            not self.schedules
            or len(schedule_ids) != len(set(schedule_ids))
            or self.manifest_schedule_digests != actual_digests
            or set(self.source_arms) != set(_ARMS)
            or len(self.source_arms) != len(_ARMS)
            or self.sealed_case_order
            != _sealed_case_order(
                source_cases,
                sealed_seed_digest=self.sealed_seed_digest,
            )
        ):
            raise ValueError("execution plan is not manifest-bound and sealed")

    @classmethod
    def create(
        cls,
        *,
        schedules: Iterable[CallerSchedule],
        manifest_schedule_digests: dict[str, str],
        arms: Iterable[str] = _ARMS,
        sealed_seed_digest: str | None,
    ) -> "BakeoffExecutionPlan":
        schedule_tuple = tuple(schedules)
        arm_tuple = tuple(arms)
        source_cases = tuple(
            ExecutionCase(arm=arm, scenario_id=schedule.scenario_id)
            for arm in arm_tuple
            for schedule in schedule_tuple
        )
        return cls(
            schedules=schedule_tuple,
            manifest_schedule_digests=tuple(sorted(manifest_schedule_digests.items())),
            source_arms=arm_tuple,
            sealed_seed_digest=sealed_seed_digest,
            sealed_case_order=_sealed_case_order(
                source_cases,
                sealed_seed_digest=sealed_seed_digest,
            ),
        )

    @property
    def case_order_digest(self) -> str:
        return _case_order_digest(self.sealed_case_order)

    def schedule(self, scenario_id: str) -> CallerSchedule | None:
        return next(
            (
                schedule
                for schedule in self.schedules
                if schedule.scenario_id == scenario_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class HarnessUsage:
    requests: int = 0
    calls: int = 0
    duration_ms: int = 0
    byte_count: int = 0
    audio_ms: int = 0
    retries: int = 0
    concurrency: int = 0
    cost_minor_units: int = 0


class ExecutionCapLedger:
    """Atomic, execution-window-wide reservation ledger."""

    _CUMULATIVE_FIELDS = (
        "requests",
        "calls",
        "duration_ms",
        "byte_count",
        "audio_ms",
        "retries",
        "cost_minor_units",
    )

    def __init__(self, caps: HarnessCaps) -> None:
        if not isinstance(caps, HarnessCaps):
            raise ValueError("harness caps are required")
        self._caps = caps
        self._usage = HarnessUsage()
        self._lock = threading.Lock()

    def reserve(
        self,
        *,
        duration_ms: int,
        byte_count: int,
        audio_ms: int,
        retry_count: int,
        cost_minor_units: int,
    ) -> bool:
        requested = {
            "requests": 1,
            "calls": 1,
            "duration_ms": duration_ms,
            "byte_count": byte_count,
            "audio_ms": audio_ms,
            "retries": retry_count,
            "cost_minor_units": cost_minor_units,
        }
        with self._lock:
            current = self._usage
            if (
                any(
                    getattr(current, name) + requested[name] >= getattr(self._caps, name)
                    for name in self._CUMULATIVE_FIELDS
                )
                or current.concurrency + 1 >= self._caps.concurrency
            ):
                return False
            self._usage = HarnessUsage(
                **{
                    name: getattr(current, name) + requested[name]
                    for name in self._CUMULATIVE_FIELDS
                },
                concurrency=current.concurrency + 1,
            )
            return True

    def release(self) -> None:
        with self._lock:
            if self._usage.concurrency < 1:
                raise RuntimeError("cap reservation is not active")
            self._usage = HarnessUsage(
                **{
                    name: getattr(self._usage, name)
                    for name in self._CUMULATIVE_FIELDS
                },
                concurrency=self._usage.concurrency - 1,
            )

    def snapshot(self) -> HarnessUsage:
        with self._lock:
            return self._usage


class SessionController(Protocol):
    @property
    def active_count(self) -> int: ...

    def begin(self, session_id: str) -> None: ...

    def close(self, session_id: str) -> None: ...

    def abort(self, session_id: str) -> None: ...

    def abort_all(self) -> None: ...


class OfflineSessionController:
    """Deterministic session-close proof surface for the no-network harness."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self.abort_count = 0
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def begin(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._active:
                raise RuntimeError("session is already active")
            self._active.add(session_id)

    def close(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._active:
                raise RuntimeError("session is not active")
            self._active.remove(session_id)

    def abort(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._active:
                self.abort_count += 1
                self._active.remove(session_id)

    def abort_all(self) -> None:
        with self._lock:
            self.abort_count += len(self._active)
            self._active.clear()


class OfflineCallerHarness:
    """Deterministic no-network harness; it retains aggregate evidence only."""

    def __init__(
        self,
        *,
        caps: HarnessCaps,
        plan: BakeoffExecutionPlan,
        stop_requested: Callable[[], bool] = lambda: False,
        session_controller: SessionController | None = None,
    ) -> None:
        if (
            not isinstance(caps, HarnessCaps)
            or not isinstance(plan, BakeoffExecutionPlan)
            or not callable(stop_requested)
        ):
            raise ValueError("harness configuration is invalid")
        self.caps = caps
        self.plan = plan
        self._stop_requested = stop_requested
        self._ledger = ExecutionCapLedger(caps)
        self._sessions = session_controller or OfflineSessionController()
        self._next_case_index = 0
        self._halted = False
        self._state_lock = threading.Lock()

    @property
    def usage(self) -> HarnessUsage:
        return self._ledger.snapshot()

    def run(
        self,
        *,
        arm: str,
        scenario_id: str,
        evidence: EphemeralEvidenceStore,
        retry_count: int = 0,
        cost_minor_units: int = 1,
    ) -> HarnessSummary:
        if not isinstance(evidence, EphemeralEvidenceStore):
            raise ValueError("ephemeral evidence store is required")
        if (
            arm not in _ARMS
            or not isinstance(scenario_id, str)
            or not scenario_id
            or isinstance(retry_count, bool)
            or not isinstance(retry_count, int)
            or retry_count < 0
            or isinstance(cost_minor_units, bool)
            or not isinstance(cost_minor_units, int)
            or cost_minor_units < 0
        ):
            evidence.teardown()
            raise ValueError("harness arm or accounting input is invalid")
        schedule = self.plan.schedule(scenario_id)
        if schedule is None:
            evidence.teardown()
            raise RuntimeError("scenario is not in the manifest-bound plan")
        input_byte_count = sum(len(segment.audio) for segment in schedule.segments)
        input_audio_ms = sum(segment.duration_ms for segment in schedule.segments)
        expected_output_bytes = 160
        expected_output_audio_ms = 20
        byte_count = input_byte_count + expected_output_bytes
        audio_ms = input_audio_ms + expected_output_audio_ms
        final_input_ms = max(
            segment.at_ms + segment.duration_ms for segment in schedule.segments
        )
        assistant_at = final_input_ms + 20
        playback_at = assistant_at + 20
        planned_close_at = playback_at + 1
        with self._state_lock:
            if (
                self._halted
                or self._next_case_index >= len(self.plan.sealed_case_order)
                or ExecutionCase(arm=arm, scenario_id=scenario_id)
                != self.plan.sealed_case_order[self._next_case_index]
            ):
                evidence.teardown()
                raise RuntimeError("candidate order is not the sealed execution order")
            if not self._ledger.reserve(
                duration_ms=planned_close_at,
                byte_count=byte_count,
                audio_ms=audio_ms,
                retry_count=retry_count,
                cost_minor_units=cost_minor_units,
            ):
                self._halted = True
                self._sessions.abort_all()
                evidence.teardown()
                raise RuntimeError("harness cap reached")
            case_index = self._next_case_index
            self._next_case_index += 1
        clock = CommonEvidenceClock()
        returned_audio = bytearray()
        transferred_bytes = 0
        transferred_audio_ms = 0
        stopped = False
        actual_close_at = 0
        session_id = (
            f"{case_index}_{arm}_{schedule.scenario_id}"
        )
        try:
            self._sessions.begin(session_id)
            for index, segment in enumerate(schedule.segments):
                if self._stop_requested():
                    stopped = True
                    with self._state_lock:
                        self._halted = True
                    self._sessions.abort_all()
                    break
                if index == 0:
                    clock.observe("caller_speech_onset", at_ms=segment.at_ms)
                transferred_bytes += len(segment.audio)
                transferred_audio_ms += segment.duration_ms
                clock.observe(
                    "input_speech_sample",
                    at_ms=segment.at_ms + segment.duration_ms,
                )
            if not stopped:
                returned_audio.extend(b"\xfe" * expected_output_bytes)
                transferred_bytes += expected_output_bytes
                transferred_audio_ms += expected_output_audio_ms
                clock.observe("assistant_sample_received", at_ms=assistant_at)
                clock.observe("playback_sample_received", at_ms=playback_at)
                actual_close_at = planned_close_at
                clock.observe("session_closed", at_ms=actual_close_at)
                self._sessions.close(session_id)
            else:
                actual_close_at = (
                    clock.snapshot().last_input_speech_sample_ms or 0
                ) + 1
                clock.observe("session_closed", at_ms=actual_close_at)
            artifact_id = f"{arm}_{schedule.scenario_id}"
            _, artifact_digest = evidence.capture(
                artifact_id=artifact_id,
                audio=bytes(returned_audio),
            )
            summary = HarnessSummary(
                arm=arm,
                scenario_id=schedule.scenario_id,
                schedule_digest=schedule.digest,
                encrypted_artifact_digest=artifact_digest,
                byte_count=transferred_bytes,
                audio_ms=transferred_audio_ms,
                duration_ms=actual_close_at,
                clock=clock.snapshot(),
                stopped=stopped,
            )
            return summary
        except BaseException:
            with self._state_lock:
                self._halted = True
            self._sessions.abort_all()
            raise
        finally:
            self._sessions.abort(session_id)
            self._ledger.release()
            for index in range(len(returned_audio)):
                returned_audio[index] = 0
            evidence.teardown()


def development_schedule() -> CallerSchedule:
    """Return the same synthetic PCMU schedule for every candidate arm."""
    return CallerSchedule(
        scenario_id="synthetic_pause",
        segments=(
            PcmuSegment(at_ms=0, duration_ms=20, audio=b"\xff" * 160),
            PcmuSegment(at_ms=100, duration_ms=20, audio=b"\x7f" * 160),
        ),
    )


def development_harness_manifest(
    *,
    sealed_seed_digest: str,
) -> dict[str, object]:
    schedule = development_schedule()
    plan = BakeoffExecutionPlan.create(
        schedules=(schedule,),
        manifest_schedule_digests={schedule.scenario_id: schedule.digest},
        sealed_seed_digest=sealed_seed_digest,
    )
    return {
        "schedule_digests": {schedule.scenario_id: schedule.digest},
        "sealed_seed_digest": sealed_seed_digest,
        "case_order_digest": plan.case_order_digest,
    }


def _validate_seed_digest(sealed_seed_digest: str) -> None:
    if (
        not isinstance(sealed_seed_digest, str)
        or len(sealed_seed_digest) != 64
        or any(character not in "0123456789abcdef" for character in sealed_seed_digest)
    ):
        raise ValueError("sealed seed digest is invalid")


def candidate_order(
    arms: Iterable[str],
    *,
    sealed_seed_digest: str | None,
) -> tuple[str, ...]:
    ordered = tuple(arms)
    if set(ordered) != set(_ARMS) or len(ordered) != len(_ARMS):
        raise ValueError("candidate order must contain every arm exactly once")
    if sealed_seed_digest is None:
        return ordered
    _validate_seed_digest(sealed_seed_digest)
    shuffled = list(ordered)
    random.Random(int(sealed_seed_digest, 16)).shuffle(shuffled)
    return tuple(shuffled)


def run_offline_self_check(*, arm: str, manifest: dict[str, object]) -> bool:
    """Bounded local fixture used by the runner; it cannot enable a route."""
    candidate = manifest.get("candidate")
    harness_manifest = manifest.get("caller_harness")
    if (
        arm not in _ARMS
        or not isinstance(candidate, dict)
        or candidate.get("arm") != arm
        or not isinstance(harness_manifest, dict)
        or set(harness_manifest)
        != {"schedule_digests", "sealed_seed_digest", "case_order_digest"}
        or not isinstance(harness_manifest.get("schedule_digests"), dict)
        or not isinstance(harness_manifest.get("sealed_seed_digest"), str)
    ):
        return False
    schedule = development_schedule()
    try:
        plan = BakeoffExecutionPlan.create(
            schedules=(schedule,),
            manifest_schedule_digests=harness_manifest["schedule_digests"],
            sealed_seed_digest=harness_manifest["sealed_seed_digest"],
        )
    except (TypeError, ValueError):
        return False
    if harness_manifest.get("case_order_digest") != plan.case_order_digest:
        return False
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="kevin-voice-bakeoff-") as temp:
        temp_root = Path(temp)
        harness = OfflineCallerHarness(
            caps=HarnessCaps(10, 10, 2_000, 10_000, 1_000, 1, 2, 10),
            plan=plan,
        )
        summaries = []
        for index, case in enumerate(plan.sealed_case_order):
            store = EphemeralEvidenceStore(
                root=temp_root / f"case_{index}",
                repo_root=repo_root,
                key=hashlib.sha256(
                    f"synthetic-offline-bakeoff-key-{index}".encode("ascii")
                ).digest(),
            )
            summaries.append(
                harness.run(
                    arm=case.arm,
                    scenario_id=case.scenario_id,
                    evidence=store,
                )
            )
        return (
            len(summaries) == len(plan.sealed_case_order)
            and {summary.arm for summary in summaries} == set(_ARMS)
            and {summary.schedule_digest for summary in summaries}
            == {schedule.digest}
            and not any(temp_root.iterdir())
        )


__all__ = [
    "CallerSchedule",
    "BakeoffExecutionPlan",
    "ClockEvidence",
    "CommonEvidenceClock",
    "EphemeralEvidenceStore",
    "ExecutionCapLedger",
    "ExecutionCase",
    "HarnessCaps",
    "HarnessSummary",
    "HarnessUsage",
    "OfflineCallerHarness",
    "OfflineSessionController",
    "PcmuSegment",
    "candidate_order",
    "development_harness_manifest",
    "development_schedule",
    "run_offline_self_check",
]
