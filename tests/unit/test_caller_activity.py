import audioop
import hashlib
from pathlib import Path

import pytest

import app.services.caller_activity as caller_activity_module
from app.services.caller_activity import CallerActivityEvent, CallerActivityTracker


FRAME_BYTES = 160
SILENCE_FRAME = b"\xff" * FRAME_BYTES
VOICED_FRAME = b"\x00" * FRAME_BYTES
CALIBRATION_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "voice_vad" / "py-webrtcvad-test-audio.raw"
)
CALIBRATION_SHA256 = (
    "3dbb730b90e0266d78d4fe03f00aba37a83780a6a5240d9084b5c01266194c1e"  # pragma: allowlist secret
)
UPSTREAM_MODE2_30MS_PATTERN = "".join(
    [
        "000000",
        "11111",
        "11111",
        "11111",
        "11111",
        "0000",
    ]
)


class RecordingClassifier:
    def __init__(self, decisions: list[bool]) -> None:
        self._decisions = iter(decisions)
        self.calls: list[tuple[bytes, int]] = []

    def __call__(self, pcm_frame: bytes, sample_rate: int) -> bool:
        self.calls.append((pcm_frame, sample_rate))
        return next(self._decisions)


def test_buffers_fragmented_input_until_a_complete_frame() -> None:
    classifier = RecordingClassifier([True])
    tracker = CallerActivityTracker(classifier=classifier, min_speech_frames=1)

    assert tracker.process_mulaw(VOICED_FRAME[:73], received_at=1.0) == ()
    assert classifier.calls == []

    assert tracker.process_mulaw(VOICED_FRAME[73:], received_at=1.25) == (
        CallerActivityEvent(kind="start", segment=1, at=1.25),
    )
    assert len(classifier.calls) == 1


def test_splits_combined_input_and_backdates_each_frame() -> None:
    classifier = RecordingClassifier([True, True, True])
    tracker = CallerActivityTracker(classifier=classifier, min_speech_frames=3)

    events = tracker.process_mulaw(VOICED_FRAME * 3, received_at=10.0)

    assert events == (CallerActivityEvent(kind="start", segment=1, at=pytest.approx(9.96)),)
    assert len(classifier.calls) == 3
    assert tracker.last_voiced_at == pytest.approx(10.0)
    assert tracker.currently_voiced is True
    assert tracker.active is True


def test_rejects_short_noise_before_activity_is_confirmed() -> None:
    classifier = RecordingClassifier([True, True, False])
    tracker = CallerActivityTracker(classifier=classifier, min_speech_frames=3)

    events = tracker.process_mulaw(VOICED_FRAME * 3, received_at=20.0)

    assert events == ()
    assert tracker.segment == 0
    assert tracker.currently_voiced is False
    assert tracker.active is False
    assert tracker.last_ended_segment == 0
    assert tracker.last_ended_at == 0.0


def test_ends_after_silence_and_backdates_to_last_voiced_frame() -> None:
    classifier = RecordingClassifier([True, True, True, False, False, False])
    tracker = CallerActivityTracker(
        classifier=classifier,
        min_speech_frames=2,
        end_silence_frames=3,
    )

    assert tracker.process_mulaw(VOICED_FRAME * 2, received_at=30.0) == (
        CallerActivityEvent(kind="start", segment=1, at=pytest.approx(29.98)),
    )
    events = tracker.process_mulaw(VOICED_FRAME + SILENCE_FRAME * 3, received_at=30.08)

    assert events == (CallerActivityEvent(kind="end", segment=1, at=pytest.approx(30.02)),)
    assert tracker.last_voiced_at == pytest.approx(30.02)
    assert tracker.currently_voiced is False
    assert tracker.active is False


def test_emits_multiple_segments_with_monotonic_ordinals() -> None:
    classifier = RecordingClassifier([True, True, False, False, True, True, False, False])
    tracker = CallerActivityTracker(
        classifier=classifier,
        min_speech_frames=2,
        end_silence_frames=2,
    )

    events = tracker.process_mulaw(VOICED_FRAME * 8, received_at=40.0)

    assert events == (
        CallerActivityEvent(kind="start", segment=1, at=pytest.approx(39.86)),
        CallerActivityEvent(kind="end", segment=1, at=pytest.approx(39.88)),
        CallerActivityEvent(kind="start", segment=2, at=pytest.approx(39.94)),
        CallerActivityEvent(kind="end", segment=2, at=pytest.approx(39.96)),
    )
    assert tracker.segment == 2


