"""Tests for the owner-run App Store notification replay (app/services/appstore_replay.py
and scripts/replay_appstore_notifications.py).

No network: httpx.MockTransport stands in for Apple's Get Notification
History endpoint. All app.* imports need the same baseline required
Settings fields as the rest of the suite (Settings is frozen at first
app.config import), so they are set here exactly like the other App Store
test modules do -- this file sorts first alphabetically among the three
run together, so it may be the first to import app.config in the session.
"""

from __future__ import annotations

import base64
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import httpx
import pytest

from app.db.contractors import get_contractor_by_subscription_uuid
from app.services.appstore_replay import (
    HistoryItem,
    ReplayReport,
    fetch_notification_history,
    replay,
    summarize,
)
from app.services.subscription import APPSTORE_PRODUCTION_URL, handle_appstore_notification
from app.webhooks.appstore import _decode_notification_payload


def _unsigned_jws(payload: dict) -> str:
    """Build a JWS-shaped (but unsigned) string -- enough for _decode_jws_payload,
    which only base64-decodes the middle segment and never checks the signature."""
    header = {"alg": "ES256"}

    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


# ---------------------------------------------------------------------------
# fetch_notification_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_paginates_across_pages():
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if "paginationToken" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "notificationHistory": [
                        {"signedPayload": "p3", "sendAttempts": []},
                    ],
                    "hasMore": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "notificationHistory": [
                    {"signedPayload": "p1", "sendAttempts": [{"sendAttemptResult": "SUCCESS"}]},
                    {"signedPayload": "p2", "sendAttempts": []},
                ],
                "hasMore": True,
                "paginationToken": "t1",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        items = await fetch_notification_history(
            base_url=APPSTORE_PRODUCTION_URL,
            start_ms=1_000,
            end_ms=2_000,
            only_failures=True,
            token_factory=lambda: "tok",
            client=client,
        )

    assert [i.signed_payload for i in items] == ["p1", "p2", "p3"]
    assert len(requests_seen) == 2
    assert "paginationToken=t1" in str(requests_seen[1].url)
    assert "paginationToken" not in str(requests_seen[0].url)


