"""Inbound SMS/MMS webhook tests.

Every provisioned Kevin number has its `sms_url` pointed at
/webhooks/twilio/mms-incoming (app/db/contractors.py), but the route was never
implemented — so every text a caller sent to a Kevin number returned 404 and
Twilio recorded an 11200 HTTP retrieval failure. The messages were lost.

The handler deliberately does not reply to the sender. Auto-replying to inbound
traffic carries A2P and consent implications; the job here is to stop dropping
messages on the floor.
"""

import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.webhooks import twilio_incoming


class _FakeFormRequest:
    def __init__(self, form: dict[str, str]):
        self._form = form

    async def form(self):
        return self._form


def _form(**kw) -> dict[str, str]:
    base = {
        "MessageSid": "SM" + "0" * 30,
        "From": "+14155559876",
        "To": "+14155551234",
        "Body": "call me back",
        "NumMedia": "0",
    }
    base.update(kw)
    return base


@pytest.fixture
def wired(monkeypatch):
    """Stub the two Firestore-touching helpers and capture what was written."""
    captured: dict = {}

    async def fake_lookup(to_number):
        if to_number == "+14155551234":
            return {"contractor_id": "c-123", "twilio_number": to_number}
        return None

    async def fake_record(contractor_id, payload):
        captured["contractor_id"] = contractor_id
        captured["payload"] = payload
        return True

    captured["pushes"] = []

    async def fake_get_device_token(contractor_id=""):
        return captured.get("device_token", "tok-abc")

    async def fake_send_regular_push(**kwargs):
        captured["pushes"].append(kwargs)
        return True

    monkeypatch.setattr(twilio_incoming, "_lookup_contractor_for_message", fake_lookup)
    monkeypatch.setattr(twilio_incoming, "_record_inbound_message", fake_record)
    # Patched at the source module: _notify_owner_of_inbound_message imports
    # these at call time, so the local `from ... import` picks up the fakes.
    monkeypatch.setattr(
        "app.services.push_notification.get_device_token", fake_get_device_token
    )
    monkeypatch.setattr(
        "app.services.push_notification.send_regular_push", fake_send_regular_push
    )
    return captured


@pytest.mark.asyncio
async def test_returns_200_not_404(wired):
    """The whole bug: this route did not exist, so Twilio got a 404."""
    response = await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form()))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_returns_empty_twiml_without_auto_reply(wired):
    response = await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form()))
    body = response.body.decode()
    assert "<Response>" in body
    assert "<Message" not in body
    assert response.media_type == "application/xml"


@pytest.mark.asyncio
async def test_records_message_against_owning_contractor(wired):
    await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form(Body="need a quote")))
    assert wired["contractor_id"] == "c-123"
    assert wired["payload"]["body"] == "need a quote"
    assert wired["payload"]["from_number"] == "+14155559876"
    assert wired["payload"]["message_sid"].startswith("SM")


@pytest.mark.asyncio
async def test_unknown_destination_still_returns_200(wired):
    """An unrecognized To must not error — Twilio retries non-2xx."""
    response = await twilio_incoming.handle_inbound_message(
        _FakeFormRequest(_form(To="+14155550000"))
    )
    assert response.status_code == 200
    assert "contractor_id" not in wired


@pytest.mark.asyncio
async def test_media_count_captured(wired):
    await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form(NumMedia="2")))
    assert wired["payload"]["num_media"] == 2


@pytest.mark.asyncio
async def test_malformed_media_count_does_not_raise(wired):
    response = await twilio_incoming.handle_inbound_message(
        _FakeFormRequest(_form(NumMedia="lots"))
    )
    assert response.status_code == 200
    assert wired["payload"]["num_media"] == 0