def test_reset_clears_state_buffer_and_segment_ordinals() -> None:
    classifier = RecordingClassifier([True, True])
    tracker = CallerActivityTracker(classifier=classifier, min_speech_frames=1)

    assert tracker.process_mulaw(VOICED_FRAME, received_at=50.0)[0].segment == 1
    assert tracker.process_mulaw(VOICED_FRAME[:80], received_at=50.01) == ()

    tracker.reset()

    assert tracker.segment == 0
    assert tracker.last_voiced_at == 0.0
    assert tracker.currently_voiced is False
    assert tracker.active is False
    assert tracker.last_ended_segment == 0
    assert tracker.last_ended_at == 0.0
    assert tracker.process_mulaw(VOICED_FRAME[80:], received_at=51.0) == ()
    assert tracker.process_mulaw(VOICED_FRAME[:80], received_at=51.02) == (
        CallerActivityEvent(kind="start", segment=1, at=51.02),
    )


def test_passes_exact_pcm_frame_and_sample_rate_to_classifier() -> None:
    mulaw_frame = bytes(range(FRAME_BYTES))
    classifier = RecordingClassifier([False])
    tracker = CallerActivityTracker(classifier=classifier)

    assert tracker.process_mulaw(mulaw_frame, received_at=60.0) == ()

    assert classifier.calls == [(audioop.ulaw2lin(mulaw_frame, 2), 8_000)]
    assert len(classifier.calls[0][0]) == 320


def test_default_webrtc_classifier_treats_silence_as_unvoiced() -> None:
    tracker = CallerActivityTracker(min_speech_frames=1)

    assert tracker.process_mulaw(SILENCE_FRAME, received_at=70.0) == ()
    assert tracker.segment == 0
    assert tracker.currently_voiced is False
    assert tracker.active is False


def test_rejected_candidate_does_not_overwrite_completed_endpoint() -> None:
    classifier = RecordingClassifier(
        [
            True,
            True,
            False,
            False,
            True,
            True,
            False,
        ]
    )
    tracker = CallerActivityTracker(
        classifier=classifier,
        min_speech_frames=2,
        end_silence_frames=2,
    )

    tracker.process_mulaw(VOICED_FRAME * 4, received_at=10.06)
    completed_segment = tracker.last_ended_segment
    completed_at = tracker.last_ended_at
    tracker.process_mulaw(VOICED_FRAME * 3, received_at=10.12)

    assert completed_segment == 1
    assert completed_at == pytest.approx(10.02)
    assert tracker.last_ended_segment == completed_segment
    assert tracker.last_ended_at == completed_at
    assert tracker.last_voiced_at > completed_at


def test_native_classifier_load_failure_is_optional(monkeypatch) -> None:
    def fail_import(_name: str):
        raise OSError("native loader unavailable")

    monkeypatch.setattr(
        caller_activity_module.importlib,
        "import_module",
        fail_import,
    )

    assert caller_activity_module._load_webrtcvad() is None


def test_default_classifier_reports_unavailable_dependency(monkeypatch) -> None:
    monkeypatch.setattr(caller_activity_module, "webrtcvad", None)

    with pytest.raises(RuntimeError, match="WebRTC VAD is unavailable"):
        CallerActivityTracker()


def test_upstream_speech_fixture_calibrates_mulaw_endpoint_bias() -> None:
    pcm = CALIBRATION_FIXTURE.read_bytes()
    assert hashlib.sha256(pcm).hexdigest() == CALIBRATION_SHA256

    upstream_frame_bytes = 8_000 * 2 * 30 // 1_000
    upstream_frames = [
        pcm[position : position + upstream_frame_bytes]
        for position in range(0, len(pcm), upstream_frame_bytes)
        if len(pcm[position : position + upstream_frame_bytes]) == upstream_frame_bytes
    ]
    vad = caller_activity_module.webrtcvad.Vad(2)
    upstream_pattern = "".join(
        "1" if vad.is_speech(frame, 8_000) else "0" for frame in upstream_frames
    )
    assert upstream_pattern == UPSTREAM_MODE2_30MS_PATTERN

    mulaw = audioop.lin2ulaw(pcm, 2)
    complete_mulaw_bytes = len(mulaw) // FRAME_BYTES * FRAME_BYTES
    twilio_frames = [
        mulaw[position : position + FRAME_BYTES]
        for position in range(0, complete_mulaw_bytes, FRAME_BYTES)
    ]
    tracker = CallerActivityTracker()
    events = []
    for index, frame in enumerate(twilio_frames + [SILENCE_FRAME] * 20):
        events.extend(
            tracker.process_mulaw(
                frame,
                received_at=(index + 1) * 0.02,
            )
        )

    assert events == [
        CallerActivityEvent(kind="start", segment=1, at=pytest.approx(0.04)),
        CallerActivityEvent(kind="end", segment=1, at=pytest.approx(0.80)),
    ]
    assert tracker.last_ended_segment == 1
    assert tracker.last_ended_at == pytest.approx(0.80)
    assert tracker.last_ended_at - 0.78 == pytest.approx(0.02)