@pytest.mark.asyncio
async def test_fetch_sends_expected_request_body_and_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"notificationHistory": [], "hasMore": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await fetch_notification_history(
            base_url=APPSTORE_PRODUCTION_URL,
            start_ms=12345,
            end_ms=67890,
            only_failures=False,
            token_factory=lambda: "my-jwt",
            client=client,
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/inApps/v1/notifications/history"
    assert captured["body"]["startDate"] == 12345
    assert captured["body"]["endDate"] == 67890
    assert captured["body"]["onlyFailures"] is False
    assert captured["auth"] == "Bearer my-jwt"


@pytest.mark.asyncio
async def test_fetch_only_failures_true_is_sent_when_requested():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"notificationHistory": [], "hasMore": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await fetch_notification_history(
            base_url=APPSTORE_PRODUCTION_URL,
            start_ms=1,
            end_ms=2,
            only_failures=True,
            token_factory=lambda: "tok",
            client=client,
        )

    assert captured["body"]["onlyFailures"] is True


@pytest.mark.asyncio
async def test_fetch_raises_runtime_error_on_non_200_with_no_partial_result():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "notificationHistory": [{"signedPayload": "p1", "sendAttempts": []}],
                    "hasMore": True,
                    "paginationToken": "t1",
                },
            )
        return httpx.Response(500, text="server error")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError) as excinfo:
            await fetch_notification_history(
                base_url=APPSTORE_PRODUCTION_URL,
                start_ms=1,
                end_ms=2,
                only_failures=True,
                token_factory=lambda: "tok",
                client=client,
            )

    assert "500" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_raises_when_max_pages_exceeded():
    """Apple always claiming hasMore=true with a fresh token every time must
    not page forever -- max_pages bounds it and reports why."""

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("paginationToken", "start")
        return httpx.Response(
            200,
            json={
                "notificationHistory": [],
                "hasMore": True,
                "paginationToken": token + "x",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError) as excinfo:
            await fetch_notification_history(
                base_url=APPSTORE_PRODUCTION_URL,
                start_ms=1,
                end_ms=2,
                only_failures=True,
                token_factory=lambda: "tok",
                client=client,
                max_pages=3,
            )

    assert "max_pages" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_stops_instead_of_looping_forever_on_a_repeated_token(caplog):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "notificationHistory": [{"signedPayload": f"p{call_count}", "sendAttempts": []}],
                "hasMore": True,
                "paginationToken": "same-token",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with caplog.at_level(logging.WARNING):
            items = await fetch_notification_history(
                base_url=APPSTORE_PRODUCTION_URL,
                start_ms=1,
                end_ms=2,
                only_failures=True,
                token_factory=lambda: "tok",
                client=client,
                max_pages=50,
            )

    # A truncated fetch (stopped early due to a repeated token) must be
    # visible in the logs, not silent.
    assert any(
        "repeated paginationToken" in record.getMessage() for record in caplog.records
    )

    # Page 1 has no token yet; page 2 carries "same-token" and Apple hands
    # back "same-token" again -- fetch must stop there rather than loop.
    assert call_count == 2
    assert len(items) == 2


@pytest.mark.asyncio
async def test_fetch_self_created_client_gets_an_explicit_timeout(monkeypatch):
    captured_kwargs = {}
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"notificationHistory": [], "hasMore": False})

    def spy_async_client(*args, **kwargs):
        captured_kwargs.update(kwargs)
        kwargs.pop("timeout", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy_async_client)

    await fetch_notification_history(
        base_url=APPSTORE_PRODUCTION_URL,
        start_ms=1,
        end_ms=2,
        only_failures=True,
        token_factory=lambda: "tok",
    )

    assert captured_kwargs.get("timeout") == 30.0


