"""Call lifecycle state machine with RTDB-backed atomic transitions.

States:
  PENDING → SCORING → SCREENING → [action]:
    → PICKUP_RINGING → CONNECTED → ENDED
    → CALLBACK_INITIATED → ENDED
    → TEXT_REPLIED → ENDED
    → VOICEMAIL_RECORDING → ENDED
    → IGNORED → ENDED
    → CALLER_HANGUP (from any active state)
    → ERROR_FORWARDED (from any active state)
"""

import time
from enum import Enum
from typing import Optional

from app.utils.logging import get_logger

logger = get_logger(__name__)


class CallState(str, Enum):
    PENDING = "pending"
    SCORING = "scoring"
    SCREENING = "screening"
    PICKUP_RINGING = "pickup_ringing"
    CONNECTED = "connected"
    CALLBACK_INITIATED = "callback_initiated"
    TEXT_REPLIED = "text_replied"
    VOICEMAIL_RECORDING = "voicemail_recording"
    IGNORED = "ignored"
    CALLER_HANGUP = "caller_hangup"
    ERROR_FORWARDED = "error_forwarded"
    ENDED = "ended"


# Valid state transitions: from_state → set of allowed to_states
VALID_TRANSITIONS = {
    CallState.PENDING: {CallState.SCORING, CallState.ERROR_FORWARDED},
    CallState.SCORING: {CallState.SCREENING, CallState.ERROR_FORWARDED},
    CallState.SCREENING: {
        CallState.PICKUP_RINGING,
        CallState.CALLBACK_INITIATED,
        CallState.TEXT_REPLIED,
        CallState.VOICEMAIL_RECORDING,
        CallState.IGNORED,
        CallState.CALLER_HANGUP,
        CallState.ERROR_FORWARDED,
    },
    CallState.PICKUP_RINGING: {
        CallState.CONNECTED,
        CallState.SCREENING,  # revert if user doesn't answer
        CallState.CALLER_HANGUP,
        CallState.ERROR_FORWARDED,
    },
    CallState.CONNECTED: {CallState.ENDED, CallState.CALLER_HANGUP},
    CallState.CALLBACK_INITIATED: {CallState.ENDED},
    CallState.TEXT_REPLIED: {CallState.ENDED, CallState.SCREENING},  # can continue screening after text
    CallState.VOICEMAIL_RECORDING: {CallState.ENDED, CallState.CALLER_HANGUP},
    CallState.IGNORED: {CallState.ENDED},
    CallState.CALLER_HANGUP: {CallState.ENDED},
    CallState.ERROR_FORWARDED: {CallState.ENDED},
    CallState.ENDED: set(),  # terminal
}

# States where the call is still active (caller is on the line)
ACTIVE_STATES = {
    CallState.PENDING,
    CallState.SCORING,
    CallState.SCREENING,
    CallState.PICKUP_RINGING,
    CallState.CONNECTED,
    CallState.TEXT_REPLIED,
    CallState.VOICEMAIL_RECORDING,
}


def can_transition(from_state: CallState, to_state: CallState) -> bool:
    """Check if a state transition is valid."""
    return to_state in VALID_TRANSITIONS.get(from_state, set())


def is_active(state: CallState) -> bool:
    """Check if the call is still active (someone is on the line)."""
    return state in ACTIVE_STATES


class ActiveCall:
    """Represents the state of an active call in RTDB."""

    def __init__(
        self,
        call_sid: str,
        caller_phone: str,
        state: CallState = CallState.PENDING,
        conference_name: str = "",
        conference_sid: str = "",
        vapi_call_id: str = "",
        trust_score: int = 50,
        caller_name: str = "",
        carrier: str = "",
        line_type: str = "",
        spam_score: float = 0,
        telegram_message_id: int = 0,
        transcript_buffer: str = "",
        contractor_id: str = "",
        ws_token: str = "",
    ):
        self.call_sid = call_sid
        self.caller_phone = caller_phone
        self.state = state
        self.conference_name = conference_name
        self.conference_sid = conference_sid
        self.vapi_call_id = vapi_call_id
        self.trust_score = trust_score
        self.caller_name = caller_name
        self.carrier = carrier
        self.line_type = line_type
        self.spam_score = spam_score
        self.telegram_message_id = telegram_message_id
        self.transcript_buffer = transcript_buffer
        self.contractor_id = contractor_id
        self.ws_token = ws_token
        self.state_updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "call_sid": self.call_sid,
            "caller_phone": self.caller_phone,
            "state": self.state.value,
            "conference_name": self.conference_name,
            "conference_sid": self.conference_sid,
            "vapi_call_id": self.vapi_call_id,
            "trust_score": self.trust_score,
            "caller_name": self.caller_name,
            "carrier": self.carrier,
            "line_type": self.line_type,
            "spam_score": self.spam_score,
            "telegram_message_id": self.telegram_message_id,
            "transcript_buffer": self.transcript_buffer,
            "contractor_id": self.contractor_id,
            "ws_token": self.ws_token,
            "state_updated_at": time.time(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ActiveCall":
        call = cls(
            call_sid=data.get("call_sid", ""),
            caller_phone=data.get("caller_phone", ""),
            state=CallState(data.get("state", "pending")),
            conference_name=data.get("conference_name", ""),
            conference_sid=data.get("conference_sid", ""),
            vapi_call_id=data.get("vapi_call_id", ""),
            trust_score=data.get("trust_score", 50),
            caller_name=data.get("caller_name", ""),
            carrier=data.get("carrier", ""),
            line_type=data.get("line_type", ""),
            spam_score=data.get("spam_score", 0),
            telegram_message_id=data.get("telegram_message_id", 0),
            transcript_buffer=data.get("transcript_buffer", ""),
            contractor_id=data.get("contractor_id", ""),
        )
        call.state_updated_at = data.get("state_updated_at", time.time())
        call.accepted = data.get("accepted", False)
        return call
