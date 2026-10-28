"""Actual API lifecycle behavior, all nine loops and conservative mode gates."""

import asyncio
import base64
import os
from unittest.mock import AsyncMock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app import main
from app.config import Settings, validate_runtime_safety


@pytest.fixture
def inert_lifecycle(monkeypatch):
    for field in ("twilio_account_sid", "twilio_auth_token", "anthropic_api_key",
                  "deepgram_api_key", "elevenlabs_api_key", "api_bearer_token"):
        monkeypatch.setattr(main.settings, field, "fictional")
    monkeypatch.setattr(main, "validate_runtime_safety", lambda: None)
    monkeypatch.setattr(main, "_housekeeping_tasks", {})
    for name in ("_post_call_worker_task", "_estimate_worker_task", "_service_request_recovery_task", "_jobber_disconnect_recovery_task"):
        monkeypatch.setattr(main, name, None)
    monkeypatch.setattr(main, "_cutover_task", None)
    monkeypatch.setattr(main, "_effective_background_worker_mode", "embedded")
    cancelled = []

    def factory(name):
        async def idle():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(name)
        idle.__name__ = name
        return idle

    for name in ("_orphan_call_cleanup", "_call_retention_sweep", "_lapsed_trial_sweep", "_expired_contractor_cleanup", "_data_purge_sweep"):
        monkeypatch.setattr(main, name, factory(name))
    from app.services import post_call_handoff, estimate_worker, service_request_recovery, jobber_disconnect_recovery
    for module, name in ((post_call_handoff, "post_call_worker_loop"), (estimate_worker, "estimate_worker_loop"),
        (service_request_recovery, "service_request_recovery_worker_loop"), (jobber_disconnect_recovery, "jobber_disconnect_recovery_worker_loop")):
        monkeypatch.setattr(module, name, factory(name))
    return cancelled


def tasks():
    return [*main._housekeeping_tasks.values(), main._post_call_worker_task, main._estimate_worker_task,
            main._service_request_recovery_task, main._jobber_disconnect_recovery_task]


def test_default_mode_is_embedded_and_other_mode_names_are_rejected():
    arguments = {"twilio_account_sid": "fictional", "twilio_auth_token": "fictional",
        "twilio_phone_number": "+15555550100", "telegram_bot_token": "fictional", "user_phone": "+15555550101"}
    assert Settings(_env_file=None, **arguments).background_worker_mode == "embedded"
    with pytest.raises(ValueError):
        Settings(_env_file=None, **arguments, background_worker_mode="disabled")


@pytest.mark.asyncio
async def test_all_nine_embedded_loops_start_once_and_shutdown_joined(inert_lifecycle, monkeypatch):
    monkeypatch.setattr(main.settings, "background_worker_mode", "embedded")
    monkeypatch.setattr(main.settings, "service_request_recovery_enabled", True)
    monkeypatch.setattr(main.settings, "jobber_disconnect_recovery_enabled", True)
    try:
        await main.startup()
        first = tasks()
        assert len(first) == 9 and all(task is not None for task in first)
        await asyncio.sleep(0)
        await main.startup()
        assert tasks() == first
    finally:
        await main.shutdown()
    assert all(task.done() and task.cancelled() for task in first)
    assert len(inert_lifecycle) == 9
    assert not main._housekeeping_tasks
    assert tasks() == [None]*4


@pytest.mark.asyncio
async def test_shutdown_joins_remaining_tasks_when_one_already_failed(inert_lifecycle, monkeypatch):
    monkeypatch.setattr(main.settings, "background_worker_mode", "embedded")
    monkeypatch.setattr(main.settings, "service_request_recovery_enabled", True)
    monkeypatch.setattr(main.settings, "jobber_disconnect_recovery_enabled", True)
    await main.startup()
    async def failed():
        raise RuntimeError("fictional task failure")
    extra = asyncio.create_task(failed())
    main._housekeeping_tasks["failed"] = extra
    all_tasks = tasks()
    await asyncio.sleep(0)
    await main.shutdown()
    assert all(task.done() for task in all_tasks)
    assert isinstance(extra.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_external_mode_starts_zero_embedded_loops(inert_lifecycle, monkeypatch):
    monkeypatch.setattr(main.settings, "background_worker_mode", "external")
    monkeypatch.setattr(main.settings, "background_worker_cutover_generation", "staging-cutover-20261013")
    monkeypatch.setattr(main.settings, "environment", "staging")
    class ActiveCutover:
        def activate_external(self, generation):
            assert generation == "staging-cutover-20261013"
            return True
        def external_active(self, generation):
            return True
    monkeypatch.setattr(main, "_get_background_cutover_store", ActiveCutover)
    await main.startup()
    try:
        assert not main._housekeeping_tasks
        assert tasks() == [None]*4
        assert (await main.health())["worker_mode"] == "external"
    finally:
        await main.shutdown()
    assert not inert_lifecycle


@pytest.mark.asyncio
async def test_incomplete_external_cutover_retains_embedded_recovery(inert_lifecycle, monkeypatch):
    monkeypatch.setattr(main.settings, "background_worker_mode", "external")
    monkeypatch.setattr(main.settings, "background_worker_cutover_generation", "staging-cutover-20261013")
    monkeypatch.setattr(main.settings, "environment", "staging")
    monkeypatch.setattr(main.settings, "service_request_recovery_enabled", True)
    monkeypatch.setattr(main.settings, "jobber_disconnect_recovery_enabled", True)

    class PendingCutover:
        def activate_external(self, generation):
            assert generation == "staging-cutover-20261013"
            return False
        def begin_embedded(self):
            return True
    monkeypatch.setattr(main, "_get_background_cutover_store", PendingCutover)

    try:
        await main.startup()
        assert len(tasks()) == 9 and all(task is not None for task in tasks())
        assert (await main.health())["worker_mode"] == "embedded_fallback"
        await asyncio.sleep(0)
    finally:
        await main.shutdown()
    assert len(inert_lifecycle) == 9


@pytest.mark.asyncio
async def test_draining_names_holding_jobs_when_they_change_and_every_fifteen_minutes(monkeypatch):
    estimates = (("estimates", "needs_attention"),)
    both = (("post_calls", "running"), ("estimates", "needs_attention"))

    class HeldCutover:
        held_jobs = ()
        def __init__(self, polls):
            self.polls, self.count = iter(polls), 0
        def begin_embedded(self):
            self.count += 1
            held = next(self.polls)
            self.held_jobs = held or ()
            return held is None

    # 181 polls of the same hold, a change, the fence turn, then release.
    store = HeldCutover([estimates] * 181 + [both, (), None])
    lines, started = [], []

    class Recorder:
        def warning(self, message, *args):
            lines.append((store.count, "warning", message % args))
        def info(self, message, *args):
            lines.append((store.count, "info", message % args))

    async def no_sleep(seconds):
        assert seconds == 5
    monkeypatch.setattr(main, "logger", Recorder())
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main, "_start_embedded_background_loops", lambda: started.append(True))
    monkeypatch.setattr(main, "_effective_background_worker_mode", "draining")

    await main._wait_for_embedded_cutover(store, fallback=False)

    held = "Background-worker drain held by ledger jobs: "
    assert lines == [
        (1, "warning", held + "estimates=needs_attention"),
        (181, "warning", held + "estimates=needs_attention"),
        (182, "warning", held + "post_calls=running, estimates=needs_attention"),
        (184, "info", "Background-worker drain released; starting embedded loops"),
    ]
    assert started == [True]
    assert main._effective_background_worker_mode == "embedded"


