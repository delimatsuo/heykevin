"""Deterministic test suite for Owner Take-a-Message shared policy and common lifecycle.

Verifies:
1. Shared policy & timing calculations:
   - Hold phrase detection without broad 'not in' exclusion
   - Monotonic 3s grace anchor calculations:
     * old hold ended 5s ago + tap now -> 3s (oldholdnewtap3)
     * tapped mid-hold + later finish -> 3s from finish (midholdlaterfinish3)
     * no hold offered -> 0s (nohold0)
     * 30s timeout action -> 0s (timeout0)
   - Text builders for speech, Gemini instructions (preserving caller language), and Relay instructions.

2. Common delivery lifecycle and durable contract (using realistic RTDB schema & Store fixture):
   - ACK failure executes delivery effect once and does not replay (ACKfailure oneeffect)
   - Delivery failure retains intent and allows retry (deliveryfailure retry)
   - Concurrent polling on the same pipeline is serialized (concurrentpoll)
   - Identity changes (changed OR cleared contractor_id, call_sid, ws_token) during prepare and freshread result in no effects and no ACK (changedORclearedcid/sid/ws duringprepare AND freshread noeffects/noack)
   - Changed claim, terminal state, or deletion in RTDB aborts delivery (changedclaim/terminal/delete)
   - Cancelled prepare triggers cleanup hook and resets pending (cancelpreparecleanup)
   - prepare returning False triggers cleanup hook and resets pending without delivery (prepareFalsecleanup)
   - Internal TypeError inside deliver() is called once without retry (internalTypeError calledonce)
   - Successful delivery and ACK resets pending to False (successpendingfalse)
   - Delivery callback can invoke pipeline guard after its own await and reject a changed owner (callback cancallguardafteritsownawait and rejectchangedowner)
"""
import asyncio
from copy import deepcopy
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

for key in (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "TELEGRAM_BOT_TOKEN",
    "USER_PHONE",
):
    os.environ.setdefault(key, "test-placeholder")

from app.config import settings
from app.services.message_taking import (
    TRANSITION_GRACE_SECONDS,
    build_gemini_instruction_text,
    build_relay_instruction_text,
    build_unavailable_speech_text,
    compute_grace_delay,
    is_owner_availability_hold,
    is_owner_availability_hold_text,
    should_apply_grace,
)
from app.services import owner_call_actions as actions


# --- 1. Shared Policy & Timing Calculations -----------------------------------

