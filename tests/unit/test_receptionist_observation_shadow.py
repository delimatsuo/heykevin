"""Staging-only, payload-free retrospective observation shadow."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.config import settings
from app.db.contractors import PROTECTED_FIELDS
from app.services.caller_turns import CallerTurnEventKind
from app.services.gemini_pipeline import GeminiPipeline
from app.services.receptionist_observation_shadow import (
    ReceptionistObservationShadow,
    build_receptionist_observation_shadow,
    compute_caller_hmac_digest,
)


TEST_CALLER = "synthetic-test-caller"
TEST_HMAC_KEY = "k" * 32


async def _noop(*_args, **_kwargs):
    return None


async def _wait_until(predicate, *, timeout_seconds: float = 0.5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for observation shadow state")
        await asyncio.sleep(0.005)


def _contractor_config(*, now: int, caller: str = TEST_CALLER) -> dict:
    return {
        "contractor_id": "synthetic-staging-contractor",
        "business_name": "Synthetic Services",
        "receptionist_observation_shadow_enabled": True,
        "receptionist_observation_shadow_expires_at": now + 600,
        "receptionist_observation_shadow_caller_digests": [
            compute_caller_hmac_digest(caller, TEST_HMAC_KEY)
        ],
    }


def _configure_staging(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "receptionist_observation_shadow_enabled", True)
    monkeypatch.setattr(
        settings,
        "receptionist_observation_shadow_caller_hmac_key",
        TEST_HMAC_KEY,
    )
    monkeypatch.setattr(
        settings,
        "receptionist_observation_shadow_max_authorization_seconds",
        3600,
    )


def test_observation_shadow_release_fields_are_server_protected():
    assert {
        "receptionist_observation_shadow_enabled",
        "receptionist_observation_shadow_expires_at",
        "receptionist_observation_shadow_caller_digests",
    } <= PROTECTED_FIELDS


@pytest.mark.parametrize(
    "change",
    [
        {"environment": "production"},
        {"environment": "development"},
        {"global_enabled": False},
        {"account_enabled": False},
        {"account_enabled": "true"},
        {"expires_at": 0},
        {"expires_at": "later"},
        {"expires_at": 5_000},
        {"caller": "different-test-caller"},
        {"hmac_key": "short"},
        {"caller_digests": []},
        {"caller_digests": ["not-a-digest"]},
    ],
)
def test_builder_fails_closed_unless_every_staging_test_gate_matches(
    monkeypatch,
    change,
):
    now = 1000
    _configure_staging(monkeypatch)
    config = _contractor_config(now=now)
    caller = change.get("caller", TEST_CALLER)
    if "environment" in change:
        monkeypatch.setattr(settings, "environment", change["environment"])
    if "global_enabled" in change:
        monkeypatch.setattr(
            settings,
            "receptionist_observation_shadow_enabled",
            change["global_enabled"],
        )
    if "hmac_key" in change:
        monkeypatch.setattr(
            settings,
            "receptionist_observation_shadow_caller_hmac_key",
            change["hmac_key"],
        )
    field_changes = {
        "account_enabled": "receptionist_observation_shadow_enabled",
        "expires_at": "receptionist_observation_shadow_expires_at",
        "caller_digests": "receptionist_observation_shadow_caller_digests",
    }
    for source, target in field_changes.items():
        if source in change:
            config[target] = change[source]

    shadow = build_receptionist_observation_shadow(
        contractor_config=config,
        caller_identifier=caller,
        now=now,
    )

    assert shadow is None


def test_builder_accepts_one_exact_short_lived_staging_test_authorization(monkeypatch):
    now = 1000
    _configure_staging(monkeypatch)

    shadow = build_receptionist_observation_shadow(
        contractor_config=_contractor_config(now=now),
        caller_identifier=TEST_CALLER,
        now=now,
    )

    assert isinstance(shadow, ReceptionistObservationShadow)


@pytest.mark.asyncio
async def test_sidecar_emits_only_payload_free_turn_metrics(caplog):
    shadow = ReceptionistObservationShadow(
        queue_size=8,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    private_transcript = "Private Caller at 123 Secret Lane callback PHONE_SENTINEL"
    private_output = "Private model response"

    with caplog.at_level(
        logging.INFO,
        logger="app.services.receptionist_observation_shadow",
    ):
        assert shadow.try_enqueue_message(
            {
                "serverContent": {
                    "inputTranscription": {"text": private_transcript},
                    "modelTurn": {"parts": [{"text": private_output}]},
                    "turnComplete": True,
                }
            }
        )
        await _wait_until(lambda: shadow.emitted_turn_count == 1)
        await shadow.stop()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=observation_shadow_turn" in messages
    assert "status=retrospective_complete" in messages
    assert "event=observation_shadow_stopped" in messages
    assert "shadow=shadow-test" in messages
    assert "transcript_codepoints=" in messages
    assert private_transcript not in messages
    assert private_output not in messages
    assert "Private Caller" not in messages
    assert "123 Secret Lane" not in messages
    assert "PHONE_SENTINEL" not in messages


@pytest.mark.asyncio
async def test_queue_saturation_drops_diagnostics_without_blocking(monkeypatch, caplog):
    shadow = ReceptionistObservationShadow(
        queue_size=1,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    monkeypatch.setattr(shadow, "_ensure_worker", lambda: True)

    with caplog.at_level(
        logging.WARNING,
        logger="app.services.receptionist_observation_shadow",
    ):
        message = {
            "serverContent": {"inputTranscription": {"text": "Synthetic request"}}
        }
        assert shadow.try_enqueue_message(message) is True
        started_at = time.monotonic()
        assert shadow.try_enqueue_message(message) is False
        elapsed_ms = (time.monotonic() - started_at) * 1000

    assert elapsed_ms < 10
    assert shadow.dropped_item_count == 1
    assert "event=observation_shadow_queue_drop" in caplog.text
    assert "serverContent" not in caplog.text


def test_streamed_model_audio_is_coalesced_before_it_can_fill_the_queue(monkeypatch):
    shadow = ReceptionistObservationShadow(
        queue_size=2,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    monkeypatch.setattr(shadow, "_ensure_worker", lambda: True)
    audio_message = {
        "serverContent": {
            "modelTurn": {
                "parts": [{"inlineData": {"data": "private-model-audio"}}]
            }
        }
    }

    assert shadow.try_enqueue_message(audio_message)
    assert all(shadow.try_enqueue_message(audio_message) for _ in range(100))
    assert shadow.try_enqueue_message({"serverContent": {"turnComplete": True}})

    assert shadow.dropped_item_count == 0
    assert shadow.ignored_message_count == 100
    assert shadow._queue.qsize() == 2


def test_enqueue_projection_removes_model_payload_and_tool_arguments(monkeypatch):
    shadow = ReceptionistObservationShadow(
        queue_size=2,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    monkeypatch.setattr(shadow, "_ensure_worker", lambda: True)

    assert shadow.try_enqueue_message(
        {
            "serverContent": {
                "inputTranscription": {"text": "Synthetic caller request"},
                "modelTurn": {
                    "parts": [
                        {
                            "text": "private-model-text",
                            "inlineData": {"data": "private-model-audio"},
                        }
                    ]
                },
            },
            "toolCall": {
                "functionCalls": [
                    {"name": "lookup", "args": {"private": "private-tool-argument"}}
                ]
            },
        }
    )

    queued = shadow._queue.get_nowait()
    serialized = json.dumps(queued.value)
    assert "Synthetic caller request" in serialized
    assert "private-model-text" not in serialized
    assert "private-model-audio" not in serialized
    assert "private-tool-argument" not in serialized


def test_reconnect_resets_model_output_coalescing_before_worker_processing(monkeypatch):
    shadow = ReceptionistObservationShadow(
        queue_size=3,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    monkeypatch.setattr(shadow, "_ensure_worker", lambda: True)
    audio_message = {"serverContent": {"modelTurn": {"parts": [{}]}}}

    assert shadow.try_enqueue_message(audio_message)
    assert shadow.try_enqueue_lifecycle(CallerTurnEventKind.RECONNECT_STARTED)
    assert shadow.try_enqueue_message(audio_message)

    assert shadow.ignored_message_count == 0
    assert shadow.dropped_item_count == 0
    assert shadow._queue.qsize() == 3


@pytest.mark.asyncio
async def test_worker_error_is_payload_free_and_never_escapes_to_live_path(
    monkeypatch,
    caplog,
):
    shadow = ReceptionistObservationShadow(
        queue_size=4,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    private_error = "failed for Private Caller at 123 Secret Lane"

    def fail_adapter(*_args, **_kwargs):
        raise RuntimeError(private_error)

    monkeypatch.setattr(shadow._adapter, "adapt_message", fail_adapter)
    with caplog.at_level(
        logging.ERROR,
        logger="app.services.receptionist_observation_shadow",
    ):
        assert shadow.try_enqueue_message(
            {"serverContent": {"inputTranscription": {"text": private_error}}}
        )
        await _wait_until(lambda: shadow.worker_error_count == 1)
        await shadow.stop()

    assert shadow.worker_error_count == 1
    assert "event=observation_shadow_worker_error" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert private_error not in caplog.text
    assert "Private Caller" not in caplog.text


@pytest.mark.asyncio
async def test_lifecycle_reconnect_and_stop_are_bounded_and_payload_free(caplog):
    shadow = ReceptionistObservationShadow(
        queue_size=8,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )

    with caplog.at_level(
        logging.INFO,
        logger="app.services.receptionist_observation_shadow",
    ):
        assert shadow.try_enqueue_message(
            {"serverContent": {"inputTranscription": {"text": "Synthetic request"}}}
        )
        assert shadow.try_enqueue_lifecycle(CallerTurnEventKind.CONNECTION_CLOSED)
        assert shadow.try_enqueue_lifecycle(CallerTurnEventKind.RECONNECT_STARTED)
        await shadow.stop(timeout_seconds=0.2)

    assert "reason=connection_closed" in caplog.text
    assert "event=observation_shadow_stopped" in caplog.text
    assert shadow.worker_task is None or shadow.worker_task.done()


@pytest.mark.asyncio
async def test_abort_synchronously_disables_and_cancels_diagnostic_worker():
    shadow = ReceptionistObservationShadow(
        queue_size=4,
        quiescence_ms=10,
        shadow_id="shadow-test",
    )
    assert shadow.try_enqueue_message(
        {"serverContent": {"inputTranscription": {"text": "Synthetic request"}}}
    )
    task = shadow.worker_task
    assert task is not None

    shadow.abort()
    await asyncio.gather(task, return_exceptions=True)

    assert task.done()
    assert shadow.try_enqueue_message({"serverContent": {"turnComplete": True}}) is False


def test_sidecar_has_no_controller_tool_persistence_or_post_call_imports():
    path = Path("app/services/receptionist_observation_shadow.py")
    tree = ast.parse(path.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden = {
        "app.services.receptionist_state",
        "app.services.dialogue_planner",
        "app.services.instruction_composer",
        "app.services.post_call",
        "app.services.post_call_handoff",
        "app.db.calls",
        "app.db.post_call_handoffs",
        "app.services.jobber",
    }
    assert imports.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_enabled_pipeline_observation_never_sends_or_changes_prompt(
    monkeypatch,
):
    now = int(time.time())
    _configure_staging(monkeypatch)
    config = _contractor_config(now=now)
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="synthetic-call",
        contractor_config=config,
        caller_phone=TEST_CALLER,
    )

    class RecordingWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    websocket = RecordingWebSocket()
    pipeline._ws = websocket
    original_prompt = pipeline._system_prompt

    pipeline._observe_receptionist_shadow(
        {"serverContent": {"inputTranscription": {"text": "Synthetic request"}}}
    )
    await pipeline._stop_receptionist_shadow()

    assert pipeline._observation_shadow is not None
    assert pipeline._system_prompt == original_prompt
    assert websocket.sent == []
    assert pipeline._assistant_instruction_pending is False


def test_pipeline_never_builds_observation_shadow_outside_staging(monkeypatch):
    now = int(time.time())
    _configure_staging(monkeypatch)
    monkeypatch.setattr(settings, "environment", "production")

    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="synthetic-call",
        contractor_config=_contractor_config(now=now),
        caller_phone=TEST_CALLER,
    )

    assert pipeline._observation_shadow is None


@pytest.mark.parametrize("operation", ["message", "lifecycle"])
def test_pipeline_contains_synchronous_shadow_enqueue_failures(
    operation,
    caplog,
):
    private_error = "failed for Private Caller at 123 Secret Lane"
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="synthetic-call",
        contractor_config={"effective_mode": "personal"},
    )

    class FailingShadow:
        def __init__(self):
            self.aborted = False

        def try_enqueue_message(self, _message):
            raise RuntimeError(private_error)

        def try_enqueue_lifecycle(self, _kind):
            raise RuntimeError(private_error)

        def abort(self):
            self.aborted = True

    failing_shadow = FailingShadow()
    pipeline._observation_shadow = failing_shadow
    with caplog.at_level(logging.ERROR, logger="app.services.gemini_pipeline"):
        if operation == "message":
            pipeline._observe_receptionist_shadow(
                {"serverContent": {"inputTranscription": {"text": private_error}}}
            )
        else:
            pipeline._observe_receptionist_shadow_lifecycle("reconnect_started")

    assert pipeline._observation_shadow is None
    assert failing_shadow.aborted is True
    assert "event=observation_shadow_enqueue_error" in caplog.text
    assert f"operation={operation}" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert private_error not in caplog.text
    assert "Private Caller" not in caplog.text
