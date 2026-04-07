"""Audio format conversion utilities for Twilio <-> Gemini.

Twilio sends/expects: mulaw 8kHz mono
Gemini expects: PCM 16-bit 16kHz mono
Gemini outputs: PCM 16-bit 24kHz mono
"""

import audioop


def mulaw_to_pcm16k(mulaw_8k: bytes) -> bytes:
    """Decode mulaw 8kHz to linear PCM 16kHz for Gemini input.

    Steps:
    1. mulaw -> linear PCM 16-bit at 8kHz
    2. Upsample 8kHz -> 16kHz
    """
    pcm_8k = audioop.ulaw2lin(mulaw_8k, 2)
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    return pcm_16k


def pcm24k_to_mulaw(pcm_24k: bytes) -> bytes:
    """Convert PCM 24kHz from Gemini output to mulaw 8kHz for Twilio.

    Steps:
    1. Downsample 24kHz -> 8kHz
    2. Linear PCM -> mulaw
    """
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)