class TestMessageTakingSharedPolicy:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Let me see if Deli is available, one moment.", True),
            ("One moment please, let me check availability.", False),
            ("Let me check if he's available.", True),
            ("I'm going to try to reach Deli for you.", True),
            ("Hold on, let me try to connect you.", True),
            ("Please hold, let me see if he's available.", True),
            ("Let me see if he's available, he might not be in his office.", True),
            # Explicit unavailability announcements MUST return False
            ("Unfortunately, Deli is not available. Can I take a message?", False),
            ("I'm sorry, Deli is not available right now. You can leave me a message.", False),
            ("He is unavailable today.", False),
            # Generic availability without established owner offer
            ("Let me check what appointment times are available", False),
            ("Let me check which parts are available", False),
            ("Let me see which appointment times are available", False),
            ("Let me check availability.", False),
            # General conversation
            ("How can I help you today?", False),
            ("Could you please spell your last name?", False),
            ("", False),
            (None, False),
        ],
    )
    def test_hold_text_detection(self, text, expected):
        assert is_owner_availability_hold(text) is expected
        assert is_owner_availability_hold_text(text) is expected

    @pytest.mark.parametrize(
        "action,hold_offered,expected",
        [
            ("decline", True, True),
            ("decline", False, False),
            ("timeout", True, False),  # 30s timeout gets 0s grace
            ("timeout", False, False),
            ("accept", True, False),
            ("other", True, False),
        ],
    )
    def test_should_apply_grace(self, action, hold_offered, expected):
        assert should_apply_grace(action, hold_offered) is expected

    def test_anchored_grace_timing_scenarios(self):
        # Scenario 1 (oldholdnewtap3): Hold ended 5 seconds ago, owner taps decline now (t=100.0)
        # Anchor is max(accepted_at=100.0, finished_at=95.0) = 100.0.
        # At t=100.0, elapsed = 0.0 -> full 3.0s grace remaining.
        assert compute_grace_delay(
            action="decline",
            hold_offered=True,
            intent_accepted_at=100.0,
            speaking_finished_at=95.0,
            now=100.0,
        ) == pytest.approx(3.0)

        # 1.2s later (t=101.2), remaining grace is 1.8s
        assert compute_grace_delay(
            action="decline",
            hold_offered=True,
            intent_accepted_at=100.0,
            speaking_finished_at=95.0,
            now=101.2,
        ) == pytest.approx(1.8)

        # 3.0s later (t=103.0), remaining grace is 0.0s
        assert compute_grace_delay(
            action="decline",
            hold_offered=True,
            intent_accepted_at=100.0,
            speaking_finished_at=95.0,
            now=103.0,
        ) == pytest.approx(0.0)

        # Scenario 2 (midholdlaterfinish3): Kevin is currently speaking hold offer, speech finishes at t=102.0.
        # Owner tapped decline at t=100.0.
        # Anchor is max(accepted_at=100.0, finished_at=102.0) = 102.0.
        # When speech finishes at t=102.0, elapsed = 0.0 -> full 3.0s grace from speech finish!
        assert compute_grace_delay(
            action="decline",
            hold_offered=True,
            intent_accepted_at=100.0,
            speaking_finished_at=102.0,
            now=102.0,
        ) == pytest.approx(3.0)

        # Scenario 3 (nohold0): Explicit decline without hold offer -> strictly 0.0s grace
        assert compute_grace_delay(
            action="decline",
            hold_offered=False,
            intent_accepted_at=100.0,
            speaking_finished_at=95.0,
            now=100.0,
        ) == 0.0

        # Scenario 4 (timeout0): 30s timeout action -> strictly 0.0s grace
        assert compute_grace_delay(
            action="timeout",
            hold_offered=True,
            intent_accepted_at=100.0,
            speaking_finished_at=95.0,
            now=100.0,
        ) == 0.0

    def test_text_builders(self):
        # Voice text with hold offered
        text_hold = build_unavailable_speech_text("Deli Matsuo", hold_offered=True)
        assert text_hold == "Unfortunately, Deli Matsuo is not available. Can I take a message?"

        # Voice text without hold offered
        text_no_hold = build_unavailable_speech_text("Deli Matsuo", hold_offered=False)
        assert "Deli Matsuo is not available right now" in text_no_hold
        assert "leave me a message" in text_no_hold

        # Default fallback to configured user_name
        text_default = build_unavailable_speech_text("", hold_offered=False)
        assert settings.user_name in text_default

        # Gemini instructions preserve caller language
        gemini_pt = build_gemini_instruction_text("Deli Matsuo", hold_offered=True, language="pt")
        assert "Deli Matsuo" in gemini_pt
        assert "unfortunately" in gemini_pt.lower()
        assert "(pt)" in gemini_pt

        gemini_es = build_gemini_instruction_text("Deli Matsuo", hold_offered=False, language="es")
        assert "Deli Matsuo" in gemini_es
        assert "(es)" in gemini_es

        gemini_en = build_gemini_instruction_text("Deli Matsuo", hold_offered=False, language="en")
        assert "(en)" not in gemini_en

        # Relay instruction
        relay_inst = build_relay_instruction_text("Deli Matsuo", hold_offered=True)
        assert "SYSTEM INSTRUCTION" in relay_inst
        assert "Deli Matsuo" in relay_inst
        assert "unfortunately" in relay_inst


# --- 2. Common Delivery Lifecycle & Durability --------------------------------

def fresh(**changes):
    return dict(contractor_id="owner", state="screening", state_updated_at=time.time(), **{"ws_token": "ws1", **changes})