@pytest.mark.parametrize("environment", ["development", "production", "test"])
def test_external_mode_forbidden_outside_staging(monkeypatch, environment):
    monkeypatch.setattr(main.settings, "background_worker_mode", "external")
    monkeypatch.setattr(main.settings, "environment", environment)
    with pytest.raises(RuntimeError, match="External background workers require"):
        validate_runtime_safety()


def _set_safe_staging_external(monkeypatch, *, environment="staging"):
    key = base64.b64encode(b"x" * 32).decode()
    for field, value in {
        "background_worker_mode": "external",
        "environment": environment,
        "background_worker_cutover_generation": "staging-cutover-20261013",
        "allow_production_resources_in_non_production": False,
        "cloud_run_url": "https://kevin-staging.example.com",
        "firestore_project_id": "kevin-staging",
        "firebase_database_url": "https://kevin-staging.firebaseio.com",
        "production_twilio_account_sid": "ACproduction",
        "twilio_account_sid": "ACstaging",
        "appstore_environment": "sandbox",
        "apns_sandbox": True,
        "transcript_encryption_key": key,
        "integration_token_encryption_keys": f'{{"1": "{key}"}}',
        "integration_token_active_key_version": "1",
    }.items():
        monkeypatch.setattr(main.settings, field, value)


@pytest.mark.parametrize("environment", ["STAGING", " staging "])
def test_external_mode_refuses_noncanonical_environment_aliases(monkeypatch, environment):
    _set_safe_staging_external(monkeypatch, environment=environment)
    with pytest.raises(RuntimeError, match="trimmed lower-case runtime name"):
        validate_runtime_safety()


@pytest.mark.parametrize("field", [
    "purge_enabled", "service_request_recovery_enabled", "jobber_disconnect_recovery_enabled",
])
def test_external_mode_refuses_unmigrated_enabled_loops(monkeypatch, field):
    _set_safe_staging_external(monkeypatch)
    monkeypatch.setattr(main.settings, field, True)
    with pytest.raises(RuntimeError, match="until its loop is separately migrated"):
        validate_runtime_safety()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper,name,interval,immediate", [
    ("_orphan_call_cleanup", "orphan_calls_once", 300, False),
    ("_call_retention_sweep", "call_retention_once", 21600, False),
    ("_lapsed_trial_sweep", "lapsed_trials_once", 21600, False),
    ("_expired_contractor_cleanup", "expired_contractors_once", 21600, False),
    ("_data_purge_sweep", "data_purge_once", 21600, True),
])
async def test_legacy_wrapper_cadences(monkeypatch, wrapper, name, interval, immediate):
    delegated = AsyncMock()
    monkeypatch.setattr(main, "_periodic_maintenance", delegated)
    await getattr(main, wrapper)()
    if immediate:
        delegated.assert_awaited_once_with(name, interval, immediate=True)
    else:
        delegated.assert_awaited_once_with(name, interval)


@pytest.mark.asyncio
@pytest.mark.parametrize("immediate", [False, True])
async def test_housekeeping_first_sleep_behavior(monkeypatch, immediate):
    from app.services import background_maintenance
    events = []
    async def once(): events.append("pass")
    async def sleep(seconds):
        events.append(("sleep", seconds))
        if (immediate and len(events) >= 2) or (not immediate and len(events) >= 3):
            raise asyncio.CancelledError()
    monkeypatch.setattr(background_maintenance, "call_retention_once", once)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._periodic_maintenance("call_retention_once", 21600, immediate=immediate)
    assert events[0] == ("pass" if immediate else ("sleep", 21600))
    assert "pass" in events
