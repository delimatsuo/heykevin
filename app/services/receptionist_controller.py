"""Call-scoped orchestration for shadow receptionist decisions."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.dialogue_planner import ActionName, NextAction
from app.services.receptionist_state import IntakeState
from app.services.receptionist_turns import ReceptionistTurnReducer


@dataclass(frozen=True)
class ShadowTurnDecision:
    """Non-sensitive metrics from one shadow planner decision."""

    turn_id: int
    action_name: ActionName
    known_fact_count: int
    asked_slot_count: int
    allowed_slot_count: int
    forbidden_slot_count: int
    instruction_chars: int
    tool_calls_allowed: bool


@dataclass(frozen=True)
class ShadowAssistantTurnObservation:
    """Non-sensitive metrics from one assistant completion event."""

    turn_id: int | None
    action_name: ActionName | None
    interrupted: bool
    committed_slot_count: int
    asked_slot_count: int


class ShadowReceptionistController:
    """Observe final caller turns without changing provider behavior."""

    def __init__(self, state: IntakeState):
        self.state = state
        self._turns = ReceptionistTurnReducer(state)
        self.last_action: NextAction | None = None

    @property
    def pending_turn_id(self) -> int | None:
        return self._turns.pending_turn_id

    @property
    def pending_action(self) -> NextAction | None:
        return self._turns.pending_action

    @classmethod
    def new(
        cls,
        *,
        call_sid: str,
        caller_phone: str = "",
        contractor_config: dict | None = None,
    ) -> "ShadowReceptionistController":
        config = contractor_config or {}
        caller_name = config.get("known_caller_name", "")
        if not isinstance(caller_name, str):
            caller_name = ""
        return cls(
            IntakeState.new(
                call_sid=call_sid,
                caller_phone=caller_phone,
                caller_name=caller_name,
                caller_source="known_call_context" if caller_name else "",
                caller_confidence=0.8 if caller_name else 0.0,
            )
        )

    def observe_caller_turn(self, text: str) -> ShadowTurnDecision | None:
        planned = self._turns.complete_caller_turn(text)
        if planned is None:
            self.last_action = self._turns.pending_action
            return None
        action = planned.action
        self.last_action = planned.action
        return ShadowTurnDecision(
            turn_id=planned.turn_id,
            action_name=action.name,
            known_fact_count=len(self.state.known_facts),
            asked_slot_count=len(self.state.asked_slots),
            allowed_slot_count=len(action.allowed_slots),
            forbidden_slot_count=len(action.forbidden_slots),
            instruction_chars=len(planned.instructions),
            tool_calls_allowed=action.tool_calls_allowed,
        )

    def observe_assistant_turn(
        self,
        *,
        interrupted: bool,
    ) -> ShadowAssistantTurnObservation:
        completed = self._turns.complete_assistant_turn(interrupted=interrupted)
        return ShadowAssistantTurnObservation(
            turn_id=completed.turn_id,
            action_name=completed.action.name if completed.action else None,
            interrupted=completed.interrupted,
            committed_slot_count=len(completed.committed_slots),
            asked_slot_count=len(self.state.asked_slots),
        )
