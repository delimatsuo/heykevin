"""The caller hears a ringing tone, not hold music, while Kevin rings the owner.

Ring-through (whitelisted contacts, expired subscriptions) parks the caller in a
conference and rings the owner's phone through the app for 20 s. The conference
waitUrl is what the caller hears meanwhile.
"""

import math
import mimetypes
import os
import struct
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.config import settings
from app.middleware.twilio_verify import verify_twilio_signature
from app.webhooks import twilio_incoming

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"
TONE_FILE = STATIC_DIR / twilio_incoming.RINGBACK_TONE_PATH.removeprefix("/static/")
BASE_URL = "https://kevin.example.test/"
# Audio types Twilio's <Play> accepts for WAV.
TWILIO_WAV_TYPES = {"audio/wav", "audio/wave", "audio/x-wav"}


@pytest.fixture
def base_url(monkeypatch):
    monkeypatch.setattr(settings, "cloud_run_url", BASE_URL)


def _tone_samples():
    with wave.open(str(TONE_FILE), "rb") as wf:
        params = wf.getparams()
        frames = wf.readframes(params.nframes)
    return params, list(struct.unpack(f"<{params.nframes}h", frames))


def _goertzel_power(samples: list[int], hz: float, rate: int) -> float:
    coeff = 2 * math.cos(2 * math.pi * hz / rate)
    prev = prev2 = 0.0
    for s in samples:
        prev, prev2 = s + coeff * prev - prev2, prev
    return prev2 ** 2 + prev ** 2 - coeff * prev * prev2


def test_ring_through_conference_waits_on_signed_ringback(base_url):
    twiml = twilio_incoming._conference_twiml("CA-test", "ring_test")
    conference = ET.fromstring(twiml).find("./Dial/Conference")
    assert conference is not None, twiml
    assert conference.text == "ring_test"
    assert conference.get("waitUrl") == "https://kevin.example.test/webhooks/twilio/ringback"
    # Twilio sends the call's phone numbers with waitUrl requests; POST keeps them out of URLs.
    assert conference.get("waitMethod") == "POST"


def test_ringback_twiml_loops_the_tone(base_url):
    play = ET.fromstring(twilio_incoming._ringback_twiml()).find("./Play")
    assert play is not None
    assert play.text == "https://kevin.example.test/static/audio/ringback-us.wav"
    assert play.get("loop") == "0", "loop forever until the conference starts"


async def test_ringback_route_is_signed_post_returning_the_loop(base_url):
    routes = [r for r in twilio_incoming.router.routes if getattr(r, "path", "") == "/webhooks/twilio/ringback"]
    assert len(routes) == 1
    assert routes[0].methods == {"POST"}
    assert verify_twilio_signature in [d.call for d in routes[0].dependant.dependencies]

    response = await twilio_incoming.handle_ringback(request=None)
    assert response.media_type == "application/xml"
    assert bytes(response.body).decode() == twilio_incoming._ringback_twiml()


def test_ringback_tone_is_served_as_twilio_playable_audio():
    assert TONE_FILE.is_file()
    mime, _ = mimetypes.guess_type(TONE_FILE.name)
    assert mime in TWILIO_WAV_TYPES


def test_ringback_tone_is_one_us_ring_cycle():
    params, samples = _tone_samples()
    rate = params.framerate
    assert (params.nchannels, params.sampwidth, rate) == (1, 2, 8000)
    assert params.nframes == 6 * rate, "one 6 s cycle; <Play loop=0> repeats it"

    # Every 100 ms of the 2 s burst must ring, so a shorter or broken burst fails.
    for start in range(0, 2 * rate, rate // 10):
        chunk = samples[start:start + rate // 10]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        assert rms > 2000, f"ring burst at {start / rate:.1f}s is too quiet ({rms:.0f})"
    assert not any(samples[2 * rate:]), "4 s gap after the burst must be silent"

    burst = samples[:2 * rate]
    ring = min(_goertzel_power(burst, hz, rate) for hz in (440, 480))
    other = max(_goertzel_power(burst, hz, rate) for hz in (350, 400, 450, 620))
    assert ring > 100 * other, "burst must be the 440 Hz + 480 Hz North American ringback"


def test_ringback_burst_fades_at_its_edges():
    _, samples = _tone_samples()
    peak = max(abs(s) for s in samples)
    # A burst that starts or stops at full level clicks; the ramp keeps its first and last samples near zero.
    assert abs(samples[0]) < peak / 50
    assert abs(samples[2 * 8000 - 1]) < peak / 50
    assert max(abs(s) for s in samples[:8]) < peak / 4