@pytest.mark.asyncio
async def test_fetch_keeps_the_last_six_send_attempts_not_the_first():
    attempts = [{"sendAttemptResult": f"R{i}"} for i in range(9)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "notificationHistory": [{"signedPayload": "p1", "sendAttempts": attempts}],
                "hasMore": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        items = await fetch_notification_history(
            base_url=APPSTORE_PRODUCTION_URL,
            start_ms=1,
            end_ms=2,
            only_failures=True,
            token_factory=lambda: "tok",
            client=client,
        )

    kept = [a["sendAttemptResult"] for a in items[0].send_attempts]
    assert kept == [f"R{i}" for i in range(3, 9)]


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_dry_run_never_calls_handler_and_verifies_once_per_item():
    items = [
        HistoryItem(signed_payload="a", send_attempts=[]),
        HistoryItem(signed_payload="b", send_attempts=[]),
    ]
    verify_calls = []

    def fake_verify(sp):
        verify_calls.append(sp)
        return {"notificationType": "DID_RENEW", "signedDate": 1000}

    handler_calls = []

    async def fake_handler(payload):
        handler_calls.append(payload)
        return True

    emitted = []
    report = await replay(
        items, apply=False, verify=fake_verify, handler=fake_handler, emit=emitted.append
    )

    assert verify_calls == ["a", "b"]
    assert handler_calls == []
    assert isinstance(report, ReplayReport)
    assert report.fetched == 2
    assert report.rejected == 0
    assert report.dry_run == report.fetched - report.rejected == 2
    assert report.applied == 0
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_replay_apply_orders_handler_calls_ascending_by_signed_date():
    items = [
        HistoryItem(signed_payload="late", send_attempts=[]),
        HistoryItem(signed_payload="early", send_attempts=[]),
        HistoryItem(signed_payload="mid", send_attempts=[]),
    ]
    payloads = {
        "late": {"notificationType": "DID_RENEW", "signedDate": 3000},
        "early": {"notificationType": "DID_RENEW", "signedDate": 1000},
        "mid": {"notificationType": "DID_RENEW", "signedDate": 2000},
    }

    def fake_verify(sp):
        return payloads[sp]

    seen_dates = []

    async def fake_handler(payload):
        seen_dates.append(payload["signedDate"])
        return True

    report = await replay(
        items, apply=True, verify=fake_verify, handler=fake_handler, emit=lambda *_: None
    )

    assert seen_dates == [1000, 2000, 3000]
    assert report.applied == 3


@pytest.mark.asyncio
async def test_replay_sorts_missing_or_invalid_signed_date_first_stable():
    items = [
        HistoryItem(signed_payload="has_date", send_attempts=[]),
        HistoryItem(signed_payload="no_date", send_attempts=[]),
        HistoryItem(signed_payload="bad_date", send_attempts=[]),
    ]
    payloads = {
        "has_date": {"notificationType": "DID_RENEW", "signedDate": 500},
        "no_date": {"notificationType": "DID_RENEW"},
        "bad_date": {"notificationType": "DID_RENEW", "signedDate": "not-a-number"},
    }

    def fake_verify(sp):
        return payloads[sp]

    order = []

    async def fake_handler(payload):
        order.append(payload)
        return True

    await replay(items, apply=True, verify=fake_verify, handler=fake_handler, emit=lambda *_: None)

    assert order == [payloads["no_date"], payloads["bad_date"], payloads["has_date"]]


@pytest.mark.asyncio
async def test_replay_counts_rejected_and_continues_to_the_others():
    items = [
        HistoryItem(signed_payload="bad", send_attempts=[]),
        HistoryItem(signed_payload="good1", send_attempts=[]),
        HistoryItem(signed_payload="good2", send_attempts=[]),
    ]

    def fake_verify(sp):
        if sp == "bad":
            raise ValueError("forged signature")
        return {"notificationType": "DID_RENEW", "signedDate": 1000}

    handled = []

    async def fake_handler(payload):
        handled.append(payload)
        return True

    report = await replay(
        items, apply=True, verify=fake_verify, handler=fake_handler, emit=lambda *_: None
    )

    assert report.fetched == 3
    assert report.rejected == 1
    assert len(handled) == 2
    assert report.applied == 2


@pytest.mark.asyncio
async def test_replay_counts_handler_false_and_error_separately_and_continues():
    items = [
        HistoryItem(signed_payload="a", send_attempts=[]),
        HistoryItem(signed_payload="b", send_attempts=[]),
        HistoryItem(signed_payload="c", send_attempts=[]),
    ]
    order = {"a": 1000, "b": 2000, "c": 3000}

    def fake_verify(sp):
        return {"notificationType": "DID_RENEW", "signedDate": order[sp], "id": sp}

    async def fake_handler(payload):
        if payload["id"] == "a":
            return True
        if payload["id"] == "b":
            return False
        raise RuntimeError("boom")

    report = await replay(
        items, apply=True, verify=fake_verify, handler=fake_handler, emit=lambda *_: None
    )

    assert report.fetched == 3
    assert report.rejected == 0
    assert report.applied == 1
    assert report.handler_false == 1
    assert report.handler_error == 1


@pytest.mark.asyncio
async def test_replay_by_type_counts_verified_notifications_only():
    items = [
        HistoryItem(signed_payload="rejected", send_attempts=[]),
        HistoryItem(signed_payload="renew1", send_attempts=[]),
        HistoryItem(signed_payload="renew2", send_attempts=[]),
        HistoryItem(signed_payload="refund", send_attempts=[]),
    ]

    def fake_verify(sp):
        if sp == "rejected":
            raise ValueError("bad")
        kind = "REFUND" if sp == "refund" else "DID_RENEW"
        return {"notificationType": kind, "signedDate": 1000}

    async def fake_handler(_payload):
        return True

    report = await replay(
        items, apply=False, verify=fake_verify, handler=fake_handler, emit=lambda *_: None
    )

    assert report.by_type == {"DID_RENEW": 2, "REFUND": 1}


@pytest.mark.asyncio
async def test_by_type_excludes_stale_items_and_stale_by_type_counts_them():
    """by_type must reconcile with the outcome counters (dry_run + applied +
    handler_false + handler_error) -- a stale item is neither applied, nor
    dry-run-counted, so it must not be in by_type either. It shows up in
    stale_by_type instead."""
    expires_ms = 1_700_000_000_000
    stale_payload = _deactivating_payload(
        "EXPIRED", expires_ms=expires_ms, app_account_token="stale-uuid", signed_date=1000
    )
    fresh_payload = _deactivating_payload(
        "REFUND", expires_ms=expires_ms, app_account_token="fresh-uuid", signed_date=2000
    )
    renew_payload = {"notificationType": "DID_RENEW", "signedDate": 3000}
    items = [
        HistoryItem(signed_payload="stale", send_attempts=[]),
        HistoryItem(signed_payload="fresh", send_attempts=[]),
        HistoryItem(signed_payload="renew", send_attempts=[]),
    ]
    payloads = {"stale": stale_payload, "fresh": fresh_payload, "renew": renew_payload}

    async def fake_lookup(token):
        if token == "stale-uuid":
            return {"subscription_expires": expires_ms / 1000.0 + 500}  # later term on file
        return {"subscription_expires": expires_ms / 1000.0}

    async def fake_handler(_payload):
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payloads[sp], handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert report.stale_by_type == {"EXPIRED": 1}
    assert report.by_type == {"REFUND": 1, "DID_RENEW": 1}
    assert sum(report.by_type.values()) == (
        report.dry_run + report.applied + report.handler_false + report.handler_error
    )


@pytest.mark.asyncio
async def test_replay_verify_non_value_error_exception_also_counts_as_rejected():
    """A malformed payload can make the verifier raise something other than
    ValueError (e.g. AttributeError on a non-dict `data`); that must still
    count as rejected and let the run continue, not abort the whole batch."""
    items = [
        HistoryItem(signed_payload="bad", send_attempts=[]),
        HistoryItem(signed_payload="good", send_attempts=[]),
    ]

    def fake_verify(sp):
        if sp == "bad":
            raise AttributeError("'NoneType' object has no attribute 'get'")
        return {"notificationType": "DID_RENEW", "signedDate": 1000}

    handled = []

    async def fake_handler(payload):
        handled.append(payload)
        return True

    report = await replay(
        items, apply=True, verify=fake_verify, handler=fake_handler, emit=lambda *_: None
    )

    assert report.rejected == 1
    assert len(handled) == 1


def test_replay_defaults_bind_the_production_implementations():
    sig = inspect.signature(replay)
    assert sig.parameters["verify"].default is _decode_notification_payload
    assert sig.parameters["handler"].default is handle_appstore_notification
    assert sig.parameters["lookup"].default is get_contractor_by_subscription_uuid


# ---------------------------------------------------------------------------
# stale-deactivation guard
#
# EXPIRED/DID_FAIL_TO_RENEW/GRACE_PERIOD_EXPIRED/REFUND/REVOKE must never be
# applied when a later paid term is already on file for the contractor --
# otherwise replaying an old deactivation regresses a customer who has since
# renewed. REFUND_REVERSED is a re-activation, not a deactivation, and is
# deliberately not covered by this guard.
# ---------------------------------------------------------------------------


def _deactivating_payload(
    notification_type: str,
    *,
    expires_ms: int,
    app_account_token: str | None = "uuid-1",
    signed_date: int = 1000,
) -> dict:
    transaction_info: dict = {"expiresDate": expires_ms}
    if app_account_token is not None:
        transaction_info["appAccountToken"] = app_account_token
    return {
        "notificationType": notification_type,
        "subtype": "",
        "signedDate": signed_date,
        "data": {
            "environment": "Production",
            "signedTransactionInfo": _unsigned_jws(transaction_info),
        },
    }


@pytest.mark.asyncio
async def test_stale_expired_is_skipped_and_handler_not_called():
    expires_ms = 1_700_000_000_000
    payload = _deactivating_payload("EXPIRED", expires_ms=expires_ms)
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    async def fake_lookup(_token):
        return {"subscription_expires": expires_ms / 1000.0 + 999}  # later term on file

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert handled == []
    assert report.stale_skipped == 1
    assert report.applied == 0
    assert report.dry_run == 0


@pytest.mark.asyncio
async def test_expired_matching_stored_expiry_exactly_applies():
    expires_ms = 1_700_000_000_000
    payload = _deactivating_payload("EXPIRED", expires_ms=expires_ms)
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    async def fake_lookup(_token):
        return {"subscription_expires": expires_ms / 1000.0}  # exactly the same term

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert len(handled) == 1
    assert report.applied == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_refund_on_the_latest_term_applies():
    expires_ms = 1_700_000_000_000
    payload = _deactivating_payload("REFUND", expires_ms=expires_ms)
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    async def fake_lookup(_token):
        return {"subscription_expires": expires_ms / 1000.0}

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert len(handled) == 1
    assert report.applied == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_refund_on_an_older_term_is_stale():
    expires_ms = 1_700_000_000_000
    payload = _deactivating_payload("REFUND", expires_ms=expires_ms)
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    async def fake_lookup(_token):
        return {"subscription_expires": expires_ms / 1000.0 + 100}  # newer term on file

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert handled == []
    assert report.stale_skipped == 1


@pytest.mark.asyncio
async def test_lookup_raising_does_not_block_apply():
    payload = _deactivating_payload("EXPIRED", expires_ms=1_700_000_000_000)
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    async def raising_lookup(_token):
        raise RuntimeError("firestore unavailable")

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=raising_lookup, emit=lambda *_: None,
    )

    assert len(handled) == 1
    assert report.applied == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_missing_app_account_token_does_not_block_apply():
    payload = _deactivating_payload(
        "EXPIRED", expires_ms=1_700_000_000_000, app_account_token=None
    )
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    lookup_calls = []

    async def fake_lookup(token):
        lookup_calls.append(token)
        return {"subscription_expires": 99_999_999_999.0}

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert lookup_calls == []  # never reached: appAccountToken absent
    assert len(handled) == 1
    assert report.applied == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_did_renew_never_triggers_the_stale_lookup():
    payload = {
        "notificationType": "DID_RENEW",
        "subtype": "",
        "signedDate": 1000,
        "data": {
            "environment": "Production",
            "signedTransactionInfo": _unsigned_jws(
                {"appAccountToken": "uuid-1", "expiresDate": 1_700_000_000_000}
            ),
        },
    }
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    lookup_calls = []

    async def fake_lookup(token):
        lookup_calls.append(token)
        return {"subscription_expires": 99_999_999_999.0}

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert lookup_calls == []
    assert len(handled) == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_bool_subscription_expires_is_treated_as_absent():
    expires_ms = 1_700_000_000_000
    payload = _deactivating_payload("EXPIRED", expires_ms=expires_ms)
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    async def fake_lookup(_token):
        return {"subscription_expires": True}  # bool -- must not read as "later"

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert len(handled) == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_non_string_notification_type_does_not_raise():
    """A raw `in` test against the frozenset would raise TypeError for an
    unhashable notificationType (e.g. a list); the str(... or "") coercion
    must prevent that and simply treat it as non-deactivating."""
    payload = {
        "notificationType": ["not", "a", "string"],
        "signedDate": 1000,
        "data": {
            "environment": "Production",
            "signedTransactionInfo": _unsigned_jws(
                {"appAccountToken": "uuid-1", "expiresDate": 1_700_000_000_000}
            ),
        },
    }
    items = [HistoryItem(signed_payload="x", send_attempts=[])]

    lookup_calls = []

    async def fake_lookup(token):
        lookup_calls.append(token)
        return {"subscription_expires": 99_999_999_999.0}

    handled = []

    async def fake_handler(p):
        handled.append(p)
        return True

    report = await replay(
        items, apply=True, verify=lambda sp: payload, handler=fake_handler,
        lookup=fake_lookup, emit=lambda *_: None,
    )

    assert lookup_calls == []  # never a deactivating type -- lookup not reached
    assert len(handled) == 1
    assert report.stale_skipped == 0


