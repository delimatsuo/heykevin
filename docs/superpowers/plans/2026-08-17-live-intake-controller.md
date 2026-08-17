# Live Gemini Intake Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** On business Gemini Live calls, inject the existing offline receptionist
planner after greeting and after each caller turn so Kevin asks the job
(`service_action`, then `service_object`) before callback or address, instead of
collecting a form.

**Architecture:** Keep `IntakeState`, `DialoguePlanner`, and
`InstructionComposer` pure. Add a small `LiveIntakeController` that owns
call-scoped state, marks `asked_slots` from Kevin's spoken turn, and returns
instruction text. `GeminiPipeline` (business mode only) sends that text through
the existing `_send_client_instruction` path. Fail closed. Do not parse
transcripts into `CallerObservation` in this slice.

**Tech Stack:** Python 3.12, existing receptionist modules, Gemini Live
WebSocket client instructions, pytest.

## Global Constraints

- Do not deploy `kevin-api` without owner authorization.
- Do not merge PR #165 or edit `.worktrees/customer-memory`.
- Do not add phrase tables or transcript parsers to `IntakeState`,
  `dialogue_planner.py`, or `instruction_composer.py`.
- Do not import receptionist modules from `VoicePipeline`.
- Do not enable controller in personal mode.
- Do not inject intake instructions while `_waiting_for_owner_availability`.
- Do not credit `asked_slots` for silence or hang-up scripts.
- Do not log transcript, phone, or caller name from the controller.
- Tests use synthetic names and `caller-id-ending-8667` / `CA_test` only.
- `PublicDemoGeminiPipeline` inherits this behavior; do not add a demo fork.

---

## File Structure

- Create `app/services/live_intake_controller.py`: call-scoped wrapper around
  `IntakeState.new`, `plan_next_action`, and `compose_turn_instructions`. Owns
  `last_action`, opening hold-speech prefix, asked-slot crediting, and the
  silence/hang-up skip list.
- Create `tests/unit/test_live_intake_controller.py`: plumber-schedule sequence,
  hold-speech, observation hook (injected, not live-extracted), silence skip.
- Modify `app/services/gemini_pipeline.py`: construct controller for business
  calls with a `call_sid`; send opening instructions after greeting; refresh
  after caller flush; credit asked slots after Kevin flush; fail closed.
- Modify `tests/unit/test_receptionist_intelligence.py`: replace the
  not-live-wired guard with business-on / personal-off / VoicePipeline-isolated
  tests and a plumber-schedule instruction payload test.

---

### Task 1: Add LiveIntakeController

**Files:**
- Create: `app/services/live_intake_controller.py`
- Create: `tests/unit/test_live_intake_controller.py`

**Interfaces:**
- Consumes: `IntakeState.new`, `IntakeState.apply_caller_observation`,
  `IntakeState.mark_slot_asked`, `plan_next_action(state: IntakeState) -> NextAction`,
  `compose_turn_instructions(state: IntakeState, action: NextAction, private_memory_lines: Iterable[str] = ()) -> str`,
  `CallerObservation`
- Produces: `HOLD_SPEECH_PREFIX: str`,
  `credits_asked_slots(kevin_text: str) -> bool`,
  `LiveIntakeController.start(*, call_sid: str, caller_phone: str = "") -> LiveIntakeController`,
  `LiveIntakeController.last_action_name: str`,
  `LiveIntakeController.opening_instructions(self) -> str`,
  `LiveIntakeController.after_caller_turn(self, observation: CallerObservation | None = None) -> str`,
  `LiveIntakeController.after_kevin_turn(self, kevin_text: str) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_live_intake_controller.py`:

