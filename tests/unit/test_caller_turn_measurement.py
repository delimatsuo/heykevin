"""Independent Gate 0B measurement and evidence-custody tests."""

import base64
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.exceptions import InvalidTag
import pytest

from app.services.caller_turn_alignment import (
    ActivityReference,
    AlignmentPolicy,
    CriticalSpan,
    CriticalSpanKind,
    FragmentMode,
)
from app.services.caller_turn_measurement import (
    ActivityMeasurementInput,
    ActivityPrimitiveRecord,
    CriticalSpanFact,
    MeasurementError,
    WireObservation,
    build_signed_record_root,
    derive_audit_capsule_accounting,
    derive_primitive_records_from_capsule,
    measure_activity,
    open_audit_capsule,
    require_reducer_agreement,
    seal_audit_capsule,
    verify_record_commitment,
    verify_signed_record_root,
)
from app.services.caller_turns import CallerTurnEvent, CallerTurnEventKind
from app.services.qualification_environment import (
    EXECUTION_IDENTITY_SCHEMA_ID,
    execution_identity_report_sha256,
)
from app.services.qualification_identity import (
    EXECUTION_DEPENDENCY_PATHS as STARTUP_DEPENDENCY_PATHS,
    RUNTIME_SITE_PACKAGES_SCHEMA_ID,
    TRUSTED_STARTUP_POLICY_SCHEMA_ID,
    canonical_json_bytes,
)


CAMPAIGN_KEY = b"c" * 32
_NATIVE_RUNTIME_CLOSURE_UNSIGNED = {
    "schema_id": "gate_0b_native_runtime_closure_v1",
    "regular_file_count": 2,
    "regular_files_sha256": "9" * 64,
    "virtual_dependency_count": 1,
    "virtual_dependencies_sha256": "a" * 64,
    "system_loader_identity_sha256": "b" * 64,
}
NATIVE_RUNTIME_CLOSURE = {
    **_NATIVE_RUNTIME_CLOSURE_UNSIGNED,
    "closure_sha256": sha256(
        canonical_json_bytes(_NATIVE_RUNTIME_CLOSURE_UNSIGNED)
    ).hexdigest(),
}
_INTERPRETER_INSTALLATION_UNSIGNED = {
    "schema_id": "gate_0b_interpreter_installation_v2",
    "python_executable_sha256": "3" * 64,
    "stdlib_source_bytecode_sha256": "4" * 64,
    "stdlib_source_bytecode_count": 1,
    "stdlib_archive_sha256": "5" * 64,
    "stdlib_archive_count": 0,
    "native_extension_sha256": "6" * 64,
    "native_extension_count": 1,
    "native_runtime_closure": NATIVE_RUNTIME_CLOSURE,
}
INTERPRETER_INSTALLATION = {
    **_INTERPRETER_INSTALLATION_UNSIGNED,
    "installation_sha256": sha256(
        canonical_json_bytes(_INTERPRETER_INSTALLATION_UNSIGNED)
    ).hexdigest(),
}
_RUNTIME_SITE_PACKAGES_UNSIGNED = {
    "schema_id": RUNTIME_SITE_PACKAGES_SCHEMA_ID,
    "source_count": 1,
    "bytecode_count": 0,
    "native_extension_count": 0,
    "metadata_data_count": 0,
    "file_count": 1,
    "files_sha256": "7" * 64,
}
RUNTIME_SITE_PACKAGES_MANIFEST = {
    **_RUNTIME_SITE_PACKAGES_UNSIGNED,
    "manifest_sha256": sha256(
        canonical_json_bytes(_RUNTIME_SITE_PACKAGES_UNSIGNED)
    ).hexdigest(),
}
SOURCE_PREFLIGHT = {
    "source_sha": "a" * 40,
    "clean": True,
    "dependencies": {
        sha256(path.encode("utf-8")).hexdigest(): {
            "worktree_sha256": "1" * 64,
            "git_blob_id": "2" * 40,
        }
        for path in STARTUP_DEPENDENCY_PATHS
    },
}
TRUSTED_STARTUP_REPORT = {
    "schema_id": TRUSTED_STARTUP_POLICY_SCHEMA_ID,
    "startup_flags": {
        "bytes_warning": 0,
        "debug": 0,
        "dev_mode": False,
        "dont_write_bytecode": 1,
        "hash_randomization": 1,
        "ignore_environment": 1,
        "inspect": 0,
        "int_max_str_digits": 4300,
        "interactive": 0,
        "isolated": 1,
        "no_site": 1,
        "no_user_site": 1,
        "optimize": 0,
        "quiet": 0,
        "safe_path": True,
        "utf8_mode": 0,
        "verbose": 0,
        "warn_default_encoding": 0,
    },
    "bytecode_write_disabled": True,
    "pycache_prefix_location_sha256": "2" * 64,
    "repo_root_location_sha256": "c" * 64,
    "python_executable_location_sha256": "d" * 64,
    "runtime_site_packages_location_sha256": "e" * 64,
    "effective_sys_path_sha256": "f" * 64,
    "effective_sys_path_entry_sha256": ["0" * 64, "1" * 64],
    "neutralized_environment": ["PYTHONHOME", "PYTHONPATH"],
    "runtime_pth_files_sha256": {},
    "ignored_startup_hook_files_sha256": {},
    "source_preflight": SOURCE_PREFLIGHT,
    "interpreter_installation": INTERPRETER_INSTALLATION,
    "runtime_site_packages_manifest": RUNTIME_SITE_PACKAGES_MANIFEST,
}
RUNTIME_ENVIRONMENT_IDENTITY = {
    "python_version": "3.12.13",
    "uv_version": "0.11.7",
    "python_executable_sha256": "3" * 64,
    "uv_executable_sha256": "4" * 64,
    "python_executable_location_sha256": "5" * 64,
    "uv_executable_location_sha256": "6" * 64,
    "platform_id": "darwin-test",
    "architecture": "arm64",
    "unicode_version": "15.0.0",
    "monotonic_clock_implementation": "mach_absolute_time",
    "monotonic_clock_resolution_ns": 1,
    "bytecode_write_disabled": True,
    "openssl_version": "OpenSSL 3.0.0",
    "ca_bundle_sha256": "7" * 64,
    "lock_sha256": "8" * 64,
    "codec_golden_sha256": "9" * 64,
    "import_sha256": {"app.services.example": "a" * 64},
    "distributions": {"test-package": "1.0.0"},
    "distribution_files_sha256": {"test-package": "b" * 64},
    "interpreter_installation": INTERPRETER_INSTALLATION,
    "runtime_site_packages_manifest": RUNTIME_SITE_PACKAGES_MANIFEST,
}
RUNTIME_IDENTITY_REPORT = {
    "schema_id": EXECUTION_IDENTITY_SCHEMA_ID,
    "source": SOURCE_PREFLIGHT,
    "environment": RUNTIME_ENVIRONMENT_IDENTITY,
    "trusted_startup": TRUSTED_STARTUP_REPORT,
}
RUNTIME_IDENTITY_SHA256 = execution_identity_report_sha256(RUNTIME_IDENTITY_REPORT)