@pytest.mark.asyncio
async def test_stale_guard_runs_in_dry_run_too_and_is_excluded_from_dry_run_count():
    expires_ms = 1_700_000_000_000
    stale_payload = _deactivating_payload(
        "EXPIRED", expires_ms=expires_ms, app_account_token="stale-uuid", signed_date=1000
    )
    fresh_payload = _deactivating_payload(
        "EXPIRED", expires_ms=expires_ms, app_account_token="fresh-uuid", signed_date=2000
    )
    items = [
        HistoryItem(signed_payload="stale", send_attempts=[]),
        HistoryItem(signed_payload="fresh", send_attempts=[]),
    ]
    payloads = {"stale": stale_payload, "fresh": fresh_payload}

    async def fake_lookup(token):
        if token == "stale-uuid":
            return {"subscription_expires": expires_ms / 1000.0 + 500}
        return {"subscription_expires": expires_ms / 1000.0}

    async def fake_handler(_p):  # pragma: no cover -- dry run never calls this
        raise AssertionError("handler must not be called in dry run")

    lines = []
    report = await replay(
        items, apply=False, verify=lambda sp: payloads[sp], handler=fake_handler,
        lookup=fake_lookup, emit=lines.append,
    )

    assert report.stale_skipped == 1
    assert report.dry_run == 1
    assert any("STALE" in line for line in lines)


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_is_pii_free_and_renders_iso_signed_date():
    full_original_id = "1234567890123456"
    payload = {
        "notificationType": "DID_RENEW",
        "subtype": "",
        "signedDate": 1735689600000,  # 2025-01-01T00:00:00Z
        "data": {
            "environment": "Production",
            "signedTransactionInfo": _unsigned_jws(
                {
                    "originalTransactionId": full_original_id,
                    "appAccountToken": "should-never-appear-uuid",
                }
            ),
        },
    }

    summary = summarize(payload, [{"sendAttemptResult": "NO_RESPONSE"}, {"sendAttemptResult": "SUCCESS"}])

    dumped = json.dumps(summary)
    assert full_original_id not in dumped
    assert "should-never-appear-uuid" not in dumped
    assert "@" not in dumped

    assert summary["type"] == "DID_RENEW"
    assert summary["subtype"] == ""
    assert summary["signed_date_iso"] == "2025-01-01T00:00:00Z"
    assert summary["environment"] == "Production"
    assert summary["original_transaction_suffix"] == full_original_id[-6:]
    assert summary["attempts"] == 2
    assert summary["last_result"] == "SUCCESS"


