"""Provider-neutral, receipt-driven lifecycle for one telephone conversation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time


class TurnLifecycle(str, Enum):
    LISTENING = "listening"
    GENERATING = "generating"
    PLAYING = "playing"
    AWAITING_REPLY = "awaiting_reply"
    REPROMPTING = "reprompting"
    AWAITING_PRESENCE = "awaiting_presence"
    RESOLVING_PRESENCE = "resolving_presence"
    REPLAYING_QUESTION = "replaying_question"
    OWNER_MESSAGE_PENDING = "owner_message_pending"
    CLOSE_PENDING = "close_pending"
    ENDED = "ended"


class PlaybackStatus(str, Enum):
    PLAYED = "played"
    CLEARED = "cleared"
    STALE = "stale"
    TIMEOUT = "timeout"


class CoordinatorDirective(str, Enum):
    NONE = "none"
    REPROMPT = "reprompt"
    CLOSE_FOR_SILENCE = "close_for_silence"
    HANGUP = "hangup"


@dataclass(frozen=True, slots=True)
class PlaybackReceipt:
    turn: int
    epoch: int
    phase: str
    status: PlaybackStatus
    reason: str = "receipt"


@dataclass(frozen=True, slots=True)
class CoordinatorOutcome:
    directive: CoordinatorDirective = CoordinatorDirective.NONE
    committed_slot: str = ""
    played: bool = False


@dataclass(frozen=True, slots=True)
class _PlaybackContract:
    response_turn: int
    caller_turn: int
    expects_input: bool
    asked_slot: str
    close_after_playback: bool
    kind: str


class VoiceTurnCoordinator:
    """Own audible completion, no-input timing, reprompting, and closing.

    Provider completion signals and local audio-duration estimates deliberately
    have no authority here. Only a matching Twilio response-end receipt with a
    ``played`` status can commit a question or authorize a close.
    """

    def __init__(
        self,
        *,
        no_input_seconds: float = 10.0,
        clock=time.monotonic,
    ) -> None:
        if no_input_seconds <= 0:
            raise ValueError("no_input_seconds must be positive")
        self.state = TurnLifecycle.LISTENING
        self._no_input_seconds = no_input_seconds
        self._clock = clock
        self._caller_turn = 0
        self._contract: _PlaybackContract | None = None
        self._deadline: float | None = None
        self._reprompt_count = 0
        self._presence_resolution_required = False

    @property
    def current_response_turn(self) -> int | None:
        return self._contract.response_turn if self._contract else None

    def begin_generation(self, caller_turn: int) -> bool:
        if (
            self.state != TurnLifecycle.LISTENING
            or caller_turn <= 0
            or caller_turn < self._caller_turn
            or self._presence_resolution_required
        ):
            return False
        self._caller_turn = caller_turn
        self._contract = None
        self._deadline = None
        self._reprompt_count = 0
        self._presence_resolution_required = False
        self.state = TurnLifecycle.GENERATING
        return True

    def begin_playback(
        self,
        *,
        response_turn: int,
        caller_turn: int,
        expects_input: bool,
        asked_slot: str = "",
        close_after_playback: bool = False,
        kind: str = "model",
    ) -> bool:
        if self.state == TurnLifecycle.ENDED or response_turn <= 0:
            return False
        if kind in {"model", "fallback"} and (
            self.state != TurnLifecycle.GENERATING
            or caller_turn != self._caller_turn
        ):
            return False
        if kind == "reprompt" and self.state != TurnLifecycle.REPROMPTING:
            return False
        if kind == "question_replay" and self.state != TurnLifecycle.REPLAYING_QUESTION:
            return False
        if kind == "owner_unavailable" and self.state != TurnLifecycle.OWNER_MESSAGE_PENDING:
            return False
        if kind == "silence_close" and self.state != TurnLifecycle.PLAYING:
            return False
        if expects_input and close_after_playback:
            raise ValueError("a turn cannot request input and close")
        if asked_slot and not expects_input:
            raise ValueError("asked_slot requires expects_input")
        if kind not in {
            "greeting",
            "model",
            "fallback",
            "reprompt",
            "question_replay",
            "owner_unavailable",
            "silence_close",
        }:
            raise ValueError("unknown playback kind")

        self._contract = _PlaybackContract(
            response_turn=response_turn,
            caller_turn=caller_turn,
            expects_input=expects_input,
            asked_slot=asked_slot,
            close_after_playback=close_after_playback,
            kind=kind,
        )
        self._deadline = None
        self.state = (
            TurnLifecycle.REPROMPTING
            if kind == "reprompt"
            else TurnLifecycle.PLAYING
        )
        return True

    def begin_question_replay(self, caller_turn: int) -> bool:
        """Authorize replay after the caller acknowledges a presence check."""
        if (
            self.state != TurnLifecycle.RESOLVING_PRESENCE
            or caller_turn <= 0
            or caller_turn != self._caller_turn
            or self._reprompt_count != 1
        ):
            return False
        self._caller_turn = caller_turn
        self._contract = None
        self._deadline = None
        self._presence_resolution_required = False
        self.state = TurnLifecycle.REPLAYING_QUESTION
        return True

    def begin_presence_resolution(self, caller_turn: int) -> bool:
        """Classify a reply to a played presence check without authorizing a slot."""
        if (
            self.state != TurnLifecycle.LISTENING
            or caller_turn <= 0
            or caller_turn < self._caller_turn
            or self._reprompt_count != 1
            or not self._presence_resolution_required
        ):
            return False
        self._caller_turn = caller_turn
        self._contract = None
        self._deadline = None
        self.state = TurnLifecycle.RESOLVING_PRESENCE
        return True

    def accept_presence_answer(self, caller_turn: int) -> bool:
        """Authorize normal planning only after a typed substantive presence reply."""
        if (
            self.state != TurnLifecycle.RESOLVING_PRESENCE
            or caller_turn != self._caller_turn
        ):
            return False
        self._reprompt_count = 0
        self._presence_resolution_required = False
        self.state = TurnLifecycle.GENERATING
        return True

    def begin_owner_message(self) -> bool:
        """Replace any active question with an owner-unavailable message contract."""
        if self.state in {TurnLifecycle.CLOSE_PENDING, TurnLifecycle.ENDED}:
            return False
        self._contract = None
        self._deadline = None
        self._reprompt_count = 0
        self._presence_resolution_required = False
        self.state = TurnLifecycle.OWNER_MESSAGE_PENDING
        return True

    def accepts_generated_turn(self, caller_turn: int) -> bool:
        return (
            self.state
            in {TurnLifecycle.GENERATING, TurnLifecycle.RESOLVING_PRESENCE}
            and caller_turn == self._caller_turn
        )

    def caller_activity(self) -> None:
        if self.state in {TurnLifecycle.CLOSE_PENDING, TurnLifecycle.ENDED}:
            return
        if self.state in {
            TurnLifecycle.REPROMPTING,
            TurnLifecycle.RESOLVING_PRESENCE,
        }:
            self._presence_resolution_required = True
        self._contract = None
        self._deadline = None
        self.state = TurnLifecycle.LISTENING

    def resolve_playback(
        self,
        receipt: PlaybackReceipt,
    ) -> CoordinatorOutcome:
        contract = self._contract
        if (
            contract is None
            or receipt.phase != "response_end"
            or receipt.turn != contract.response_turn
            or receipt.epoch != contract.response_turn
        ):
            return CoordinatorOutcome()

        if receipt.status != PlaybackStatus.PLAYED:
            self._contract = None
            self._deadline = None
            self._presence_resolution_required = False
            self.state = TurnLifecycle.LISTENING
            return CoordinatorOutcome()

        self._contract = None
        if contract.close_after_playback:
            self._deadline = None
            self.state = TurnLifecycle.CLOSE_PENDING
            return CoordinatorOutcome(
                directive=CoordinatorDirective.HANGUP,
                played=True,
            )

        self.state = (
            TurnLifecycle.AWAITING_PRESENCE
            if contract.kind == "reprompt"
            else TurnLifecycle.AWAITING_REPLY
        )
        self._presence_resolution_required = contract.kind == "reprompt"
        if not contract.expects_input:
            self._reprompt_count = 1
        self._deadline = self._clock() + self._no_input_seconds
        return CoordinatorOutcome(
            committed_slot=contract.asked_slot if contract.expects_input else "",
            played=True,
        )

    def delivery_failed(self, response_turn: int) -> bool:
        contract = self._contract
        if contract is None or contract.response_turn != response_turn:
            return False
        self._contract = None
        self._deadline = None
        self._presence_resolution_required = False
        self.state = TurnLifecycle.LISTENING
        return True

    def due_action(self) -> CoordinatorDirective:
        if (
            self.state
            not in {TurnLifecycle.AWAITING_REPLY, TurnLifecycle.AWAITING_PRESENCE}
            or self._deadline is None
            or self._clock() < self._deadline
        ):
            return CoordinatorDirective.NONE

        self._deadline = None
        if self.state == TurnLifecycle.AWAITING_REPLY and self._reprompt_count == 0:
            self._reprompt_count = 1
            self.state = TurnLifecycle.REPROMPTING
            return CoordinatorDirective.REPROMPT

        self.state = TurnLifecycle.PLAYING
        return CoordinatorDirective.CLOSE_FOR_SILENCE

    def mark_ended(self) -> None:
        self._contract = None
        self._deadline = None
        self._presence_resolution_required = False
        self.state = TurnLifecycle.ENDED