def _events(text: str = "book service today") -> tuple[CallerTurnEvent, ...]:
    return (
        CallerTurnEvent(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, 10, 1, 1, text),
        CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
    )


def _activity_input() -> ActivityMeasurementInput:
    reference = ActivityReference(
        3,
        "en",
        "book service today",
        critical_spans=(CriticalSpan(CriticalSpanKind.CORRECTION, "service today"),),
    )
    return ActivityMeasurementInput(
        policy_ms=250,
        activity_ordinal=3,
        split="development",
        language="en",
        condition="clean",
        scenario_tags=("standard",),
        references=(reference,),
        events=_events(),
        expected_epoch=1,
        expected_lifecycle_status="retrospective_complete",
        advance_to_ms=270,
        wire=WireObservation(timing_covered=True, first_audio_ms=500),
    )


def test_independent_reducers_must_agree_before_payload_discard() -> None:
    primary = _events()
    independent = tuple(primary)

    agreed = require_reducer_agreement(primary, independent)

    assert agreed == primary
    with pytest.raises(MeasurementError, match="reducer disagreement"):
        require_reducer_agreement(
            primary,
            (replace(primary[0], text="different"), primary[1]),
        )


def test_audit_capsule_is_allowlisted_and_sealed_to_custodian_key() -> None:
    private_key = X25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    payload = {
        "schema_id": "gate_0b_audit_capsule_v6",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "source_fact_bundle_sha256": "1" * 64,
        "execution_started_at": "2026-07-15T15:00:00Z",
        "execution_completed_at": "2026-07-15T15:01:00Z",
        "provider_revision": None,
        "runtime_identity_before_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_after_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_before": RUNTIME_IDENTITY_REPORT,
        "runtime_identity_after": RUNTIME_IDENTITY_REPORT,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 1,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 300,
                    "input_audio_duration_ms": 20,
                    "output_audio_bytes": 48_000,
                    "input_audio_tokens": 8,
                    "output_audio_tokens": 4,
                    "input_text_tokens": 2,
                    "output_text_tokens": 1,
                }
            ],
        },
        "sessions": [
            {
                "session_ordinal": 1,
                "split": "development",
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 10,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "purpose recorded synthetic phrase",
                    }
                ],
                "event_activity_ordinals": [3],
                "wire_facts": [
                    {
                        "kind": "connection_open",
                        "at_ms": 0,
                        "response_ordinal": None,
                        "activity_ordinal": None,
                        "sequence": 0,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "caller_audio_sent",
                        "at_ms": 5,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 1,
                        "epoch": 1,
                        "audio_bytes": 640,
                    }
                ],
            }
        ],
        "activities": [
            {
                "activity_ordinal": 3,
                "session_ordinal": 1,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "purpose recorded synthetic phrase",
                "critical_spans": [
                    {"kind": "correction", "text": "synthetic phrase", "language": "en"}
                ],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "speech_end_at_ms": 80,
                "advance_to_ms": 300,
            }
        ],
        "no_speech_windows": [],
    }

    envelope = seal_audit_capsule(
        payload,
        custodian_public_key=public_key,
        custodian_key_id="audit_custodian_1",
    )

    assert "purpose recorded" not in json.dumps(envelope)
    assert open_audit_capsule(
        envelope,
        custodian_private_key=private_key,
        expected_key_id="audit_custodian_1",
    ) == payload
    for field in ("runtime_identity_after_sha256", "runtime_identity_after"):
        missing_identity = deepcopy(payload)
        missing_identity.pop(field)
        with pytest.raises(MeasurementError, match="fields"):
            seal_audit_capsule(
                missing_identity,
                custodian_public_key=public_key,
                custodian_key_id="audit_custodian_1",
            )
    drifted = deepcopy(payload)
    drifted["runtime_identity_after_sha256"] = "3" * 64
    with pytest.raises(MeasurementError, match="runtime identity drifted"):
        seal_audit_capsule(
            drifted,
            custodian_public_key=public_key,
            custodian_key_id="audit_custodian_1",
        )
    noncanonical_time = deepcopy(payload)
    noncanonical_time["execution_started_at"] = "2026-07-15T15:00:00.000Z"
    with pytest.raises(MeasurementError, match="timestamp"):
        seal_audit_capsule(
            noncanonical_time,
            custodian_public_key=public_key,
            custodian_key_id="audit_custodian_1",
        )
    with pytest.raises(InvalidTag):
        open_audit_capsule(
            envelope,
            custodian_private_key=X25519PrivateKey.generate(),
            expected_key_id="audit_custodian_1",
        )

    leaked = dict(payload)
    leaked["provider_request_id"] = "forbidden"
    with pytest.raises(MeasurementError, match="audit capsule field"):
        seal_audit_capsule(
            leaked,
            custodian_public_key=public_key,
            custodian_key_id="audit_custodian_1",
        )

    nested = deepcopy(payload)
    nested["no_speech_windows"] = [
        {
            "window_ordinal": 0,
            "split": "development",
            "condition": "silence",
            "wire_facts": [
                {
                    "kind": "false_activity",
                    "at_ms": 10,
                    "response_ordinal": None,
                    "activity_ordinal": None,
                    "sequence": 0,
                    "epoch": 1,
                    "audio_bytes": 0,
                    "secret_value": "forbidden",
                }
            ],
        }
    ]
    with pytest.raises(MeasurementError, match="wire fact"):
        seal_audit_capsule(
            nested,
            custodian_public_key=public_key,
            custodian_key_id="audit_custodian_1",
        )