```python
"""Live intake controller sequences job questions before callback."""

from app.services.dialogue_planner import ActionName
from app.services.live_intake_controller import (
    HOLD_SPEECH_PREFIX,
    LiveIntakeController,
    credits_asked_slots,
)
from app.services.receptionist_state import (
    CallerObservation,
    Intent,
    ServiceAction,
)


def test_opening_instructions_hold_speech_and_ask_service_action():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )

    text = controller.opening_instructions()

    assert text.startswith(HOLD_SPEECH_PREFIX)
    assert "ask_one_clarifying_question" in text
    assert "service_action" in text
    assert "callback number" in text
    assert "service address" in text
    assert controller.last_action_name == ActionName.ASK_ONE_CLARIFYING_QUESTION.value
    assert controller.last_action is not None
    assert controller.last_action.allowed_slots == ("service_action",)


def test_schedule_request_asks_job_before_callback_without_extraction():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    controller.opening_instructions()
    first = controller.after_caller_turn()
    controller.after_kevin_turn(
        "Is this a repair, replacement, installation, or inspection?"
    )
    second = controller.after_caller_turn()

    assert "Allowed slots: service_action." in first
    assert "Allowed slots: service_object." in second
    assert "callback_preference" not in second
    assert "service address" in second


def test_known_toilet_replacement_does_not_reask_action_or_object():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    text = controller.after_caller_turn(
        CallerObservation(
            intent=Intent.PRICING_QUESTION,
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )

    assert "Do not ask:" in text
    assert "whether this is repair, replacement, installation, or inspection" in text
    assert "which fixture, appliance, or object this is" in text
    assert controller.last_action is not None
    assert "service_action" not in controller.last_action.allowed_slots
    assert "service_object" not in controller.last_action.allowed_slots


def test_silence_and_hangup_scripts_do_not_credit_asked_slots():
    assert credits_asked_slots("Are you still there?") is False
    assert credits_asked_slots(
        "I'm going to hang up for now. Please call back when you're ready. Goodbye."
    ) is False
    assert credits_asked_slots(
        "Is this a repair, replacement, installation, or inspection?"
    ) is True

    controller = LiveIntakeController.start(call_sid="CA_test")
    controller.after_caller_turn()
    controller.after_kevin_turn("Are you still there?")
    text = controller.after_caller_turn()

    assert "Allowed slots: service_action." in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_live_intake_controller.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.live_intake_controller'`.

- [ ] **Step 3: Write the minimal controller**

Create `app/services/live_intake_controller.py`:

```python
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
        if observation is not None:
            self.state.apply_caller_observation(observation)
        return self._compose()

    def after_kevin_turn(self, kevin_text: str) -> None:
        if self.last_action is None or not credits_asked_slots(kevin_text):
            return
        for slot in self.last_action.allowed_slots:
            self.state.mark_slot_asked(slot)

    def _compose(self) -> str:
        self.last_action = plan_next_action(self.state)
        return compose_turn_instructions(self.state, self.last_action)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_live_intake_controller.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/live_intake_controller.py tests/unit/test_live_intake_controller.py
git commit -m "$(cat <<'EOF'
Add live intake controller for job-before-callback sequencing.

EOF
)"
```

---

### Task 2: Prove Gemini business wiring and isolation

**Files:**
- Modify: `tests/unit/test_receptionist_intelligence.py:2888-2904`
- Modify: `app/services/gemini_pipeline.py` (implementation in Task 3; this task
  writes the failing tests)

**Interfaces:**
- Consumes: `LiveIntakeController.start`, `HOLD_SPEECH_PREFIX`,
  `GeminiPipeline._send_greeting`, `GeminiPipeline._flush_caller_transcript`,
  `GeminiPipeline._flush_kevin_transcript`, `GeminiPipeline._send_client_instruction`
- Produces: `GeminiPipeline._live_intake: LiveIntakeController | None`,
  `GeminiPipeline._send_opening_intake_instructions`,
  `GeminiPipeline._refresh_live_intake_after_caller`,
  `GeminiPipeline._credit_live_intake_after_kevin`

- [ ] **Step 1: Replace the not-live-wired guard and add wiring tests**

In `tests/unit/test_receptionist_intelligence.py`, delete
`test_stateful_receptionist_controller_is_not_live_wired_in_this_slice` and
append:

