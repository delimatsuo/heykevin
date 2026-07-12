"""Provider-neutral receptionist turn transitions shared by live shadow and replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.services.dialogue_planner import NextAction, plan_next_action
from app.services.instruction_composer import compose_turn_instructions
from app.services.receptionist_state import IntakeState


@dataclass(frozen=True)
class PlannedReceptionistTurn:
    turn_id: int
    action: NextAction
    instructions: str


@dataclass(frozen=True)
class CompletedAssistantTurn:
    turn_id: int | None
    action: NextAction | None
    instructions: str
    interrupted: bool
    committed_slots: tuple[str, ...]


class ReceptionistTurnReducer:
    """Apply explicit caller/assistant completion events to one call state."""

    def __init__(
        self,
        state: IntakeState,
        *,
        private_memory_lines: tuple[str, ...] = (),
    ):
        self.state = state
        self.private_memory_lines = private_memory_lines
        self._next_turn_id = 1
        self._pending_turn: PlannedReceptionistTurn | None = None

    @property
    def pending_turn_id(self) -> int | None:
        return self._pending_turn.turn_id if self._pending_turn else None

    @property
    def pending_action(self) -> NextAction | None:
        return self._pending_turn.action if self._pending_turn else None

    @property
    def pending_instructions(self) -> str:
        return self._pending_turn.instructions if self._pending_turn else ""

    def complete_caller_turn(self, text: str) -> PlannedReceptionistTurn | None:
        """Plan once per caller turn; later fragments amend state only."""
        self.state.observe_caller_turn(text)
        if self._pending_turn is not None:
            self._pending_turn = self._plan_turn(self._pending_turn.turn_id)
            return None

        planned = self._plan_turn(self._next_turn_id)
        self._next_turn_id += 1
        self._pending_turn = planned
        return planned

    def _plan_turn(self, turn_id: int) -> PlannedReceptionistTurn:
        action = plan_next_action(self.state)
        return PlannedReceptionistTurn(
            turn_id=turn_id,
            action=action,
            instructions=compose_turn_instructions(
                self.state,
                action,
                private_memory_lines=self.private_memory_lines,
            ),
        )

    def complete_assistant_turn(
        self,
        *,
        interrupted: bool,
        asked_slots: Iterable[str] | None = None,
    ) -> CompletedAssistantTurn:
        """Commit asked slots only after a completed assistant turn."""
        pending = self._pending_turn
        self._pending_turn = None
        if pending is None:
            return CompletedAssistantTurn(
                turn_id=None,
                action=None,
                instructions="",
                interrupted=interrupted,
                committed_slots=(),
            )

        committed_slots: tuple[str, ...] = ()
        if not interrupted:
            candidates = pending.action.allowed_slots if asked_slots is None else asked_slots
            committed_slots = tuple(
                dict.fromkeys(str(slot) for slot in candidates if str(slot))
            )
            for slot in committed_slots:
                self.state.mark_slot_asked(slot)

        return CompletedAssistantTurn(
            turn_id=pending.turn_id,
            action=pending.action,
            instructions=pending.instructions,
            interrupted=interrupted,
            committed_slots=committed_slots,
        )