def test_summarize_handles_missing_signed_date_and_no_attempts():
    payload = {"notificationType": "TEST", "subtype": "", "data": {}}

    summary = summarize(payload, [])

    assert summary["signed_date_iso"] == ""
    assert summary["attempts"] == 0
    assert summary["last_result"] is None
    assert summary["original_transaction_suffix"] == ""


# ---------------------------------------------------------------------------
# CLI (scripts/replay_appstore_notifications.py)
# ---------------------------------------------------------------------------


def _load_cli_module():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "replay_appstore_notifications", root / "scripts" / "replay_appstore_notifications.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _forbid_fetch_and_replay(monkeypatch, mod):
    async def _unexpected_fetch(**_kwargs):
        raise AssertionError("fetch_notification_history should not be called")

    async def _unexpected_replay(*_args, **_kwargs):
        raise AssertionError("replay should not be called")

    monkeypatch.setattr(mod, "fetch_notification_history", _unexpected_fetch)
    monkeypatch.setattr(mod, "replay", _unexpected_replay)


def test_cli_rejects_days_over_180(monkeypatch):
    mod = _load_cli_module()
    _forbid_fetch_and_replay(monkeypatch, mod)

    rc = mod.main(["--days", "181", "--environment", "sandbox"])

    assert rc == 2