```python
def _personal_config() -> dict:
    return {
        "owner_name": "Deli Matsuo",
        "mode": "personal",
        "effective_mode": "personal",
    }


def _last_instruction_text(pipeline: GeminiPipeline) -> str:
    payload = pipeline._ws.sent_payloads[-1]
    if "client_content" in payload:
        return payload["client_content"]["turns"][0]["parts"][0]["text"]
    return payload["realtime_input"]["text"]


async def _noop_audio(_chunk: bytes):
    return None


async def _noop_transcript(_speaker: str, _text: str):
    return None


def test_gemini_business_pipeline_starts_live_intake_controller():
    pipeline = GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
        caller_phone="caller-id-ending-8667",
    )

    assert pipeline._live_intake is not None
    assert pipeline._live_intake.state.call_sid == "CA_test"


def test_gemini_personal_pipeline_does_not_start_live_intake_controller():
    pipeline = GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid="CA_test",
        contractor_config=_personal_config(),
    )

    assert pipeline._live_intake is None


def test_voice_pipeline_does_not_import_live_intake_or_receptionist_policy():
    import app.services.voice_pipeline as voice_pipeline

    source = inspect.getsource(voice_pipeline)
    assert "live_intake_controller" not in source
    assert "receptionist_state" not in source
    assert "dialogue_planner" not in source
    assert "instruction_composer" not in source
    assert not hasattr(voice_pipeline.VoicePipeline, "_live_intake")


@pytest.mark.asyncio
async def test_gemini_sends_hold_speech_intake_after_greeting(monkeypatch):
    sent: list[str] = []
    pipeline = GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
        caller_phone="caller-id-ending-8667",
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket()

    async def fake_send(text: str):
        sent.append(text)

    monkeypatch.setattr(pipeline, "_send_client_instruction", fake_send)
    await pipeline._send_greeting()
    await pipeline._send_opening_intake_instructions()

    assert sent[0].startswith("Say exactly this greeting and nothing else:")
    assert sent[1].startswith(
        "Do not speak yet. Wait until the caller finishes talking."
    )
    assert "Allowed slots: service_action." in sent[1]


@pytest.mark.asyncio
async def test_gemini_schedule_turn_asks_object_after_action_question():
    pipeline = GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
        caller_phone="caller-id-ending-8667",
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket()
    await pipeline._send_opening_intake_instructions()
    pipeline._caller_transcript_buf = ["Hi, I need to schedule an appointment."]
    await pipeline._flush_caller_transcript()
    pipeline._kevin_transcript_buf = [
        "Is this a repair, replacement, installation, or inspection?"
    ]
    await pipeline._flush_kevin_transcript()
    payloads_before_second = len(pipeline._ws.sent_payloads)
    pipeline._caller_transcript_buf = ["Replacement."]
    await pipeline._flush_caller_transcript()

    assert len(pipeline._ws.sent_payloads) > payloads_before_second
    text = _last_instruction_text(pipeline)
    assert "Allowed slots: service_object." in text
    assert "callback_preference" not in text


@pytest.mark.asyncio
async def test_live_intake_errors_fail_closed(monkeypatch):
    pipeline = GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket()

    def boom(_observation=None) -> str:
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(pipeline._live_intake, "after_caller_turn", boom)
    pipeline._caller_transcript_buf = ["Hello"]
    await pipeline._flush_caller_transcript()

    assert pipeline._connected is True
    assert pipeline._caller_turn_number == 1
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_receptionist_intelligence.py::test_gemini_business_pipeline_starts_live_intake_controller tests/unit/test_receptionist_intelligence.py::test_gemini_personal_pipeline_does_not_start_live_intake_controller tests/unit/test_receptionist_intelligence.py::test_voice_pipeline_does_not_import_live_intake_or_receptionist_policy tests/unit/test_receptionist_intelligence.py::test_gemini_sends_hold_speech_intake_after_greeting tests/unit/test_receptionist_intelligence.py::test_gemini_schedule_turn_asks_object_after_action_question tests/unit/test_receptionist_intelligence.py::test_live_intake_errors_fail_closed tests/unit/test_receptionist_intelligence.py::test_stateful_receptionist_controller_is_not_live_wired_in_this_slice -q
```

Expected: FAIL because `_live_intake` is missing and the old isolation test is
gone (`not found` is acceptable for the old name). The new tests must fail on
`AttributeError: 'GeminiPipeline' object has no attribute '_live_intake'` or
missing `_send_opening_intake_instructions`, not on import errors from Task 1.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/unit/test_receptionist_intelligence.py
git commit -m "$(cat <<'EOF'
Test Gemini business intake wiring and ElevenLabs isolation.