def test_custodian_capsule_derives_all_development_policies_and_no_speech_records() -> None:
    capsule = {
        "schema_id": "gate_0b_audit_capsule_v6",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "source_fact_bundle_sha256": "1" * 64,
        "execution_started_at": "2026-07-15T15:00:00Z",
        "execution_completed_at": "2026-07-15T15:01:00Z",
        "provider_revision": None,
        "runtime_identity_before_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_after_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_before": RUNTIME_IDENTITY_REPORT,
        "runtime_identity_after": RUNTIME_IDENTITY_REPORT,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 1,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 300,
                    "input_audio_duration_ms": 20,
                    "output_audio_bytes": 48_000,
                    "input_audio_tokens": 8,
                    "output_audio_tokens": 4,
                    "input_text_tokens": 2,
                    "output_text_tokens": 1,
                },
                {
                    "kind": "no_speech_window",
                    "ordinal": 0,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 100,
                    "input_audio_duration_ms": 20,
                    "output_audio_bytes": 0,
                    "input_audio_tokens": 6,
                    "output_audio_tokens": 0,
                    "input_text_tokens": 1,
                    "output_text_tokens": 0,
                },
            ],
        },
        "sessions": [
            {
                "session_ordinal": 1,
                "split": "development",
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 30,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "book service today",
                    },
                    {
                        "kind": "turn_complete",
                        "at_ms": 40,
                        "sequence": 2,
                        "epoch": 1,
                        "text": "",
                    },
                ],
                "event_activity_ordinals": [3, 3],
                "wire_facts": [
                    {
                        "kind": "caller_activity_start",
                        "at_ms": 0,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 0,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "caller_audio_sent",
                        "at_ms": 20,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 1,
                        "epoch": 1,
                        "audio_bytes": 640,
                    },
                    {
                        "kind": "caller_activity_end",
                        "at_ms": 100,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 2,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "response_open",
                        "at_ms": 220,
                        "response_ordinal": 1,
                        "activity_ordinal": 3,
                        "sequence": 3,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "audio_received",
                        "at_ms": 220,
                        "response_ordinal": 1,
                        "activity_ordinal": 3,
                        "sequence": 4,
                        "epoch": 1,
                        "audio_bytes": 48_000,
                    },
                    {
                        "kind": "response_terminal",
                        "at_ms": 230,
                        "response_ordinal": 1,
                        "activity_ordinal": 3,
                        "sequence": 5,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "teardown_complete",
                        "at_ms": 240,
                        "response_ordinal": None,
                        "activity_ordinal": None,
                        "sequence": 6,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                    {
                        "kind": "caller_speech_end",
                        "at_ms": 80,
                        "response_ordinal": None,
                        "activity_ordinal": 3,
                        "sequence": 7,
                        "epoch": 1,
                        "audio_bytes": 0,
                    },
                ],
            }
        ],
        "activities": [
            {
                "activity_ordinal": 3,
                "session_ordinal": 1,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "book service today",
                "critical_spans": [
                    {"kind": "correction", "text": "service today", "language": "en"}
                ],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "speech_end_at_ms": 80,
                "advance_to_ms": 900,
            }
        ],
        "no_speech_windows": [
            {
                "window_ordinal": 0,
                "split": "development",
                "condition": "silence",
                "wire_facts": [],
            }
        ],
    }

    _normalize_wire_sequences(capsule)
    activity_records, no_speech_records = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(100, 250, 500, 750),
        commitment_key=CAMPAIGN_KEY,
    )

    assert [record.policy_ms for record in activity_records] == [100, 250, 500, 750]
    assert all(record.first_audio_ms == 140 for record in activity_records)
    assert all(record.commitment for record in activity_records)
    assert len(no_speech_records) == 1
    assert no_speech_records[0].commitment

    usage, failures = derive_audit_capsule_accounting(capsule)
    assert usage == {
        "metadata_complete": True,
        "provider_requests": 2,
        "wall_clock_seconds": 1,
        "input_audio_seconds": 1,
        "output_audio_seconds": 1,
        "input_audio_tokens": 14,
        "output_audio_tokens": 4,
        "input_text_tokens": 3,
        "output_text_tokens": 1,
    }
    assert failures == ()


def _literal_wire_capsule() -> dict[str, object]:
    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        response_ordinal: int | None = None,
        activity_ordinal: int | None = 3,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response_ordinal,
            "activity_ordinal": activity_ordinal,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    return {
        "schema_id": "gate_0b_audit_capsule_v6",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "source_fact_bundle_sha256": "1" * 64,
        "execution_started_at": "2026-07-15T15:00:00Z",
        "execution_completed_at": "2026-07-15T15:01:00Z",
        "provider_revision": None,
        "runtime_identity_before_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_after_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_before": RUNTIME_IDENTITY_REPORT,
        "runtime_identity_after": RUNTIME_IDENTITY_REPORT,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 1,
                    "metadata_complete": True,
                    "complete": True,
                    "error_code": None,
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 900,
                    "input_audio_duration_ms": 100,
                    "output_audio_bytes": 640,
                    "input_audio_tokens": 1,
                    "output_audio_tokens": 1,
                    "input_text_tokens": 0,
                    "output_text_tokens": 0,
                }
            ],
        },
        "sessions": [
            {
                "session_ordinal": 1,
                "split": "development",
                "events": [
                    {
                        "kind": "input_transcript_fragment",
                        "at_ms": 30,
                        "sequence": 1,
                        "epoch": 1,
                        "text": "book service today",
                    },
                    {
                        "kind": "turn_complete",
                        "at_ms": 40,
                        "sequence": 2,
                        "epoch": 1,
                        "text": "",
                    },
                ],
                "event_activity_ordinals": [3, 3],
                "wire_facts": [
                    fact("connection_open", 0, 0, activity_ordinal=None),
                    fact("caller_activity_start", 0, 1),
                    fact("caller_audio_sent", 20, 2, audio_bytes=640),
                    fact("caller_speech_end", 80, 3),
                    fact("caller_activity_end", 100, 4),
                    fact("response_open", 220, 5, response_ordinal=1),
                    fact(
                        "audio_received",
                        220,
                        6,
                        response_ordinal=1,
                        audio_bytes=640,
                    ),
                    fact("response_terminal", 230, 7, response_ordinal=1),
                    fact(
                        "teardown_complete",
                        240,
                        8,
                        activity_ordinal=None,
                    ),
                ],
            }
        ],
        "activities": [
            {
                "activity_ordinal": 3,
                "session_ordinal": 1,
                "split": "development",
                "language": "en",
                "condition": "clean",
                "scenario_tags": ["standard"],
                "reference_text": "book service today",
                "critical_spans": [],
                "expected_lifecycle_status": "retrospective_complete",
                "expected_epoch": 1,
                "speech_end_at_ms": 80,
                "advance_to_ms": 900,
            }
        ],
        "no_speech_windows": [],
    }


def _normalize_wire_sequences(capsule: dict[str, object]) -> None:
    sessions = capsule["sessions"]
    assert isinstance(sessions, list)
    session = sessions[0]
    facts = session["wire_facts"]
    assert isinstance(facts, list)
    if not any(fact["kind"] == "connection_open" for fact in facts):
        facts.append(
            {
                "kind": "connection_open",
                "at_ms": 0,
                "response_ordinal": None,
                "activity_ordinal": None,
                "sequence": -1,
                "epoch": 1,
                "audio_bytes": 0,
            }
        )
    facts.sort(key=lambda value: (value["at_ms"], value["sequence"]))
    for sequence, fact in enumerate(facts):
        fact["sequence"] = sequence
    sent_audio = [fact for fact in facts if fact["kind"] == "caller_audio_sent"]
    session["event_activity_ordinals"] = [
        (
            min(
                (fact for fact in sent_audio if fact["epoch"] == event["epoch"]),
                key=lambda fact: (fact["at_ms"], fact["sequence"]),
            )
            if event["kind"] == "reconnect_started"
            else max(
                (
                    fact
                    for fact in sent_audio
                    if fact["epoch"] == event["epoch"]
                    and fact["at_ms"] <= event["at_ms"]
                ),
                key=lambda fact: (fact["at_ms"], fact["sequence"]),
            )
        )["activity_ordinal"]
        for event in session["events"]
    ]
    windows = capsule["no_speech_windows"]
    assert isinstance(windows, list)
    for window in windows:
        window_facts = window["wire_facts"]
        assert isinstance(window_facts, list)
        if not any(fact["kind"] == "connection_open" for fact in window_facts):
            window_facts.append(
                {
                    "kind": "connection_open",
                    "at_ms": 0,
                    "response_ordinal": None,
                    "activity_ordinal": None,
                    "sequence": 0,
                    "epoch": 1,
                    "audio_bytes": 0,
                }
            )
        window_facts.sort(key=lambda value: (value["at_ms"], value["sequence"]))
        for sequence, fact in enumerate(window_facts):
            fact["sequence"] = sequence


