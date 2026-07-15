"""Tests for dry-run-first Gemini caller-turn qualification."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts.qualify_gemini_caller_turn_assembly import (
    OFFICIAL_ENDPOINTS,
    QualificationConfig,
    QualificationError,
    build_preregistration,
    canonical_json_sha256,
    canonicalize_qualification_setup,
    run_qualification,
    run_session_attempt,
    validate_audio_manifest,
)


SOURCE_SHA = "b" * 40
MODEL_RESOURCE = "models/gemini-3.1-flash-live-preview"
ENDPOINT = next(iter(OFFICIAL_ENDPOINTS))
CURRENT_MANIFEST = Path("tests/fixtures/caller_turn_audio/manifest.json")
RUNNER_PATH = Path("scripts/qualify_gemini_caller_turn_assembly.py")
EVALUATOR_PATH = Path("scripts/evaluate_caller_turn_assembly.py")
PIPELINE_PATH = Path("app/services/gemini_pipeline.py")


def _file_sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _provider_setup() -> dict[str, object]:
    document = {
        "api_version": "v1beta",
        "endpoint": ENDPOINT,
        "setup": {
            "model": MODEL_RESOURCE,
            "generation_config": {
                "response_modalities": ["AUDIO"],
                "temperature": 0.2,
                "speech_config": {
                    "voice_config": {
                        "prebuilt_voice_config": {"voice_name": "Puck"}
                    }
                },
                "thinking_config": {"thinking_level": "minimal"},
            },
            "system_instruction": {
                "parts": [{"text": "Synthetic qualification prompt only"}]
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "start_of_speech_sensitivity": "START_SENSITIVITY_HIGH",
                    "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                    "prefix_padding_ms": 100,
                    "silence_duration_ms": 500,
                },
                "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
                "turn_coverage": "TURN_INCLUDES_ONLY_ACTIVITY",
            },
            "tools": [{"function_declarations": [{"name": "synthetic_lookup"}]}],
        },
        "synthetic_prompt_fixture_sha256": "c" * 64,
        "tool_response_policy": "mock_responses_only",
        "reconnect_policy": {
            "max_attempts": 1,
            "context_restoration": "synthetic_transcript_digest_only",
            "retry_backoff_ms": [0],
        },
        "turn_assembly_policy": {"quiescence_ms": 250},
        "websocket_policy": {
            "max_message_bytes": 1048576,
            "open_timeout_seconds": 5,
            "setup_timeout_seconds": 5,
            "ping_interval_seconds": 10,
            "ping_timeout_seconds": 5,
            "close_timeout_seconds": 1,
        },
        "runner_identity": {
            "source_sha": SOURCE_SHA,
            "file_sha256": _file_sha(RUNNER_PATH),
        },
        "evaluator_identity": {
            "source_sha": SOURCE_SHA,
            "file_sha256": _file_sha(EVALUATOR_PATH),
        },
        "immutable_pipeline_identity": {
            "source_sha": SOURCE_SHA,
            "file_sha256": _file_sha(PIPELINE_PATH),
        },
    }
    setup = document["setup"]
    document["immutable_pipeline_setup"] = {
        "api_version": "v1beta",
        "endpoint": ENDPOINT,
        "model_resource": "models/gemini-2.5-flash-native-audio-latest",
        "system_instruction_sha256": canonical_json_sha256(
            setup["system_instruction"]
        ),
        "synthetic_prompt_fixture_sha256": "c" * 64,
        "generation_config": {
            **setup["generation_config"],
            "temperature": 0.4,
            "thinking_config": {"thinking_budget": 0},
        },
        "input_audio_transcription": setup["input_audio_transcription"],
        "output_audio_transcription": setup["output_audio_transcription"],
        "realtime_input_config": setup["realtime_input_config"],
        "tool_declarations_sha256": canonical_json_sha256(setup["tools"]),
        "tool_response_policy": "live_tool_execution",
        "reconnect_policy": {
            "max_attempts": 1,
            "context_restoration": "bounded_transcript_text",
            "retry_backoff_ms": [0],
        },
        "turn_assembly_policy": {"quiescence_ms": None},
        "websocket_policy": {
            "max_message_bytes": 10485760,
            "open_timeout_seconds": 10,
            "setup_timeout_seconds": 10,
            "ping_interval_seconds": 20,
            "ping_timeout_seconds": 20,
            "close_timeout_seconds": 10,
        },
    }
    return document


def _behavior_projection(document: dict[str, object]) -> dict[str, object]:
    setup = document["setup"]
    return {
        "api_version": document["api_version"],
        "endpoint": document["endpoint"],
        "model_resource": setup["model"],
        "system_instruction_sha256": canonical_json_sha256(
            setup["system_instruction"]
        ),
        "synthetic_prompt_fixture_sha256": document[
            "synthetic_prompt_fixture_sha256"
        ],
        "generation_config": setup["generation_config"],
        "input_audio_transcription": setup["input_audio_transcription"],
        "output_audio_transcription": setup["output_audio_transcription"],
        "realtime_input_config": setup["realtime_input_config"],
        "tool_declarations_sha256": canonical_json_sha256(setup["tools"]),
        "tool_response_policy": document["tool_response_policy"],
        "reconnect_policy": document["reconnect_policy"],
        "turn_assembly_policy": document["turn_assembly_policy"],
        "websocket_policy": document["websocket_policy"],
    }


def _diff_leaves(left: object, right: object, prefix: str = ""):
    if isinstance(left, dict) and isinstance(right, dict) and set(left) == set(right):
        result = []
        for key in sorted(left):
            path = f"{prefix}.{key}" if prefix else key
            result.extend(_diff_leaves(left[key], right[key], path))
        return result
    return [] if left == right else [(prefix, left, right)]


def _write_json(path: Path, value: object) -> str:
    path.write_text(json.dumps(value, sort_keys=True))
    return _file_sha(path)


def _qualification_config(tmp_path: Path, **overrides) -> QualificationConfig:
    manifest = tmp_path / "manifest.json"
    manifest_sha = _write_json(
        manifest,
        {
            "version": 1,
            "collection_status": "pending",
            "provenance_policy": {
                "synthetic_scripts_only": True,
                "purpose_recorded_adult_speakers_only": True,
                "real_call_data_prohibited": True,
                "production_audio_prohibited": True,
            },
            "speakers": [],
            "cases": [],
        },
    )
    setup_document = _provider_setup()
    setup = tmp_path / "provider-setup.json"
    setup_sha = _write_json(setup, setup_document)
    deviations = tmp_path / "deviations.json"
    diff = _diff_leaves(
        setup_document["immutable_pipeline_setup"],
        _behavior_projection(setup_document),
    )
    deviations_sha = _write_json(
        deviations,
        {
            "version": 1,
            "review_status": "pending_gate_0b",
            "deviations": [
                {
                    "field": field,
                    "immutable_value_sha256": canonical_json_sha256(immutable),
                    "qualification_value_sha256": canonical_json_sha256(qualification),
                    "reason_code": "reviewed_immutable_setup_deviation",
                }
                for field, immutable, qualification in diff
            ],
        },
    )
    canonical = canonicalize_qualification_setup(
        json.loads(setup.read_text()),
        setup_file_sha256=setup_sha,
        deviations_sha256=deviations_sha,
    )
    values = {
        "execute": False,
        "model_resource": MODEL_RESOURCE,
        "api_version": "v1beta",
        "endpoint": ENDPOINT,
        "project": "kevin-qualification-test",
        "credential_ref": "QUALIFICATION_GEMINI_API_KEY",
        "manifest_path": manifest,
        "manifest_sha256": manifest_sha,
        "setup_path": setup,
        "setup_file_sha256": setup_sha,
        "canonical_setup_sha256": canonical_json_sha256(canonical),
        "deviations_path": deviations,
        "deviations_sha256": deviations_sha,
        "source_sha": SOURCE_SHA,
        "attempt_cap": 20,
        "wall_clock_cap_seconds": 900,
        "session_timeout_seconds": 30,
        "max_cost_usd": 25.0,
        "max_cost_per_attempt_usd": 1.0,
    }
    values.update(overrides)
    return QualificationConfig(**values)


def test_preregistration_contains_exact_identity_caps_setup_and_deviations(tmp_path):
    registration = build_preregistration(_qualification_config(tmp_path))

    assert registration["schema_version"] == 1
    assert registration["execution_requested"] is False
    assert registration["model_resource"] == MODEL_RESOURCE
    assert registration["api_version"] == "v1beta"
    assert registration["endpoint"] == ENDPOINT
    assert registration["project"] == "kevin-qualification-test"
    assert registration["credential_ref"] == "QUALIFICATION_GEMINI_API_KEY"
    assert registration["caps"] == {
        "attempts": 20,
        "wall_clock_seconds": 900,
        "session_timeout_seconds": 30,
        "max_cost_usd": 25.0,
        "max_cost_per_attempt_usd": 1.0,
    }
    assert registration["canonical_setup_sha256"] == canonical_json_sha256(
        registration["canonical_setup"]
    )
    assert registration["canonical_setup"]["system_instruction_sha256"]
    assert registration["canonical_setup"]["tool_declarations_sha256"]
    assert registration["canonical_setup"]["api_version"] == "v1beta"
    assert registration["canonical_setup"]["endpoint"] == ENDPOINT
    assert registration["canonical_setup"]["runner_identity"]["file_sha256"] == _file_sha(
        RUNNER_PATH
    )
    assert registration["canonical_setup"]["evaluator_identity"][
        "file_sha256"
    ] == _file_sha(EVALUATOR_PATH)
    assert {item["field"] for item in registration["deviations"]} >= {
        "model_resource",
        "generation_config.temperature",
        "turn_assembly_policy.quiescence_ms",
    }
    assert all(
        item["reason_code"] == "reviewed_immutable_setup_deviation"
        for item in registration["deviations"]
    )
    serialized = json.dumps(registration)
    assert "Synthetic qualification prompt only" not in serialized
    assert "function_declarations" not in serialized


def test_default_dry_run_never_reads_credential_or_calls_transport(tmp_path):
    config = _qualification_config(tmp_path)
    transport_calls = []

    def forbidden_connect(*_args, **_kwargs):
        transport_calls.append(True)
        raise AssertionError("dry-run must not connect")

    class CredentialTrap(dict):
        def __getitem__(self, key):
            raise AssertionError(f"dry-run read credential {key}")

    report = asyncio.run(
        run_qualification(
            config,
            connect=forbidden_connect,
            environ=CredentialTrap({config.credential_ref: "valid-looking-secret"}),
        )
    )

    assert report["status"] == "dry_run_blocked"
    assert report["failure_counts"] == {"manifest_collection_pending": 1}
    assert transport_calls == []
    assert report["provider_execution_authorized"] is False
    assert report["release_authorized"] is False


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"project": "kevin-491315"}, "production_project_forbidden"),
        ({"project": "kevin-production"}, "production_project_forbidden"),
        ({"credential_ref": "GEMINI_API_KEY"}, "credential_ref_not_dedicated"),
        ({"credential_ref": "QUALIFICATION_LIVE_CALL_KEY"}, "credential_ref_not_dedicated"),
        ({"endpoint": "wss://example.invalid/ws"}, "endpoint_not_official"),
        ({"manifest_sha256": "d" * 64}, "manifest_digest_mismatch"),
        ({"attempt_cap": 0}, "attempt_cap_invalid"),
        ({"attempt_cap": 1001}, "attempt_cap_invalid"),
        ({"wall_clock_cap_seconds": 0}, "wall_clock_cap_invalid"),
        ({"session_timeout_seconds": 0}, "session_timeout_invalid"),
        ({"max_cost_usd": 0}, "cost_cap_invalid"),
        ({"max_cost_per_attempt_usd": 0}, "cost_cap_invalid"),
        ({"max_cost_per_attempt_usd": 2}, "cost_cap_invalid"),
    ],
)
def test_preregistration_rejects_unsafe_or_unbounded_configuration(
    tmp_path,
    changes,
    code,
):
    with pytest.raises(QualificationError) as caught:
        build_preregistration(_qualification_config(tmp_path, **changes))

    assert caught.value.code == code
    assert str(caught.value) == code


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["setup"]["system_instruction"]["parts"][0].update(text="drift"),
        lambda value: value["setup"]["generation_config"].update(temperature=0.4),
        lambda value: value["setup"].update(input_audio_transcription={"language": "en"}),
        lambda value: value["setup"]["realtime_input_config"][
            "automatic_activity_detection"
        ].update(silence_duration_ms=600),
        lambda value: value["setup"]["realtime_input_config"].update(
            activity_handling="NO_INTERRUPTION"
        ),
        lambda value: value["setup"].update(tools=[]),
        lambda value: value["reconnect_policy"].update(
            max_attempts=2,
            retry_backoff_ms=[0, 0],
        ),
        lambda value: value["turn_assembly_policy"].update(quiescence_ms=300),
        lambda value: value["websocket_policy"].update(max_message_bytes=2048),
        lambda value: value["runner_identity"].update(file_sha256="d" * 64),
        lambda value: value["evaluator_identity"].update(file_sha256="d" * 64),
    ],
)
def test_canonical_setup_digest_detects_every_behavioral_drift(tmp_path, mutate):
    config = _qualification_config(tmp_path)
    changed = json.loads(config.setup_path.read_text())
    mutate(changed)
    changed_sha = _write_json(config.setup_path, changed)

    with pytest.raises(QualificationError) as caught:
        build_preregistration(replace(config, setup_file_sha256=changed_sha))

    assert caught.value.code in {
        "canonical_setup_digest_mismatch",
        "runner_identity_mismatch",
        "evaluator_identity_mismatch",
    }


def test_unexplained_immutable_pipeline_deviation_is_a_hard_failure(tmp_path):
    config = _qualification_config(tmp_path)
    deviations = json.loads(config.deviations_path.read_text())
    deviations["deviations"] = deviations["deviations"][1:]
    deviations_sha = _write_json(config.deviations_path, deviations)
    setup_document = json.loads(config.setup_path.read_text())
    canonical = canonicalize_qualification_setup(
        setup_document,
        setup_file_sha256=config.setup_file_sha256,
        deviations_sha256=deviations_sha,
    )
    changed = replace(
        config,
        deviations_sha256=deviations_sha,
        canonical_setup_sha256=canonical_json_sha256(canonical),
    )

    with pytest.raises(QualificationError) as caught:
        build_preregistration(changed)

    assert caught.value.code == "setup_deviation_unexplained"


def test_canonical_setup_rejects_payload_hidden_in_reported_configuration(tmp_path):
    document = _provider_setup()
    document["setup"]["generation_config"]["prompt"] = "must-not-persist"
    setup_path = tmp_path / "setup.json"
    setup_sha = _write_json(setup_path, document)

    with pytest.raises(QualificationError) as caught:
        canonicalize_qualification_setup(
            document,
            setup_file_sha256=setup_sha,
            deviations_sha256="d" * 64,
        )

    assert caught.value.code == "setup_document_contains_payload"


def test_repo_manifest_is_truthful_pending_synthetic_consent_contract():
    summary = validate_audio_manifest(CURRENT_MANIFEST, require_execution_ready=False)
    manifest = json.loads(CURRENT_MANIFEST.read_text())

    assert summary.collection_status == "pending"
    assert summary.case_count == 0
    assert manifest["provenance_policy"] == {
        "synthetic_scripts_only": True,
        "purpose_recorded_adult_speakers_only": True,
        "real_call_data_prohibited": True,
        "production_audio_prohibited": True,
    }


@pytest.mark.parametrize(
    "manifest_change",
    [
        lambda value: value["provenance_policy"].update(
            purpose_recorded_adult_speakers_only=False
        ),
        lambda value: value.update(collection_status="real_calls"),
    ],
)
def test_manifest_rejects_missing_consent_policy_or_real_call_label(
    tmp_path,
    manifest_change,
):
    manifest = json.loads(CURRENT_MANIFEST.read_text())
    manifest_change(manifest)
    path = tmp_path / "manifest.json"
    _write_json(path, manifest)

    with pytest.raises(QualificationError) as caught:
        validate_audio_manifest(path, require_execution_ready=False)

    assert caught.value.code == "manifest_provenance_invalid"


class _FakeSocket:
    def __init__(self, *, acknowledgement=None, messages=(), receive_error=None, delay=0):
        self.acknowledgement = acknowledgement or {"setupComplete": {}}
        self.messages = list(messages)
        self.receive_error = receive_error
        self.delay = delay
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return json.dumps(self.acknowledgement)

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index < len(self.messages):
            value = self.messages[self._index]
            self._index += 1
            return json.dumps(value) if isinstance(value, dict) else value
        if self.receive_error is not None:
            raise self.receive_error
        raise StopAsyncIteration


class _FakeConnect:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self.sockets.pop(0)


def _attempt(connect, *, timeout=1, reconnects=0):
    return asyncio.run(
        run_session_attempt(
            endpoint=ENDPOINT,
            credential="test-only-credential",
            provider_setup=_provider_setup()["setup"],
            audio_bytes=b"synthetic-audio",
            connect=connect,
            websocket_policy=_provider_setup()["websocket_policy"],
            quiescence_ms=250,
            session_timeout_seconds=timeout,
            max_reconnect_attempts=reconnects,
        )
    )


def test_mocked_websocket_reduces_events_without_raw_payloads():
    socket = _FakeSocket(
        messages=[
            {"serverContent": {"inputTranscription": {"text": "Private synthetic text"}}},
            {"serverContent": {"interrupted": True}},
            {"toolCall": {"functionCalls": [{"name": "lookup"}]}},
            {"toolCallCancellation": {"ids": ["tool-id"]}},
        ]
    )

    result = _attempt(_FakeConnect(socket)).redacted_report_dict()
    serialized = json.dumps(result)

    assert result["event_type_counts"] == {
        "connection_closed": 1,
        "input_transcript_fragment": 1,
        "interrupted": 1,
        "tool_call_started": 1,
        "tool_call_cancelled": 1,
    }
    assert result["turn_status_counts"]["cancelled"] == 1
    assert "Private synthetic text" not in serialized
    assert "synthetic-audio" not in serialized
    assert "test-only-credential" not in serialized


@pytest.mark.parametrize(
    ("socket", "error_code"),
    [
        (_FakeSocket(acknowledgement={"unexpected": True}), "setup_rejected"),
        (_FakeSocket(delay=0.05), "setup_timeout"),
        (_FakeSocket(receive_error=RuntimeError("private exception")), "provider_closed"),
        (_FakeSocket(receive_error=asyncio.CancelledError()), "cancelled"),
        (_FakeSocket(messages=["not-json"]), "malformed_message"),
        (
            _FakeSocket(messages=[json.dumps({"unknown": "x" * 70000})]),
            "oversized_message",
        ),
    ],
)
def test_mocked_websocket_failures_are_bounded_and_nonauthorizing(socket, error_code):
    timeout = 0.01 if error_code == "setup_timeout" else 1
    result = _attempt(_FakeConnect(socket), timeout=timeout).redacted_report_dict()

    assert result["error_code"] == error_code
    assert "private exception" not in json.dumps(result)
    assert result["complete"] is False


def test_mocked_reconnect_marks_first_epoch_partial_and_second_complete():
    first = _FakeSocket(
        messages=[
            {"serverContent": {"inputTranscription": {"text": "First epoch"}}}
        ],
        receive_error=ConnectionError("closed"),
    )
    second = _FakeSocket(
        messages=[
            {"serverContent": {"inputTranscription": {"text": "Second epoch"}}},
            {"serverContent": {"turnComplete": True}},
        ]
    )

    result = _attempt(_FakeConnect(first, second), reconnects=1).redacted_report_dict()

    assert result["reconnect_count"] == 1
    assert result["turn_status_counts"] == {
        "partial": 1,
        "retrospective_complete": 1,
    }
    assert result["error_code"] is None


def test_normal_socket_close_classifies_pending_transcript_as_partial():
    socket = _FakeSocket(
        messages=[
            {"serverContent": {"inputTranscription": {"text": "Pending text"}}}
        ]
    )

    result = _attempt(_FakeConnect(socket)).redacted_report_dict()

    assert result["turn_status_counts"] == {"partial": 1}
    assert result["turn_close_reason_counts"] == {"connection_closed": 1}
    assert result["complete"] is False
