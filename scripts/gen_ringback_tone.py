#!/usr/bin/env python3
"""Generate the ringing tone a caller hears while Kevin rings the owner's phone.

Usage:
    python scripts/gen_ringback_tone.py [output_path]

Writes `app/static/audio/ringback-us.wav` by default: one cycle of the North
American ringback tone (440 Hz + 480 Hz, 2 s on, 4 s off) as 8 kHz mono 16-bit
PCM, the same format as `message-pause-3s.wav`. The conference waitUrl
(`/webhooks/twilio/ringback`) plays it with `<Play loop="0">`, so it repeats
for as long as the caller waits.

Each burst fades in and out over a few milliseconds so its edges don't click.
"""

import math
import struct
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 8000
TONES_HZ = (440, 480)
ON_SECONDS = 2
OFF_SECONDS = 4
AMPLITUDE = 0.12  # per tone, as a fraction of full scale
RAMP_SECONDS = 0.005

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "app" / "static" / "audio" / "ringback-us.wav"


def ringback_frames() -> bytes:
    """PCM frames for one ring cycle: a burst, then silence."""
    burst_len = SAMPLE_RATE * ON_SECONDS
    ramp_len = round(SAMPLE_RATE * RAMP_SECONDS)
    burst = []
    for n in range(burst_len):
        value = sum(math.sin(2 * math.pi * hz * n / SAMPLE_RATE) for hz in TONES_HZ)
        edge = min(n, burst_len - 1 - n)
        if edge < ramp_len:
            value *= 0.5 - 0.5 * math.cos(math.pi * edge / ramp_len)  # raised-cosine fade
        burst.append(round(value * AMPLITUDE * 32767))
    tone = struct.pack(f"<{burst_len}h", *burst)
    return tone + bytes(2 * SAMPLE_RATE * OFF_SECONDS)


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    with wave.open(str(output), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(ringback_frames())
    sys.stdout.write(f"wrote {output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