@pytest.mark.parametrize(
    ("mutation", "field"),
    (
        ("premature", "premature_current_audio_count"),
        ("gap", "response_gap_violation_count"),
        ("after_terminal", "audio_after_terminal_count"),
        ("missing_terminal", "response_timeout_count"),
    ),
)
def test_literal_capsule_wire_mutations_derive_failing_primitives(
    mutation: str,
    field: str,
) -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    response_open = next(fact for fact in facts if fact["kind"] == "response_open")
    response_audio = next(fact for fact in facts if fact["kind"] == "audio_received")
    response_terminal = next(
        fact for fact in facts if fact["kind"] == "response_terminal"
    )
    teardown = next(fact for fact in facts if fact["kind"] == "teardown_complete")
    if mutation == "premature":
        response_open["at_ms"] = 70
        response_audio["at_ms"] = 70
    elif mutation == "gap":
        facts.append({**response_audio, "at_ms": 721})
        response_terminal["at_ms"] = 730
        teardown["at_ms"] = 740
    elif mutation == "after_terminal":
        facts.append({**response_audio, "at_ms": 235})
        teardown["at_ms"] = 240
    else:
        facts.remove(response_terminal)
    _normalize_wire_sequences(capsule)

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert getattr(records[0], field) == 1
    if mutation == "premature":
        assert records[0].first_audio_ms == 0


def test_capsule_first_audio_latency_uses_labeled_speech_end() -> None:
    capsule = _literal_wire_capsule()
    activity = capsule["activities"][0]  # type: ignore[index]
    activity.update(
        {
            "speech_end_at_ms": 80,
        }
    )

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert records[0].first_audio_ms == 140
    assert records[0].premature_current_audio_count == 0


def test_causal_cancellation_tail_distinguishes_zero_from_missing_evidence() -> None:
    capsule = _literal_wire_capsule()
    sessions = capsule["sessions"]
    activities = capsule["activities"]
    assert isinstance(sessions, list)
    assert isinstance(activities, list)
    session = sessions[0]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 50,
            "sequence": 1,
            "epoch": 1,
            "text": "first phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 120,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 250,
            "sequence": 3,
            "epoch": 1,
            "text": "second phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 320,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        response: int | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    session["wire_facts"] = [
        fact("caller_activity_start", 0, 0, activity=2),
        fact("caller_audio_sent", 20, 1, activity=2, audio_bytes=640),
        fact("caller_activity_end", 100, 2, activity=2),
        fact("response_open", 150, 3, activity=2, response=1),
        fact("audio_received", 150, 4, activity=2, response=1, audio_bytes=320),
        fact("tool_call_open", 160, 5, activity=2),
        fact("caller_activity_start", 200, 6, activity=3),
        fact("caller_audio_sent", 250, 7, activity=3, audio_bytes=640),
        fact("audio_received", 250, 8, activity=2, response=1, audio_bytes=320),
        fact("tool_call_cancelled", 250, 9, activity=3),
        fact("interrupted", 250, 10, activity=3),
        fact("response_terminal", 250, 11, activity=2, response=1),
        fact("caller_activity_end", 300, 12, activity=3),
        fact("response_open", 400, 13, activity=3, response=2),
        fact("audio_received", 400, 14, activity=3, response=2, audio_bytes=320),
        fact("response_terminal", 410, 15, activity=3, response=2),
        fact("teardown_complete", 420, 16, activity=None),
    ]
    session["wire_facts"].extend(
        (
            fact("caller_speech_end", 80, 17, activity=2),
            fact("caller_speech_end", 280, 18, activity=3),
        )
    )
    activities[:] = [
        {
            "activity_ordinal": 2,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "interaction_stress",
            "scenario_tags": ["synchronous_tool_use"],
            "reference_text": "first phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 80,
            "advance_to_ms": 800,
        },
        {
            "activity_ordinal": 3,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "interaction_stress",
            "scenario_tags": ["tool_cancellation_interruption"],
            "reference_text": "second phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 280,
            "advance_to_ms": 800,
        },
    ]

    _normalize_wire_sequences(capsule)
    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )
    cancellation = next(record for record in records if record.activity_ordinal == 3)
    assert cancellation.interruption_tail_ms == 0

    post_cancellation_audio = deepcopy(capsule)
    delayed_facts = post_cancellation_audio["sessions"][0]["wire_facts"]  # type: ignore[index]
    terminal = next(
        value
        for value in delayed_facts
        if value["kind"] == "response_terminal" and value["response_ordinal"] == 1
    )
    terminal["at_ms"] = 310
    delayed_facts.append(
        fact(
            "audio_received",
            300,
            99,
            activity=2,
            response=1,
            audio_bytes=320,
        )
    )
    _normalize_wire_sequences(post_cancellation_audio)
    records, _ = derive_primitive_records_from_capsule(
        post_cancellation_audio,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )
    cancellation = next(record for record in records if record.activity_ordinal == 3)
    assert cancellation.interruption_tail_ms == 50

    no_open = deepcopy(capsule)
    no_open["sessions"][0]["wire_facts"] = [  # type: ignore[index]
        fact
        for fact in no_open["sessions"][0]["wire_facts"]  # type: ignore[index]
        if fact["kind"] != "tool_call_open"
    ]
    _normalize_wire_sequences(no_open)
    records, _ = derive_primitive_records_from_capsule(
        no_open,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )
    cancellation = next(record for record in records if record.activity_ordinal == 3)
    assert cancellation.interruption_tail_ms is None

    wrong_epoch = deepcopy(capsule)
    open_fact = next(
        fact
        for fact in wrong_epoch["sessions"][0]["wire_facts"]  # type: ignore[index]
        if fact["kind"] == "tool_call_open"
    )
    open_fact["epoch"] = 2
    with pytest.raises(MeasurementError, match="topology"):
        derive_primitive_records_from_capsule(
            wrong_epoch,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def _two_activity_assembly_capsule(
    event_specs: tuple[tuple[str, int, int, str], ...],
    *,
    restarted: bool = False,
    first_lifecycle: str = "retrospective_complete",
) -> dict[str, object]:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]

    session["events"] = [
        {
            "kind": kind,
            "at_ms": at_ms,
            "sequence": sequence,
            "epoch": epoch,
            "text": text,
        }
        for sequence, (kind, at_ms, epoch, text) in enumerate(event_specs, start=1)
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        epoch: int,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": None,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": epoch,
            "audio_bytes": audio_bytes,
        }

    if restarted:
        session["wire_facts"] = [
            fact("connection_open", 0, 0, activity=None, epoch=1),
            fact("caller_activity_start", 0, 1, activity=2, epoch=1),
            fact("caller_audio_sent", 20, 2, activity=2, epoch=1, audio_bytes=640),
            fact("caller_speech_end", 60, 3, activity=2, epoch=1),
            fact("caller_activity_end", 70, 4, activity=2, epoch=1),
            fact("teardown_complete", 75, 5, activity=None, epoch=1),
            fact("connection_open", 80, 6, activity=None, epoch=2),
            fact("caller_activity_start", 80, 7, activity=3, epoch=2),
            fact("caller_audio_sent", 90, 8, activity=3, epoch=2, audio_bytes=640),
            fact("caller_speech_end", 150, 9, activity=3, epoch=2),
            fact("caller_activity_end", 160, 10, activity=3, epoch=2),
            fact("teardown_complete", 800, 11, activity=None, epoch=2),
        ]
        capsule["accounting"]["units"][0]["provider_request_count"] = 2  # type: ignore[index]
        activity_bounds = ((60, 70, 1), (150, 160, 2))
    else:
        session["wire_facts"] = [
            fact("connection_open", 0, 0, activity=None, epoch=1),
            fact("caller_activity_start", 0, 1, activity=2, epoch=1),
            fact("caller_audio_sent", 20, 2, activity=2, epoch=1, audio_bytes=640),
            fact("caller_speech_end", 80, 3, activity=2, epoch=1),
            fact("caller_activity_end", 100, 4, activity=2, epoch=1),
            fact("caller_activity_start", 150, 5, activity=3, epoch=1),
            fact("caller_audio_sent", 180, 6, activity=3, epoch=1, audio_bytes=640),
            fact("caller_speech_end", 260, 7, activity=3, epoch=1),
            fact("caller_activity_end", 280, 8, activity=3, epoch=1),
            fact("teardown_complete", 800, 9, activity=None, epoch=1),
        ]
        activity_bounds = ((80, 100, 1), (260, 280, 1))

    capsule["activities"] = [
        {
            "activity_ordinal": ordinal,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": reference,
            "critical_spans": [],
            "expected_lifecycle_status": lifecycle,
            "expected_epoch": epoch,
            "speech_end_at_ms": speech_end_at_ms,
            "advance_to_ms": 900,
        }
        for ordinal, reference, lifecycle, (
            speech_end_at_ms,
            _activity_end_at_ms,
            epoch,
        ) in (
            (2, "first phrase", first_lifecycle, activity_bounds[0]),
            (3, "second phrase", "retrospective_complete", activity_bounds[1]),
        )
    ]
    _normalize_wire_sequences(capsule)
    return capsule


