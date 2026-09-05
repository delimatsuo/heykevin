"""Screening summary service for live call screening push notifications.

Extracts who is calling and why from the early turns of the screening conversation,
and sends an in-place APNs notification update (`apns-collapse-id`) so the lock screen
shows e.g. "Jonathan from Geico: Wants to talk about insurance renewal — Tap to answer".
"""

from __future__ import annotations

import json
import re
from typing import Optional
import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _sanitize_transcript(transcript: str, max_chars: int = 1500) -> str:
    """Keep raw transcript bounded before prompting."""
    if not transcript:
        return ""
    clean = transcript.replace("<", "[").replace(">", "]")
    return clean[:max_chars]


def _fallback_extraction(transcript: str, caller_phone: str = "", known_caller_name: str = "") -> dict:
    """Fast deterministic extraction when LLM is slow or unavailable."""
    caller_name = known_caller_name.strip()
    reason = ""

    lines = [line.strip() for line in transcript.split("\n") if line.strip()]
    caller_utterances = []
    for line in lines:
        lower = line.lower()
        if lower.startswith("caller:"):
            caller_utterances.append(line[7:].strip())
        elif lower.startswith("user:"):
            caller_utterances.append(line[5:].strip())
        elif not lower.startswith("kevin:") and not lower.startswith("assistant:"):
            caller_utterances.append(line)

    if caller_utterances:
        first_statement = caller_utterances[0]
        match = re.search(r"(?:this is|it's|i'm|i am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+from\s+[A-Z][a-z]+)?)", first_statement, re.IGNORECASE)
        if match and not caller_name:
            caller_name = match.group(1)

        cleaned = first_statement.strip()
        if len(cleaned) > 80:
            cleaned = cleaned[:77].rstrip() + "..."
        reason = cleaned

    if not caller_name:
        caller_name = known_caller_name or (caller_phone if caller_phone else "Screening Call")
    if not reason:
        reason = "Speaking with Kevin"

    return {
        "caller_name": caller_name,
        "reason": reason,
    }


async def extract_screening_summary(
    transcript: str,
    caller_phone: str = "",
    known_caller_name: str = "",
    timeout_seconds: float = 3.0,
) -> dict:
    """Extract who is calling and why from the early screening conversation.

    Returns dict with `caller_name` and `reason`.
    """
    cleaned_transcript = _sanitize_transcript(transcript)
    if not cleaned_transcript:
        return _fallback_extraction(transcript, caller_phone, known_caller_name)

    if not settings.anthropic_api_key:
        return _fallback_extraction(cleaned_transcript, caller_phone, known_caller_name)

    prompt = f"""A phone call is being screened live. Analyze the conversation so far and extract who is calling and what they want.
Return ONLY valid JSON with two fields:
- caller_name: string (caller name, and business/company if mentioned, e.g. "Jonathan from Geico" or "Jonathan" or "Dr. Smith's Office")
- reason: string (brief one-line summary under 60 chars of why they are calling, e.g. "Wants to talk about insurance renewal")

Known caller ID (may be empty): {known_caller_name or caller_phone}

<transcript>
{cleaned_transcript}
</transcript>"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.anthropic_model,
                    "max_tokens": 80,
                    "thinking": {"type": "disabled"},
                    "system": "Extract who is calling and why from this call transcript. Return ONLY valid JSON.",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=timeout_seconds,
            )

            if response.status_code == 200:
                data = response.json()
                text = next(
                    (
                        block.get("text")
                        for block in data.get("content", [])
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
                    ),
                    None,
                )
                if text:
                    if "```" in text:
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                    result = json.loads(text.strip())
                    name = result.get("caller_name", "").strip() or known_caller_name or caller_phone or "Screening Call"
                    reason = result.get("reason", "").strip() or "Speaking with Kevin"
                    return {"caller_name": name, "reason": reason}
    except Exception as e:
        logger.warning(f"Screening summary extraction failed or timed out: {e}")

    return _fallback_extraction(cleaned_transcript, caller_phone, known_caller_name)


async def extract_and_send_screening_summary(
    *,
    contractor_id: str,
    call_sid: str,
    caller_phone: str = "",
    known_caller_name: str = "",
    transcript: str = "",
    collapse_id: Optional[str] = None,
) -> bool:
    """Extract screening details and dispatch the in-place APNs notification update."""
    if not contractor_id or not call_sid:
        return False

    summary = await extract_screening_summary(
        transcript=transcript,
        caller_phone=caller_phone,
        known_caller_name=known_caller_name,
    )

    from app.services.push_notification import send_screening_summary_push
    return await send_screening_summary_push(
        contractor_id=contractor_id,
        call_sid=call_sid,
        caller_phone=caller_phone,
        caller_name=summary.get("caller_name", ""),
        reason=summary.get("reason", ""),
        collapse_id=collapse_id,
    )
