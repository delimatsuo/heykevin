"""AI diagnosis and cost estimation via Gemini."""

import base64
import json

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


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


async def analyze_media(
    media_bytes: bytes,
    media_type: str,
    services_list: list,
    business_name: str,
    text_description: str = "",
) -> dict:
    """Analyze uploaded media with Gemini and return diagnosis + cost estimate.

    Returns dict with: diagnosis, matched_services, estimate_min, estimate_max,
    requires_manual_investigation, confidence.
    """
    formatted_services = _format_services_for_estimate(services_list)

    prompt = f"""You are a diagnostic assistant for {business_name}.

Analyze this media from a customer describing a problem they need help with.
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

    # Build Gemini request
    parts = []

    # Add media
    media_b64 = base64.b64encode(media_bytes).decode("utf-8")
    parts.append({
        "inline_data": {
            "mime_type": media_type,
            "data": media_b64,
        }
    })

    # Add text prompt
    parts.append({"text": prompt})

    request_body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 500,
        },
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GEMINI_API_URL}?key={settings.gemini_api_key}",
                json=request_body,
                timeout=60.0,
            )

            if response.status_code != 200:
                logger.error(f"Gemini API error: {response.status_code} {response.text[:200]}")
                return _manual_investigation_result()

            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return _manual_investigation_result()

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")

            # Parse JSON from response (handle markdown code blocks)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text.strip())

            # Ensure required fields
            result.setdefault("diagnosis", None)
            result.setdefault("matched_services", [])
            result.setdefault("estimate_min", None)
            result.setdefault("estimate_max", None)
            result.setdefault("requires_manual_investigation", False)
            result.setdefault("confidence", "low")

            # Force manual investigation if low confidence
            if result["confidence"] == "low":
                result["requires_manual_investigation"] = True

            logger.info(f"AI estimate: confidence={result['confidence']}, "
                        f"diagnosis={str(result.get('diagnosis', ''))[:50]}")
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
