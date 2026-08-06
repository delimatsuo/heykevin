"""Privacy contracts for post-call processing telemetry."""

import ast
import inspect
import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import post_call


@pytest.mark.asyncio
async def test_post_call_failure_log_excludes_transcript_and_exception_message(
    monkeypatch,
    caplog,
):
    private_transcript = "Caller: private medical and financial details"
    private_error = "provider echoed private medical and financial details"

    async def fail_business(*_args, **_kwargs):
        raise RuntimeError(private_error)

    monkeypatch.setattr(post_call, "_process_business", fail_business)
    caplog.set_level(logging.INFO, logger="app.services.post_call")

    result = await post_call.process_post_call(
        transcript_lines=[private_transcript],
        caller_phone="test-caller-number",
        call_sid="CA1234567890FULL",
        contractor={"effective_mode": "business"},
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "post_call_event event=processing_error" in messages
    assert "call=CA123456" in messages
    assert "exception_type=RuntimeError" in messages
    assert "CA1234567890FULL" not in messages
    assert private_transcript not in messages
    assert private_error not in messages
    assert result.status == "failed"
    assert result.failed_effects == ("processing",)
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_post_call_exception_helper_logs_type_only(caplog):
    private_error = "private caller address and provider response"
    caplog.set_level(logging.INFO, logger="app.services.post_call")

    post_call._log_post_call_exception(
        "delivery_error",
        RuntimeError(private_error),
        "CA1234567890FULL",
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "post_call_event event=delivery_error" in messages
    assert "call=CA123456" in messages
    assert "exception_type=RuntimeError" in messages
    assert private_error not in messages
    assert "CA1234567890FULL" not in messages


def test_post_call_logger_calls_do_not_embed_sensitive_runtime_values():
    forbidden_names = {
        "call_type",
        "caller_name",
        "caller_phone",
        "contractor_phone",
        "e",
        "exc",
        "job_id",
        "owner_phone",
    }
    tree = ast.parse(inspect.getsource(post_call))

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        function = call.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "logger"
        ):
            continue

        assert not any(keyword.arg == "exc_info" for keyword in call.keywords)
        referenced_names = {
            node.id
            for argument in call.args
            for node in ast.walk(argument)
            if isinstance(node, ast.Name)
        }
        assert referenced_names.isdisjoint(forbidden_names), ast.unparse(call)
