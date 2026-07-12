"""Call-scoped orchestration for shadow receptionist decisions."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.dialogue_planner import ActionName, NextAction, plan_next_action
from app.services.instruction_composer import compose_turn_instructions
from app.services.receptionist_state import IntakeState


@dataclass(frozen=True)
class ShadowTurnDecision:
    """Non-sensitive metrics from one shadow planner decision."""

    action_name: ActionName
    known_fact_count: int
    asked_slot_count: int
    allowed_slot_count: int
    forbidden_slot_count: int
    instruction_chars: int
    tool_calls_allowed: bool


class ShadowReceptionistController:
    """Observe final caller turns without changing provider behavior."""

    def __init__(self, state: IntakeState):
        self.state = state
        self.last_action: NextAction | None = None

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

    def observe_caller_turn(self, text: str) -> ShadowTurnDecision:
        self.state.observe_caller_turn(text)
        action = plan_next_action(self.state)
        instructions = compose_turn_instructions(self.state, action)
        self.last_action = action
        return ShadowTurnDecision(
            action_name=action.name,
            known_fact_count=len(self.state.known_facts),
            asked_slot_count=len(self.state.asked_slots),
            allowed_slot_count=len(action.allowed_slots),
            forbidden_slot_count=len(action.forbidden_slots),
            instruction_chars=len(instructions),
            tool_calls_allowed=action.tool_calls_allowed,
        )
