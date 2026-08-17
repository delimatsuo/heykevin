"""Call-scoped wrapper that turns receptionist policy into live instructions."""

from __future__ import annotations

from app.services.dialogue_planner import NextAction, plan_next_action
from app.services.instruction_composer import compose_turn_instructions
from app.services.receptionist_state import CallerObservation, IntakeState

HOLD_SPEECH_PREFIX = (
    "Do not speak yet. Wait until the caller finishes talking. "
    "Then follow the allowed next action. Do not greet again."
)

_SKIP_ASKED_SLOT_MARKERS = (
    "are you still there",
    "hang up for now",
)


def credits_asked_slots(kevin_text: str) -> bool:
    lowered = kevin_text.casefold()
    return not any(marker in lowered for marker in _SKIP_ASKED_SLOT_MARKERS)


class LiveIntakeController:
    """Owns one call's IntakeState and the last planned action."""

    def __init__(self, state: IntakeState) -> None:
        self.state = state
        self.last_action: NextAction | None = None
        self._credit_kevin_turns = False

    @classmethod
    def start(
        cls,
        *,
        call_sid: str,
        caller_phone: str = "",
    ) -> "LiveIntakeController":
        return cls(IntakeState.new(call_sid=call_sid, caller_phone=caller_phone))

    @property
    def last_action_name(self) -> str:
        if self.last_action is None:
            return "none"
        return self.last_action.name.value

    def opening_instructions(self) -> str:
        return f"{HOLD_SPEECH_PREFIX}\n\n{self._compose()}"

    def after_caller_turn(
        self,
        observation: CallerObservation | None = None,
    ) -> str:
        self._credit_kevin_turns = True
        if observation is not None:
            self.state.apply_caller_observation(observation)
        return self._compose()

    def after_kevin_turn(self, kevin_text: str) -> None:
        if (
            not self._credit_kevin_turns
            or self.last_action is None
            or not credits_asked_slots(kevin_text)
        ):
            return
        for slot in self.last_action.allowed_slots:
            self.state.mark_slot_asked(slot)

    def _compose(self) -> str:
        self.last_action = plan_next_action(self.state)
        return compose_turn_instructions(self.state, self.last_action)
