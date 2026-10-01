"""Screening summary service for live call screening push notifications.

Extracts who is calling and why from the early turns of the screening conversation,
and sends an in-place APNs notification update (`apns-collapse-id`) so the lock screen
shows e.g. "Jonathan from Geico: Wants to talk about insurance renewal — Tap to answer".
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Callable, Optional
import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)
REASON_PUBLISH_TIMEOUT_SECONDS = 1.0


def _sanitize_transcript(transcript: str, max_chars: int = 1500) -> str:
    """Keep raw transcript bounded before prompting."""
    if not transcript:
        return ""
    clean = transcript.replace("<", "[").replace(">", "]")
    return clean[:max_chars]


def _fallback_extraction(
    transcript: str,
    caller_phone: str = "",
    known_caller_name: str = "",
    user_language: str = "en",
) -> dict:
    """Fast deterministic extraction when LLM is slow or unavailable."""
    from app.services.personal_language import is_portuguese
    is_pt = is_portuguese(user_language)
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
        match = re.search(
            r"(?:this is|it's|i'm|i am|aqui é(?:\s+o|\s+a)?|sou o|sou a|é o|é a)\s+([a-zA-ZÀ-ÿ.]+(?:\s+[a-zA-ZÀ-ÿ.]+)*(?:\s+(?:from|da|do)\s+[a-zA-ZÀ-ÿ.]+)?)" ,
            first_statement,
            re.IGNORECASE,
        )
        if match and not caller_name:
            caller_name = match.group(1).strip()

        cleaned = first_statement.strip()
        if len(cleaned) > 80:
            cleaned = cleaned[:77].rstrip() + "..."
        reason = cleaned

    default_name = "Chamada em triagem" if is_pt else "Screening Call"
    default_reason = "Conversando com Kevin" if is_pt else "Speaking with Kevin"

    if not caller_name:
        caller_name = known_caller_name or (caller_phone if caller_phone else default_name)
    if not reason:
        reason = default_reason

    return {
        "caller_name": caller_name,
        "reason": reason,
    }


async def extract_screening_summary(
    transcript: str,
    caller_phone: str = "",
    known_caller_name: str = "",
    timeout_seconds: float = 3.0,
    user_language: str = "en",
) -> dict:
    """Extract who is calling and why from the early screening conversation.

    Returns dict with `caller_name` and `reason`.
    """
    from app.services.personal_language import is_portuguese
    is_pt = is_portuguese(user_language)

    cleaned_transcript = _sanitize_transcript(transcript)
    if not cleaned_transcript:
        return _fallback_extraction(transcript, caller_phone, known_caller_name, user_language=user_language)

    if not settings.anthropic_api_key:
        return _fallback_extraction(cleaned_transcript, caller_phone, known_caller_name, user_language=user_language)

    if is_pt:
        prompt = f"""A phone call is being screened live. Analyze the conversation so far and extract who is calling and what they want.
Return ONLY valid JSON with two fields:
- caller_name: string (caller name, and business/company if mentioned, e.g. "Jonathan from Geico" or "Jonathan" or "Dr. Smith's Office")
- reason: string (brief one-line summary under 60 chars in Brazilian Portuguese of why they are calling, e.g. "Quer falar sobre renovação do seguro")

Never translate personal names or callback phone digits.

Known caller ID (may be empty): {known_caller_name or caller_phone}

<transcript>
{cleaned_transcript}
</transcript>"""
        system_instruction = "Extract who is calling and why from this call transcript in Brazilian Portuguese. Return ONLY valid JSON."
        default_name = known_caller_name or caller_phone or "Chamada em triagem"
        default_reason = "Conversando com Kevin"
    else:
        prompt = f"""A phone call is being screened live. Analyze the conversation so far and extract who is calling and what they want.
Return ONLY valid JSON with two fields:
- caller_name: string (caller name, and business/company if mentioned, e.g. "Jonathan from Geico" or "Jonathan" or "Dr. Smith's Office")
- reason: string (brief one-line summary under 60 chars of why they are calling, e.g. "Wants to talk about insurance renewal")

Known caller ID (may be empty): {known_caller_name or caller_phone}

<transcript>
{cleaned_transcript}
</transcript>"""
        system_instruction = "Extract who is calling and why from this call transcript. Return ONLY valid JSON."
        default_name = known_caller_name or caller_phone or "Screening Call"
        default_reason = "Speaking with Kevin"

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
                    "system": system_instruction,
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
                    name = result.get("caller_name", "").strip() or default_name
                    reason = result.get("reason", "").strip() or default_reason
                    return {"caller_name": name, "reason": reason}
    except Exception as e:
        logger.warning(f"Screening summary extraction failed or timed out: {e}")

    return _fallback_extraction(cleaned_transcript, caller_phone, known_caller_name, user_language=user_language)


async def extract_and_send_screening_summary(
    *,
    contractor_id: str,
    call_sid: str,
    caller_phone: str = "",
    known_caller_name: str = "",
    transcript: str = "",
    collapse_id: Optional[str] = None,
    is_active: Optional[Callable[[], bool]] = None,
    ws_token: str = "",
    user_language: str = "en",
) -> bool:
    """Extract screening details and dispatch the in-place APNs notification update."""
    if not contractor_id or not call_sid:
        return False

    if is_active is not None and not is_active():
        return False

    summary = await extract_screening_summary(
        transcript=transcript,
        caller_phone=caller_phone,
        known_caller_name=known_caller_name,
        user_language=user_language,
    )

    if is_active is not None and not is_active():
        return False

    reason = summary.get("reason", "") if isinstance(summary, dict) else ""

    if ws_token:
        try:
            from app.services.owner_call_actions import publish_screening_reason
            # Optional metadata must not indefinitely delay the existing alert.
            async with asyncio.timeout(REASON_PUBLISH_TIMEOUT_SECONDS):
                published = await publish_screening_reason(
                    call_sid=call_sid,
                    contractor_id=contractor_id,
                    ws_token=ws_token,
                    reason=reason,
                )
            if not published:
                return False
        except Exception as e:
            logger.warning("Screening reason publish failed: %s", type(e).__name__)

    if is_active is not None and not is_active():
        return False

    from app.services.push_notification import send_screening_summary_push
    return await send_screening_summary_push(
        contractor_id=contractor_id,
        call_sid=call_sid,
        caller_phone=caller_phone,
        caller_name=summary.get("caller_name", "") if isinstance(summary, dict) else "",
        reason=reason,
        collapse_id=collapse_id,
        user_language=user_language,
    )
