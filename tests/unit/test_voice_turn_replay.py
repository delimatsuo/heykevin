"""Offline paired replay tests for Gemini turn detection experiments."""

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from app.services.voice_turn_replay import (
    VoiceTurnBenchmarkThresholds,
    VoiceTurnObservation,
    build_gemini_setup_message,
    build_paired_schedule,
    build_replay_inputs,
    evaluate_voice_turn_benchmark,
    load_voice_turn_cases,
    render_voice_turn_case,
    voice_turn_manifest_identity,
)
from scripts.benchmark_gemini_turn_detection import run_benchmark


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


@pytest.mark.asyncio
async def test_network_benchmark_fails_closed_without_provider_credential(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    args = argparse.Namespace(
        manifest=MANIFEST,
        model="gemini-3.1-flash-live-preview",
        trials_per_case=1,
        seed=29,
        min_attempts_per_arm=6,
        min_paired_attempts=6,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=1.0,
        terminal_timeout_seconds=1.0,
    )

    report = await run_benchmark(args)

    assert report == {"status": "fail", "error": "credential_unavailable"}


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
        model="gemini-3.1-flash-live-preview",
        trials_per_case=1,
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