@pytest.mark.asyncio
async def test_persistence_failure_returns_500_for_redelivery(monkeypatch):
    """A Firestore blip must make Twilio redeliver, not lose the message.

    Returning 200 on a failed write acknowledges a message that was never
    stored. Redelivery is idempotent because records are keyed by MessageSid.
    """

    async def fake_lookup(to_number):
        return {"contractor_id": "c-123"}

    async def boom(contractor_id, payload):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(twilio_incoming, "_lookup_contractor_for_message", fake_lookup)
    monkeypatch.setattr(twilio_incoming, "_record_inbound_message", boom)

    response = await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form()))
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_mms_attachments_are_persisted(wired):
    """An image-only MMS must keep its MediaUrl/MediaContentType, not just a count."""
    await twilio_incoming.handle_inbound_message(
        _FakeFormRequest(
            _form(
                Body="",
                NumMedia="2",
                MediaUrl0="https://api.twilio.com/media/ME0",
                MediaContentType0="image/jpeg",
                MediaUrl1="https://api.twilio.com/media/ME1",
                MediaContentType1="image/png",
            )
        )
    )
    assert wired["payload"]["media"] == [
        {"url": "https://api.twilio.com/media/ME0", "content_type": "image/jpeg"},
        {"url": "https://api.twilio.com/media/ME1", "content_type": "image/png"},
    ]


@pytest.mark.asyncio
async def test_unparseable_sender_is_stored_verbatim(wired):
    """Short codes and alphanumeric senders will not normalize to E.164."""
    await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form(From="12345")))
    assert wired["payload"]["from_number"] == "12345"


# --- Surfacing replies to the owner -----------------------------------------
# Storing a reply is not the same as the owner seeing it. Nothing reads
# contractors/{id}/inbound_messages, so before this every caller reply landed in
# Firestore unseen. Appointment confirmations now invite a reply ("Reply STOP to
# opt out"), and inviting replies into a black hole is worse than not texting.


@pytest.mark.asyncio
async def test_owner_is_notified_when_a_caller_replies(wired):
    await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form()))

    assert len(wired["pushes"]) == 1
    assert wired["pushes"][0]["contractor_id"] == "c-123"
    assert wired["pushes"][0]["device_token"] == "tok-abc"


@pytest.mark.asyncio
async def test_push_body_carries_no_caller_identity_or_message_text(wired):
    """Push bodies are lock-screen visible and land in OS logs.

    Matches _safe_incoming_call_push_body: the content stays in the app behind
    auth. A regression that inlines the caller's number or words fails here.
    """
    await twilio_incoming.handle_inbound_message(
        _FakeFormRequest(_form(Body="my card number is 4111 1111 1111 1111"))
    )

    push = wired["pushes"][0]
    blob = f"{push.get('title', '')} {push.get('body', '')}"
    assert "4111" not in blob
    assert "card number" not in blob
    assert "+14155559876" not in blob
    assert "9876" not in blob


@pytest.mark.asyncio
async def test_unknown_destination_sends_no_push(wired):
    await twilio_incoming.handle_inbound_message(
        _FakeFormRequest(_form(To="+14155550000"))
    )
    assert wired["pushes"] == []


@pytest.mark.asyncio
async def test_missing_device_token_still_returns_200(wired):
    """An owner with no registered device must not cause Twilio redelivery."""
    wired["device_token"] = ""
    response = await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form()))
    assert response.status_code == 200
    assert wired["pushes"] == []
    # The message itself is still stored — notification is the best-effort part.
    assert wired["contractor_id"] == "c-123"


@pytest.mark.asyncio
async def test_push_failure_does_not_fail_the_webhook(wired, monkeypatch):
    """Twilio would redeliver on non-2xx and re-store an already-stored message."""

    async def boom(**kwargs):
        raise RuntimeError("APNs down")

    monkeypatch.setattr("app.services.push_notification.send_regular_push", boom)

    response = await twilio_incoming.handle_inbound_message(_FakeFormRequest(_form()))
    assert response.status_code == 200
    assert wired["contractor_id"] == "c-123"