class Store:
    def __init__(self, record):
        self.record = record
        self.lock = asyncio.Lock()
        self.retry_with = None

    async def transaction(self, sid, callback):
        async with self.lock:
            first = callback(deepcopy(self.record))
            if self.retry_with is not None:
                self.record, self.retry_with = self.retry_with, None
                first = callback(deepcopy(self.record))
            self.record = deepcopy(first)
            return deepcopy(first)

    async def read(self, sid):
        return deepcopy(self.record)


@pytest.fixture
def backend(monkeypatch):
    store = Store(fresh())
    monkeypatch.setattr(actions, "_run_rtdb_transaction", store.transaction)
    monkeypatch.setattr(actions, "read_record", store.read)
    monkeypatch.setattr(
        "app.services.legacy_call_commands._run_legacy_command_transaction",
        AsyncMock(return_value={"type": "take_message"}),
    )
    monkeypatch.setattr(
        "app.services.legacy_call_commands.read_legacy_command",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.legacy_call_commands.delete_legacy_command_conditional",
        AsyncMock(return_value=None),
    )
    return store


async def act(action="decline", op="op1"):
    return await actions.handle_owner_call_action(
        call_sid="CA1", contractor_id="owner", action=action, operation_id=op
    )


class ControlledTime:
    def __init__(self, monotonic_value=100.0):
        self.monotonic_value = monotonic_value

    def monotonic(self):
        return self.monotonic_value

    def __getattr__(self, item):
        return getattr(time, item)


