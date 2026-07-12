"""Offline paired replay tests for Gemini turn detection experiments."""

import argparse
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
)
from scripts.benchmark_gemini_turn_detection import run_benchmark


FIXTURE_DIR = Path("tests/fixtures/voice_vad")
MANIFEST = FIXTURE_DIR / "turn_replay_manifest.json"


def test_voice_turn_manifest_renders_live_codec_conditions():
    cases = load_voice_turn_cases(MANIFEST)

    assert len(cases) == 6
    rendered = render_voice_turn_case(cases[0])

    assert rendered.sample_rate_hz == 16_000
    assert rendered.speech_start_ms == 180
    assert rendered.speech_end_ms == 780
    assert rendered.duration_ms >= rendered.speech_end_ms + 500
    assert len(rendered.mulaw8) == rendered.duration_ms * 8


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
