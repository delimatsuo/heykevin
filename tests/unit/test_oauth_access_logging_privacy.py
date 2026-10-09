"""Unit tests for OAuth callback container access-log privacy filter."""

import io
import logging
import uuid
import pytest
from uvicorn.logging import AccessFormatter, DefaultFormatter

from app.utils.logging import (
    JSONFormatter,
    OAuthCallbackAccessLogFilter,
    setup_logging,
)


@pytest.fixture(autouse=True)
def restore_logging_environment():
    """Snapshot and restore logging configuration across all touched loggers."""
    loggers_to_save = [
        "",  # root
        "uvicorn.access",
        "httpx",
        "httpcore",
        "twilio",
        "twilio.http_client",
        "twilio.async_http_client",
    ]
    snapshots = {}
    for name in loggers_to_save:
        logger = logging.getLogger(name)
        snapshots[name] = {
            "level": logger.level,
            "handlers": list(logger.handlers),
            "filters": list(logger.filters),
            "disabled": logger.disabled,
            "propagate": logger.propagate,
        }
    try:
        yield
    finally:
        for name, state in snapshots.items():
            logger = logging.getLogger(name)
            logger.setLevel(state["level"])
            logger.handlers.clear()
            for h in state["handlers"]:
                logger.addHandler(h)
            logger.filters.clear()
            for f in state["filters"]:
                logger.addFilter(f)
            logger.disabled = state["disabled"]
            logger.propagate = state["propagate"]


