"""Live-view transcript pushes must not drop the last line of a burst.

On call CAa5e0de the caller spoke, Kevin replied 0.7s later, and then the
call went silent. The inline throttle (push only if a full window has passed)
suppressed Kevin's line and nothing ever arrived to flush it, so the app's
live view was missing the one line that mattered. A suppressed push must
schedule a trailing flush for when the window reopens.
"""

import asyncio
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.webhooks.media_stream import LiveTranscriptPusher


@pytest.fixture
def writes(monkeypatch):
    written = []

    async def fake_update(call_sid, data):
        written.append((call_sid, data.get("transcript_buffer", "")))
        return True

    monkeypatch.setattr(
        "app.webhooks.media_stream.update_active_call", fake_update
    )
    return written


@pytest.mark.asyncio
async def test_burst_followed_by_silence_still_flushes_the_last_line(writes):
    lines: list[str] = []
    pusher = LiveTranscriptPusher("CA_test", lines, throttle=0.05)

    lines.append("Caller: Do you guys do toilet replacement?")
    pusher.push()
    lines.append("Kevin: Let me see if Deli is available, one moment.")
    pusher.push()  # inside the throttle window — the CAa5e0de drop
    await asyncio.sleep(0.15)  # silence; no further lines ever arrive

    assert writes, "nothing was pushed at all"
    assert "Kevin: Let me see if Deli is available" in writes[-1][1]


@pytest.mark.asyncio
async def test_rapid_lines_coalesce_into_one_trailing_flush(writes):
    lines: list[str] = []
    pusher = LiveTranscriptPusher("CA_test", lines, throttle=0.05)

    lines.append("Caller: Hi")
    pusher.push()
    for i in range(5):
        lines.append(f"Kevin: part {i}")
        pusher.push()
    await asyncio.sleep(0.15)

    # One immediate push plus one trailing flush — not one write per line.
    assert len(writes) == 2
    assert "Kevin: part 4" in writes[-1][1]


@pytest.mark.asyncio
async def test_spaced_lines_push_immediately(writes):
    lines: list[str] = []
    pusher = LiveTranscriptPusher("CA_test", lines, throttle=0.02)

    lines.append("Caller: Hi")
    pusher.push()
    await asyncio.sleep(0.05)
    lines.append("Kevin: Hello!")
    pusher.push()
    await asyncio.sleep(0.01)

    assert len(writes) == 2