def test_session_replay_marks_cross_activity_merge_inside_quiescence() -> None:
    capsule = _two_activity_assembly_capsule(
        (
            ("input_transcript_fragment", 50, 1, "first phrase"),
            ("turn_complete", 100, 1, ""),
            ("input_transcript_fragment", 200, 1, "second phrase"),
            ("turn_complete", 220, 1, ""),
        )
    )

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    by_activity = {record.activity_ordinal: record for record in records}
    assert all(record.assembled_turn_count == 1 for record in records)
    assert all(record.observed_lifecycle_status == "retrospective_complete" for record in records)
    assert all(record.contamination_count == 1 for record in records)
    assert all(record.duplicate_count == 0 for record in records)
    assert by_activity[2].hypothesis_characters == len("firstphrase")
    assert by_activity[3].hypothesis_characters == len("secondphrase")


def test_session_replay_preserves_clean_terminal_separation() -> None:
    capsule = _two_activity_assembly_capsule(
        (
            ("input_transcript_fragment", 50, 1, "first phrase"),
            ("turn_complete", 100, 1, ""),
            ("input_transcript_fragment", 400, 1, "second phrase"),
            ("turn_complete", 420, 1, ""),
        )
    )

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert all(record.assembled_turn_count == 1 for record in records)
    assert all(record.observed_lifecycle_status == "retrospective_complete" for record in records)
    assert all(record.contamination_count == 0 for record in records)
    assert all(record.duplicate_count == 0 for record in records)
    assert all(record.late_fragment_mutation_count == 0 for record in records)


def test_session_replay_marks_foreign_late_fragment_on_all_merged_owners() -> None:
    capsule = _two_activity_assembly_capsule(
        (
            ("input_transcript_fragment", 50, 1, "first phrase"),
            ("turn_complete", 100, 1, ""),
            ("input_transcript_fragment", 200, 1, "second phrase"),
            ("turn_complete", 500, 1, ""),
        )
    )

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    by_activity = {record.activity_ordinal: record for record in records}
    assert all(record.assembled_turn_count == 1 for record in records)
    assert all(record.contamination_count == 1 for record in records)
    assert by_activity[2].late_fragment_mutation_count == 0
    assert by_activity[3].late_fragment_mutation_count == 0


