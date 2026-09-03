"""twilio-python's REST client logs every request at INFO — including the
URL and query string, which carry caller phone numbers for lookup/list
calls (twilio/http/__init__.py log_request/log_response in twilio 9.10.4).
setup_logging must keep the SDK's loggers at WARNING, the same way it
already silences httpx/httpcore."""

import logging

import pytest

from app.utils.logging import setup_logging

TWILIO_LOGGER_NAMES = ("twilio", "twilio.http_client", "twilio.async_http_client")


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Snapshot and restore global logging state so this module's calls to
    setup_logging() don't leak handlers/levels into other test modules."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_root_level = root.level

    saved_logger_levels = {
        name: logging.getLogger(name).level
        for name in (*TWILIO_LOGGER_NAMES, "httpx", "httpcore")
    }

    yield

    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_root_level)
    for name, level in saved_logger_levels.items():
        logging.getLogger(name).setLevel(level)


def test_setup_logging_silences_twilio_sdk_loggers_to_warning():
    setup_logging("INFO")

    for name in TWILIO_LOGGER_NAMES:
        assert logging.getLogger(name).level == logging.WARNING


def test_twilio_http_client_info_log_with_caller_number_is_suppressed(caplog):
    setup_logging("INFO")
    # setup_logging() replaces the root logger's handlers wholesale (it
    # clears them before installing its own JSON handler), which detaches
    # caplog's own capture handler. Reattach it so caplog can observe what
    # actually propagates after setup_logging() has run.
    logging.getLogger().addHandler(caplog.handler)
    logger = logging.getLogger("twilio.http_client")

    with caplog.at_level(logging.INFO):
        logger.info(
            "GET Request: https://lookups.twilio.com/v2/PhoneNumbers/+14165550123"
        )
        logger.warning("rate limited")

    messages = [record.getMessage() for record in caplog.records]
    assert "rate limited" in messages
    assert not any("+14165550123" in message for message in messages)
    assert len(caplog.records) == 1


def test_setup_logging_is_idempotent_for_handlers_and_levels():
    setup_logging("INFO")
    setup_logging("INFO")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    for name in TWILIO_LOGGER_NAMES:
        assert logging.getLogger(name).level == logging.WARNING
