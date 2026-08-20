"""Unit tests for AI video estimate analysis with Gemini Files API."""

import json
import os

import httpx
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.config import settings
from app.services import ai_estimate

_RealAsyncClient = httpx.AsyncClient


# 6. Video routes to Files API: resumable upload called, polled to ACTIVE, generateContent carries file_data and no inline_data.
@pytest.mark.asyncio
async def test_video_routes_to_files_api_resumable_upload_and_active_poll(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    requests_made = []
    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session123"

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request)
        url_str = str(request.url)

        # 1. Start resumable session
        if "upload/v1beta/files" in url_str and request.method == "POST" and "session123" not in url_str:
            assert request.headers.get("x-goog-upload-protocol") == "resumable"
            assert request.headers.get("x-goog-upload-command") == "start"
            return httpx.Response(
                200,
                headers={"x-goog-upload-url": upload_url},
                json={"upload_url": upload_url},
            )

        # 2. Upload video bytes
        if "session123" in url_str and request.method == "POST":
            assert request.headers.get("x-goog-upload-command") == "upload, finalize"
            return httpx.Response(
                200,
                json={
                    "file": {
                        "name": "files/video123",
                        "uri": "https://generativelanguage.googleapis.com/v1beta/files/video123",
                        "state": "PROCESSING",
                    }
                },
            )

        # 3. Poll file status
        if "v1beta/files/video123" in url_str and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "name": "files/video123",
                    "uri": "https://generativelanguage.googleapis.com/v1beta/files/video123",
                    "state": "ACTIVE",
                },
            )

        # 4. generateContent
        if "generateContent" in url_str and request.method == "POST":
            body = json.loads(request.content)
            parts = body["contents"][0]["parts"]
            # Assert file_data is present and inline_data is absent
            file_data_parts = [p for p in parts if "file_data" in p]
            inline_data_parts = [p for p in parts if "inline_data" in p]

            assert len(file_data_parts) == 1
            assert len(inline_data_parts) == 0
            assert file_data_parts[0]["file_data"]["file_uri"] == "https://generativelanguage.googleapis.com/v1beta/files/video123"
            assert file_data_parts[0]["file_data"]["mime_type"] == "video/mp4"

            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps({
                                            "diagnosis": "Water leak in supply line",
                                            "matched_services": [{"name": "Pipe Repair", "price_min": 150, "price_max": 300}],
                                            "estimate_min": 150,
                                            "estimate_max": 300,
                                            "requires_manual_investigation": False,
                                            "confidence": "high",
                                        })
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(fake_handler)
    monkeypatch.setattr(ai_estimate.httpx, "AsyncClient", lambda *a, **kw: _RealAsyncClient(transport=transport))

    result = await ai_estimate.analyze_media(
        media_bytes=b"fake-video-bytes-data",
        media_type="video/mp4",
        services_list=[{"name": "Pipe Repair", "price_min": 150, "price_max": 300}],
        business_name="Acme Plumbing",
        poll_interval_seconds=0.01,
    )

    assert result["diagnosis"] == "Water leak in supply line"
    assert result["estimate_min"] == 150
    assert result["estimate_max"] == 300
    assert result["confidence"] == "high"
    assert len(requests_made) == 4


# 7. Image still uses inline_data and never touches the Files API (regression).
@pytest.mark.asyncio
async def test_image_uses_inline_data_and_never_touches_files_api(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    requests_made = []

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request)
        url_str = str(request.url)

        # Fail if Files API is called for an image
        if "upload/v1beta/files" in url_str:
            raise AssertionError("Files API must not be called for image analysis")

        if "generateContent" in url_str and request.method == "POST":
            body = json.loads(request.content)
            parts = body["contents"][0]["parts"]
            file_data_parts = [p for p in parts if "file_data" in p]
            inline_data_parts = [p for p in parts if "inline_data" in p]

            assert len(file_data_parts) == 0
            assert len(inline_data_parts) == 1
            assert inline_data_parts[0]["inline_data"]["mime_type"] == "image/jpeg"

            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps({
                                            "diagnosis": "Damaged drywall",
                                            "matched_services": [{"name": "Drywall Patch", "price_min": 80, "price_max": 120}],
                                            "estimate_min": 80,
                                            "estimate_max": 120,
                                            "requires_manual_investigation": False,
                                            "confidence": "high",
                                        })
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(fake_handler)
    monkeypatch.setattr(ai_estimate.httpx, "AsyncClient", lambda *a, **kw: _RealAsyncClient(transport=transport))

    result = await ai_estimate.analyze_media(
        media_bytes=b"fake-image-bytes",
        media_type="image/jpeg",
        services_list=[{"name": "Drywall Patch", "price_min": 80, "price_max": 120}],
        business_name="Acme Handyman",
    )

    assert result["diagnosis"] == "Damaged drywall"
    assert len(requests_made) == 1


# 8. Stuck in PROCESSING past the timeout → raises the failure the caller maps to failed (no infinite poll; assert bounded call count).
@pytest.mark.asyncio
async def test_video_stuck_in_processing_times_out_with_bounded_polls(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")

    poll_count = 0
    upload_url = "https://generativelanguage.googleapis.com/upload/v1beta/files/session456"

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        url_str = str(request.url)

        if "upload/v1beta/files" in url_str and request.method == "POST" and "session456" not in url_str:
            return httpx.Response(200, headers={"x-goog-upload-url": upload_url}, json={})

        if "session456" in url_str and request.method == "POST":
            return httpx.Response(
                200,
                json={"file": {"name": "files/stuck_video", "state": "PROCESSING"}},
            )

        if "v1beta/files/stuck_video" in url_str and request.method == "GET":
            poll_count += 1
            return httpx.Response(
                200,
                json={"name": "files/stuck_video", "state": "PROCESSING"},
            )

        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(fake_handler)
    monkeypatch.setattr(ai_estimate.httpx, "AsyncClient", lambda *a, **kw: _RealAsyncClient(transport=transport))

    # Configure short timeout (e.g. 0.05s with 0.01s interval -> ~5 polls max)
    with pytest.raises((TimeoutError, RuntimeError)) as exc_info:
        await ai_estimate.analyze_media(
            media_bytes=b"fake-video-bytes",
            media_type="video/mp4",
            services_list=[],
            business_name="Acme",
            poll_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )

    assert "timed out" in str(exc_info.value).lower() or "timeout" in str(type(exc_info.value)).lower()
    assert 1 <= poll_count <= 10  # Assert bounded call count, never infinite
