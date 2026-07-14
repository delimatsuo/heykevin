"""Compose compact per-turn model instructions from receptionist state."""

from __future__ import annotations

from collections.abc import Iterable
import re

from app.services.dialogue_planner import NextAction
from app.services.receptionist_state import IntakeState, ServiceAction


FORBIDDEN_SLOT_PHRASES = {
    "caller_name": "the caller's name",
    "service_action": "whether this is repair, replacement, installation, or inspection",
    "service_object": "which fixture, appliance, or object this is",
    "callback_number": "callback number",
    "callback_confirmation": "callback number confirmation",
    "service_address": "service address",
}

PRIVATE_SOURCE_PATTERN = re.compile(
    r"\b(?:Jobber|PRIVATE_SOURCE|CRM)\b(?:\s+note)?:?\s*", re.IGNORECASE
)
CALLER_ID_SENTINEL_PATTERN = re.compile(r"\bcaller-id-ending-\d{4}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"\+?\d[\d .()\-]{7,}\d")
SECRET_MARKER_PATTERN = re.compile(r"\bSENSITIVE_SENTINEL\b", re.IGNORECASE)


def compose_turn_instructions(
    state: IntakeState,
    action: NextAction,
    private_memory_lines: Iterable[str] = (),
) -> str:
    sections: list[str] = ["Current state:"]
    sections.extend(_state_lines(state))

    memory_lines = tuple(
        sanitized
        for line in private_memory_lines
        if (sanitized := sanitize_private_memory_line(line))
    )
    if memory_lines:
        sections.append("")
        sections.append("Private memory:")
        sections.extend(f"- {line}" for line in memory_lines)

    sections.append("")
    sections.append("Allowed next action:")
    sections.append(f"- {action.name.value}: {action.reason}.")
    sections.append(f"- Spoken shape: {action.max_spoken_shape}.")
    if action.allowed_slots:
        sections.append(f"- Allowed slots: {', '.join(action.allowed_slots)}.")
    else:
        sections.append("- Allowed slots: none.")

    forbidden_lines = _forbidden_lines(action.forbidden_slots)
    if forbidden_lines:
        sections.append("")
        sections.append("Do not ask:")
        sections.extend(f"- {line}." for line in forbidden_lines)

    sections.append("")
    sections.append("Speaking style:")
    sections.append("- Be brief, natural, and professional.")
    sections.append("- Ask at most one question; one question maximum.")
    sections.append("- Do not mention private memory sources.")
    sections.append("- Do not expose full phone numbers.")
    sections.append(
        "- Keep tool side effects disabled unless the allowed action explicitly permits them."
    )

    return "\n".join(sections)


def _state_lines(state: IntakeState) -> list[str]:
    lines: list[str] = []
    if state.caller_identity.name and state.caller_identity.confidence >= 0.8:
        lines.append(f"- Caller is {state.caller_identity.name}.")
    else:
        lines.append("- Caller identity is unknown.")

    if state.callback_phone_last_four:
        lines.append(f"- Callback number ending: {state.callback_phone_last_four}.")
    elif state.caller_phone_last_four:
        lines.append(f"- Caller ID ending: {state.caller_phone_last_four}.")

    if state.service_object:
        lines.append(f"- Service object: {state.service_object}.")
    else:
        lines.append("- Service object: unknown.")

    if state.service_action != ServiceAction.UNKNOWN:
        lines.append(f"- Service action: {state.service_action.value}.")
    else:
        lines.append("- Service action: unknown.")

    lines.append(f"- Intent: {state.intent.value}.")
    lines.append(f"- Callback intent: {state.callback_intent.value}.")
    lines.append(f"- Callback confirmation: {state.callback_confirmation.value}.")
    lines.append(f"- Address need: {state.address_need.value}.")
    lines.append(f"- Language: {state.language}.")
    return lines


def _forbidden_lines(forbidden_slots: tuple[str, ...]) -> list[str]:
    return [FORBIDDEN_SLOT_PHRASES.get(slot, slot.replace("_", " ")) for slot in forbidden_slots]


def sanitize_private_memory_line(line: str) -> str:
    sanitized = PRIVATE_SOURCE_PATTERN.sub("", line)
    sanitized = CALLER_ID_SENTINEL_PATTERN.sub("caller ID ending [redacted]", sanitized)
    sanitized = PHONE_PATTERN.sub("[redacted phone]", sanitized)
    sanitized = SECRET_MARKER_PATTERN.sub("[redacted secret]", sanitized)
    return " ".join(sanitized.split())