def test_session_replay_attributes_restart_finalization_without_staling_new_epoch() -> None:
    capsule = _two_activity_assembly_capsule(
        (
            ("input_transcript_fragment", 50, 1, "first phrase"),
            ("reconnect_started", 80, 2, ""),
            ("input_transcript_fragment", 120, 2, "second phrase"),
            ("turn_complete", 130, 2, ""),
        ),
        restarted=True,
        first_lifecycle="partial",
    )

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    by_activity = {record.activity_ordinal: record for record in records}
    assert by_activity[2].assembled_turn_count == 1
    assert by_activity[2].observed_lifecycle_status == "partial"
    assert by_activity[2].stale_count == 0
    assert by_activity[2].cross_epoch_acceptance_count == 0
    assert by_activity[3].assembled_turn_count == 1
    assert by_activity[3].observed_lifecycle_status == "retrospective_complete"
    assert by_activity[3].stale_count == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "omitted_reconnect",
        "duplicate_reconnect",
        "duplicate_connection_open",
        "inflated_provider_request_count",
        "stale_old_epoch_event",
        "inconsistent_activity_epoch",
    ),
)
def test_capsule_rejects_inconsistent_restart_and_request_topology(
    mutation: str,
) -> None:
    capsule = _two_activity_assembly_capsule(
        (
            ("input_transcript_fragment", 50, 1, "first phrase"),
            ("reconnect_started", 80, 2, ""),
            ("input_transcript_fragment", 120, 2, "second phrase"),
            ("turn_complete", 130, 2, ""),
        ),
        restarted=True,
        first_lifecycle="partial",
    )
    session = capsule["sessions"][0]  # type: ignore[index]
    events = session["events"]
    owners = session["event_activity_ordinals"]
    facts = session["wire_facts"]
    assert isinstance(events, list)
    assert isinstance(owners, list)
    assert isinstance(facts, list)

    if mutation == "omitted_reconnect":
        marker_index = next(
            index for index, event in enumerate(events) if event["kind"] == "reconnect_started"
        )
        events.pop(marker_index)
        owners.pop(marker_index)
    elif mutation == "duplicate_reconnect":
        marker_index = next(
            index for index, event in enumerate(events) if event["kind"] == "reconnect_started"
        )
        events.insert(marker_index + 1, dict(events[marker_index]))
        owners.insert(marker_index + 1, owners[marker_index])
    elif mutation == "duplicate_connection_open":
        second_open = next(
            fact
            for fact in facts
            if fact["kind"] == "connection_open" and fact["epoch"] == 2
        )
        facts.append(dict(second_open))
        _normalize_wire_sequences(capsule)
    elif mutation == "inflated_provider_request_count":
        capsule["accounting"]["units"][0]["provider_request_count"] = 3  # type: ignore[index]
    elif mutation == "stale_old_epoch_event":
        marker_index = next(
            index for index, event in enumerate(events) if event["kind"] == "reconnect_started"
        )
        events.insert(
            marker_index + 1,
            {
                "kind": "input_transcript_fragment",
                "at_ms": 90,
                "sequence": 0,
                "epoch": 1,
                "text": "stale fragment",
            },
        )
        owners.insert(marker_index + 1, 2)
    else:
        capsule["activities"][1]["expected_epoch"] = 3  # type: ignore[index]

    for sequence, event in enumerate(events, start=1):
        event["sequence"] = sequence

    with pytest.raises(MeasurementError, match="topology"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_multi_activity_capsule_attributes_one_assembled_turn_per_activity() -> None:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 50,
            "sequence": 1,
            "epoch": 1,
            "text": "first phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 100,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 500,
            "sequence": 3,
            "epoch": 1,
            "text": "second phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 550,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        response: int | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    session["wire_facts"] = [
        fact("caller_activity_start", 0, 0, activity=2),
        fact("caller_audio_sent", 20, 1, activity=2, audio_bytes=640),
        fact("caller_activity_end", 100, 2, activity=2),
        fact("response_open", 200, 3, activity=2, response=1),
        fact("audio_received", 200, 4, activity=2, response=1, audio_bytes=320),
        fact("response_terminal", 220, 5, activity=2, response=1),
        fact("caller_activity_start", 450, 6, activity=3),
        fact("caller_audio_sent", 470, 7, activity=3, audio_bytes=640),
        fact("caller_activity_end", 550, 8, activity=3),
        fact("response_open", 650, 9, activity=3, response=2),
        fact("audio_received", 650, 10, activity=3, response=2, audio_bytes=320),
        fact("response_terminal", 670, 11, activity=3, response=2),
        fact("teardown_complete", 700, 12, activity=None),
    ]
    session["wire_facts"].extend(
        (
            fact("caller_speech_end", 80, 13, activity=2),
            fact("caller_speech_end", 530, 14, activity=3),
        )
    )
    capsule["activities"] = [
        {
            "activity_ordinal": 2,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "first phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 80,
            "advance_to_ms": 900,
        },
        {
            "activity_ordinal": 3,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "second phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 530,
            "advance_to_ms": 900,
        },
    ]

    _normalize_wire_sequences(capsule)
    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    by_activity = {record.activity_ordinal: record for record in records}
    assert {
        ordinal: (
            record.assignment_status,
            record.observed_lifecycle_status,
            record.assembled_turn_count,
            record.duplicate_count,
        )
        for ordinal, record in by_activity.items()
    } == {
        2: ("matched", "retrospective_complete", 1, 0),
        3: ("matched", "retrospective_complete", 1, 0),
    }
    assert by_activity[2].hypothesis_characters == len("firstphrase")
    assert by_activity[3].hypothesis_characters == len("secondphrase")


def test_multi_activity_capsule_does_not_reassign_reversed_exact_transcripts() -> None:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 50,
            "sequence": 1,
            "epoch": 1,
            "text": "second phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 100,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 500,
            "sequence": 3,
            "epoch": 1,
            "text": "first phrase",
        },
        {
            "kind": "turn_complete",
            "at_ms": 550,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]

    def fact(
        kind: str,
        at_ms: int,
        sequence: int,
        *,
        activity: int | None,
        response: int | None = None,
        audio_bytes: int = 0,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "at_ms": at_ms,
            "response_ordinal": response,
            "activity_ordinal": activity,
            "sequence": sequence,
            "epoch": 1,
            "audio_bytes": audio_bytes,
        }

    session["wire_facts"] = [
        fact("caller_activity_start", 0, 0, activity=2),
        fact("caller_audio_sent", 20, 1, activity=2, audio_bytes=640),
        fact("caller_activity_end", 100, 2, activity=2),
        fact("response_open", 200, 3, activity=2, response=1),
        fact("audio_received", 200, 4, activity=2, response=1, audio_bytes=320),
        fact("response_terminal", 220, 5, activity=2, response=1),
        fact("caller_activity_start", 450, 6, activity=3),
        fact("caller_audio_sent", 470, 7, activity=3, audio_bytes=640),
        fact("caller_activity_end", 550, 8, activity=3),
        fact("response_open", 650, 9, activity=3, response=2),
        fact("audio_received", 650, 10, activity=3, response=2, audio_bytes=320),
        fact("response_terminal", 670, 11, activity=3, response=2),
        fact("teardown_complete", 700, 12, activity=None),
    ]
    session["wire_facts"].extend(
        (
            fact("caller_speech_end", 80, 13, activity=2),
            fact("caller_speech_end", 530, 14, activity=3),
        )
    )
    capsule["activities"] = [
        {
            "activity_ordinal": 2,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "first phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 80,
            "advance_to_ms": 900,
        },
        {
            "activity_ordinal": 3,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "second phrase",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 530,
            "advance_to_ms": 900,
        },
    ]

    _normalize_wire_sequences(capsule)
    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    assert all(record.contamination_count == 1 for record in records)
    assert all(
        record.substitutions + record.insertions + record.deletions > 0
        for record in records
    )


def test_measurement_detects_foreign_fragment_even_when_whole_turn_matches() -> None:
    current = ActivityReference(3, "en", "book service today with customer details")
    foreign = ActivityReference(4, "en", "intruder token")
    measurement = replace(
        _activity_input(),
        references=(current, foreign),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                current.text,
            ),
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                15,
                2,
                1,
                foreign.text,
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 3, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.contamination_count == 1


def test_measurement_detects_unique_partial_foreign_subphrase() -> None:
    current = ActivityReference(
        3,
        "en",
        "please schedule the annual heating system inspection for next Tuesday morning",
    )
    foreign = ActivityReference(
        4,
        "en",
        "the caller reported a blue leak beneath the kitchen sink yesterday",
    )
    measurement = replace(
        _activity_input(),
        activity_ordinal=3,
        references=(current, foreign),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                "please schedule the annual heating system inspection ",
            ),
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                15,
                2,
                1,
                "blue leak ",
            ),
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                18,
                3,
                1,
                "for next Tuesday morning",
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 4, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.contamination_count == 1


