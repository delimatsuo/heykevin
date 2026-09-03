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
import json
import os
from pathlib import Path

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import httpx
import pytest

from app.services.appstore_replay import (
    HistoryItem,
    ReplayReport,
    fetch_notification_history,
    replay,
    summarize,
)


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
            base_url="https://api.storekit.itunes.apple.com",
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
            base_url="https://api.storekit.itunes.apple.com",
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
            base_url="https://api.storekit.itunes.apple.com",
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
                base_url="https://api.storekit.itunes.apple.com",
                start_ms=1,
                end_ms=2,
                only_failures=True,
                token_factory=lambda: "tok",
                client=client,
            )

    assert "500" in str(excinfo.value)


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
