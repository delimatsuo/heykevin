"""Aggregate-only Deepgram Flux turn-detection feasibility tests."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import scripts.benchmark_deepgram_flux_turn_detection as flux_benchmark
from scripts.benchmark_deepgram_flux_turn_detection import (
    _AttemptState,
    _receive_flux_events,
    build_flux_scenarios,
    build_flux_url,
    evaluate_flux_observations,
    FluxObservation,
    FluxThresholds,
    run_benchmark,
)


FIXTURE_MANIFEST = Path("tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json")


def test_flux_scenarios_are_deterministic_bilingual_and_transcript_free():
    first = build_flux_scenarios(
        FIXTURE_MANIFEST,
        pause_durations_ms=(500, 800, 1_200),
    )
    second = build_flux_scenarios(
        FIXTURE_MANIFEST,
        pause_durations_ms=(500, 800, 1_200),
    )

    assert first == second
    assert len(first) == 8
    assert {scenario.language for scenario in first} == {"en-US", "es-419"}
    assert sum(scenario.pause_duration_ms == 0 for scenario in first) == 2
    assert sum(scenario.pause_duration_ms > 0 for scenario in first) == 6
    assert all(scenario.sample_rate_hz == 16_000 for scenario in first)
    assert all(scenario.pcm16 for scenario in first)
    assert all(not hasattr(scenario, "transcript") for scenario in first)
    for scenario in first:
        assert len(scenario.pcm16) == scenario.duration_ms * 32
        assert 0 < scenario.speech_end_ms <= scenario.duration_ms
        if scenario.pause_duration_ms:
            assert 0 < scenario.pause_start_ms < scenario.speech_end_ms
            assert scenario.pause_end_ms == (scenario.pause_start_ms + scenario.pause_duration_ms)
            pause_start = scenario.pause_start_ms * 32
            pause_end = scenario.pause_end_ms * 32
            assert scenario.pcm16[pause_start:pause_end] == (b"\x00" * (pause_end - pause_start))


def test_flux_url_is_explicit_bounded_and_contains_no_credential():
    url = build_flux_url(
        model="flux-general-multi",
        eot_threshold=0.7,
        eot_timeout_ms=5_000,
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "api.deepgram.com"
    assert parsed.path == "/v2/listen"
    assert query == {
        "encoding": ["linear16"],
        "eot_threshold": ["0.7"],
        "eot_timeout_ms": ["5000"],
        "mip_opt_out": ["true"],
        "model": ["flux-general-multi"],
        "sample_rate": ["16000"],
    }
    assert "key" not in query
    assert "token" not in url.lower()


@pytest.mark.parametrize(
    ("model", "threshold", "timeout_ms"),
    [
        ("flux-general-en", 0.7, 5_000),
        ("flux-general-multi", 0.49, 5_000),
        ("flux-general-multi", 0.91, 5_000),
        ("flux-general-multi", 0.7, 499),
        ("flux-general-multi", 0.7, 10_001),
    ],
)
def test_flux_url_rejects_unqualified_configuration(
    model,
    threshold,
    timeout_ms,
):
    with pytest.raises(ValueError):
        build_flux_url(
            model=model,
            eot_threshold=threshold,
            eot_timeout_ms=timeout_ms,
        )


@pytest.mark.asyncio
async def test_flux_receiver_erases_provider_payload_and_uses_safe_error_codes(
    caplog,
):
    private_transcript = "private caller transcript sentinel"
    private_request_id = "private-provider-request-identifier"
    private_error = "private provider error with account details"
    websocket = _FakeFluxWebSocket(
        [
            {"type": "Connected", "request_id": private_request_id},
            {
                "type": "TurnInfo",
                "event": "EndOfTurn",
                "transcript": private_transcript,
                "languages": ["en"],
                "request_id": private_request_id,
            },
            {
                "type": "Error",
                "code": "PRIVATE_PROVIDER_CODE",
                "description": private_error,
            },
        ]
    )
    state = _AttemptState()

    await _receive_flux_events(
        websocket,
        state,
        expected_language="en-US",
        clock=lambda: 10.25,
    )

    assert state.connected.is_set()
    assert state.end_of_turn_arrivals == [10.25]
    assert state.language_matches == [True]
    assert state.error == "provider_error"
    assert set(vars(state)) == {
        "connected",
        "updated",
        "end_of_turn_arrivals",
        "language_matches",
        "error",
    }
    serialized = json.dumps(
        {
            "arrivals": state.end_of_turn_arrivals,
            "matches": state.language_matches,
            "error": state.error,
        }
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    for private_value in (
        private_transcript,
        private_request_id,
        private_error,
        "PRIVATE_PROVIDER_CODE",
    ):
        assert private_value not in serialized
        assert private_value not in messages


def test_flux_evaluator_advances_clean_feasibility_without_approving_live_use():
    observations = [
        FluxObservation(
            language="en-US",
            pause_duration_ms=0,
            premature_end_count=0,
            decision_latency_ms=240,
            language_match=True,
        ),
        FluxObservation(
            language="es-419",
            pause_duration_ms=800,
            premature_end_count=0,
            decision_latency_ms=320,
            language_match=True,
        ),
    ]

    report = evaluate_flux_observations(
        observations,
        thresholds=FluxThresholds(min_attempts=2),
    )

    assert report["status"] == "pass"
    assert report["candidate_decision"] == "advance_to_offline_corpus"
    assert report["qualification"]["eligible"] is False
    assert set(report["qualification"]["blockers"]) == {
        "development_corpus_incomplete",
        "holdout_unavailable",
        "hosted_network_egress_unapproved",
        "model_revision_unreported",
    }
    assert report["sample"] == {
        "attempts": 2,
        "decisions": 2,
        "premature_ends": 0,
        "missing_decisions": 0,
        "provider_errors": 0,
        "language_matches": 2,
    }
    assert report["metrics"]["speech_end_to_decision_p95_ms"] == 320
    assert report["metrics"]["speech_end_to_decision_max_ms"] == 320
    assert set(report["buckets"]["languages"]) == {"en-US", "es-419"}
    assert set(report["buckets"]["scenarios"]) == {
        "baseline",
        "internal_pause_500ms",
        "internal_pause_800ms",
        "internal_pause_1200ms",
    }
    assert report["buckets"]["languages"]["en-US"]["attempts"] == 1
    assert report["buckets"]["scenarios"]["internal_pause_800ms"]["premature_ends"] == 0


def test_flux_evaluator_rejects_premature_missing_slow_and_error_evidence():
    observations = [
        FluxObservation(
            language="en-US",
            pause_duration_ms=500,
            premature_end_count=1,
            decision_latency_ms=900,
            language_match=False,
        ),
        FluxObservation(
            language="es-419",
            pause_duration_ms=0,
            premature_end_count=0,
            decision_latency_ms=None,
            language_match=None,
            error="provider_timeout",
        ),
    ]

    report = evaluate_flux_observations(
        observations,
        thresholds=FluxThresholds(min_attempts=2),
    )
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert report["candidate_decision"] == "reject"
    assert gates["premature_semantic_ends"]["passed"] is False
    assert gates["missing_semantic_ends_or_errors"]["passed"] is False
    assert gates["semantic_decision_coverage"]["passed"] is False
    assert gates["speech_end_to_decision_p95_ms"]["passed"] is False
    assert gates["speech_end_to_decision_max_ms"]["passed"] is False


@pytest.mark.asyncio
async def test_flux_benchmark_fails_safely_without_credential():
    args = argparse.Namespace()

    report = await run_benchmark(args, api_key="")

    assert report == {"status": "fail", "error": "credential_unavailable"}


def test_flux_evaluator_rejects_non_allowlisted_bucket_labels():
    observation = FluxObservation(
        language="caller-provided-private-label",
        pause_duration_ms=0,
        premature_end_count=0,
        decision_latency_ms=100,
        language_match=True,
    )

    with pytest.raises(ValueError, match="language is not allowlisted"):
        evaluate_flux_observations(
            [observation],
            thresholds=FluxThresholds(min_attempts=1),
        )


@pytest.mark.asyncio
async def test_flux_attempt_keeps_credential_in_header_and_classifies_events(
    monkeypatch,
):
    release_events = asyncio.Event()
    timing = {}
    websocket = _CapturingFluxWebSocket()
    connection = _FakeFluxConnection(websocket)

    def connect(url, **kwargs):
        connection.url = url
        connection.kwargs = kwargs
        return connection

    async def receive_events(
        _websocket,
        state,
        *,
        expected_language,
    ):
        assert expected_language == "en-US"
        state.connected.set()
        await release_events.wait()
        started_at = timing["started_at"]
        state.end_of_turn_arrivals.extend([started_at + 0.5, started_at + 1.24])
        state.language_matches.extend([True, True])
        await asyncio.Future()

    async def stream_scenario(_websocket, _scenario, started_at):
        timing["started_at"] = started_at
        release_events.set()
        await asyncio.sleep(0)

    async def continue_silence(*_args, **_kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(flux_benchmark.websockets, "connect", connect)
    monkeypatch.setattr(flux_benchmark, "_receive_flux_events", receive_events)
    monkeypatch.setattr(flux_benchmark, "_stream_scenario", stream_scenario)
    monkeypatch.setattr(
        flux_benchmark,
        "_continue_silence_until_decision",
        continue_silence,
    )
    scenario = flux_benchmark.FluxScenario(
        scenario_id="en_us_test",
        language="en-US",
        pcm16=b"\x00" * (1_500 * 32),
        sample_rate_hz=16_000,
        speech_end_ms=1_000,
        duration_ms=1_500,
    )

    observation = await flux_benchmark.run_flux_attempt(
        api_key="credential-sentinel",  # pragma: allowlist secret
        scenario=scenario,
        url=flux_benchmark.build_flux_url(
            model="flux-general-multi",
            eot_threshold=0.7,
            eot_timeout_ms=5_000,
        ),
        connect_timeout_seconds=1,
        decision_timeout_seconds=1,
    )

    assert "credential-sentinel" not in connection.url
    assert connection.kwargs["additional_headers"] == {"Authorization": "Token credential-sentinel"}
    assert observation == flux_benchmark.FluxObservation(
        language="en-US",
        pause_duration_ms=0,
        premature_end_count=1,
        decision_latency_ms=240,
        language_match=True,
    )
    assert websocket.sent == [json.dumps({"type": "CloseStream"})]


@pytest.mark.asyncio
async def test_flux_stream_uses_recommended_80ms_binary_chunks(monkeypatch):
    websocket = _CapturingFluxWebSocket()
    sleep_targets = []

    async def no_sleep(target):
        sleep_targets.append(target)
        await asyncio.sleep(0)

    monkeypatch.setattr(flux_benchmark, "_sleep_until", no_sleep)
    scenario = flux_benchmark.FluxScenario(
        scenario_id="en_us_chunk_test",
        language="en-US",
        pcm16=b"\x01\x00" * (16_000 * 160 // 1_000),
        sample_rate_hz=16_000,
        speech_end_ms=80,
        duration_ms=160,
    )

    await flux_benchmark._stream_scenario(
        websocket,
        scenario,
        started_at=1.0,
    )

    assert len(websocket.sent) == 2
    assert all(isinstance(chunk, bytes) for chunk in websocket.sent)
    assert all(len(chunk) == 2_560 for chunk in websocket.sent)
    assert sleep_targets == pytest.approx([1.08, 1.16])


class _FakeFluxWebSocket:
    def __init__(self, messages: list[dict]):
        self._messages = [json.dumps(message) for message in messages]

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _CapturingFluxWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class _FakeFluxConnection:
    def __init__(self, websocket):
        self.websocket = websocket
        self.url = None
        self.kwargs = None

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        return False