def test_measurement_detects_foreign_subphrase_inside_one_large_fragment() -> None:
    current = ActivityReference(
        3,
        "en",
        "please schedule the annual heating system inspection for next Tuesday morning",
    )
    foreign = ActivityReference(
        4,
        "en",
        "the caller reported a blue leak beneath the kitchen sink yesterday",
    )
    measurement = replace(
        _activity_input(),
        activity_ordinal=3,
        references=(current, foreign),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                (
                    "please schedule the annual heating system inspection blue leak "
                    "for next Tuesday morning"
                ),
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.contamination_count == 1


def test_measurement_detects_unique_foreign_word_inside_one_large_fragment() -> None:
    current = ActivityReference(
        3,
        "en",
        "please schedule the annual heating system inspection for next Tuesday morning",
    )
    foreign = ActivityReference(
        4,
        "en",
        "the caller reported a blue leak beneath the kitchen sink yesterday",
    )
    measurement = replace(
        _activity_input(),
        activity_ordinal=3,
        references=(current, foreign),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                (
                    "please schedule the annual heating system inspection blue "
                    "for next Tuesday morning"
                ),
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.contamination_count == 1


def test_measurement_detects_unique_foreign_chinese_character() -> None:
    current = ActivityReference(3, "zh", "请安排下周二上午检查供暖系统")
    foreign = ActivityReference(4, "zh", "厨房水槽下面发现漏水问题")
    measurement = replace(
        _activity_input(),
        activity_ordinal=3,
        language="zh",
        references=(current, foreign),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                "请安排下周二上午检查供暖漏系统",
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.contamination_count == 1


def test_measurement_detects_single_token_shared_by_foreign_references() -> None:
    current = ActivityReference(
        3,
        "en",
        "please schedule the annual heating system inspection for next Tuesday morning",
    )
    measurement = replace(
        _activity_input(),
        activity_ordinal=3,
        references=(
            current,
            ActivityReference(4, "en", "the caller reported a blue kitchen leak"),
            ActivityReference(5, "en", "the customer requested a blue bathroom repair"),
        ),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                (
                    "please schedule the annual heating system inspection blue "
                    "for next Tuesday morning"
                ),
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.contamination_count == 1


def test_measurement_does_not_apply_chinese_character_units_to_english_reference() -> None:
    current = ActivityReference(
        3,
        "en",
        "please schedule the annual heating system inspection for next Tuesday morning",
    )
    measurement = replace(
        _activity_input(),
        activity_ordinal=3,
        references=(
            current,
            ActivityReference(4, "en", "the caller reported a blue kitchen leak"),
            ActivityReference(5, "zh", "厨房水槽下面发现漏水问题"),
        ),
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                1,
                1,
                (
                    "please schedule the annual heating system inspection b "
                    "for next Tuesday morning"
                ),
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 2, 1),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.contamination_count == 0


def test_compensating_missing_and_extra_turns_follow_causal_activity_ownership() -> None:
    capsule = _literal_wire_capsule()
    capsule["schema_id"] = "gate_0b_audit_capsule_v6"
    session = capsule["sessions"][0]  # type: ignore[index]
    session["events"] = [
        {
            "kind": "input_transcript_fragment",
            "at_ms": 500,
            "sequence": 1,
            "epoch": 1,
            "text": "book service today",
        },
        {
            "kind": "turn_complete",
            "at_ms": 510,
            "sequence": 2,
            "epoch": 1,
            "text": "",
        },
        {
            "kind": "input_transcript_fragment",
            "at_ms": 800,
            "sequence": 3,
            "epoch": 1,
            "text": "inspect kitchen sink",
        },
        {
            "kind": "turn_complete",
            "at_ms": 810,
            "sequence": 4,
            "epoch": 1,
            "text": "",
        },
    ]
    session["event_activity_ordinals"] = [4, 4, 4, 4]
    facts = session["wire_facts"]
    assert isinstance(facts, list)
    facts.extend(
        [
            {
                "kind": "caller_activity_start",
                "at_ms": 400,
                "response_ordinal": None,
                "activity_ordinal": 4,
                "sequence": 20,
                "epoch": 1,
                "audio_bytes": 0,
            },
            {
                "kind": "caller_audio_sent",
                "at_ms": 420,
                "response_ordinal": None,
                "activity_ordinal": 4,
                "sequence": 21,
                "epoch": 1,
                "audio_bytes": 640,
            },
            {
                "kind": "caller_speech_end",
                "at_ms": 460,
                "response_ordinal": None,
                "activity_ordinal": 4,
                "sequence": 22,
                "epoch": 1,
                "audio_bytes": 0,
            },
            {
                "kind": "caller_activity_end",
                "at_ms": 480,
                "response_ordinal": None,
                "activity_ordinal": 4,
                "sequence": 23,
                "epoch": 1,
                "audio_bytes": 0,
            },
            {
                "kind": "response_open",
                "at_ms": 500,
                "response_ordinal": 2,
                "activity_ordinal": 4,
                "sequence": 24,
                "epoch": 1,
                "audio_bytes": 0,
            },
            {
                "kind": "audio_received",
                "at_ms": 500,
                "response_ordinal": 2,
                "activity_ordinal": 4,
                "sequence": 25,
                "epoch": 1,
                "audio_bytes": 640,
            },
            {
                "kind": "response_terminal",
                "at_ms": 850,
                "response_ordinal": 2,
                "activity_ordinal": 4,
                "sequence": 26,
                "epoch": 1,
                "audio_bytes": 0,
            },
        ]
    )
    activities = capsule["activities"]
    assert isinstance(activities, list)
    activities.append(
        {
            "activity_ordinal": 4,
            "session_ordinal": 1,
            "split": "development",
            "language": "en",
            "condition": "clean",
            "scenario_tags": ["standard"],
            "reference_text": "inspect kitchen sink",
            "critical_spans": [],
            "expected_lifecycle_status": "retrospective_complete",
            "expected_epoch": 1,
            "speech_end_at_ms": 460,
            "advance_to_ms": 1_200,
        }
    )
    _normalize_wire_sequences(capsule)

    records, _ = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=CAMPAIGN_KEY,
    )

    by_activity = {record.activity_ordinal: record for record in records}
    assert by_activity[3].assembled_turn_count == 0
    assert by_activity[3].observed_lifecycle_status == "missing"
    assert by_activity[4].assembled_turn_count == 2
    assert by_activity[4].duplicate_count == 1


def test_capsule_rejects_missing_event_ownership_entry() -> None:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]
    session["event_activity_ordinals"] = [3]

    with pytest.raises(MeasurementError, match="ownership cardinality"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_capsule_rejects_event_owner_that_disagrees_with_actual_sent_audio() -> None:
    capsule = _literal_wire_capsule()
    session = capsule["sessions"][0]  # type: ignore[index]
    session["event_activity_ordinals"] = [4, 4]

    with pytest.raises(MeasurementError, match="ownership is not causal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_duplicate_terminal_cannot_hide_audio_after_the_first_terminal() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    terminal = next(fact for fact in facts if fact["kind"] == "response_terminal")
    facts.append({**terminal, "at_ms": 240})
    _normalize_wire_sequences(capsule)

    with pytest.raises(MeasurementError, match="response-terminal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_orphan_terminal_is_rejected_by_capsule_recomputation() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    terminal = next(fact for fact in facts if fact["kind"] == "response_terminal")
    facts.append({**terminal, "at_ms": 225, "response_ordinal": 99})
    _normalize_wire_sequences(capsule)

    with pytest.raises(MeasurementError, match="response-terminal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_audio_reusing_closed_response_ordinal_with_wrong_identity_is_rejected() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    response_audio = next(fact for fact in facts if fact["kind"] == "audio_received")
    facts.append({**response_audio, "at_ms": 235, "activity_ordinal": 4})
    _normalize_wire_sequences(capsule)

    with pytest.raises(MeasurementError, match="causally attributable"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_response_activity_label_must_match_latest_actual_caller_audio() -> None:
    capsule = _literal_wire_capsule()
    facts = capsule["sessions"][0]["wire_facts"]  # type: ignore[index]
    assert isinstance(facts, list)
    for fact in facts:
        if fact["kind"] in {"response_open", "audio_received", "response_terminal"}:
            fact["activity_ordinal"] = 4

    with pytest.raises(MeasurementError, match="ownership is not causal"):
        derive_primitive_records_from_capsule(
            capsule,
            policies_ms=(250,),
            commitment_key=CAMPAIGN_KEY,
        )


def test_capsule_accounting_rejects_missing_units_identity_and_unbounded_failures() -> None:
    capsule = {
        "schema_id": "gate_0b_audit_capsule_v6",
        "campaign_id": "campaign_1",
        "policy_ms": 250,
        "source_fact_bundle_sha256": "1" * 64,
        "execution_started_at": "2026-07-15T15:00:00Z",
        "execution_completed_at": "2026-07-15T15:01:00Z",
        "provider_revision": None,
        "runtime_identity_before_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_after_sha256": RUNTIME_IDENTITY_SHA256,
        "runtime_identity_before": RUNTIME_IDENTITY_REPORT,
        "runtime_identity_after": RUNTIME_IDENTITY_REPORT,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": "development",
            "units": [
                {
                    "kind": "session",
                    "ordinal": 9,
                    "metadata_complete": False,
                    "complete": False,
                    "error_code": "private provider exception text",
                    "provider_request_count": 1,
                    "observed_elapsed_ms": 1,
                    "input_audio_duration_ms": 1,
                    "output_audio_bytes": 0,
                    "input_audio_tokens": 0,
                    "output_audio_tokens": 0,
                    "input_text_tokens": 0,
                    "output_text_tokens": 0,
                }
            ],
        },
        "sessions": [],
        "activities": [],
        "no_speech_windows": [],
    }

    with pytest.raises(MeasurementError, match="accounting"):
        derive_audit_capsule_accounting(capsule)


def test_measurement_recomputes_alignment_lifecycle_and_keyed_commitment() -> None:
    record = measure_activity(
        _activity_input(),
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assignment_status == "matched"
    assert record.observed_lifecycle_status == "retrospective_complete"
    assert record.assembled_turn_count == 1
    assert record.reference_characters == record.hypothesis_characters
    assert (record.substitutions, record.insertions, record.deletions) == (0, 0, 0)
    assert record.reference_words == record.hypothesis_words == 3
    assert record.critical_spans == (
        CriticalSpanFact(kind="correction", exact=True),
    )
    assert verify_record_commitment(record, commitment_key=CAMPAIGN_KEY) is True

    tampered = replace(record, substitutions=1)
    assert verify_record_commitment(tampered, commitment_key=CAMPAIGN_KEY) is False


def test_measurement_counts_prior_epoch_events_as_stale_and_fails_fidelity() -> None:
    current = _activity_input()
    measurement = replace(
        current,
        expected_epoch=2,
        events=(
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                5,
                1,
                1,
                "stale prior epoch text",
            ),
            CallerTurnEvent(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                10,
                2,
                2,
                "book service today",
            ),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 20, 3, 2),
        ),
    )

    record = measure_activity(
        measurement,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.stale_count == 1
    assert record.cross_epoch_acceptance_count == 0
    assert record.observed_lifecycle_status == "retrospective_complete"
    assert record.insertions + record.substitutions + record.deletions > 0


def test_measurement_preserves_low_fidelity_and_duplicate_failures() -> None:
    duplicated = replace(
        _activity_input(),
        events=(
            *_events("book service someday"),
            CallerTurnEvent(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, 300, 3, 1, "extra"),
            CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 310, 4, 1),
        ),
        advance_to_ms=600,
    )

    record = measure_activity(
        duplicated,
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    assert record.assembled_turn_count == 2
    assert record.duplicate_count == 1
    assert record.late_fragment_mutation_count == 1
    assert record.substitutions > 0 or record.insertions > 0 or record.deletions > 0


def test_primitive_cross_fields_prevent_wer_and_ambiguity_bypass() -> None:
    record = measure_activity(
        _activity_input(),
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )

    with pytest.raises(MeasurementError, match="requires word measurements"):
        replace(
            record,
            reference_words=None,
            hypothesis_words=None,
            word_substitutions=None,
            word_insertions=None,
            word_deletions=None,
        )
    with pytest.raises(MeasurementError, match="ambiguous assignment"):
        replace(record, assignment_status="ambiguous")


def test_signed_merkle_root_binds_complete_order_independent_record_set() -> None:
    first = measure_activity(
        _activity_input(),
        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
        commitment_key=CAMPAIGN_KEY,
    )
    second = replace(
        first,
        activity_ordinal=4,
        commitment="",
    )
    second = ActivityPrimitiveRecord.with_commitment(second, commitment_key=CAMPAIGN_KEY)
    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    signed = build_signed_record_root(
        activity_records=(second, first),
        no_speech_records=(),
        campaign_id="campaign_1",
        signing_key=signing_key,
        key_id="evidence_custodian_1",
    )

    assert verify_signed_record_root(
        signed,
        activity_records=(first, second),
        no_speech_records=(),
        campaign_id="campaign_1",
        public_key=public_key,
        expected_key_id="evidence_custodian_1",
    ) is True
    changed_signature = dict(signed)
    changed_signature["signature"] = base64.b64encode(b"x" * 64).decode("ascii")
    assert verify_signed_record_root(
        changed_signature,
        activity_records=(first, second),
        no_speech_records=(),
        campaign_id="campaign_1",
        public_key=public_key,
        expected_key_id="evidence_custodian_1",
    ) is False