EOF
)"
```

Do not leave `test_stateful_receptionist_controller_is_not_live_wired_in_this_slice`
in the file after this task.

---

### Task 3: Wire GeminiPipeline

**Files:**
- Modify: `app/services/gemini_pipeline.py:125-243` (`__init__` after
  `_system_prompt` is built)
- Modify: `app/services/gemini_pipeline.py:473-478` (after `_send_greeting`)
- Modify: `app/services/gemini_pipeline.py:1266-1294` (end of
  `_flush_caller_transcript`)
- Modify: `app/services/gemini_pipeline.py:1296-1329` (end of
  `_flush_kevin_transcript`)
- Add helpers near `_send_client_instruction` at
  `app/services/gemini_pipeline.py:1643`

**Interfaces:**
- Consumes: `LiveIntakeController.start`, `opening_instructions`,
  `after_caller_turn`, `after_kevin_turn`, `last_action_name`,
  `effective_mode` (already used in `__init__`)
- Produces: the four pipeline methods named in Task 2

- [ ] **Step 1: Construct the controller in `__init__`**

After `self._system_prompt = build_system_prompt(...)` in
`GeminiPipeline.__init__`, add:

```python
        self._live_intake = None
        if mode != "personal" and call_sid:
            from app.services.live_intake_controller import LiveIntakeController

            self._live_intake = LiveIntakeController.start(
                call_sid=call_sid,
                caller_phone=caller_phone,
            )
```

Keep the import inside the branch so personal-mode construction does not load
the controller unless needed. `inspect.getsource(gemini_pipeline)` may still
contain the string `live_intake_controller`; that is required.

- [ ] **Step 2: Add fail-closed send helpers**

Place these methods next to `_send_client_instruction`:

```python
    def _intake_injection_allowed(self) -> bool:
        return (
            self._live_intake is not None
            and not self._waiting_for_owner_availability
        )

    async def _send_opening_intake_instructions(self) -> None:
        if not self._intake_injection_allowed():
            return
        try:
            text = self._live_intake.opening_instructions()
        except Exception as error:
            self._log_voice_timing(
                "intake_instruction_error",
                exception_type=type(error).__name__,
            )
            return
        await self._send_live_intake_text(text)

    async def _refresh_live_intake_after_caller(self) -> None:
        if not self._intake_injection_allowed():
            return
        try:
            text = self._live_intake.after_caller_turn()
        except Exception as error:
            self._log_voice_timing(
                "intake_instruction_error",
                exception_type=type(error).__name__,
            )
            return
        await self._send_live_intake_text(text)

    def _credit_live_intake_after_kevin(self, kevin_text: str) -> None:
        if self._live_intake is None:
            return
        try:
            self._live_intake.after_kevin_turn(kevin_text)
        except Exception as error:
            self._log_voice_timing(
                "intake_credit_error",
                exception_type=type(error).__name__,
            )

    async def _send_live_intake_text(self, text: str) -> None:
        await self._send_client_instruction(text)
        self._log_voice_timing(
            "intake_instruction",
            action=(
                self._live_intake.last_action_name
                if self._live_intake is not None
                else "none"
            ),
        )
```

- [ ] **Step 3: Call the helpers from greeting and transcript flushes**

In `start()`, after `await self._send_greeting()` and before
`self._audio_input_ready.set()`:

```python
            await self._send_opening_intake_instructions()
```

At the end of `_flush_caller_transcript`, after `_mark_caller_activity()` and
the urgency block, add:

```python
        await self._refresh_live_intake_after_caller()
```

At the end of `_flush_kevin_transcript`, after the owner-availability hold
block and before the goodbye `return`, add:

```python
        self._credit_live_intake_after_kevin(full_text)
```

- [ ] **Step 4: Run the wiring tests**

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_live_intake_controller.py tests/unit/test_receptionist_intelligence.py::test_gemini_business_pipeline_starts_live_intake_controller tests/unit/test_receptionist_intelligence.py::test_gemini_personal_pipeline_does_not_start_live_intake_controller tests/unit/test_receptionist_intelligence.py::test_voice_pipeline_does_not_import_live_intake_or_receptionist_policy tests/unit/test_receptionist_intelligence.py::test_gemini_sends_hold_speech_intake_after_greeting tests/unit/test_receptionist_intelligence.py::test_gemini_schedule_turn_asks_object_after_action_question tests/unit/test_receptionist_intelligence.py::test_live_intake_errors_fail_closed tests/unit/test_receptionist_intelligence.py::test_gemini_transcript_flush_records_response_timing -q
```

Expected: PASS. If
`test_gemini_schedule_turn_asks_object_after_action_question` cannot see
`service_object` because `_send_client_instruction` is used and the fake
websocket stores JSON, assert on
`pipeline._ws.sent_payloads` client_content / realtime_input text as the
pipeline actually encodes it (`_build_text_instruction_payload`). On the
default test model that is:

