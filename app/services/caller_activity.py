"""Bounded caller-activity tracking for 8 kHz Twilio audio."""

from collections.abc import Callable
from dataclasses import dataclass
import importlib

from app.utils.audio import mulaw_to_pcm8k


SAMPLE_RATE = 8_000
MULAW_FRAME_BYTES = 160
FRAME_DURATION_SECONDS = 0.02

FrameClassifier = Callable[[bytes, int], bool]


def _load_webrtcvad():
    """Load the optional native classifier without breaking voice startup."""
    try:
        return importlib.import_module("webrtcvad")
    except Exception:
        return None


webrtcvad = _load_webrtcvad()


@dataclass(frozen=True, slots=True)
class CallerActivityEvent:
    kind: str
    segment: int
    at: float


class CallerActivityTracker:
    """Detect confirmed caller-activity segments from Twilio mulaw audio."""

    def __init__(
        self,
        classifier: FrameClassifier | None = None,
        *,
        min_speech_frames: int = 3,
        end_silence_frames: int = 15,
    ) -> None:
        if min_speech_frames < 1:
            raise ValueError("min_speech_frames must be at least 1")
        if end_silence_frames < 1:
            raise ValueError("end_silence_frames must be at least 1")

        if classifier is None:
            if webrtcvad is None:
                raise RuntimeError("WebRTC VAD is unavailable")
            classifier = webrtcvad.Vad(2).is_speech

        self._classifier = classifier
        self._min_speech_frames = min_speech_frames
        self._end_silence_frames = end_silence_frames
        self.reset()

    @property
    def segment(self) -> int:
        return self._segment

    @property
    def last_voiced_at(self) -> float:
        return self._last_voiced_at

    @property
    def currently_voiced(self) -> bool:
        return self._currently_voiced

    @property
    def active(self) -> bool:
        return self._active

    @property
    def last_ended_segment(self) -> int:
        return self._last_ended_segment

    @property
    def last_ended_at(self) -> float:
        return self._last_ended_at

    def process_mulaw(
        self,
        mulaw_bytes: bytes,
        *,
        received_at: float,
    ) -> tuple[CallerActivityEvent, ...]:
        """Process complete 20 ms frames, retaining only a partial trailing frame."""
        self._mulaw_buffer.extend(mulaw_bytes)
        frame_count = len(self._mulaw_buffer) // MULAW_FRAME_BYTES
        if frame_count == 0:
            return ()

        complete_bytes = frame_count * MULAW_FRAME_BYTES
        complete_audio = bytes(self._mulaw_buffer[:complete_bytes])
        del self._mulaw_buffer[:complete_bytes]

        first_frame_at = received_at - (frame_count - 1) * FRAME_DURATION_SECONDS
        events: list[CallerActivityEvent] = []

        for index in range(frame_count):
            offset = index * MULAW_FRAME_BYTES
            mulaw_frame = complete_audio[offset : offset + MULAW_FRAME_BYTES]
            pcm_frame = mulaw_to_pcm8k(mulaw_frame)
            frame_at = first_frame_at + index * FRAME_DURATION_SECONDS
            is_voiced = bool(self._classifier(pcm_frame, SAMPLE_RATE))
            self._currently_voiced = is_voiced

            if is_voiced:
                self._process_voiced_frame(frame_at, events)
            else:
                self._process_silent_frame(events)

        return tuple(events)

    def reset(self) -> None:
        self._mulaw_buffer = bytearray()
        self._segment = 0
        self._last_voiced_at = 0.0
        self._currently_voiced = False
        self._active = False
        self._candidate_speech_frames = 0
        self._candidate_started_at: float | None = None
        self._silence_frames = 0
        self._last_ended_segment = 0
        self._last_ended_at = 0.0

    def _process_voiced_frame(
        self,
        frame_at: float,
        events: list[CallerActivityEvent],
    ) -> None:
        self._last_voiced_at = frame_at
        self._silence_frames = 0

        if self._active:
            return

        if self._candidate_speech_frames == 0:
            self._candidate_started_at = frame_at
        self._candidate_speech_frames += 1

        if self._candidate_speech_frames < self._min_speech_frames:
            return

        self._segment += 1
        self._active = True
        events.append(
            CallerActivityEvent(
                kind="start",
                segment=self._segment,
                at=self._candidate_started_at
                if self._candidate_started_at is not None
                else frame_at,
            )
        )
        self._candidate_speech_frames = 0
        self._candidate_started_at = None

    def _process_silent_frame(self, events: list[CallerActivityEvent]) -> None:
        if not self._active:
            self._candidate_speech_frames = 0
            self._candidate_started_at = None
            return

        self._silence_frames += 1
        if self._silence_frames < self._end_silence_frames:
            return

        self._last_ended_segment = self._segment
        self._last_ended_at = self._last_voiced_at
        events.append(
            CallerActivityEvent(kind="end", segment=self._segment, at=self._last_voiced_at)
        )
        self._active = False
        self._silence_frames = 0
