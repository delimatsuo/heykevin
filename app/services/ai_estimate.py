"""AI diagnosis and cost estimation via Gemini."""

import asyncio
import base64
import json

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
FILES_API_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
FILES_API_POLL_TIMEOUT_SECONDS = 120.0
FILES_API_POLL_INTERVAL_SECONDS = 2.0


def _format_services_for_estimate(services: list) -> str:
    """Format service list for the Gemini estimate prompt."""
    if not services:
        return "No service pricing available."
    lines = []
    for s in services:
        name = s.get("name", "")
        pmin = s.get("price_min", 0)
        pmax = s.get("price_max", 0)
        if pmin == pmax:
            lines.append(f"- {name}: ${pmin}")
        else:
            lines.append(f"- {name}: ${pmin}-${pmax}")
    return "\n".join(lines)


def _build_estimate_prompt(
    business_name: str,
    services_list: list,
    text_description: str = "",
) -> str:
    formatted_services = _format_services_for_estimate(services_list)
    return f"""You are a diagnostic assistant for {business_name}.

Analyze this media from a customer describing a problem they need help with.
The caller may describe the problem out loud — use what they say as well as what is visible.
{f'The customer also described the issue as: "{text_description}"' if text_description else ''}

Based on what you see/hear, provide:
1. A likely diagnosis of the issue (2-3 sentences max)
2. Match it to the most relevant services from this price list:
{formatted_services}
3. An estimated cost range based on the matched services

IMPORTANT: If you cannot confidently identify the issue, if the media is unclear,
or if the problem doesn't match any services in the list, you MUST respond with
requires_manual_investigation: true and set diagnosis to null.

Return valid JSON only, no other text:
{{
  "diagnosis": "string or null",
  "matched_services": [{{"name": "service name", "price_min": 0, "price_max": 0}}],
  "estimate_min": 0,
  "estimate_max": 0,
  "requires_manual_investigation": false,
  "confidence": "high"
}}

confidence must be one of: "high", "medium", "low"
If confidence is "low", set requires_manual_investigation to true."""


def _parse_gemini_response(data: dict) -> dict:
    candidates = data.get("candidates", [])
    if not candidates:
        return _manual_investigation_result()

    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    result = json.loads(text.strip())

    result.setdefault("diagnosis", None)
    result.setdefault("matched_services", [])
    result.setdefault("estimate_min", None)
    result.setdefault("estimate_max", None)
    result.setdefault("requires_manual_investigation", False)
    result.setdefault("confidence", "low")

    if result["confidence"] == "low":
        result["requires_manual_investigation"] = True

    return result


