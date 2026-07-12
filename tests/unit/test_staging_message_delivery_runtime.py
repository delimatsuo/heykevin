"""Staging-only message-delivery control-plane tests."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


SCRIPT_PATH = Path("scripts/manage_staging_message_delivery.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "manage_staging_message_delivery", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGcloud:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        *args: str,
        timeout: int | None = None,
        operation: str = "gcloud mutation",
    ) -> None:
        del timeout, operation
        self.calls.append(args)


class SnapshotGcloud(FakeGcloud):
    def __init__(self, responses):
        super().__init__()
        self.responses = responses

    def json(
        self,
        *args: str,
        timeout: int | None = None,
        operation: str = "gcloud read",
    ):
        del timeout, operation
        for prefix, response in self.responses:
            if args[: len(prefix)] == prefix:
                return deepcopy(response)
        raise AssertionError(f"Unexpected gcloud read: {args}")


def _ready_responses(module, channel):
    policies = []
    for position, policy in enumerate(
        module.build_desired_policies((channel,)).values(), start=1
    ):
        stored = deepcopy(policy)
        stored["name"] = (
            f"projects/{module.RUNTIME_PROJECT}/alertPolicies/policy-{position}"
        )
        policies.append(stored)

    indexes = []
    for index in module.REQUIRED_INDEXES:
        indexes.append(
            {
                "collectionGroup": module.COLLECTION_GROUP,
                "queryScope": "COLLECTION",
                "state": "READY",
                "fields": [
                    {"fieldPath": field, "order": "ASCENDING"}
                    for field in index.fields
                ],
            }
        )

    return (
        (
            ("run", "services", "describe"),
            {
                "metadata": {
                    "annotations": {"run.googleapis.com/minScale": "1"}
                },
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "run.googleapis.com/cpu-throttling": "false"
                            }
                        }
                    }
                },
                "status": {
                    "latestReadyRevisionName": "kevin-api-staging-ready",
                    "traffic": [
                        {
                            "revisionName": "kevin-api-staging-ready",
                            "percent": 100,
                        }
                    ],
                },
            },
        ),
        (("firestore", "indexes", "composite", "list"), indexes),
        (
            ("firestore", "fields", "ttls", "list"),
            [
                {
                    "name": (
                        f"projects/{module.FIRESTORE_PROJECT}/databases/(default)/"
                        f"collectionGroups/{module.COLLECTION_GROUP}/fields/"
                        f"{module.TTL_FIELD}"
                    ),
                    "ttlConfig": {"state": "ACTIVE"},
                }
            ],
        ),
        (("monitoring", "policies", "list"), policies),
        (
            ("beta", "monitoring", "channels", "list"),
            [{"name": channel, "enabled": True, "verificationStatus": "VERIFIED"}],
        ),
    )


def test_targets_are_immutable_and_staging_only():
    module = _load_module()

    module.validate_target(
        runtime_project=module.RUNTIME_PROJECT,
        firestore_project=module.FIRESTORE_PROJECT,
        region=module.REGION,
        service=module.SERVICE,
    )

    invalid_targets = (
        (module.RUNTIME_PROJECT, module.RUNTIME_PROJECT, module.REGION, module.SERVICE),
        (module.RUNTIME_PROJECT, module.FIRESTORE_PROJECT, module.REGION, "kevin-api"),
        ("another-project", module.FIRESTORE_PROJECT, module.REGION, module.SERVICE),
        (module.RUNTIME_PROJECT, module.FIRESTORE_PROJECT, "europe-west1", module.SERVICE),
    )
    for runtime_project, firestore_project, region, service in invalid_targets:
        with pytest.raises(module.PreparationError):
            module.validate_target(
                runtime_project=runtime_project,
                firestore_project=firestore_project,
                region=region,
                service=service,
            )


def test_parse_indexes_accepts_current_gcloud_name_only_shape():
    module = _load_module()
    expected = module.REQUIRED_INDEXES[0]

    parsed = module._parse_indexes(
        (
            {
                "name": (
                    f"projects/{module.FIRESTORE_PROJECT}/databases/(default)/"
                    f"collectionGroups/{module.COLLECTION_GROUP}/indexes/index-1"
                ),
                "queryScope": "COLLECTION",
                "state": "READY",
                "fields": [
                    {"fieldPath": field, "order": "ASCENDING"}
                    for field in expected.fields
                ],
            },
        )
    )

    assert parsed[expected] == "READY"


def test_alert_policies_are_payload_free_and_cover_required_events():
    module = _load_module()
    channel = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"

    policies = module.build_desired_policies((channel,))
    rendered = json.dumps(policies, sort_keys=True)

    assert len(policies) == 4
    assert 'resource.labels.service_name=\\"kevin-api-staging\\"' in rendered
    assert 'resource.labels.location=\\"us-central1\\"' in rendered
    for event in module.REQUIRED_ALERT_EVENTS:
        assert event in rendered
    for policy in policies.values():
        assert policy["notificationChannels"] == [channel]
        assert policy["enabled"] is True
        assert policy["alertStrategy"]["notificationRateLimit"] == {"period": "300s"}
        assert "labelExtractors" not in json.dumps(policy)


def test_monitoring_api_output_fields_do_not_create_policy_drift():
    module = _load_module()
    channels = (
        f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1",
        f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-2",
    )
    desired = next(iter(module.build_desired_policies(channels).values()))
    stored = deepcopy(desired)
    stored["name"] = f"projects/{module.RUNTIME_PROJECT}/alertPolicies/policy-1"
    stored["creationRecord"] = {"mutateTime": "2026-07-12T00:00:00Z"}
    stored["conditions"][0]["name"] = "condition-1"
    stored["alertStrategy"]["notificationPrompts"] = ["OPENED"]
    stored["notificationChannels"].reverse()

    assert module._policy_matches(stored, desired)


def test_notification_channel_resolution_fails_closed_when_ambiguous():
    module = _load_module()
    channel_1 = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"
    channel_2 = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-2"

    with pytest.raises(module.PreparationError, match="notification channel"):
        module.resolve_notification_channels(
            configured="",
            managed_policies=(),
            enabled_channels=(channel_1, channel_2),
        )

    assert module.resolve_notification_channels(
        configured="",
        managed_policies=(),
        enabled_channels=(channel_1,),
    ) == (channel_1,)

    assert module.resolve_notification_channels(
        configured=channel_2,
        managed_policies=(),
        enabled_channels=(channel_1, channel_2),
    ) == (channel_2,)


def test_notification_channel_resolution_preserves_existing_managed_route():
    module = _load_module()
    channel = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"
    policies = ({"notificationChannels": [channel]}, {"notificationChannels": [channel]})

    assert module.resolve_notification_channels(
        configured="",
        managed_policies=policies,
        enabled_channels=(channel,),
    ) == (channel,)


def test_notification_channel_resources_accept_the_known_project_number():
    module = _load_module()
    channel = (
        f"projects/{module.RUNTIME_PROJECT_NUMBER}/notificationChannels/channel-1"
    )

    assert module.resolve_notification_channels(
        configured=channel,
        managed_policies=(),
        enabled_channels=(channel,),
    ) == (channel,)


def test_notification_channel_alias_resolves_to_provider_canonical_name():
    module = _load_module()
    configured = (
        f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"
    )
    canonical = (
        f"projects/{module.RUNTIME_PROJECT_NUMBER}/notificationChannels/channel-1"
    )

    assert module.resolve_notification_channels(
        configured=configured,
        managed_policies=(),
        enabled_channels=(canonical,),
    ) == (canonical,)

    assert module.resolve_notification_channels(
        configured="",
        managed_policies=({"notificationChannels": [configured]},),
        enabled_channels=(canonical,),
    ) == (canonical,)


def test_runtime_preparation_preserves_latest_traffic_behavior():
    module = _load_module()
    gcloud = FakeGcloud()

    module.apply_runtime_configuration(gcloud)

    assert len(gcloud.calls) == 1
    command = gcloud.calls[0]
    assert command[:4] == ("run", "services", "update", module.SERVICE)
    assert f"--project={module.RUNTIME_PROJECT}" in command
    assert f"--region={module.REGION}" in command
    assert "--no-cpu-throttling" in command
    assert "--min=1" in command
    assert "--deploy-health-check" in command
    assert "--no-traffic" not in command
    assert "deploy" not in command


def test_stale_staging_traffic_split_is_restored_to_latest():
    module = _load_module()
    gcloud = FakeGcloud()

    module.restore_latest_traffic(gcloud)

    assert gcloud.calls == [
        (
            "run",
            "services",
            "update-traffic",
            module.SERVICE,
            f"--project={module.RUNTIME_PROJECT}",
            f"--region={module.REGION}",
            "--to-latest",
        )
    ]


def test_latest_revision_must_receive_all_traffic():
    module = _load_module()

    assert module._latest_receives_all_traffic(
        {
            "status": {
                "latestReadyRevisionName": "latest",
                "traffic": [{"revisionName": "latest", "percent": 100}],
            }
        }
    )
    assert not module._latest_receives_all_traffic(
        {
            "status": {
                "latestReadyRevisionName": "latest",
                "traffic": [
                    {"revisionName": "latest", "percent": 5},
                    {"revisionName": "previous", "percent": 95},
                ],
            }
        }
    )


def test_index_creation_is_exact_and_scoped_to_staging_firestore():
    module = _load_module()
    gcloud = FakeGcloud()

    for index in module.REQUIRED_INDEXES:
        module.create_index(gcloud, index)

    assert len(gcloud.calls) == 3
    for command, index in zip(gcloud.calls, module.REQUIRED_INDEXES, strict=True):
        assert command[:4] == ("firestore", "indexes", "composite", "create")
        assert f"--project={module.FIRESTORE_PROJECT}" in command
        assert "--collection-group=message_delivery_receipts" in command
        field_flags = tuple(arg for arg in command if arg.startswith("--field-config="))
        assert field_flags == tuple(
            f"--field-config=field-path={field},order=ascending"
            for field in index.fields
        )


def test_ttl_creation_is_exact_and_scoped_to_staging_firestore():
    module = _load_module()
    gcloud = FakeGcloud()

    module.enable_ttl(gcloud)

    assert gcloud.calls == [
        (
            "firestore",
            "fields",
            "ttls",
            "update",
            "expires_at",
            "--collection-group=message_delivery_receipts",
            "--database=(default)",
            f"--project={module.FIRESTORE_PROJECT}",
            "--enable-ttl",
        )
    ]


def test_ready_snapshot_is_idempotent():
    module = _load_module()
    channel = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"
    gcloud = SnapshotGcloud(_ready_responses(module, channel))

    snapshot = module.read_snapshot(gcloud)
    channels = module.resolve_notification_channels(
        configured="",
        managed_policies=tuple(snapshot.managed_policies.values()),
        enabled_channels=snapshot.enabled_channels,
    )

    assert module.assess(snapshot, channels) == ()
    module.apply_changes(gcloud, snapshot, channels)
    assert gcloud.calls == []


def test_ambiguous_notification_route_fails_before_any_mutation():
    module = _load_module()
    channel_1 = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"
    channel_2 = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-2"
    responses = list(_ready_responses(module, channel_1))
    responses[-2] = (("monitoring", "policies", "list"), [])
    responses[-1] = (
        ("beta", "monitoring", "channels", "list"),
        [
            {"name": channel_1, "enabled": True},
            {"name": channel_2, "enabled": True},
        ],
    )
    gcloud = SnapshotGcloud(tuple(responses))

    with pytest.raises(module.PreparationError, match="notification channel"):
        module.run("apply", gcloud)

    assert gcloud.calls == []


def test_missing_controls_are_applied_once_with_runtime_last():
    module = _load_module()
    channel = f"projects/{module.RUNTIME_PROJECT}/notificationChannels/channel-1"
    snapshot = module.Snapshot(
        runtime_ready=False,
        latest_receives_all_traffic=True,
        indexes={index: None for index in module.REQUIRED_INDEXES},
        ttl_state=None,
        managed_policies={},
        unknown_policy_keys=(),
        enabled_channels=(channel,),
    )
    gcloud = FakeGcloud()

    module.apply_changes(gcloud, snapshot, (channel,))

    assert len(gcloud.calls) == 9
    assert gcloud.calls[-1][:4] == ("run", "services", "update", module.SERVICE)
    policy_calls = [
        command
        for command in gcloud.calls
        if command[:3] == ("monitoring", "policies", "create")
    ]
    assert len(policy_calls) == 4
    for command in policy_calls:
        policy_arg = next(
            arg for arg in command if arg.startswith("--policy-from-file=")
        )
        assert not Path(policy_arg.split("=", 1)[1]).exists()


def test_gcloud_failures_do_not_forward_provider_output(monkeypatch):
    module = _load_module()

    class FailedResult:
        returncode = 1
        stdout = "sensitive-payload"
        stderr = "provider-token"

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: FailedResult())

    with pytest.raises(module.PreparationError) as exc_info:
        module.Gcloud().json(
            "monitoring",
            "policies",
            "list",
            operation="staging Monitoring policy read",
        )

    message = str(exc_info.value)
    assert message == "staging Monitoring policy read failed"
    assert "sensitive-payload" not in message
    assert "provider-token" not in message