class TestMessageTakingCommonLifecycle:
    @pytest.mark.asyncio
    async def test_ack_failure_single_effect_no_replay(self, backend, monkeypatch):
        """ACK failure executes delivery effect once and does not replay on subsequent retry."""
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
        )
        deliver = AsyncMock(return_value=True)
        real_ack = actions.acknowledge_owner_action
        monkeypatch.setattr(
            actions,
            "acknowledge_owner_action",
            AsyncMock(side_effect=RuntimeError("storage failure")),
        )

        with pytest.raises(RuntimeError, match="storage failure"):
            await actions.consume_message_intent(pipeline, deliver)

        assert backend.record["owner_action_status"] == "message_requested"
        assert deliver.await_count == 1

        monkeypatch.setattr(actions, "acknowledge_owner_action", real_ack)
        assert await actions.consume_message_intent(pipeline, deliver) is True
        assert deliver.await_count == 1  # No second delivery!
        assert backend.record["owner_action_status"] == "taking_message"

    @pytest.mark.asyncio
    async def test_delivery_failure_retains_intent_and_retries(self, backend):
        """Delivery returning False retains intent as retryable, then retries on next poll."""
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
        )
        deliver = AsyncMock(side_effect=[False, True])

        assert not await actions.consume_message_intent(pipeline, deliver)
        assert backend.record["owner_action_status"] == "message_requested"

        assert await actions.consume_message_intent(pipeline, deliver) is True
        assert deliver.await_count == 2
        assert backend.record["owner_action_status"] == "taking_message"

    @pytest.mark.asyncio
    async def test_concurrent_poll_serialized(self, backend):
        """Concurrent polls on the same pipeline are serialized, delivering only once."""
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
        )
        delivery_count = 0

        async def deliver():
            nonlocal delivery_count
            delivery_count += 1
            await asyncio.sleep(0.01)
            return True

        res1, res2 = await asyncio.gather(
            actions.consume_message_intent(pipeline, deliver),
            actions.consume_message_intent(pipeline, deliver),
        )

        assert delivery_count == 1
        assert backend.record["owner_action_status"] == "taking_message"

    @pytest.mark.parametrize("change_stage", ["prepare", "freshread"])
    @pytest.mark.parametrize(
        "mutation",
        [
            "cid_changed",
            "cid_cleared",
            "sid_changed",
            "sid_cleared",
            "ws_changed",
            "ws_cleared",
        ],
    )
    @pytest.mark.asyncio
    async def test_changed_or_cleared_cid_sid_ws_during_prepare_and_freshread(
        self, backend, monkeypatch, change_stage, mutation
    ):
        """Identity mutations during prepare or freshread result in no effects, no ACK, and pending cleared."""
        backend.record = fresh(call_sid="CA1", ws_token="ws1")
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )
        delivery_called = False
        prepare_entered = False
        prepare_completed = False

        async def deliver():
            nonlocal delivery_called
            delivery_called = True
            return True

        def apply_mutation():
            if mutation == "cid_changed":
                pipeline._contractor_config["contractor_id"] = "other_owner"
            elif mutation == "cid_cleared":
                pipeline._contractor_config["contractor_id"] = ""
            elif mutation == "sid_changed":
                pipeline._call_sid = "CA_OTHER"
            elif mutation == "sid_cleared":
                pipeline._call_sid = ""
            elif mutation == "ws_changed":
                pipeline._command_ws_token = "ws2"
            elif mutation == "ws_cleared":
                pipeline._command_ws_token = ""

        if change_stage == "prepare":
            async def prepare(intent):
                nonlocal prepare_entered
                prepare_entered = True
                assert pipeline._message_taking_pending is True
                apply_mutation()
                return True

            pipeline._prepare_message_delivery = prepare
        else:
            async def prepare(intent):
                nonlocal prepare_entered, prepare_completed
                prepare_entered = True
                assert pipeline._message_taking_pending is True
                prepare_completed = True
                return True

            pipeline._prepare_message_delivery = prepare

            real_read = backend.read
            guard_read_mutated = False

            async def mutating_read(sid):
                nonlocal guard_read_mutated
                rec = await real_read(sid)
                if prepare_completed and not guard_read_mutated:
                    apply_mutation()
                    guard_read_mutated = True
                return rec

            monkeypatch.setattr(actions, "read_record", mutating_read)

        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is False
        assert prepare_entered is True
        if change_stage == "freshread":
            assert guard_read_mutated is True
        assert delivery_called is False
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert backend.record["owner_action_status"] == "message_requested"

    @pytest.mark.asyncio
    async def test_control_pass_valid_fixture_delivers_and_acknowledges(
        self, backend
    ):
        """Unmutated ws1 record and pipeline yields prepare, deliver, and taking_message ACK."""
        backend.record = fresh(call_sid="CA1", ws_token="ws1")
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )
        prepare_called = 0

        async def prepare(intent):
            nonlocal prepare_called
            prepare_called += 1
            assert pipeline._message_taking_pending is True
            assert intent.get("contractor_id") == "owner"
            return True

        pipeline._prepare_message_delivery = prepare
        deliver = AsyncMock(return_value=True)

        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is True
        assert prepare_called == 1
        assert deliver.await_count == 1
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert backend.record["owner_action_status"] == "taking_message"

    @pytest.mark.parametrize(
        "record_mutation",
        [
            "deleted",
            "terminal_ended",
            "claim_nonce_changed",
            "operation_id_changed",
            "accepted_true",
        ],
    )
    @pytest.mark.asyncio
    async def test_changed_claim_terminal_or_deletion_aborts(
        self, backend, record_mutation
    ):
        """Durable changes (claim, terminal state, deletion) abort without delivery or ACK."""
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
        )
        delivery_called = False

        async def deliver():
            nonlocal delivery_called
            delivery_called = True
            return True

        async def prepare(intent):
            if record_mutation == "deleted":
                backend.record = None
            elif record_mutation == "terminal_ended":
                backend.record["state"] = "ended"
            elif record_mutation == "claim_nonce_changed":
                backend.record["claim_nonce"] = "different_nonce"
            elif record_mutation == "operation_id_changed":
                backend.record["owner_operation_id"] = "different_op"
                backend.record["message_intent"]["operation_id"] = "different_op"
            elif record_mutation == "accepted_true":
                backend.record["accepted"] = True
            return True

        pipeline._prepare_message_delivery = prepare
        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is False
        assert delivery_called is False

    @pytest.mark.asyncio
    async def test_cancelled_prepare_triggers_cleanup(self, backend):
        """Cancellation during prepare resets pending and executes cleanup hook."""
        await act("decline")
        finish_called = False
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )

        async def prepare(intent):
            assert pipeline._message_taking_pending is True
            raise asyncio.CancelledError()

        def finish():
            nonlocal finish_called
            finish_called = True

        pipeline._prepare_message_delivery = prepare
        pipeline._finish_message_delivery_attempt = finish

        with pytest.raises(asyncio.CancelledError):
            await actions.consume_message_intent(pipeline, AsyncMock(return_value=True))

        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_called is True

    @pytest.mark.asyncio
    async def test_prepare_false_triggers_cleanup(self, backend):
        """prepare returning False resets pending, executes cleanup hook, and skips delivery."""
        await act("decline")
        finish_called = False
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )

        async def prepare(intent):
            assert pipeline._message_taking_pending is True
            return False

        def finish():
            nonlocal finish_called
            finish_called = True

        pipeline._prepare_message_delivery = prepare
        pipeline._finish_message_delivery_attempt = finish
        deliver = AsyncMock(return_value=True)

        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is False
        assert deliver.await_count == 0
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_called is True
        assert backend.record["owner_action_status"] == "message_requested"

    @pytest.mark.asyncio
    async def test_internal_type_error_called_once(self, backend):
        """Internal TypeError inside deliver() propagates directly and is not caught for retry."""
        await act("decline")
        finish_called = False
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )
        deliver_calls = 0

        async def deliver():
            nonlocal deliver_calls
            deliver_calls += 1
            raise TypeError("internal deliver failure")

        def finish():
            nonlocal finish_called
            finish_called = True

        pipeline._finish_message_delivery_attempt = finish

        with pytest.raises(TypeError, match="internal deliver failure"):
            await actions.consume_message_intent(pipeline, deliver)

        assert deliver_calls == 1
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_called is True

    @pytest.mark.asyncio
    async def test_success_pending_false(self, backend):
        """Successful delivery and ACK resets pending to False and executes cleanup hook."""
        await act("decline")
        finish_called = False
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )

        async def prepare(intent):
            assert pipeline._message_taking_pending is True
            return True

        def finish():
            nonlocal finish_called
            finish_called = True

        pipeline._prepare_message_delivery = prepare
        pipeline._finish_message_delivery_attempt = finish
        deliver = AsyncMock(return_value=True)

        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is True
        assert deliver.await_count == 1
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_called is True
        assert backend.record["owner_action_status"] == "taking_message"

    @pytest.mark.asyncio
    async def test_callback_can_call_guard_after_own_await_and_reject_changed_owner(
        self, backend
    ):
        """Engines can call attached guard after their own awaits to reject changed owner before effects."""
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
        )
        effect_applied = False

        async def deliver():
            nonlocal effect_applied
            await asyncio.sleep(0.01)
            backend.record["contractor_id"] = "other_owner"
            guard = getattr(pipeline, "_message_delivery_guard", None)
            assert guard is not None and callable(guard)
            guard_ok = await guard()
            if not guard_ok:
                return False
            effect_applied = True
            return True

        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is False
        assert effect_applied is False
        assert backend.record["owner_action_status"] == "message_requested"

    @pytest.mark.parametrize("failure_stage", ["prepare_failure", "delivery_failure"])
    @pytest.mark.asyncio
    async def test_unchanged_accepted_at_retry_and_new_claim(
        self, backend, monkeypatch, failure_stage
    ):
        """accepted_at is preserved on retry after prepare/delivery failure, and updated on new claim."""
        fake_time = ControlledTime(100.0)
        monkeypatch.setattr(actions, "time", fake_time)

        backend.record = fresh(call_sid="CA1", ws_token="ws1")
        await act("decline", op="op1")

        finish_calls = 0

        def finish():
            nonlocal finish_calls
            finish_calls += 1

        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
            _finish_message_delivery_attempt=finish,
        )

        captured_accepted_at = []

        if failure_stage == "prepare_failure":
            prepare_return_values = [False, True]

            async def prepare(intent):
                captured_accepted_at.append(intent.get("accepted_at"))
                assert pipeline._message_taking_pending is True
                return prepare_return_values.pop(0)

            pipeline._prepare_message_delivery = prepare
            deliver = AsyncMock(return_value=True)
        else:
            async def prepare(intent):
                captured_accepted_at.append(intent.get("accepted_at"))
                assert pipeline._message_taking_pending is True
                return True

            pipeline._prepare_message_delivery = prepare
            deliver = AsyncMock(side_effect=[False, True])

        # Attempt 1 at t=100.0 -> fails at prepare or delivery
        res1 = await actions.consume_message_intent(pipeline, deliver)
        assert res1 is False
        assert captured_accepted_at == [100.0]
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_calls == 1
        assert backend.record["owner_action_status"] == "message_requested"
        # Ensure RTDB record is not mutated with accepted_at
        assert "accepted_at" not in backend.record
        assert "accepted_at" not in backend.record.get("message_intent", {})

        # Attempt 2 at t=110.0 -> retry same claim must retain accepted_at=100.0
        fake_time.monotonic_value = 110.0
        res2 = await actions.consume_message_intent(pipeline, deliver)
        assert res2 is True
        assert captured_accepted_at == [100.0, 100.0]
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_calls == 2
        assert backend.record["owner_action_status"] == "taking_message"
        assert "accepted_at" not in backend.record
        assert "accepted_at" not in backend.record.get("message_intent", {})

        # Attempt 3 at t=120.0 with a new claim key -> gets legitimate new accepted_at=120.0
        backend.record = fresh(call_sid="CA1", ws_token="ws1")
        await act("decline", op="op2")
        fake_time.monotonic_value = 120.0

        if failure_stage == "prepare_failure":
            prepare_return_values.append(True)
        else:
            deliver.side_effect = [True]

        res3 = await actions.consume_message_intent(pipeline, deliver)
        assert res3 is True
        assert captured_accepted_at == [100.0, 100.0, 120.0]
        assert pipeline._message_taking_pending is False
        assert not hasattr(pipeline, "_message_delivery_guard")
        assert finish_calls == 3
        assert backend.record["owner_action_status"] == "taking_message"
        assert "accepted_at" not in backend.record
        assert "accepted_at" not in backend.record.get("message_intent", {})

    @pytest.mark.parametrize(
        "target,mutation",
        [
            ("pipeline", "missing"),
            ("pipeline", "empty"),
            ("pipeline", "none"),
            ("record", "missing"),
            ("record", "empty"),
            ("record", "none"),
            ("record", "different"),
        ],
    )
    @pytest.mark.asyncio
    async def test_token_missing_empty_none_or_different_aborts_without_delivery_or_ack(
        self, backend, target, mutation
    ):
        """Invalid pipeline or record ws_token prevents prepare and deliver calls, retaining message_requested."""
        backend.record = fresh(call_sid="CA1", ws_token="ws1")
        await act("decline")
        pipeline = SimpleNamespace(
            _call_sid="CA1",
            _contractor_config={"contractor_id": "owner"},
            _command_ws_token="ws1",
            _connected=True,
            _message_taking_pending=False,
        )
        prepare = AsyncMock(return_value=True)
        pipeline._prepare_message_delivery = prepare
        deliver = AsyncMock(return_value=True)

        if target == "pipeline":
            if mutation == "missing":
                del pipeline._command_ws_token
            elif mutation == "empty":
                pipeline._command_ws_token = ""
            elif mutation == "none":
                pipeline._command_ws_token = None
        elif target == "record":
            if mutation == "missing":
                backend.record.pop("ws_token", None)
            elif mutation == "empty":
                backend.record["ws_token"] = ""
            elif mutation == "none":
                backend.record["ws_token"] = None
            elif mutation == "different":
                backend.record["ws_token"] = "different_ws"

        result = await actions.consume_message_intent(pipeline, deliver)
        assert result is False
        assert prepare.await_count == 0
        assert deliver.await_count == 0
        assert backend.record["owner_action_status"] == "message_requested"