def _create_test_access_logger(formatter: logging.Formatter | None = None) -> tuple[logging.Logger, io.StringIO]:
    """Creates an isolated Logger with a StreamHandler capturing output to a StringIO stream."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    if formatter is None:
        handler.setFormatter(
            AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s')
        )
    else:
        handler.setFormatter(formatter)

    test_logger = logging.Logger(f"test.uvicorn.access.{uuid.uuid4().hex}", level=logging.INFO)
    test_logger.propagate = False
    test_logger.addHandler(handler)
    test_logger.addFilter(OAuthCallbackAccessLogFilter())
    return test_logger, stream


def test_jobber_callback_query_redacted():
    test_logger, stream = _create_test_access_logger()
    client = "192.168.1.100:54321"
    method = "GET"
    query = "code=SENTINEL_JOBBER_CODE_12345&state=SENTINEL_JOBBER_STATE_67890"
    target = f"/api/integrations/jobber/callback?{query}"
    http_version = "1.1"
    status = 200

    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        target,
        http_version,
        status,
    )

    output = stream.getvalue()
    assert client in output
    assert method in output
    assert "/api/integrations/jobber/callback" in output
    assert "1.1" in output
    assert "200" in output

    # Must contain no callback query key/value, code, state, or query separator
    assert "SENTINEL_JOBBER_CODE_12345" not in output
    assert "SENTINEL_JOBBER_STATE_67890" not in output
    assert "code=" not in output
    assert "state=" not in output
    assert "?" not in output


def test_google_calendar_callback_query_redacted():
    test_logger, stream = _create_test_access_logger()
    client = "10.0.0.1:43210"
    method = "GET"
    query = (
        "code=SENTINEL_GCAL_CODE_ABCDE&"
        "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar&"
        "state=SENTINEL_GCAL_STATE_XYZ"
    )
    target = f"/api/integrations/google-calendar/callback?{query}"
    http_version = "2.0"
    status = 302

    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        target,
        http_version,
        status,
    )

    output = stream.getvalue()
    assert client in output
    assert method in output
    assert "/api/integrations/google-calendar/callback" in output
    assert "2.0" in output
    assert "302" in output

    assert "SENTINEL_GCAL_CODE_ABCDE" not in output
    assert "SENTINEL_GCAL_STATE_XYZ" not in output
    assert "code=" not in output
    assert "scope=" not in output
    assert "state=" not in output
    assert "?" not in output


def test_oauth_callback_error_query_redacted():
    test_logger, stream = _create_test_access_logger()
    client = "127.0.0.1:50000"
    method = "GET"
    query = "error=access_denied&error_description=SENTINEL_ERROR_DESC_FORBIDDEN"
    target = f"/api/integrations/jobber/callback?{query}"
    http_version = "1.1"
    status = 403

    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        target,
        http_version,
        status,
    )

    output = stream.getvalue()
    assert client in output
    assert method in output
    assert "/api/integrations/jobber/callback" in output
    assert "1.1" in output
    assert "403" in output

    assert "SENTINEL_ERROR_DESC_FORBIDDEN" not in output
    assert "access_denied" not in output
    assert "error=" not in output
    assert "error_description=" not in output
    assert "?" not in output


def test_oauth_callback_arbitrary_and_repeated_params_redacted():
    test_logger, stream = _create_test_access_logger()
    client = "172.16.0.5:8080"
    method = "GET"
    query = "foo=bar&foo=baz&token=SENTINEL_TOKEN_SECRET&extra_arg=123"
    target = f"/api/integrations/jobber/callback?{query}"
    http_version = "1.1"
    status = 200

    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        target,
        http_version,
        status,
    )

    output = stream.getvalue()
    assert "/api/integrations/jobber/callback" in output
    assert "SENTINEL_TOKEN_SECRET" not in output
    assert "foo=" not in output
    assert "token=" not in output
    assert "extra_arg=" not in output
    assert "?" not in output


def test_oauth_callback_trailing_slash_preserves_path_spelling_and_redacts_query():
    test_logger, stream = _create_test_access_logger()
    client = "127.0.0.1:12345"
    method = "GET"
    query = "code=SENTINEL_TRAILING_SLASH_CODE&state=SENTINEL_TRAILING_STATE"
    target = f"/api/integrations/jobber/callback/?{query}"
    http_version = "1.1"
    status = 200

    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        target,
        http_version,
        status,
    )

    output = stream.getvalue()
    assert client in output
    assert method in output
    assert "/api/integrations/jobber/callback/" in output
    assert "1.1" in output
    assert "200" in output
    assert "SENTINEL_TRAILING_SLASH_CODE" not in output
    assert "SENTINEL_TRAILING_STATE" not in output
    assert "?" not in output

    # Also test Google Calendar trailing slash
    stream.truncate(0)
    stream.seek(0)
    gcal_target = "/api/integrations/google-calendar/callback/?code=SENTINEL_GCAL_TRAILING"
    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        gcal_target,
        http_version,
        status,
    )
    output2 = stream.getvalue()
    assert "/api/integrations/google-calendar/callback/" in output2
    assert "SENTINEL_GCAL_TRAILING" not in output2
    assert "?" not in output2


def test_oauth_callback_no_query_preserved_unchanged():
    test_logger, stream = _create_test_access_logger()
    client = "127.0.0.1:12345"
    method = "GET"
    http_version = "1.1"
    status = 200

    # Jobber no query
    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        "/api/integrations/jobber/callback",
        http_version,
        status,
    )
    assert "/api/integrations/jobber/callback" in stream.getvalue()

    # Google Calendar no query
    stream.truncate(0)
    stream.seek(0)
    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        "/api/integrations/google-calendar/callback",
        http_version,
        status,
    )
    assert "/api/integrations/google-calendar/callback" in stream.getvalue()

    # Trailing slash no query
    stream.truncate(0)
    stream.seek(0)
    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        "/api/integrations/jobber/callback/",
        http_version,
        status,
    )
    assert "/api/integrations/jobber/callback/" in stream.getvalue()


def test_ordinary_access_records_preserved_unchanged():
    test_logger, stream = _create_test_access_logger()
    client = "10.1.1.5:9000"
    method = "GET"
    http_version = "1.1"
    status = 200

    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        method,
        "/api/contractors?filter=active&sort=desc",
        http_version,
        status,
    )
    output = stream.getvalue()
    assert client in output
    assert "/api/contractors?filter=active&sort=desc" in output
    assert "200" in output

    stream.truncate(0)
    stream.seek(0)
    test_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        client,
        "POST",
        "/api/calls",
        http_version,
        201,
    )
    assert "/api/calls" in stream.getvalue()


def test_unsupported_format_and_shapes_dropped_without_output_or_error():
    test_logger, stream = _create_test_access_logger()
    filter_instance = OAuthCallbackAccessLogFilter()

    # 1. Non-matching message format
    record_wrong_msg = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="GET /api/integrations/jobber/callback?code=SENTINEL_WRONG_MSG HTTP/1.1",
        args=(),
        exc_info=None,
    )
    assert filter_instance.filter(record_wrong_msg) is False
    test_logger.handle(record_wrong_msg)
    assert stream.getvalue() == ""
    assert "SENTINEL_WRONG_MSG" not in stream.getvalue()

    # 2. Too few arguments (4 instead of 5)
    record_few_args = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_4_ARGS", "1.1"),
        exc_info=None,
    )
    assert filter_instance.filter(record_few_args) is False
    test_logger.handle(record_few_args)
    assert stream.getvalue() == ""
    assert "SENTINEL_4_ARGS" not in stream.getvalue()

    # 3. Too many arguments (6 instead of 5)
    record_many_args = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1",
            "GET",
            "/api/integrations/jobber/callback?code=SENTINEL_6_ARGS",
            "1.1",
            200,
            "extra",
        ),
        exc_info=None,
    )
    assert filter_instance.filter(record_many_args) is False
    test_logger.handle(record_many_args)
    assert stream.getvalue() == ""
    assert "SENTINEL_6_ARGS" not in stream.getvalue()

    # 4. List arguments instead of tuple
    record_list_args = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=["127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_LIST_ARGS", "1.1", 200],
        exc_info=None,
    )
    assert filter_instance.filter(record_list_args) is False
    test_logger.handle(record_list_args)
    assert stream.getvalue() == ""
    assert "SENTINEL_LIST_ARGS" not in stream.getvalue()

    # 5. Non-tuple / None args
    record_none_args = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=None,
        exc_info=None,
    )
    assert filter_instance.filter(record_none_args) is False
    test_logger.handle(record_none_args)
    assert stream.getvalue() == ""


def test_unsupported_argument_types_dropped():
    test_logger, stream = _create_test_access_logger()
    filter_instance = OAuthCallbackAccessLogFilter()

    cases = [
        # Non-string client
        (127, "GET", "/api/integrations/jobber/callback?code=SENTINEL_INT_CLIENT", "1.1", 200),
        # Non-string method
        ("127.0.0.1", 100, "/api/integrations/jobber/callback?code=SENTINEL_INT_METHOD", "1.1", 200),
        # Non-string target
        ("127.0.0.1", "GET", ["/api/integrations/jobber/callback?code=SENTINEL_LIST_TARGET"], "1.1", 200),
        # Non-string http version
        ("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_FLOAT_VER", 1.1, 200),
        # String status instead of int
        ("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_STR_STATUS", "1.1", "200"),
        # Bool status (True)
        ("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_BOOL_TRUE", "1.1", True),
        # Bool status (False)
        ("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_BOOL_FALSE", "1.1", False),
    ]

    for args in cases:
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='%s - "%s %s HTTP/%s" %d',
            args=args,
            exc_info=None,
        )
        assert filter_instance.filter(record) is False
        test_logger.handle(record)
        assert stream.getvalue() == ""


def test_exception_and_stack_bearing_records_dropped():
    test_logger, stream = _create_test_access_logger()
    filter_instance = OAuthCallbackAccessLogFilter()

    # exc_info tuple
    record_exc = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_EXC_INFO", "1.1", 500),
        exc_info=(RuntimeError, RuntimeError("SENTINEL_ERROR_MSG"), None),
    )
    assert filter_instance.filter(record_exc) is False
    test_logger.handle(record_exc)
    assert stream.getvalue() == ""
    assert "SENTINEL_EXC_INFO" not in stream.getvalue()
    assert "SENTINEL_ERROR_MSG" not in stream.getvalue()

    # exc_text
    record_exc_text = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_EXC_TEXT", "1.1", 500),
        exc_info=None,
    )
    record_exc_text.exc_text = "Traceback with SENTINEL_TRACEBACK_SECRET"
    assert filter_instance.filter(record_exc_text) is False
    test_logger.handle(record_exc_text)
    assert stream.getvalue() == ""
    assert "SENTINEL_EXC_TEXT" not in stream.getvalue()
    assert "SENTINEL_TRACEBACK_SECRET" not in stream.getvalue()

    # stack_info
    record_stack = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/api/integrations/jobber/callback?code=SENTINEL_STACK_INFO", "1.1", 200),
        exc_info=None,
        sinfo="Stack trace with SENTINEL_STACK_SECRET",
    )
    assert filter_instance.filter(record_stack) is False
    test_logger.handle(record_stack)
    assert stream.getvalue() == ""
    assert "SENTINEL_STACK_INFO" not in stream.getvalue()
    assert "SENTINEL_STACK_SECRET" not in stream.getvalue()


def test_setup_logging_installs_filter_idempotently_and_preserves_existing():
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.filters.clear()
    uvicorn_access.handlers.clear()

    # Add existing pre-existing filter and handler
    existing_filter = logging.Filter(name="pre_existing_filter")
    existing_handler = logging.NullHandler()
    uvicorn_access.addFilter(existing_filter)
    uvicorn_access.addHandler(existing_handler)

    assert existing_filter in uvicorn_access.filters
    assert existing_handler in uvicorn_access.handlers

    # Call setup_logging once
    setup_logging()

    # Verify filter installed and pre-existing items preserved
    matching_filters = [f for f in uvicorn_access.filters if isinstance(f, OAuthCallbackAccessLogFilter)]
    assert len(matching_filters) == 1
    assert existing_filter in uvicorn_access.filters
    assert existing_handler in uvicorn_access.handlers

    # Call setup_logging second time
    setup_logging()

    # Verify idempotency (still exactly 1 instance, no duplicates)
    matching_filters_after = [f for f in uvicorn_access.filters if isinstance(f, OAuthCallbackAccessLogFilter)]
    assert len(matching_filters_after) == 1
    assert existing_filter in uvicorn_access.filters
    assert existing_handler in uvicorn_access.handlers


def test_all_supported_formatters_with_oauth_callback_filter():
    formatters = [
        ("standard", logging.Formatter("%(message)s")),
        ("json", JSONFormatter()),
        (
            "uvicorn_access",
            AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'),
        ),
        (
            "uvicorn_default",
            DefaultFormatter("%(levelprefix)s %(message)s"),
        ),
    ]

    for fmt_name, formatter in formatters:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)

        test_logger = logging.Logger(f"test.fmt.{fmt_name}.{uuid.uuid4().hex}", level=logging.INFO)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        test_logger.addFilter(OAuthCallbackAccessLogFilter())

        client = "127.0.0.1:54321"
        method = "GET"
        target = "/api/integrations/jobber/callback?code=SENTINEL_FMT_CODE&state=SENTINEL_FMT_STATE"
        http_version = "1.1"
        status = 200

        test_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            client,
            method,
            target,
            http_version,
            status,
        )

        output = stream.getvalue()
        assert "/api/integrations/jobber/callback" in output
        assert "SENTINEL_FMT_CODE" not in output
        assert "SENTINEL_FMT_STATE" not in output
        assert "code=" not in output
        assert "state=" not in output
        assert "?" not in output


def test_nonstandard_message_with_valid_arguments_is_suppressed():
    test_logger, stream = _create_test_access_logger(JSONFormatter())
    client = "127.0.0.1:54321"
    method = "GET"
    target = "/api/integrations/jobber/callback?code=SENTINEL_VALID_SHAPE_CODE&state=SENTINEL_VALID_SHAPE_STATE"
    http_version = "1.1"
    status = 200

    test_logger.info(
        'SENTINEL_UNSUPPORTED_LITERAL %s - "%s %s HTTP/%s" %d',
        client,
        method,
        target,
        http_version,
        status,
    )

    assert stream.getvalue() == ""
