import json

from app.utils import logging as log_utils


def test_trace_event_emits_canonical_json_fields_without_raw_text(capsys):
    log_utils.setup_logging("INFO")
    logger = log_utils.get_logger("tests.trace")

    log_utils.trace_event(
        logger,
        "voice_turn_llm_end",
        call_sid="CA123",
        contractor_id="contractor-1234567890",
        caller_phone="+16504228667",
        turn_id=3,
        stage="llm",
        status="ok",
        duration_ms=421,
        provider="anthropic",
        utterance_chars=48,
        raw_text="caller said my card number is 4111 1111 1111 1111",
    )

    logged = json.loads(capsys.readouterr().out)
    assert logged["message"] == "call_trace"
    assert logged["event"] == "voice_turn_llm_end"
    assert logged["call_sid"] == "CA123"
    assert logged["contractor_id"] == "contractor-1234567890"
    assert logged["caller_phone"] == "***8667"
    assert logged["turn_id"] == 3
    assert logged["stage"] == "llm"
    assert logged["status"] == "ok"
    assert logged["duration_ms"] == 421
    assert logged["provider"] == "anthropic"
    assert logged["utterance_chars"] == 48
    assert "raw_text" not in logged

    serialized = json.dumps(logged)
    assert "4111 1111 1111 1111" not in serialized
    assert "caller said" not in serialized


def test_trace_event_emits_no_spoken_response_diagnostics(capsys):
    log_utils.setup_logging("INFO")
    logger = log_utils.get_logger("tests.trace")

    log_utils.trace_event(
        logger,
        "voice_turn_no_spoken_response",
        call_sid="CA123",
        contractor_id="contractor-1234567890",
        turn_id=9,
        stage="llm",
        status="fallback",
        reason="empty_response",
        stop_reason="end_turn",
        content_block_types=[],
        has_caller_name=True,
        has_callback_number=True,
        has_issue=True,
        has_service_area=True,
        urgency_detected=False,
        raw_text="do not log this",
    )

    logged = json.loads(capsys.readouterr().out)

    assert logged["event"] == "voice_turn_no_spoken_response"
    assert logged["stop_reason"] == "end_turn"
    assert logged["content_block_types"] == []
    assert logged["has_caller_name"] is True
    assert logged["has_callback_number"] is True
    assert logged["has_issue"] is True
    assert logged["has_service_area"] is True
    assert logged["urgency_detected"] is False
    assert "raw_text" not in logged
    assert "do not log this" not in json.dumps(logged)