```python
{
    "client_content": {
        "turns": [{"role": "user", "parts": [{"text": text}]}],
        "turn_complete": True,
    }
}
```

If the assertion needs a helper, extract the last sent instruction text with:

```python
def _last_instruction_text(pipeline: GeminiPipeline) -> str:
    payload = pipeline._ws.sent_payloads[-1]
    if "client_content" in payload:
        return payload["client_content"]["turns"][0]["parts"][0]["text"]
    return payload["realtime_input"]["text"]
```

Put that helper in the test file, not in production code.

- [ ] **Step 5: Commit**

```bash
git add app/services/gemini_pipeline.py tests/unit/test_receptionist_intelligence.py
git commit -m "$(cat <<'EOF'
Wire business Gemini calls to the live intake controller.

EOF
)"
```

---

### Task 4: Guard owner-hold and verify the slice

**Files:**
- Modify: `tests/unit/test_receptionist_intelligence.py` (add owner-hold test)
- Modify: `app/services/gemini_pipeline.py` only if `_intake_injection_allowed`
  was omitted in Task 3

**Interfaces:**
- Consumes: `GeminiPipeline._waiting_for_owner_availability`,
  `_intake_injection_allowed`
- Produces: no new types

- [ ] **Step 1: Write the owner-hold skip test**

```python
@pytest.mark.asyncio
async def test_live_intake_does_not_inject_during_owner_availability_hold():
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket()
    pipeline._waiting_for_owner_availability = True
    sent_before = len(pipeline._ws.sent_payloads)
    pipeline._caller_transcript_buf = ["Please get Deli now."]
    await pipeline._flush_caller_transcript()

    assert len(pipeline._ws.sent_payloads) == sent_before
```

- [ ] **Step 2: Run it and confirm it fails or passes**

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_receptionist_intelligence.py::test_live_intake_does_not_inject_during_owner_availability_hold -q
```

Expected: PASS if Task 3 included `_intake_injection_allowed`. FAIL if caller
flush still sends intake instructions during hold; then keep the Task 3 guard
and re-run.

- [ ] **Step 3: Run the focused receptionist suite**

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_live_intake_controller.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_state.py tests/unit/test_receptionist_intelligence.py -q
```

Expected: PASS. `test_stateful_receptionist_controller_is_not_live_wired_in_this_slice`
must not exist.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_receptionist_intelligence.py app/services/gemini_pipeline.py
git commit -m "$(cat <<'EOF'
Skip live intake injection during owner-availability hold.

EOF
)"
```

If `gemini_pipeline.py` is unchanged in this task, commit the test only.

---

## Verification

Run:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin"
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 .venv/bin/python -m pytest tests/unit/test_live_intake_controller.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_state.py tests/unit/test_receptionist_intelligence.py tests/unit/test_phase0_voice_tool_gates.py tests/unit/test_appointment_requests.py -q
```

Expected: PASS.

Live proof is a staging plumber call that says "I need to schedule an
appointment" and hears a job question before name/address/callback. Unit tests
do not authorize production deploy.

---

## Spec coverage

| Requirement | Task |
| --- | --- |
| Ask job details before callback/address on a schedule request | Task 1 plumber sequence, Task 3 Gemini flush |
| Hold speech so opening instructions do not talk over the greeting | Task 1 `HOLD_SPEECH_PREFIX`, Task 3 after `_send_greeting` |
| Do not re-ask known action/object when an observation is supplied | Task 1 observation test (hook only; no live extractor) |
| Personal mode unchanged | Task 2 personal constructor test |
| ElevenLabs `VoicePipeline` unwired | Task 2 source isolation test |
| Fail closed | Task 2/3 error test |
| Owner-hold instructions still win | Task 4 |
| No phrase tables in `IntakeState` | File structure; extractor out of scope |
| No PR #165 / no caller SMS / no auto-book | Global constraints |

## Placeholder scan

No TBD, "implement later", or "similar to Task N" steps. Observation extraction
is intentionally absent; tests that need a filled `CallerObservation` construct
one directly.

## Type consistency

`LiveIntakeController.start`, `opening_instructions`, `after_caller_turn`,
`after_kevin_turn`, `last_action_name`, `_live_intake`,
`_send_opening_intake_instructions`, `_refresh_live_intake_after_caller`, and
`_credit_live_intake_after_kevin` are the only names later tasks may use.
