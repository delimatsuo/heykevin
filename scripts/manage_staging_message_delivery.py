#!/usr/bin/env python3
"""Audit or prepare staging infrastructure for message-delivery workers."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUNTIME_PROJECT = "kevin-491315"
RUNTIME_PROJECT_NUMBER = "752910912062"
FIRESTORE_PROJECT = "kevin-staging-491315"
REGION = "us-central1"
SERVICE = "kevin-api-staging"
COLLECTION_GROUP = "message_delivery_receipts"
TTL_FIELD = "expires_at"

READ_TIMEOUT_SECONDS = 90
MUTATION_TIMEOUT_SECONDS = 1_800
DEFAULT_WAIT_SECONDS = 1_800
POLL_SECONDS = 20


class PreparationError(RuntimeError):
    """A payload-free staging preparation failure."""


@dataclass(frozen=True)
class IndexSpec:
    fields: tuple[str, ...]


@dataclass(frozen=True)
class PolicySpec:
    key: str
    display_name: str
    events: tuple[str, ...]


REQUIRED_INDEXES = (
    IndexSpec(("status", "created_at")),
    IndexSpec(("status", "next_reconcile_at")),
    IndexSpec(("call_projection_pending", "call_projection_next_at")),
)

POLICY_SPECS = (
    PolicySpec(
        key="terminal_failures",
        display_name="Hey Kevin staging delivery terminal failures",
        events=("message_delivery event=terminal_failure",),
    ),
    PolicySpec(
        key="durability_failures",
        display_name="Hey Kevin staging delivery durability failures",
        events=(
            "message_delivery event=callback_config_invalid",
            "message_delivery event=receipt_registration_error",
            "message_delivery event=receipt_registration_failed",
            "message_delivery event=submission_persist_failed",
            "message_delivery event=submission_failure_store_error",
            "message_delivery event=receipt_storage_error",
            "message_delivery event=call_projection_error",
            "message_delivery event=call_projection_failed",
        ),
    ),
    PolicySpec(
        key="worker_failures",
        display_name="Hey Kevin staging delivery worker failures",
        events=(
            "message_delivery event=reconciliation_list_failed",
            "message_delivery event=reconciliation_fetch_failed",
            "message_delivery event=projection_list_failed",
            "post_call_handoff event=worker_component_error",
        ),
    ),
    PolicySpec(
        key="reconciliation_pending",
        display_name="Hey Kevin staging delivery reconciliation pending",
        events=("message_delivery event=reconciliation_pending",),
    ),
)

REQUIRED_ALERT_EVENTS = tuple(
    event for policy in POLICY_SPECS for event in policy.events
)

_RESOURCE_PROJECT_RE = (
    rf"(?:{re.escape(RUNTIME_PROJECT)}|{re.escape(RUNTIME_PROJECT_NUMBER)})"
)
_CHANNEL_RE = re.compile(
    rf"projects/{_RESOURCE_PROJECT_RE}/notificationChannels/[A-Za-z0-9_-]+"
)
_POLICY_NAME_RE = re.compile(
    rf"projects/{_RESOURCE_PROJECT_RE}/alertPolicies/[A-Za-z0-9_-]+"
)
_MANAGED_LABELS = {
    "managed_by": "github_actions",
    "environment": "staging",
    "component": "message_delivery",
    "control_plane": "receipts",
}


@dataclass(frozen=True)
class Snapshot:
    runtime_ready: bool
    latest_receives_all_traffic: bool
    indexes: Mapping[IndexSpec, str | None]
    ttl_state: str | None
    managed_policies: Mapping[str, Mapping[str, Any]]
    unknown_policy_keys: tuple[str, ...]
    enabled_channels: tuple[str, ...]


class Gcloud:
    """Run gcloud without forwarding provider output into release logs."""

    def _execute(self, args: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
        try:
            result = subprocess.run(
                ["gcloud", *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise PreparationError("gcloud is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise PreparationError("gcloud command timed out") from exc

        if result.returncode != 0:
            raise PreparationError("gcloud command failed")
        return result

    def json(self, *args: str, timeout: int = READ_TIMEOUT_SECONDS) -> Any:
        result = self._execute((*args, "--format=json", "--quiet"), timeout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PreparationError("gcloud returned invalid JSON") from exc

    def run(self, *args: str, timeout: int | None = None) -> None:
        self._execute(
            (*args, "--quiet"),
            timeout if timeout is not None else MUTATION_TIMEOUT_SECONDS,
        )


def validate_target(
    *, runtime_project: str, firestore_project: str, region: str, service: str
) -> None:
    expected = (RUNTIME_PROJECT, FIRESTORE_PROJECT, REGION, SERVICE)
    received = (runtime_project, firestore_project, region, service)
    if received != expected or runtime_project == firestore_project:
        raise PreparationError("refused non-staging target")


def _validate_channel_name(name: str) -> None:
    if _CHANNEL_RE.fullmatch(name) is None:
        raise PreparationError("invalid staging notification channel resource")


def _channel_id(name: str) -> str:
    _validate_channel_name(name)
    return name.rsplit("/", 1)[-1]


def build_log_filter(events: Sequence[str]) -> str:
    clauses = []
    for event in events:
        escaped = event.replace("\\", "\\\\").replace('"', '\\"')
        clauses.append(
            f'(textPayload:"{escaped}" OR jsonPayload.message:"{escaped}")'
        )

    joined = "\n  OR ".join(clauses)
    return (
        'resource.type="cloud_run_revision"\n'
        f'resource.labels.service_name="{SERVICE}"\n'
        f'resource.labels.location="{REGION}"\n'
        f"(\n  {joined}\n)"
    )


def build_desired_policies(
    notification_channels: Sequence[str],
) -> dict[str, dict[str, Any]]:
    channels = tuple(sorted(set(notification_channels)))
    if not channels:
        raise PreparationError("at least one notification channel is required")
    for channel in channels:
        _validate_channel_name(channel)

    policies: dict[str, dict[str, Any]] = {}
    for spec in POLICY_SPECS:
        policies[spec.key] = {
            "displayName": spec.display_name,
            "documentation": {
                "content": (
                    "Staging-only message-delivery control-plane alert. "
                    "Follow docs/message-delivery-receipts.md; do not include "
                    "message payloads, transcripts, or phone numbers in incident notes."
                ),
                "mimeType": "text/markdown",
            },
            "conditions": [
                {
                    "displayName": f"Staging log match: {spec.key}",
                    "conditionMatchedLog": {"filter": build_log_filter(spec.events)},
                }
            ],
            "combiner": "OR",
            "alertStrategy": {
                "notificationRateLimit": {"period": "300s"},
                "autoClose": "1800s",
            },
            "notificationChannels": list(channels),
            "enabled": True,
            "userLabels": {**_MANAGED_LABELS, "policy_key": spec.key},
        }
    return policies


def resolve_notification_channels(
    *,
    configured: str,
    managed_policies: Sequence[Mapping[str, Any]],
    enabled_channels: Sequence[str],
) -> tuple[str, ...]:
    enabled = tuple(sorted(set(enabled_channels)))
    enabled_by_id: dict[str, str] = {}
    for channel in enabled:
        channel_id = _channel_id(channel)
        if channel_id in enabled_by_id and enabled_by_id[channel_id] != channel:
            raise PreparationError("duplicate staging notification channel identity")
        enabled_by_id[channel_id] = channel

    configured_channels = tuple(
        sorted(set(part for part in re.split(r"[,\s]+", configured.strip()) if part))
    )
    if configured_channels:
        configured_ids = tuple(sorted({_channel_id(channel) for channel in configured_channels}))
        if not set(configured_ids).issubset(enabled_by_id):
            raise PreparationError(
                "configured notification channel is unavailable or disabled"
            )
        return tuple(enabled_by_id[channel_id] for channel_id in configured_ids)

    existing_routes: set[tuple[str, ...]] = set()
    for policy in managed_policies:
        route_names = tuple(sorted(set(policy.get("notificationChannels") or ())))
        if not route_names:
            continue
        existing_routes.add(tuple(sorted({_channel_id(name) for name in route_names})))

    if len(existing_routes) > 1:
        raise PreparationError("managed policies have ambiguous notification channels")
    if existing_routes:
        route_ids = next(iter(existing_routes))
        if not set(route_ids).issubset(enabled_by_id):
            raise PreparationError("managed notification channel is unavailable or disabled")
        return tuple(enabled_by_id[channel_id] for channel_id in route_ids)

    if len(enabled_by_id) == 1:
        return tuple(enabled_by_id.values())
    raise PreparationError(
        "notification channel selection is missing or ambiguous"
    )


def apply_runtime_configuration(gcloud: Gcloud) -> None:
    gcloud.run(
        "run",
        "services",
        "update",
        SERVICE,
        f"--project={RUNTIME_PROJECT}",
        f"--region={REGION}",
        "--no-cpu-throttling",
        "--min=1",
        "--deploy-health-check",
    )


def restore_latest_traffic(gcloud: Gcloud) -> None:
    gcloud.run(
        "run",
        "services",
        "update-traffic",
        SERVICE,
        f"--project={RUNTIME_PROJECT}",
        f"--region={REGION}",
        "--to-latest",
    )


def create_index(gcloud: Gcloud, index: IndexSpec) -> None:
    field_flags = tuple(
        f"--field-config=field-path={field},order=ascending"
        for field in index.fields
    )
    gcloud.run(
        "firestore",
        "indexes",
        "composite",
        "create",
        f"--project={FIRESTORE_PROJECT}",
        "--database=(default)",
        f"--collection-group={COLLECTION_GROUP}",
        "--query-scope=collection",
        *field_flags,
    )


def enable_ttl(gcloud: Gcloud) -> None:
    gcloud.run(
        "firestore",
        "fields",
        "ttls",
        "update",
        TTL_FIELD,
        f"--collection-group={COLLECTION_GROUP}",
        "--database=(default)",
        f"--project={FIRESTORE_PROJECT}",
        "--enable-ttl",
    )


def _require_mapping(value: Any, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PreparationError(f"unexpected {operation} response")
    return value


def _require_list(value: Any, operation: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise PreparationError(f"unexpected {operation} response")
    return value


def _nested_mapping(value: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, Mapping) else {}


def _runtime_is_ready(runtime: Mapping[str, Any]) -> bool:
    service_annotations = _nested_mapping(runtime, "metadata", "annotations")
    template_annotations = _nested_mapping(
        runtime, "spec", "template", "metadata", "annotations"
    )
    template_scaling = _nested_mapping(runtime, "spec", "template", "scaling")
    service_scaling = _nested_mapping(runtime, "spec", "scaling")

    cpu_throttling = template_annotations.get(
        "run.googleapis.com/cpu-throttling", "true"
    )
    minimum_instances = (
        service_annotations.get("run.googleapis.com/minScale")
        or template_annotations.get("autoscaling.knative.dev/minScale")
        or service_scaling.get("minInstanceCount")
        or template_scaling.get("minInstanceCount")
        or 0
    )
    try:
        minimum = int(minimum_instances)
    except (TypeError, ValueError):
        return False
    return str(cpu_throttling).lower() == "false" and minimum >= 1


def _latest_receives_all_traffic(runtime: Mapping[str, Any]) -> bool:
    status = _nested_mapping(runtime, "status")
    latest_revision = status.get("latestReadyRevisionName")
    traffic = status.get("traffic")
    if not isinstance(latest_revision, str) or not isinstance(traffic, list):
        return False
    percentage = 0
    for target in traffic:
        if not isinstance(target, Mapping):
            continue
        if target.get("revisionName") != latest_revision:
            continue
        try:
            percentage += int(target.get("percent", 0))
        except (TypeError, ValueError):
            return False
    return percentage == 100


def _parse_indexes(items: Sequence[Mapping[str, Any]]) -> dict[IndexSpec, str | None]:
    states: dict[IndexSpec, str | None] = {index: None for index in REQUIRED_INDEXES}
    for item in items:
        if item.get("collectionGroup") != COLLECTION_GROUP:
            continue
        if str(item.get("queryScope", "")).upper() != "COLLECTION":
            continue

        fields = []
        valid = True
        for field in item.get("fields") or ():
            if not isinstance(field, Mapping):
                valid = False
                break
            field_path = field.get("fieldPath")
            if field_path == "__name__":
                continue
            if not isinstance(field_path, str):
                valid = False
                break
            if str(field.get("order", "")).upper() != "ASCENDING":
                valid = False
                break
            fields.append(field_path)
        if not valid:
            continue

        spec = IndexSpec(tuple(fields))
        if spec in states:
            state = str(item.get("state", "STATE_UNSPECIFIED")).upper()
            if states[spec] == "READY" and state != "READY":
                continue
            states[spec] = state
    return states


def _parse_ttl_state(items: Sequence[Mapping[str, Any]]) -> str | None:
    matches = []
    expected_suffix = f"/collectionGroups/{COLLECTION_GROUP}/fields/{TTL_FIELD}"
    for item in items:
        if str(item.get("name", "")).endswith(expected_suffix):
            ttl_config = item.get("ttlConfig")
            if isinstance(ttl_config, Mapping):
                matches.append(str(ttl_config.get("state", "STATE_UNSPECIFIED")).upper())
    if len(matches) > 1:
        raise PreparationError("duplicate staging TTL configuration")
    return matches[0] if matches else None


def _parse_managed_policies(
    items: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], tuple[str, ...]]:
    policies: dict[str, Mapping[str, Any]] = {}
    unknown = []
    known_keys = {spec.key for spec in POLICY_SPECS}
    for item in items:
        labels = item.get("userLabels")
        if not isinstance(labels, Mapping):
            continue
        if any(labels.get(key) != value for key, value in _MANAGED_LABELS.items()):
            continue
        key = labels.get("policy_key")
        if not isinstance(key, str) or key not in known_keys:
            unknown.append(str(key or "missing"))
            continue
        if key in policies:
            raise PreparationError("duplicate managed staging alert policy")
        policies[key] = item
    return policies, tuple(sorted(unknown))


def _parse_enabled_channels(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    channels = []
    for item in items:
        if item.get("enabled") is not True:
            continue
        if item.get("verificationStatus") == "UNVERIFIED":
            continue
        name = item.get("name")
        if isinstance(name, str) and _CHANNEL_RE.fullmatch(name):
            channels.append(name)
    return tuple(sorted(set(channels)))


def read_snapshot(gcloud: Gcloud) -> Snapshot:
    runtime = _require_mapping(
        gcloud.json(
            "run",
            "services",
            "describe",
            SERVICE,
            f"--project={RUNTIME_PROJECT}",
            f"--region={REGION}",
        ),
        "Cloud Run",
    )
    indexes = _require_list(
        gcloud.json(
            "firestore",
            "indexes",
            "composite",
            "list",
            f"--project={FIRESTORE_PROJECT}",
            "--database=(default)",
        ),
        "Firestore index",
    )
    ttls = _require_list(
        gcloud.json(
            "firestore",
            "fields",
            "ttls",
            "list",
            f"--project={FIRESTORE_PROJECT}",
            "--database=(default)",
            f"--collection-group={COLLECTION_GROUP}",
        ),
        "Firestore TTL",
    )
    policies = _require_list(
        gcloud.json(
            "monitoring",
            "policies",
            "list",
            f"--project={RUNTIME_PROJECT}",
        ),
        "Monitoring policy",
    )
    channels = _require_list(
        gcloud.json(
            "beta",
            "monitoring",
            "channels",
            "list",
            f"--project={RUNTIME_PROJECT}",
        ),
        "Monitoring channel",
    )

    managed_policies, unknown_policy_keys = _parse_managed_policies(policies)
    return Snapshot(
        runtime_ready=_runtime_is_ready(runtime),
        latest_receives_all_traffic=_latest_receives_all_traffic(runtime),
        indexes=_parse_indexes(indexes),
        ttl_state=_parse_ttl_state(ttls),
        managed_policies=managed_policies,
        unknown_policy_keys=unknown_policy_keys,
        enabled_channels=_parse_enabled_channels(channels),
    )


def _policy_matches(
    existing: Mapping[str, Any], desired: Mapping[str, Any]
) -> bool:
    if existing.get("displayName") != desired.get("displayName"):
        return False
    if existing.get("documentation") != desired.get("documentation"):
        return False
    if existing.get("combiner") != desired.get("combiner"):
        return False
    if sorted(existing.get("notificationChannels") or ()) != sorted(
        desired.get("notificationChannels") or ()
    ):
        return False
    if existing.get("enabled", True) is not True:
        return False

    existing_conditions = existing.get("conditions")
    desired_conditions = desired.get("conditions")
    if not isinstance(existing_conditions, list) or not isinstance(
        desired_conditions, list
    ):
        return False
    if len(existing_conditions) != 1 or len(desired_conditions) != 1:
        return False
    existing_condition = existing_conditions[0]
    desired_condition = desired_conditions[0]
    if not isinstance(existing_condition, Mapping) or not isinstance(
        desired_condition, Mapping
    ):
        return False
    if existing_condition.get("displayName") != desired_condition.get("displayName"):
        return False
    existing_log_match = existing_condition.get("conditionMatchedLog")
    desired_log_match = desired_condition.get("conditionMatchedLog")
    if not isinstance(existing_log_match, Mapping) or not isinstance(
        desired_log_match, Mapping
    ):
        return False
    if existing_log_match.get("filter") != desired_log_match.get("filter"):
        return False

    existing_strategy = existing.get("alertStrategy")
    desired_strategy = desired.get("alertStrategy")
    if not isinstance(existing_strategy, Mapping) or not isinstance(
        desired_strategy, Mapping
    ):
        return False
    existing_rate_limit = existing_strategy.get("notificationRateLimit")
    desired_rate_limit = desired_strategy.get("notificationRateLimit")
    if not isinstance(existing_rate_limit, Mapping) or not isinstance(
        desired_rate_limit, Mapping
    ):
        return False
    if existing_rate_limit.get("period") != desired_rate_limit.get("period"):
        return False
    if existing_strategy.get("autoClose") != desired_strategy.get("autoClose"):
        return False

    existing_labels = existing.get("userLabels")
    desired_labels = desired.get("userLabels")
    if not isinstance(existing_labels, Mapping) or not isinstance(
        desired_labels, Mapping
    ):
        return False
    return all(existing_labels.get(key) == value for key, value in desired_labels.items())


def assess(snapshot: Snapshot, channels: Sequence[str]) -> tuple[str, ...]:
    violations = []
    if not snapshot.runtime_ready:
        violations.append("runtime_not_ready")
    if not snapshot.latest_receives_all_traffic:
        violations.append("latest_revision_not_serving_all_traffic")
    for index, state in snapshot.indexes.items():
        label = "+".join(index.fields)
        if state is None:
            violations.append(f"index_missing:{label}")
        elif state != "READY":
            violations.append(f"index_not_ready:{label}:{state}")
    if snapshot.ttl_state != "ACTIVE":
        violations.append(f"ttl_not_active:{snapshot.ttl_state or 'missing'}")
    if snapshot.unknown_policy_keys:
        violations.append("unexpected_managed_alert_policy")

    desired_policies = build_desired_policies(channels)
    for key, desired in desired_policies.items():
        existing = snapshot.managed_policies.get(key)
        if existing is None:
            violations.append(f"alert_policy_missing:{key}")
        elif not _policy_matches(existing, desired):
            violations.append(f"alert_policy_drift:{key}")
    return tuple(violations)


def _preflight_mutations(snapshot: Snapshot) -> None:
    if snapshot.unknown_policy_keys:
        raise PreparationError("unexpected managed staging alert policy")
    for state in snapshot.indexes.values():
        if state not in {None, "CREATING", "READY"}:
            raise PreparationError("staging index requires manual repair")
    if snapshot.ttl_state not in {None, "CREATING", "ACTIVE"}:
        raise PreparationError("staging TTL requires manual repair")
    for policy in snapshot.managed_policies.values():
        name = policy.get("name")
        if not isinstance(name, str) or _POLICY_NAME_RE.fullmatch(name) is None:
            raise PreparationError("managed staging alert policy has invalid identity")


def _write_policy(
    gcloud: Gcloud,
    policy: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
) -> None:
    payload = dict(policy)
    command = ["monitoring", "policies"]
    if existing is None:
        command.extend(("create", f"--project={RUNTIME_PROJECT}"))
    else:
        name = existing["name"]
        payload["name"] = name
        command.extend(("update", name, f"--project={RUNTIME_PROJECT}"))

    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="kevin-alert-", suffix=".json", delete=False
        ) as handle:
            path = Path(handle.name)
            os.chmod(path, 0o600)
            json.dump(payload, handle, sort_keys=True)
        gcloud.run(*command, f"--policy-from-file={path}")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def apply_changes(
    gcloud: Gcloud, snapshot: Snapshot, channels: Sequence[str]
) -> None:
    _preflight_mutations(snapshot)
    desired_policies = build_desired_policies(channels)

    for index, state in snapshot.indexes.items():
        if state is None:
            create_index(gcloud, index)
    if snapshot.ttl_state is None:
        enable_ttl(gcloud)
    for key, desired in desired_policies.items():
        existing = snapshot.managed_policies.get(key)
        if existing is None or not _policy_matches(existing, desired):
            _write_policy(gcloud, desired, existing)
    if not snapshot.runtime_ready:
        apply_runtime_configuration(gcloud)
    if not snapshot.latest_receives_all_traffic:
        restore_latest_traffic(gcloud)


def wait_until_ready(
    gcloud: Gcloud, channels: Sequence[str], wait_seconds: int
) -> Snapshot:
    if wait_seconds < 0 or wait_seconds > 3_600:
        raise PreparationError("invalid staging readiness wait")
    deadline = time.monotonic() + wait_seconds
    while True:
        current = read_snapshot(gcloud)
        _preflight_mutations(current)
        violations = assess(current, channels)
        if not violations:
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PreparationError("staging controls did not become ready in time")
        time.sleep(min(POLL_SECONDS, remaining))


def _parse_wait_seconds() -> int:
    raw = os.getenv("STAGING_PREPARE_WAIT_SECONDS", str(DEFAULT_WAIT_SECONDS))
    try:
        wait_seconds = int(raw)
    except ValueError as exc:
        raise PreparationError("invalid staging readiness wait") from exc
    if wait_seconds < 0 or wait_seconds > 3_600:
        raise PreparationError("invalid staging readiness wait")
    return wait_seconds


def run(mode: str, gcloud: Gcloud) -> Snapshot:
    validate_target(
        runtime_project=RUNTIME_PROJECT,
        firestore_project=FIRESTORE_PROJECT,
        region=REGION,
        service=SERVICE,
    )
    current = read_snapshot(gcloud)
    channels = resolve_notification_channels(
        configured=os.getenv("STAGING_ALERT_NOTIFICATION_CHANNELS", ""),
        managed_policies=tuple(current.managed_policies.values()),
        enabled_channels=current.enabled_channels,
    )

    if mode == "audit":
        violations = assess(current, channels)
        if violations:
            raise PreparationError(
                "staging controls are not ready: " + ",".join(violations)
            )
        return current
    if mode != "apply":
        raise PreparationError("invalid staging operation")

    _preflight_mutations(current)
    apply_changes(gcloud, current, channels)
    return wait_until_ready(gcloud, channels, _parse_wait_seconds())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit or prepare staging message-delivery infrastructure."
    )
    parser.add_argument("mode", choices=("audit", "apply"))
    args = parser.parse_args(argv)

    try:
        snapshot = run(args.mode, Gcloud())
    except PreparationError as exc:
        print(f"staging_message_delivery status=failed reason={exc}", file=sys.stderr)
        return 1

    print(
        "staging_message_delivery status=ready "
        f"mode={args.mode} indexes={len(snapshot.indexes)} "
        f"alert_policies={len(snapshot.managed_policies)} "
        f"notification_channels={len(snapshot.enabled_channels)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
