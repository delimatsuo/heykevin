"""Offline calibration gates for caller-activity endpoint control."""

import argparse
from hashlib import sha256
from pathlib import Path

from app.services.caller_activity_replay import evaluate_caller_activity_replay
from app.services.voice_turn_replay import (
    VoiceTurnReplayCase,
    load_voice_turn_cases,
    voice_turn_manifest_identity,
)
from scripts.evaluate_caller_activity_replay import run_evaluation


FLEURS_MANIFEST = Path(
    "tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json"
)


def test_current_webrtc_settings_fail_closed_on_labeled_fleurs_corpus():
    report = evaluate_caller_activity_replay(
        load_voice_turn_cases(FLEURS_MANIFEST),
    )

    assert report["status"] == "fail"
    assert report["sample"] == {"cases": 6}
    assert report["diagnostics"]["single_segment_cases"] == 0
    assert report["diagnostics"]["false_pre_roll_starts"] == 3
    assert report["diagnostics"]["premature_end_events"] == 9
    assert report["diagnostics"]["boundary_coverage_rate"] == 1.0
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}
    assert failed == {
        "single_segment_coverage",
        "false_pre_roll_starts",
        "premature_end_events",
    }
    assert "case_index" not in str(report)


def test_caller_activity_replay_passes_one_clean_labeled_segment(tmp_path):
    source = (
        b"\x00\x00" * 4_000
        + b"\xe8\x03" * 4_000
        + b"\x00\x00" * 4_000
    )
    source_path = tmp_path / "clean.raw"
    source_path.write_bytes(source)
    case = VoiceTurnReplayCase(
        name="clean",
        source_path=source_path,
        source_sha256=sha256(source).hexdigest(),
        source_sample_rate_hz=8_000,
        source_speech_start_ms=500,
        source_speech_end_ms=1_000,
    )

    report = evaluate_caller_activity_replay(
        [case],
        classifier_factory=lambda _mode: lambda pcm, _rate: any(pcm),
    )

    assert report["status"] == "pass"
    assert report["diagnostics"] == {
        "single_segment_cases": 1,
        "false_pre_roll_starts": 0,
        "premature_end_events": 0,
        "missing_start_cases": 0,
        "missing_final_end_cases": 0,
        "boundary_coverage_rate": 1.0,
        "start_absolute_error_p95_ms": 20,
        "start_absolute_error_max_ms": 20,
        "final_end_absolute_error_p95_ms": 0,
        "final_end_absolute_error_max_ms": 0,
        "endpoint_confirmation_delay_ms": 300,
    }
    assert all(gate["passed"] for gate in report["gates"])


def test_caller_activity_cli_report_includes_corpus_identity():
    args = argparse.Namespace(
        manifest=FLEURS_MANIFEST,
        mode=2,
        min_speech_frames=3,
        end_silence_frames=15,
        boundary_tolerance_ms=150,
        max_endpoint_confirmation_delay_ms=500,
    )

    report = run_evaluation(args)

    identity = voice_turn_manifest_identity(FLEURS_MANIFEST)
    assert report["status"] == "fail"
    assert {
        key: report["configuration"][key]
        for key in identity
    } == identity