def test_cli_rejects_start_after_end(monkeypatch):
    mod = _load_cli_module()
    _forbid_fetch_and_replay(monkeypatch, mod)

    rc = mod.main(
        ["--start", "2026-02-01", "--end", "2026-01-01", "--environment", "sandbox"]
    )

    assert rc == 2


def test_cli_dry_run_success_path_never_touches_network(monkeypatch, capsys):
    mod = _load_cli_module()

    async def fake_fetch(*, base_url, start_ms, end_ms, only_failures):
        assert only_failures is True  # default: only failures
        assert base_url  # some URL was resolved
        return [HistoryItem(signed_payload="p1", send_attempts=[])]

    async def fake_replay(items, *, apply):
        assert apply is False  # default: dry run
        assert len(items) == 1
        return ReplayReport(fetched=1, dry_run=1, by_type={"DID_RENEW": 1})

    monkeypatch.setattr(mod, "fetch_notification_history", fake_fetch)
    monkeypatch.setattr(mod, "replay", fake_replay)

    rc = mod.main(["--days", "7", "--environment", "sandbox"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "sandbox" in out
    assert "fetched=1" in out
    assert "dry_run=1" in out


def test_cli_apply_flag_is_forwarded_to_replay(monkeypatch):
    mod = _load_cli_module()

    async def fake_fetch(**_kwargs):
        return []

    captured = {}

    async def fake_replay(items, *, apply):
        captured["apply"] = apply
        return ReplayReport(fetched=0)

    monkeypatch.setattr(mod, "fetch_notification_history", fake_fetch)
    monkeypatch.setattr(mod, "replay", fake_replay)

    rc = mod.main(["--days", "7", "--environment", "production", "--apply"])

    assert rc == 0
    assert captured["apply"] is True


def test_cli_missing_environment_exits_2(monkeypatch):
    mod = _load_cli_module()
    _forbid_fetch_and_replay(monkeypatch, mod)
    monkeypatch.delenv("APPSTORE_ENVIRONMENT", raising=False)

    rc = mod.main(["--days", "7"])

    assert rc == 2


def test_cli_days_and_start_end_are_mutually_exclusive(monkeypatch):
    mod = _load_cli_module()
    _forbid_fetch_and_replay(monkeypatch, mod)

    rc = mod.main(
        ["--days", "5", "--start", "2026-08-01", "--end", "2026-08-02", "--environment", "sandbox"]
    )

    assert rc == 2


def test_cli_start_end_window_is_inclusive_of_the_end_date(monkeypatch):
    """A single-day --start/--end window must cover the whole end day, not
    exclude it -- otherwise --end 2026-08-31 silently drops Aug 31 itself."""
    mod = _load_cli_module()

    captured = {}

    async def fake_fetch(*, base_url, start_ms, end_ms, only_failures):
        captured["start_ms"] = start_ms
        captured["end_ms"] = end_ms
        return []

    async def fake_replay(items, *, apply):
        return ReplayReport(fetched=0)

    monkeypatch.setattr(mod, "fetch_notification_history", fake_fetch)
    monkeypatch.setattr(mod, "replay", fake_replay)

    rc = mod.main(
        ["--start", "2026-08-01", "--end", "2026-08-01", "--environment", "sandbox"]
    )

    assert rc == 0
    assert captured["end_ms"] - captured["start_ms"] == 24 * 3600 * 1000


def test_cli_replay_exception_is_caught_and_reported(monkeypatch, capsys):
    mod = _load_cli_module()

    async def fake_fetch(**_kwargs):
        return [HistoryItem(signed_payload="p1", send_attempts=[])]

    async def fake_replay(items, *, apply):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "fetch_notification_history", fake_fetch)
    monkeypatch.setattr(mod, "replay", fake_replay)

    rc = mod.main(["--days", "7", "--environment", "sandbox"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "fetched=1" in out
    assert "aborted" in out