async def _analyze_video(
    client: httpx.AsyncClient,
    media_bytes: bytes,
    media_type: str,
    prompt: str,
    poll_timeout_seconds: float = FILES_API_POLL_TIMEOUT_SECONDS,
    poll_interval_seconds: float = FILES_API_POLL_INTERVAL_SECONDS,
) -> dict:
    """Analyze video via Gemini Files API: resumable upload -> poll ACTIVE -> generateContent."""
    # 1. Initiate resumable upload session
    init_headers = {
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(media_bytes)),
        "X-Goog-Upload-Header-Content-Type": media_type,
        "Content-Type": "application/json",
    }
    init_body = json.dumps({"file": {"displayName": "estimate_video"}})
    init_resp = await client.post(
        f"{FILES_API_UPLOAD_URL}?key={settings.gemini_api_key}",
        headers=init_headers,
        content=init_body,
        timeout=30.0,
    )
    if init_resp.status_code != 200:
        raise RuntimeError(f"Files API start failed: {init_resp.status_code} {init_resp.text[:200]}")

    upload_url = init_resp.headers.get("x-goog-upload-url") or init_resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Files API did not return an upload URL")

    # 2. Upload file bytes
    upload_headers = {
        "Content-Length": str(len(media_bytes)),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    upload_resp = await client.post(
        upload_url,
        headers=upload_headers,
        content=media_bytes,
        timeout=60.0,
    )
    if upload_resp.status_code != 200:
        raise RuntimeError(f"Files API upload failed: {upload_resp.status_code} {upload_resp.text[:200]}")

    file_info = upload_resp.json().get("file", {})
    file_name = file_info.get("name")  # e.g. "files/abc123xyz"
    file_uri = file_info.get("uri")
    file_state = file_info.get("state", "PROCESSING")

    if not file_name:
        raise RuntimeError("Files API upload response missing file resource name")

    # 3. Poll until ACTIVE (or give up after poll_timeout_seconds)
    max_poll_attempts = max(1, int(poll_timeout_seconds / max(0.1, poll_interval_seconds)))
    if file_state != "ACTIVE":
        for _ in range(max_poll_attempts):
            poll_resp = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={settings.gemini_api_key}",
                timeout=30.0,
            )
            if poll_resp.status_code != 200:
                raise RuntimeError(f"Files API poll error: {poll_resp.status_code} {poll_resp.text[:200]}")
            poll_data = poll_resp.json()
            state = poll_data.get("state", "")
            if state == "ACTIVE":
                file_uri = poll_data.get("uri", file_uri)
                break
            if state in ("FAILED", "ERROR"):
                raise RuntimeError(f"Files API processing failed with state: {state}")
            await asyncio.sleep(poll_interval_seconds)
        else:
            raise TimeoutError(f"Files API processing timed out waiting for ACTIVE state after {poll_timeout_seconds}s")

    # 4. generateContent with file_data
    resolved_uri = file_uri or f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
    gen_body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "file_data": {
                            "mime_type": media_type,
                            "file_uri": resolved_uri,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 500,
        },
    }

    gen_resp = await client.post(
        f"{GEMINI_API_URL}?key={settings.gemini_api_key}",
        json=gen_body,
        timeout=60.0,
    )
    if gen_resp.status_code != 200:
        raise RuntimeError(f"Gemini generateContent error: {gen_resp.status_code} {gen_resp.text[:200]}")

    result = _parse_gemini_response(gen_resp.json())
    logger.info(
        f"AI video estimate: confidence={result['confidence']}, "
        f"diagnosis={str(result.get('diagnosis', ''))[:50]}"
    )
    return result


async def _analyze_image(
    client: httpx.AsyncClient,
    media_bytes: bytes,
    media_type: str,
    prompt: str,
) -> dict:
    """Analyze image via inline base64 in generateContent."""
    media_b64 = base64.b64encode(media_bytes).decode("utf-8")
    request_body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": media_type,
                            "data": media_b64,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 500,
        },
    }

    try:
        response = await client.post(
            f"{GEMINI_API_URL}?key={settings.gemini_api_key}",
            json=request_body,
            timeout=60.0,
        )
        if response.status_code != 200:
            logger.error(f"Gemini API error: {response.status_code} {response.text[:200]}")
            return _manual_investigation_result()

        result = _parse_gemini_response(response.json())
        logger.info(
            f"AI estimate: confidence={result['confidence']}, "
            f"diagnosis={str(result.get('diagnosis', ''))[:50]}"
        )
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Gemini response: {e}")
        return _manual_investigation_result()
    except httpx.TimeoutException:
        logger.error("Gemini analysis timed out")
        return _manual_investigation_result()
    except Exception as e:
        logger.error(f"AI estimate error: {e}", exc_info=True)
        return _manual_investigation_result()


async def analyze_media(
    media_bytes: bytes,
    media_type: str,
    services_list: list,
    business_name: str,
    text_description: str = "",
    poll_timeout_seconds: float = FILES_API_POLL_TIMEOUT_SECONDS,
    poll_interval_seconds: float = FILES_API_POLL_INTERVAL_SECONDS,
) -> dict:
    """Analyze uploaded media with Gemini and return diagnosis + cost estimate.

    Images use the inline_data path; videos use the Files API resumable upload path.
    Returns dict with: diagnosis, matched_services, estimate_min, estimate_max,
    requires_manual_investigation, confidence.
    """
    prompt = _build_estimate_prompt(business_name, services_list, text_description)

    async with httpx.AsyncClient() as client:
        if (media_type or "").startswith("video/"):
            return await _analyze_video(
                client,
                media_bytes=media_bytes,
                media_type=media_type,
                prompt=prompt,
                poll_timeout_seconds=poll_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        else:
            return await _analyze_image(
                client,
                media_bytes=media_bytes,
                media_type=media_type,
                prompt=prompt,
            )


def _manual_investigation_result() -> dict:
    """Return a standard 'requires manual investigation' result."""
    return {
        "diagnosis": None,
        "matched_services": [],
        "estimate_min": None,
        "estimate_max": None,
        "requires_manual_investigation": True,
        "confidence": "low",
    }
