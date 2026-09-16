"""Owner Take-a-Message conversational transition and timing policy."""
from __future__ import annotations

import time
from typing import Optional

from app.config import settings

TRANSITION_GRACE_SECONDS = 3.0


def is_owner_availability_hold(text: str) -> bool:
    """Return True when Kevin has told the caller he is checking/trying owner availability."""
    if not isinstance(text, str):
        return False
    normalized = f" {text.lower()} "
    if "not available" in normalized or "unavailable" in normalized:
        return False

    hold_markers = (
        "let me see if",
        "let me see",
        "let me check if",
        "let me check",
        "i'm going to try",
        "i will try",
        "i'll try",
        "let me try",
        "one moment",
        "please hold",
        "hold on",
    )
    owner_markers = (
        "available",
        "availability",
        "reach",
        "get ahold",
        "get a hold",
        "connect you",
        "transfer you",
        "try",
    )
    return any(marker in normalized for marker in hold_markers) and any(
        marker in normalized for marker in owner_markers
    )


# Backward-compatible alias
is_owner_availability_hold_text = is_owner_availability_hold


def should_apply_grace(action: str, hold_offered: bool) -> bool:
    """The 3s grace applies once for explicit owner decline when a hold offer was begun/completed."""
    return action == "decline" and bool(hold_offered)


def compute_grace_delay(
    action: str = "decline",
    hold_offered: bool = False,
    intent_accepted_at: Optional[float] = None,
    speaking_finished_at: Optional[float] = None,
    now: Optional[float] = None,
    **kwargs,
) -> float:
    """Calculate remaining grace delay in seconds using a monotonic clock.

    The 3s pause is anchored at the LATER of:
    - when the owner intent was first accepted for transition
    - when current speech/playout finishes

    Timeout actions (30s) always receive 0.0s grace delay.
    """
    if not should_apply_grace(action, hold_offered):
        return 0.0
    now = time.monotonic() if now is None else now
    accepted_at = intent_accepted_at if intent_accepted_at is not None and intent_accepted_at > 0 else now
    speaking_at = speaking_finished_at if speaking_finished_at is not None and speaking_finished_at > 0 else 0.0
    anchor = max(accepted_at, speaking_at)
    elapsed = max(0.0, now - anchor)
    return max(0.0, TRANSITION_GRACE_SECONDS - elapsed)


def build_unavailable_speech_text(
    owner_name: str = "",
    hold_offered: bool = False,
) -> str:
    """Spoken unavailability text for VoicePipeline."""
    owner = owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else settings.user_name
    if hold_offered:
        return f"Unfortunately, {owner} is not available. Can I take a message?"
    return f"I'm sorry, {owner} is not available right now. You can leave me a message and I'll make sure they get it."


def build_gemini_instruction_text(
    owner_name: str = "",
    hold_offered: bool = False,
    language: Optional[str] = "en",
) -> str:
    """Model instruction for Gemini Live on owner decline/message-taking."""
    owner = owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else settings.user_name
    lang_note = f" Respond in the caller's language ({language})." if language and language != "en" else ""
    if hold_offered:
        return (
            f"The owner ({owner}) is unavailable. Say unfortunately {owner} is not available and ask if you can take a message.{lang_note} "
            f"Do not offer another availability check or put the caller on hold."
        )
    return (
        f"The owner ({owner}) is unavailable. Tell the caller {owner} is not available and offer to take a message.{lang_note} "
        f"Do not offer to check availability or put the caller on hold."
    )


def build_relay_instruction_text(
    owner_name: str = "",
    hold_offered: bool = False,
) -> str:
    """System instruction for ConversationRelay on owner decline/message-taking."""
    owner = owner_name.strip() if isinstance(owner_name, str) and owner_name.strip() else settings.user_name
    if hold_offered:
        return (
            f"SYSTEM INSTRUCTION: {owner} is unavailable. Tell the caller unfortunately {owner} is not available "
            f"and ask if you can take a message. Do not offer another availability check."
        )
    return (
        f"SYSTEM INSTRUCTION: {owner} is unavailable. Tell the caller and offer to take a message. "
        f"Do not offer to check availability."
    )
