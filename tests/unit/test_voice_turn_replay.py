"""Offline paired replay tests for Gemini turn detection experiments."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from websockets.exceptions import ConnectionClosedError

from app.services.voice_turn_replay import (
    APPROVED_QUALIFICATION_CORPUS_SHA256,
    APPROVED_QUALIFICATION_MANIFEST_SHA256,
    DEVELOPER_PROVIDER,
    RenderedVoiceTurn,
    VERTEX_PROVIDER,
    VoiceReplayAttempt,
    VoiceReplayInput,
    VoiceTurnBenchmarkThresholds,
    VoiceTurnObservation,
    build_gemini_activity_message,
    build_gemini_audio_message,
    build_gemini_setup_message,
    build_paired_schedule,
    build_replay_inputs,
    evaluate_gemini_provider_matrix,
    evaluate_voice_turn_benchmark,
    load_voice_turn_cases,
    render_voice_turn_case,
    voice_turn_manifest_identity,
)
import scripts.benchmark_gemini_turn_detection as benchmark_module
import scripts.qualify_gemini_live_providers as qualification_module
from scripts.benchmark_gemini_turn_detection import (
    _ProviderConnection,
    run_attempt,
    run_benchmark,
)
from scripts.qualify_gemini_live_providers import (
    MANIFEST as QUALIFICATION_MANIFEST,
    run_qualification_matrix,
)


FIXTURE_DIR = Path("tests/fixtures/voice_vad")
MANIFEST = FIXTURE_DIR / "turn_replay_manifest.json"
FLEURS_MANIFEST = FIXTURE_DIR / "fleurs_turn_replay_manifest.json"


def test_voice_turn_manifest_renders_live_codec_conditions():
    cases = load_voice_turn_cases(MANIFEST)

    assert len(cases) == 6
    rendered = render_voice_turn_case(cases[0])

    assert rendered.sample_rate_hz == 16_000
    assert rendered.speech_start_ms == 180
    assert rendered.speech_end_ms == 780
    assert rendered.duration_ms >= rendered.speech_end_ms + 500
    assert len(rendered.mulaw8) == rendered.duration_ms * 8


def test_fleurs_manifest_renders_two_labeled_languages():
    cases = load_voice_turn_cases(FLEURS_MANIFEST)

    assert len(cases) == 6
    assert {case.source_path.name for case in cases} == {
        "fleurs-en_us-test-row-0-trimmed.raw",
        "fleurs-es_419-test-row-0-trimmed.raw",
    }
    english = render_voice_turn_case(cases[0])
    spanish = render_voice_turn_case(cases[3])
    assert (english.speech_start_ms, english.speech_end_ms) == (500, 8_100)
    assert (spanish.speech_start_ms, spanish.speech_end_ms) == (500, 10_460)
    assert english.duration_ms == english.speech_end_ms + 500
    assert spanish.duration_ms == spanish.speech_end_ms + 500


def test_voice_turn_manifest_identity_is_stable_and_corpus_specific():
    legacy = voice_turn_manifest_identity(MANIFEST)
    fleurs = voice_turn_manifest_identity(FLEURS_MANIFEST)

    assert fleurs == voice_turn_manifest_identity(FLEURS_MANIFEST)
    assert fleurs["manifest_sha256"] == hashlib.sha256(
        FLEURS_MANIFEST.read_bytes()
    ).hexdigest()
    assert len(fleurs["corpus_sha256"]) == 64
    assert legacy["manifest_sha256"] != fleurs["manifest_sha256"]
    assert legacy["corpus_sha256"] != fleurs["corpus_sha256"]


def test_voice_turn_manifest_rejects_source_outside_fixture_directory(tmp_path):
    outside = tmp_path / "outside.raw"
    outside.write_bytes(b"\x00\x00" * 800)
    manifest = tmp_path / "fixtures" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        """{
          "version": 1,
          "source_pcm": "../outside.raw",
          "source_sample_rate_hz": 8000,
          "source_speech_start_ms": 10,
          "source_speech_end_ms": 50,
          "cases": [{"name": "escape"}]
        }"""
    )

    with pytest.raises(ValueError, match="source_pcm"):
        load_voice_turn_cases(manifest)


def test_voice_turn_manifest_supports_path_confined_multi_source_v2(tmp_path):
    sources = {}
    for source_id in ("en_us", "es_419"):
        source_path = tmp_path / f"{source_id}.raw"
        source_path.write_bytes(b"\x00\x00" * 8_000)
        sources[source_id] = {
            "source_pcm": source_path.name,
            "source_sha256_chunks": [
                hashlib.sha256(source_path.read_bytes()).hexdigest()
            ],
            "source_sample_rate_hz": 8_000,
            "source_speech_start_ms": 100,
            "source_speech_end_ms": 500,
        }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "sources": sources,
                "cases": [
                    {"name": "english", "source": "en_us"},
                    {"name": "spanish", "source": "es_419"},
                ],
            }
        )
    )

    cases = load_voice_turn_cases(manifest)

    assert [case.name for case in cases] == ["english", "spanish"]
    assert [case.source_path.name for case in cases] == ["en_us.raw", "es_419.raw"]


def test_voice_turn_manifest_v2_rejects_unknown_case_source(tmp_path):
    source_path = tmp_path / "source.raw"
    source_path.write_bytes(b"\x00\x00" * 8_000)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "sources": {
                    "known": {
                        "source_pcm": source_path.name,
                        "source_sha256_chunks": [
                            hashlib.sha256(source_path.read_bytes()).hexdigest()
                        ],
                        "source_sample_rate_hz": 8_000,
                        "source_speech_start_ms": 100,
                        "source_speech_end_ms": 500,
                    }
                },
                "cases": [{"name": "unknown_source", "source": "missing"}],
            }
        )
    )

    with pytest.raises(ValueError, match="case source"):
        load_voice_turn_cases(manifest)


def test_manual_replay_orders_activity_signals_around_identical_audio():
    case = load_voice_turn_cases(MANIFEST)[-1]
    rendered = render_voice_turn_case(case)

    automatic = build_replay_inputs(rendered, arm="automatic")
    manual = build_replay_inputs(rendered, arm="manual")

    automatic_audio = [event.audio for event in automatic if event.kind == "audio"]
    manual_audio = [event.audio for event in manual if event.kind == "audio"]
    assert automatic_audio == manual_audio
    assert manual[0].kind == "activity_start"
    assert manual[1].kind == "audio"
    assert manual[0].at_ms == manual[1].at_ms == 0
    assert [event.kind for event in manual].count("activity_end") == 1
    assert manual[-1].kind == "activity_end"
    assert manual[-1].at_ms == rendered.duration_ms
    assert rendered.duration_ms - rendered.speech_end_ms >= 500
    audio_events = [event for event in manual if event.kind == "audio"]
    assert sum(event.duration_ms for event in audio_events) == rendered.duration_ms
    endpoint_events = [
        event
        for event in audio_events
        if event.at_ms < rendered.speech_end_ms
        <= event.at_ms + event.duration_ms
    ]
    assert len(endpoint_events) == 1


def test_paired_schedule_is_balanced_reproducible_and_interleaved():
    first = build_paired_schedule(case_count=6, trials_per_case=5, seed=29)
    second = build_paired_schedule(case_count=6, trials_per_case=5, seed=29)

    assert first == second
    assert len(first) == 60
    assert sum(item.arm == "automatic" for item in first) == 30
    assert sum(item.arm == "manual" for item in first) == 30
    assert {
        (item.case_index, item.trial)
        for item in first
    } == {(case_index, trial) for case_index in range(6) for trial in range(1, 6)}
    for case_index in range(6):
        for trial in range(1, 6):
            arms = {
                item.arm
                for item in first
                if item.case_index == case_index and item.trial == trial
            }
            assert arms == {"automatic", "manual"}


def test_gemini_setup_requires_explicit_model_and_separates_vad_arms():
    with pytest.raises(ValueError, match="explicit"):
        build_gemini_setup_message(
            "gemini-2.5-flash-native-audio-latest",
            arm="automatic",
        )

    automatic = build_gemini_setup_message(
        "gemini-2.5-flash-native-audio-preview-12-2025",
        arm="automatic",
    )
    manual = build_gemini_setup_message(
        "gemini-2.5-flash-native-audio-preview-12-2025",
        arm="manual",
    )

    automatic_detection = automatic["setup"]["realtimeInputConfig"][
        "automaticActivityDetection"
    ]
    manual_detection = manual["setup"]["realtimeInputConfig"][
        "automaticActivityDetection"
    ]
    assert automatic_detection["silenceDurationMs"] == 500
    assert manual_detection == {"disabled": True}
    assert automatic["setup"]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0
    }


def test_vertex_setup_pins_resource_and_preserves_zero_retention():
    developer = build_gemini_setup_message(
        "gemini-3.1-flash-live-preview",
        arm="automatic",
        provider=DEVELOPER_PROVIDER,
    )
    vertex = build_gemini_setup_message(
        "gemini-live-2.5-flash-native-audio",
        arm="automatic",
        provider=VERTEX_PROVIDER,
        project="example-project",
        location="us-central1",
    )

    assert vertex["setup"]["model"] == (
        "projects/example-project/locations/us-central1/publishers/google/"
        "models/gemini-live-2.5-flash-native-audio"
    )
    assert (
        vertex["setup"]["realtimeInputConfig"]
        == developer["setup"]["realtimeInputConfig"]
    )
    assert "thinkingConfig" not in vertex["setup"]["generationConfig"]
    serialized = json.dumps(vertex)
    assert "sessionResumption" not in serialized
    assert "session_resumption" not in serialized
    assert "tools" not in vertex["setup"]


@pytest.mark.parametrize(
    ("project", "location"),
    [
        ("", "us-central1"),
        ("example-project/escape", "us-central1"),
        ("example-project", "global"),
    ],
)
def test_vertex_setup_rejects_unapproved_resource_scope(project, location):
    with pytest.raises(ValueError, match="Vertex"):
        build_gemini_setup_message(
            "gemini-live-2.5-flash-native-audio",
            arm="automatic",
            provider=VERTEX_PROVIDER,
            project=project,
            location=location,
        )


def test_provider_audio_envelopes_and_activity_events_are_explicit():
    developer = build_gemini_audio_message(
        b"public-fixture",
        provider=DEVELOPER_PROVIDER,
    )
    vertex = build_gemini_audio_message(
        b"public-fixture",
        provider=VERTEX_PROVIDER,
    )

    assert developer["realtimeInput"]["audio"]["mimeType"] == (
        "audio/pcm;rate=16000"
    )
    assert "mediaChunks" not in developer["realtimeInput"]
    assert vertex["realtimeInput"]["mediaChunks"] == [{
        "data": "cHVibGljLWZpeHR1cmU=",
        "mimeType": "audio/pcm;rate=16000",
    }]
    assert build_gemini_activity_message("activity_start") == {
        "realtimeInput": {"activityStart": {}}
    }
    assert build_gemini_activity_message("activity_end") == {
        "realtimeInput": {"activityEnd": {}}
    }
    with pytest.raises(ValueError, match="activity"):
        build_gemini_activity_message("audio_stream_end")


class _FakeProviderSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.activity_ended = asyncio.Event()

    async def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        if message.get("realtimeInput", {}).get("activityEnd") == {}:
            self.activity_ended.set()

    async def recv(self) -> str:
        return json.dumps({"setupComplete": {}})

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        await self.activity_ended.wait()
        yield json.dumps({
            "serverContent": {
                "modelTurn": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "audio/pcm;rate=24000",
                            "data": "cHVibGlj",
                        }
                    }]
                },
                "turnComplete": True,
            }
        })
        await asyncio.Event().wait()


class _ProviderErrorSocket(_FakeProviderSocket):
    async def _messages(self):
        await self.activity_ended.wait()
        yield json.dumps({
            "error": {
                "message": "sensitive-provider-detail",
                "request_id": "sensitive-request-id",
            }
        })


class _ProviderClosedSocket(_FakeProviderSocket):
    async def _messages(self):
        await self.activity_ended.wait()
        raise ConnectionClosedError(None, None)
        yield ""  # pragma: no cover


class _SetupTimeoutSocket(_FakeProviderSocket):
    async def recv(self) -> str:
        await asyncio.Event().wait()
        return ""  # pragma: no cover


class _NoResponseSocket(_FakeProviderSocket):
    async def _messages(self):
        await self.activity_ended.wait()
        await asyncio.Event().wait()
        yield ""  # pragma: no cover


class _NoTerminalSocket(_FakeProviderSocket):
    async def _messages(self):
        await self.activity_ended.wait()
        yield json.dumps({
            "serverContent": {
                "modelTurn": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "audio/pcm;rate=24000",
                            "data": "cHVibGlj",
                        }
                    }]
                }
            }
        })
        await asyncio.Event().wait()


class _ProviderSocketContext:
    def __init__(self, socket: _FakeProviderSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _FakeProviderSocket:
        return self.socket

    async def __aexit__(self, *_args) -> None:
        return None


def _stub_short_manual_replay(monkeypatch) -> None:
    rendered = RenderedVoiceTurn(
        mulaw8=b"",
        sample_rate_hz=16_000,
        speech_start_ms=0,
        speech_end_ms=1,
        duration_ms=1,
        frame_pattern_ms=(1,),
    )
    inputs = (
        VoiceReplayInput(kind="activity_start", at_ms=0),
        VoiceReplayInput(kind="audio", at_ms=0, audio=b"\x00\x00", duration_ms=1),
        VoiceReplayInput(kind="activity_end", at_ms=1),
    )
    monkeypatch.setattr(benchmark_module, "render_voice_turn_case", lambda _case: rendered)
    monkeypatch.setattr(
        benchmark_module,
        "build_replay_inputs",
        lambda _rendered, *, arm: inputs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "project", "location", "audio_field"),
    [
        (
            DEVELOPER_PROVIDER,
            "gemini-3.1-flash-live-preview",
            None,
            None,
            "audio",
        ),
        (
            VERTEX_PROVIDER,
            "gemini-live-2.5-flash-native-audio",
            "example-project",
            "us-central1",
            "mediaChunks",
        ),
    ],
)
async def test_provider_websocket_lifecycle_is_protocol_safe(
    monkeypatch,
    provider,
    model,
    project,
    location,
    audio_field,
):
    _stub_short_manual_replay(monkeypatch)
    socket = _FakeProviderSocket()
    monkeypatch.setattr(
        benchmark_module.websockets,
        "connect",
        lambda *_args, **_kwargs: _ProviderSocketContext(socket),
    )

    observation = await run_attempt(
        connection=_ProviderConnection(
            provider=provider,
            url="wss://provider.invalid",
            project=project,
            location=location,
        ),
        model=model,
        case=object(),
        attempt=VoiceReplayAttempt(case_index=0, trial=1, arm="manual"),
        response_timeout_seconds=0.1,
        terminal_timeout_seconds=0.1,
    )

    assert observation.error is None
    assert observation.turn_complete is True
    assert socket.sent[0]["setup"]["model"].endswith(model)
    audio_messages = [
        message
        for message in socket.sent
        if audio_field in message.get("realtimeInput", {})
    ]
    assert len(audio_messages) == 1


@pytest.mark.asyncio
async def test_provider_exception_details_are_not_retained(monkeypatch):
    _stub_short_manual_replay(monkeypatch)

    class FailedConnection:
        async def __aenter__(self):
            raise RuntimeError("sensitive-provider-detail")

        async def __aexit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(
        benchmark_module.websockets,
        "connect",
        lambda *_args, **_kwargs: FailedConnection(),
    )

    observation = await run_attempt(
        connection=_ProviderConnection(
            provider=DEVELOPER_PROVIDER,
            url="wss://provider.invalid?key=secret",
        ),
        model="gemini-3.1-flash-live-preview",
        case=object(),
        attempt=VoiceReplayAttempt(case_index=0, trial=1, arm="manual"),
        response_timeout_seconds=0.1,
        terminal_timeout_seconds=0.1,
    )

    assert observation.error == "provider_error"
    assert "sensitive-provider-detail" not in repr(observation)
    assert "secret" not in repr(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("socket", "expected_error"),
    [
        (_ProviderErrorSocket(), "provider_error"),
        (_ProviderClosedSocket(), "provider_closed"),
    ],
)
async def test_receiver_failures_use_bounded_error_codes(
    monkeypatch,
    socket,
    expected_error,
):
    _stub_short_manual_replay(monkeypatch)
    monkeypatch.setattr(
        benchmark_module.websockets,
        "connect",
        lambda *_args, **_kwargs: _ProviderSocketContext(socket),
    )

    observation = await run_attempt(
        connection=_ProviderConnection(
            provider=VERTEX_PROVIDER,
            url="wss://provider.invalid",
            project="example-project",
            location="us-central1",
        ),
        model="gemini-live-2.5-flash-native-audio",
        case=object(),
        attempt=VoiceReplayAttempt(case_index=0, trial=1, arm="manual"),
        response_timeout_seconds=0.1,
        terminal_timeout_seconds=0.1,
    )

    assert observation.error == expected_error
    assert "sensitive-provider-detail" not in repr(observation)
    assert "sensitive-request-id" not in repr(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("socket", "expected_error"),
    [
        (_NoResponseSocket(), "first_audio_timeout"),
        (_NoTerminalSocket(), "turn_complete_timeout"),
    ],
)
async def test_provider_wait_timeouts_are_phase_specific(
    monkeypatch,
    socket,
    expected_error,
):
    _stub_short_manual_replay(monkeypatch)
    monkeypatch.setattr(
        benchmark_module.websockets,
        "connect",
        lambda *_args, **_kwargs: _ProviderSocketContext(socket),
    )

    observation = await run_attempt(
        connection=_ProviderConnection(
            provider=VERTEX_PROVIDER,
            url="wss://provider.invalid",
            project="example-project",
            location="us-central1",
        ),
        model="gemini-live-2.5-flash-native-audio",
        case=object(),
        attempt=VoiceReplayAttempt(case_index=0, trial=1, arm="manual"),
        response_timeout_seconds=0.01,
        terminal_timeout_seconds=0.01,
    )

    assert observation.error == expected_error


@pytest.mark.asyncio
async def test_provider_setup_timeout_is_phase_specific(monkeypatch):
    _stub_short_manual_replay(monkeypatch)
    monkeypatch.setattr(benchmark_module, "SETUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        benchmark_module.websockets,
        "connect",
        lambda *_args, **_kwargs: _ProviderSocketContext(_SetupTimeoutSocket()),
    )

    observation = await run_attempt(
        connection=_ProviderConnection(
            provider=VERTEX_PROVIDER,
            url="wss://provider.invalid",
            project="example-project",
            location="us-central1",
        ),
        model="gemini-live-2.5-flash-native-audio",
        case=object(),
        attempt=VoiceReplayAttempt(case_index=0, trial=1, arm="manual"),
        response_timeout_seconds=0.01,
        terminal_timeout_seconds=0.01,
    )

    assert observation.error == "setup_timeout"


@pytest.mark.asyncio
async def test_network_benchmark_fails_closed_without_provider_credential(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    args = argparse.Namespace(
        manifest=MANIFEST,
        provider=DEVELOPER_PROVIDER,
        model="gemini-3.1-flash-live-preview",
        project=None,
        location=None,
        trials_per_case=1,
        max_provider_attempts=60,
        seed=29,
        min_attempts_per_arm=6,
        min_paired_attempts=6,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=1.0,
        terminal_timeout_seconds=1.0,
    )

    report = await run_benchmark(args)

    assert report == {
        "status": "fail",
        "error": "credential_unavailable",
        "decision_scope": "offline_diagnostic_only",
        "release_authorized": False,
    }


@pytest.mark.asyncio
async def test_network_benchmark_reports_corpus_identity(monkeypatch):
    async def fake_attempt(*, attempt, **_kwargs):
        return VoiceTurnObservation(
            case_index=attempt.case_index,
            trial=attempt.trial,
            arm=attempt.arm,
            first_audio_after_speech_end_ms=(
                1_900 if attempt.arm == "automatic" else 1_200
            ),
            first_audio_after_activity_end_ms=(
                None if attempt.arm == "automatic" else 700
            ),
            turn_complete=True,
            interruption_events=0,
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-only-placeholder")
    monkeypatch.setattr(
        "scripts.benchmark_gemini_turn_detection.run_attempt",
        fake_attempt,
    )
    args = argparse.Namespace(
        manifest=FLEURS_MANIFEST,
        provider=DEVELOPER_PROVIDER,
        model="gemini-3.1-flash-live-preview",
        project=None,
        location=None,
        trials_per_case=1,
        max_provider_attempts=60,
        seed=29,
        min_attempts_per_arm=6,
        min_paired_attempts=6,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=1.0,
        terminal_timeout_seconds=1.0,
    )

    report = await run_benchmark(args)

    assert report["status"] == "pass"
    identity = voice_turn_manifest_identity(FLEURS_MANIFEST)
    assert {
        key: report["configuration"][key]
        for key in identity
    } == identity
    assert report["configuration"]["provider"] == DEVELOPER_PROVIDER
    assert report["configuration"]["qualification_scope"] is False
    assert report["decision_scope"] == "offline_diagnostic_only"
    assert report["release_authorized"] is False


@pytest.mark.asyncio
async def test_vertex_benchmark_report_omits_project_and_bearer_token(monkeypatch):
    async def fake_attempt(*, attempt, **_kwargs):
        return VoiceTurnObservation(
            case_index=attempt.case_index,
            trial=attempt.trial,
            arm=attempt.arm,
            first_audio_after_speech_end_ms=(
                1_900 if attempt.arm == "automatic" else 1_200
            ),
            first_audio_after_activity_end_ms=(
                None if attempt.arm == "automatic" else 700
            ),
            turn_complete=True,
            interruption_events=0,
        )

    monkeypatch.setattr(
        "scripts.benchmark_gemini_turn_detection._load_vertex_access_token",
        lambda: "private-bearer-token",
    )
    monkeypatch.setattr(
        "scripts.benchmark_gemini_turn_detection.run_attempt",
        fake_attempt,
    )
    args = argparse.Namespace(
        manifest=FLEURS_MANIFEST,
        provider=VERTEX_PROVIDER,
        model="gemini-live-2.5-flash-native-audio",
        project="private-project-id",
        location="us-central1",
        trials_per_case=1,
        max_provider_attempts=60,
        seed=29,
        min_attempts_per_arm=6,
        min_paired_attempts=6,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=1.0,
        terminal_timeout_seconds=1.0,
    )

    report = await run_benchmark(args)

    assert report["status"] == "pass"
    assert report["configuration"]["provider"] == VERTEX_PROVIDER
    assert report["configuration"]["location"] == "us-central1"
    serialized = json.dumps(report)
    assert "private-project-id" not in serialized
    assert "private-bearer-token" not in serialized


@pytest.mark.asyncio
async def test_network_benchmark_enforces_per_run_attempt_ceiling(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-placeholder")
    args = argparse.Namespace(
        manifest=FLEURS_MANIFEST,
        provider=DEVELOPER_PROVIDER,
        model="gemini-3.1-flash-live-preview",
        project=None,
        location=None,
        trials_per_case=5,
        max_provider_attempts=59,
        seed=29,
        min_attempts_per_arm=30,
        min_paired_attempts=30,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=1.0,
        terminal_timeout_seconds=1.0,
    )

    report = await run_benchmark(args)

    assert report == {
        "status": "fail",
        "error": "attempt_limit_exceeded",
        "decision_scope": "offline_diagnostic_only",
        "release_authorized": False,
    }


@pytest.mark.asyncio
async def test_network_benchmark_stops_after_first_provider_error(monkeypatch):
    calls = 0

    async def failed_attempt(*, attempt, **_kwargs):
        nonlocal calls
        calls += 1
        return VoiceTurnObservation(
            case_index=attempt.case_index,
            trial=attempt.trial,
            arm=attempt.arm,
            first_audio_after_speech_end_ms=None,
            first_audio_after_activity_end_ms=None,
            turn_complete=False,
            interruption_events=0,
            error="provider_error",
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-only-placeholder")
    monkeypatch.setattr(
        "scripts.benchmark_gemini_turn_detection.run_attempt",
        failed_attempt,
    )
    args = argparse.Namespace(
        manifest=FLEURS_MANIFEST,
        provider=DEVELOPER_PROVIDER,
        model="gemini-3.1-flash-live-preview",
        project=None,
        location=None,
        trials_per_case=1,
        max_provider_attempts=12,
        seed=7,
        min_attempts_per_arm=6,
        min_paired_attempts=6,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=1.0,
        terminal_timeout_seconds=1.0,
    )

    report = await run_benchmark(args)

    assert report["status"] == "fail"
    assert report["sample"]["attempts"] == 1
    assert calls == 1


def _passing_observations() -> list[VoiceTurnObservation]:
    observations = []
    for pair in range(30):
        case_index = pair % 6
        trial = pair // 6 + 1
        observations.extend([
            VoiceTurnObservation(
                case_index=case_index,
                trial=trial,
                arm="automatic",
                first_audio_after_speech_end_ms=1900,
                first_audio_after_activity_end_ms=None,
                turn_complete=True,
                interruption_events=1 if pair < 5 else 0,
            ),
            VoiceTurnObservation(
                case_index=case_index,
                trial=trial,
                arm="manual",
                first_audio_after_speech_end_ms=1200,
                first_audio_after_activity_end_ms=700,
                turn_complete=True,
                interruption_events=0,
            ),
        ])
    return observations


def test_voice_turn_benchmark_passes_complete_aggregate_treatment():
    report = evaluate_voice_turn_benchmark(_passing_observations())

    assert report["status"] == "pass"
    assert report["sample"] == {
        "attempts": 60,
        "automatic_attempts": 30,
        "manual_attempts": 30,
        "paired_attempts": 30,
    }
    assert report["diagnostics"]["automatic"]["interruption_events"] == 5
    assert report["diagnostics"]["manual"] == {
        "completed_turns": 30,
        "completion_rate": 1.0,
        "premature_responses": 0,
        "interruption_events": 0,
        "speech_end_to_first_audio_p95_ms": 1200,
        "speech_end_to_first_audio_max_ms": 1200,
        "activity_end_to_first_audio_p95_ms": 700,
        "activity_end_to_first_audio_max_ms": 700,
        "errors": 0,
        "error_counts": {},
    }
    assert all(gate["passed"] for gate in report["gates"])
    assert "case_index" not in str(report)


def test_voice_turn_benchmark_fails_small_incomplete_or_premature_treatment():
    observations = _passing_observations()[:4]
    observations[1] = VoiceTurnObservation(
        case_index=0,
        trial=1,
        arm="manual",
        first_audio_after_speech_end_ms=-100,
        first_audio_after_activity_end_ms=None,
        turn_complete=False,
        interruption_events=1,
        error="provider_timeout",
    )
    thresholds = VoiceTurnBenchmarkThresholds(min_attempts_per_arm=2)

    report = evaluate_voice_turn_benchmark(observations, thresholds=thresholds)
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}

    assert report["status"] == "fail"
    assert {
        "manual_completion_rate",
        "manual_premature_responses",
        "manual_interruption_events",
        "manual_errors",
        "manual_latency_coverage",
        "manual_activity_end_latency_coverage",
    } <= failed
    assert report["diagnostics"]["manual"]["error_counts"] == {
        "provider_timeout": 1
    }


def _provider_report(
    provider: str,
    seed: int,
    *,
    status: str = "pass",
    automatic_p95_ms: int = 1_800,
    automatic_max_ms: int = 2_000,
) -> dict:
    model = {
        DEVELOPER_PROVIDER: "gemini-3.1-flash-live-preview",
        VERTEX_PROVIDER: "gemini-live-2.5-flash-native-audio",
    }[provider]
    return {
        "status": status,
        "release_authorized": False,
        "configuration": {
            "provider": provider,
            "model": model,
            "seed": seed,
            "qualification_scope": True,
            "manifest_sha256": APPROVED_QUALIFICATION_MANIFEST_SHA256,
            "corpus_sha256": APPROVED_QUALIFICATION_CORPUS_SHA256,
        },
        "diagnostics": {
            "automatic": {
                "speech_end_to_first_audio_p95_ms": automatic_p95_ms,
                "speech_end_to_first_audio_max_ms": automatic_max_ms,
            }
        },
    }


def test_provider_matrix_selects_lower_worse_seed_automatic_latency():
    reports = [
        _provider_report(DEVELOPER_PROVIDER, 29, automatic_p95_ms=1_700),
        _provider_report(DEVELOPER_PROVIDER, 41, automatic_p95_ms=1_900),
        _provider_report(VERTEX_PROVIDER, 29, automatic_p95_ms=1_650),
        _provider_report(VERTEX_PROVIDER, 41, automatic_p95_ms=1_800),
    ]

    decision = evaluate_gemini_provider_matrix(reports)

    assert decision["status"] == "pass"
    assert decision["decision"] == VERTEX_PROVIDER
    assert decision["providers"][DEVELOPER_PROVIDER][
        "worst_automatic_speech_end_to_first_audio_p95_ms"
    ] == 1_900
    assert decision["providers"][VERTEX_PROVIDER]["qualified"] is True
    assert decision["decision_scope"] == "offline_candidate_only"
    assert decision["release_authorized"] is False
    assert "diagnostics" not in decision


def test_provider_matrix_selects_only_provider_that_passes_both_seeds():
    reports = [
        _provider_report(DEVELOPER_PROVIDER, 29),
        _provider_report(DEVELOPER_PROVIDER, 41, status="fail"),
        _provider_report(VERTEX_PROVIDER, 29),
        _provider_report(VERTEX_PROVIDER, 41),
    ]

    decision = evaluate_gemini_provider_matrix(reports)

    assert decision["status"] == "pass"
    assert decision["decision"] == VERTEX_PROVIDER
    assert decision["providers"][DEVELOPER_PROVIDER]["failed_seeds"] == [41]


def test_provider_matrix_tie_is_inconclusive():
    reports = [
        _provider_report(DEVELOPER_PROVIDER, 29),
        _provider_report(DEVELOPER_PROVIDER, 41),
        _provider_report(VERTEX_PROVIDER, 29),
        _provider_report(VERTEX_PROVIDER, 41),
    ]

    decision = evaluate_gemini_provider_matrix(reports)

    assert decision["status"] == "fail"
    assert decision["decision"] == "inconclusive_tie"


def test_provider_matrix_rejects_mixed_corpora_or_incomplete_seed_set():
    mixed = [
        _provider_report(DEVELOPER_PROVIDER, 29),
        _provider_report(DEVELOPER_PROVIDER, 41),
        _provider_report(VERTEX_PROVIDER, 29),
        _provider_report(VERTEX_PROVIDER, 41),
    ]
    mixed[-1]["configuration"]["corpus_sha256"] = "c" * 64

    with pytest.raises(ValueError, match="approved corpus"):
        evaluate_gemini_provider_matrix(mixed)
    with pytest.raises(ValueError, match="matrix"):
        evaluate_gemini_provider_matrix(mixed[:-1])


def _smoke_report(provider: str, *, ready: bool = True) -> dict:
    return {
        "status": "pass" if ready else "fail",
        "configuration": {
            "provider": provider,
            "model": {
                DEVELOPER_PROVIDER: "gemini-3.1-flash-live-preview",
                VERTEX_PROVIDER: "gemini-live-2.5-flash-native-audio",
            }[provider],
        },
        "sample": {
            "attempts": 12,
            "automatic_attempts": 6,
            "manual_attempts": 6,
            "paired_attempts": 6,
        },
        "diagnostics": {
            "automatic": {
                "completed_turns": 6 if ready else 5,
                "errors": 0 if ready else 1,
                "error_counts": {} if ready else {"provider_closed": 1},
                "speech_end_to_first_audio_p95_ms": 1_800,
                "speech_end_to_first_audio_max_ms": 2_000,
            },
            "manual": {
                "completed_turns": 6,
                "errors": 0,
                "error_counts": {},
                "speech_end_to_first_audio_p95_ms": 1_200,
                "speech_end_to_first_audio_max_ms": 1_400,
                "activity_end_to_first_audio_p95_ms": 700,
                "activity_end_to_first_audio_max_ms": 800,
            },
        },
    }


def test_smoke_summary_exposes_only_bounded_error_counts():
    report = _smoke_report(VERTEX_PROVIDER, ready=False)
    report["diagnostics"]["automatic"]["error_counts"] = {
        "provider_closed": 1,
        "sensitive-provider-detail": 2,
        "provider_timeout": True,
    }

    summary = qualification_module._smoke_summary(report)

    assert summary["automatic"]["error_counts"] == {
        "other": 2,
        "provider_closed": 1,
    }
    assert "sensitive-provider-detail" not in json.dumps(summary)


@pytest.mark.asyncio
async def test_qualification_matrix_stops_after_incomplete_smoke_from_any_cwd(
    monkeypatch,
    tmp_path,
):
    calls = []

    async def fake_benchmark(args):
        calls.append(
            (
                args.provider,
                args.seed,
                args.trials_per_case,
                args.qualification_mode,
                args.manifest,
            )
        )
        return _smoke_report(
            args.provider,
            ready=args.provider == DEVELOPER_PROVIDER,
        )

    monkeypatch.setattr(
        "scripts.qualify_gemini_live_providers.run_benchmark",
        fake_benchmark,
    )
    monkeypatch.chdir(tmp_path)

    result = await run_qualification_matrix(project="private-project-id")

    assert result["status"] == "fail"
    assert result["decision"] == "smoke_blocked"
    assert result["attempt_ceiling"] == 264
    assert result["decision_scope"] == "offline_candidate_only"
    assert result["release_authorized"] is False
    assert len(calls) == 2
    assert QUALIFICATION_MANIFEST.is_absolute()
    assert all(call[3] is True for call in calls)
    assert all(call[4] == QUALIFICATION_MANIFEST for call in calls)
    assert result["smoke"][VERTEX_PROVIDER]["automatic"]["error_counts"] == {
        "provider_closed": 1
    }
    assert "private-project-id" not in json.dumps(result)


@pytest.mark.asyncio
async def test_qualification_matrix_rejects_mutated_corpus_before_network(
    monkeypatch,
    tmp_path,
):
    fixture_copy = tmp_path / "voice_vad"
    shutil.copytree(FIXTURE_DIR, fixture_copy)
    mutated_manifest = fixture_copy / FLEURS_MANIFEST.name
    mutated_manifest.write_bytes(mutated_manifest.read_bytes() + b"\n")

    async def unexpected_benchmark(_args):
        raise AssertionError("network benchmark must not start")

    monkeypatch.setattr(qualification_module, "MANIFEST", mutated_manifest)
    monkeypatch.setattr(qualification_module, "run_benchmark", unexpected_benchmark)

    with pytest.raises(ValueError, match="approved corpus"):
        await qualification_module.run_qualification_matrix(
            project="private-project-id"
        )


@pytest.mark.asyncio
async def test_qualification_matrix_runs_fixed_264_attempt_program(monkeypatch):
    calls = []

    async def fake_benchmark(args):
        calls.append(
            (
                args.provider,
                args.seed,
                args.trials_per_case,
                args.qualification_mode,
            )
        )
        if args.trials_per_case == 1:
            return _smoke_report(args.provider)
        p95 = 1_700 if args.provider == VERTEX_PROVIDER else 1_900
        return _provider_report(
            args.provider,
            args.seed,
            automatic_p95_ms=p95,
            automatic_max_ms=p95 + 200,
        )

    monkeypatch.setattr(
        "scripts.qualify_gemini_live_providers.run_benchmark",
        fake_benchmark,
    )

    result = await run_qualification_matrix(project="private-project-id")

    assert result["status"] == "pass"
    assert result["decision"] == VERTEX_PROVIDER
    assert result["attempt_ceiling"] == 264
    assert result["attempts_scheduled"] == 264
    assert result["decision_scope"] == "offline_candidate_only"
    assert result["release_authorized"] is False
    assert len(calls) == 6
    assert {call[1] for call in calls if call[2] == 5} == {29, 41}
    assert all(call[3] is True for call in calls)
    assert "private-project-id" not in json.dumps(result)
