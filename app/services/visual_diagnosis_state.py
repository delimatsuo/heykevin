"""Pure structural state machine for visual diagnosis A0-A2.

The machine is intentionally provider-, storage-, actor-, and policy-neutral.
It accepts already-constructed immutable events and returns typed decisions.  A
caller must inject identifiers and times; this module never reads a clock or
generates an identifier.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from app.services.visual_diagnosis_contracts import (
    ActionKind,
    ActionStatus,
    AnalysisStatus,
    ArtifactReference,
    CaseStatus,
    ConsentStatus,
    CONTROL_RECEIPT_CAP,
    DeletionStatus,
    Decision,
    DeliveryStatus,
    EVIDENCE_SCOPE,
    EventKind,
    EventSource,
    MediaAsset,
    MediaRole,
    MediaStatus,
    MediaValidation,
    ORDINARY_RECEIPT_CAP,
    OPAQUE_REF_PATTERN,
    PacketStatus,
    PendingCustomerAction,
    Projection,
    ProjectionRole,
    Receipt,
    StateVector,
    VisualTriageCase,
    VisualTriageEvent,
)


class TransitionRejected(ValueError):
    """Internal fail-closed transition error carrying a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_CONTROL_TERMINALS = {
    EventKind.CONSENT_DECLINED,
    EventKind.CONSENT_WITHDRAWN,
    EventKind.CASE_CLOSED,
    EventKind.CASE_CANCELLED,
    EventKind.CASE_EXPIRED,
}
_CONTROL_KINDS = _CONTROL_TERMINALS | {
    EventKind.DELETION_REQUESTED,
    EventKind.DELETION_RETRY_RECORDED,
    EventKind.DELETION_VERIFIED,
}
_DELETION_KINDS = {
    EventKind.DELETION_REQUESTED,
    EventKind.DELETION_RETRY_RECORDED,
    EventKind.DELETION_VERIFIED,
}
_MEDIA_ACTION_KINDS = {ActionKind.RATING_PLATE, ActionKind.TARGETED_RECAPTURE}
_TERMINAL_CASES = {CaseStatus.CLOSED, CaseStatus.CANCELLED, CaseStatus.EXPIRED}


class VisualTriageStateMachine:
    """A deterministic, in-memory aggregate for the synthetic A0-A2 slice."""

    def __init__(self) -> None:
        self._case_id: str | None = None
        self._contractor_id: str | None = None
        self._call_sid_ref: str | None = None
        self._supported_scenario = "hvac.not_cooling_with_outdoor_unit_noise"
        self._revision = 0
        self._state = StateVector()
        self._pending: PendingCustomerAction | None = None
        self._resolved_customer_actions: list[PendingCustomerAction] = []
        self._question_count = 0
        self._rating_plate_count = 0
        self._recapture_count = 0
        self._media_assets: dict[str, MediaAsset] = {}
        self._analysis_artifact: ArtifactReference | None = None
        self._candidate_set: ArtifactReference | None = None
        self._policy_versions: tuple[str, ...] = ()
        self._ordinary_receipts: list[Receipt] = []
        self._control_receipts: list[Receipt] = []
        self._ledger: dict[str, Receipt] = {}
        self._deletion_retry_count = 0
        self._created_at: datetime | None = None
        self._expires_at: datetime | None = None
        self._completed_at: datetime | None = None
        self._deleted_at: datetime | None = None
        self._analysis_retry_count = 0
        self._policy_receipt_ref: str | None = None

    @property
    def case(self) -> VisualTriageCase | None:
        return self.snapshot()

    @property
    def current_revision(self) -> int:
        return self._revision

    def snapshot(self) -> VisualTriageCase | None:
        if self._case_id is None or self._contractor_id is None:
            return None
        return VisualTriageCase(
            case_id=self._case_id,
            contractor_id=self._contractor_id,
            call_sid_ref=self._call_sid_ref,
            supported_scenario=self._supported_scenario,
            aggregate_revision=self._revision,
            state_vector=self._state,
            pending_customer_action=self._pending,
            resolved_customer_actions=tuple(self._resolved_customer_actions),
            question_count=self._question_count,
            rating_plate_request_count=self._rating_plate_count,
            recapture_count=self._recapture_count,
            media_assets=tuple(self._media_assets.values()),
            analysis_artifact=self._analysis_artifact,
            candidate_set=self._candidate_set,
            policy_versions=self._policy_versions,
            external_receipt_slots=(),
            ordinary_receipts=tuple(self._ordinary_receipts),
            control_receipts=tuple(self._control_receipts),
            deletion_retry_count=self._deletion_retry_count,
            created_at=self._created_at,
            expires_at=self._expires_at,
            completed_at=self._completed_at,
            deleted_at=self._deleted_at,
        )

    def apply(self, event: VisualTriageEvent) -> Decision:
        """Validate and apply one event, returning a non-throwing decision."""

        if not isinstance(event, VisualTriageEvent):
            return self._reject("invalid_event_type", reveal=False)
        if self._case_id is not None and (
            event.case_id != self._case_id or event.contractor_id != self._contractor_id
        ):
            return self._reject("binding_mismatch", reveal=False)
        try:
            event.assert_integrity()
        except (TypeError, ValueError):
            return self._reject("event_integrity_mismatch", reveal=False)
        if self._case_id is None:
            if event.kind is not EventKind.CASE_CREATED:
                return self._reject("aggregate_not_found", reveal=False)
        elif event.case_id != self._case_id or event.contractor_id != self._contractor_id:
            return self._reject("binding_mismatch", reveal=False)

        prior = self._ledger.get(event.event_id)
        if prior is not None:
            if prior.semantic_envelope_fingerprint != event.semantic_envelope_fingerprint:
                return self._reject("event_id_conflict")
            if self._state.deletion is DeletionStatus.VERIFIED and event.kind is not EventKind.DELETION_VERIFIED:
                return self._reject("verified_deletion_terminal")
            projection = self._projection(
                role=ProjectionRole.INTERNAL,
                historical_revision=prior.resulting_revision,
                historical_decision_code=prior.historical_decision_code,
            )
            return Decision(
                accepted=True,
                replayed=True,
                decision_code="replayed",
                historical_decision_code=prior.historical_decision_code,
                historical_revision=prior.resulting_revision,
                current_revision=self._revision,
                projection=projection,
            )

        if event.source_kind is not EventSource.SYNTHETIC:
            return self._reject("source_kind_mismatch", reveal=False)

        if self._state.deletion is DeletionStatus.PENDING and event.kind not in _DELETION_KINDS:
            return self._reject("deletion_pending")
        if self._state.deletion is DeletionStatus.VERIFIED:
            return self._reject("verified_deletion_terminal")
        if self._case_id is not None and event.expected_revision != self._revision:
            return self._reject("stale_or_future_revision")
        if self._case_id is None and event.expected_revision != 0:
            return self._reject("stale_or_future_revision")
        if event.kind in _CONTROL_KINDS:
            if len(self._control_receipts) >= CONTROL_RECEIPT_CAP:
                return self._reject("control_lane_capacity")
        elif len(self._ordinary_receipts) >= ORDINARY_RECEIPT_CAP:
            return self._reject("ordinary_lane_capacity")

        backup = self._backup()
        try:
            decision_code = self._dispatch(event)
        except TransitionRejected as error:
            self._restore(backup)
            return self._reject(error.code)
        except (TypeError, ValueError):
            self._restore(backup)
            return self._reject("invalid_structural_payload")

        self._revision += 1
        receipt = Receipt(
            event_id=event.event_id,
            kind=event.kind,
            semantic_envelope_fingerprint=event.semantic_envelope_fingerprint,
            historical_decision_code=decision_code,
            resulting_revision=self._revision,
        )
        self._ledger[event.event_id] = receipt
        if event.kind in _CONTROL_KINDS:
            self._control_receipts.append(receipt)
        else:
            self._ordinary_receipts.append(receipt)
        return Decision(
            accepted=True,
            decision_code=decision_code,
            current_revision=self._revision,
            projection=self._projection(role=ProjectionRole.INTERNAL),
        )

    def project(self, role: ProjectionRole) -> Projection:
        """Return a role-safe structural projection with deletion precedence."""

        return self._projection(role=role)

    def _reject(self, code: str, *, reveal: bool = True) -> Decision:
        return Decision(
            accepted=False,
            decision_code=code,
            current_revision=self._revision if reveal else 0,
            projection=self._projection(role=ProjectionRole.INTERNAL) if reveal and self._case_id else None,
        )

    def _projection(
        self,
        *,
        role: ProjectionRole,
        historical_revision: int | None = None,
        historical_decision_code: str | None = None,
    ) -> Projection:
        deletion = self._state.deletion
        if deletion is DeletionStatus.PENDING:
            reason = "deletion_pending"
            next_action = None
        elif deletion is DeletionStatus.VERIFIED:
            reason = "verified_deleted"
            next_action = None
        elif self._state.case in _TERMINAL_CASES:
            reason = "case_terminal"
            next_action = None
        elif self._pending is not None:
            reason = "customer_action_pending"
            next_action = "resolve_customer_action"
        elif self._state.media is MediaStatus.AWAITING_REQUIRED:
            reason = "media_required"
            next_action = "upload_initial_symptom_media"
        elif self._state.analysis is AnalysisStatus.READY:
            reason = "analysis_ready"
            next_action = "start_analysis"
        elif self._state.contractor_packet is PacketStatus.READY:
            reason = "packet_ready"
            next_action = "record_packet_delivery"
        else:
            reason = "ok"
            next_action = None
        pending_kind = self._pending.kind if self._pending is not None else None
        pending_status = self._pending.status if self._pending is not None else None
        pending_request = (
            self._pending.request_id
            if self._pending is not None and role in {
                ProjectionRole.INTERNAL,
                ProjectionRole.CUSTOMER,
                ProjectionRole.AUDIT,
            }
            else None
        )
        pending_locale = (
            self._pending.locale
            if self._pending is not None and role in {
                ProjectionRole.INTERNAL,
                ProjectionRole.CUSTOMER,
                ProjectionRole.AUDIT,
            }
            else None
        )
        action_detail_roles = {ProjectionRole.INTERNAL, ProjectionRole.CUSTOMER, ProjectionRole.AUDIT}
        pending_copy = self._pending.copy_ref if self._pending is not None and role in action_detail_roles else None
        pending_bucket = self._pending.budget_bucket if self._pending is not None and role in {ProjectionRole.INTERNAL, ProjectionRole.AUDIT} else None
        pending_optionality = self._pending.optionality if self._pending is not None and role in action_detail_roles else None
        pending_refusal = self._pending.safe_refusal_code if self._pending is not None and role in action_detail_roles else None
        pending_options = self._pending.response_option_codes if self._pending is not None and role in action_detail_roles else ()
        pending_expires = self._pending.expires_at if self._pending is not None and role in action_detail_roles else None
        if role is ProjectionRole.AUDIT:
            next_action = None
        elif role is ProjectionRole.CUSTOMER and next_action in {"start_analysis", "record_packet_delivery"}:
            next_action = None
        elif role is ProjectionRole.CONTRACTOR and next_action in {
            "resolve_customer_action",
            "upload_initial_symptom_media",
            "start_analysis",
        }:
            next_action = None
        return Projection(
            role=role,
            case_status=self._state.case,
            consent_status=self._state.consent,
            media_status=self._state.media,
            analysis_status=self._state.analysis,
            contractor_packet_status=self._state.contractor_packet,
            customer_delivery_status=self._state.customer_delivery,
            deletion_status=deletion,
            aggregate_revision=self._revision,
            historical_replay=historical_revision is not None,
            historical_revision=historical_revision,
            historical_decision_code=historical_decision_code,
            next_action=next_action,
            reason_code=reason,
            pending_action_kind=pending_kind,
            pending_action_status=pending_status,
            pending_action_request_ref=pending_request,
            pending_action_locale=pending_locale,
            pending_action_copy_ref=pending_copy,
            pending_action_budget_bucket=pending_bucket,
            pending_action_optionality=pending_optionality,
            pending_action_safe_refusal_code=pending_refusal,
            pending_action_response_option_codes=pending_options,
            pending_action_expires_at=pending_expires,
        )

    def _backup(self) -> dict[str, Any]:
        return {
            "case_id": self._case_id,
            "contractor_id": self._contractor_id,
            "call_sid_ref": self._call_sid_ref,
            "supported_scenario": self._supported_scenario,
            "state": self._state,
            "pending": self._pending,
            "resolved_customer_actions": list(self._resolved_customer_actions),
            "question_count": self._question_count,
            "rating_plate_count": self._rating_plate_count,
            "recapture_count": self._recapture_count,
            "media_assets": dict(self._media_assets),
            "analysis_artifact": self._analysis_artifact,
            "candidate_set": self._candidate_set,
            "policy_versions": self._policy_versions,
            "deletion_retry_count": self._deletion_retry_count,
            "created_at": self._created_at,
            "expires_at": self._expires_at,
            "completed_at": self._completed_at,
            "deleted_at": self._deleted_at,
            "analysis_retry_count": self._analysis_retry_count,
            "policy_receipt_ref": self._policy_receipt_ref,
        }

    def _restore(self, backup: dict[str, Any]) -> None:
        self._case_id = backup["case_id"]
        self._contractor_id = backup["contractor_id"]
        self._call_sid_ref = backup["call_sid_ref"]
        self._supported_scenario = backup["supported_scenario"]
        self._state = backup["state"]
        self._pending = backup["pending"]
        self._resolved_customer_actions = backup["resolved_customer_actions"]
        self._question_count = backup["question_count"]
        self._rating_plate_count = backup["rating_plate_count"]
        self._recapture_count = backup["recapture_count"]
        self._media_assets = backup["media_assets"]
        self._analysis_artifact = backup["analysis_artifact"]
        self._candidate_set = backup["candidate_set"]
        self._policy_versions = backup["policy_versions"]
        self._deletion_retry_count = backup["deletion_retry_count"]
        self._created_at = backup["created_at"]
        self._expires_at = backup["expires_at"]
        self._completed_at = backup["completed_at"]
        self._deleted_at = backup["deleted_at"]
        self._analysis_retry_count = backup["analysis_retry_count"]
        self._policy_receipt_ref = backup["policy_receipt_ref"]

    def _dispatch(self, event: VisualTriageEvent) -> str:
        handlers = {
            EventKind.CASE_CREATED: self._case_created,
            EventKind.CONSENT_REQUESTED: self._consent_requested,
            EventKind.CONSENT_GRANTED: self._consent_granted,
            EventKind.CONSENT_DECLINED: self._consent_declined,
            EventKind.CONSENT_WITHDRAWN: self._consent_withdrawn,
            EventKind.MEDIA_ACTION_ISSUED: self._media_action_issued,
            EventKind.DIAGNOSTIC_QUESTION_ISSUED: self._diagnostic_question_issued,
            EventKind.CUSTOMER_ACTION_RESOLVED: self._customer_action_resolved,
            EventKind.UPLOAD_STARTED: self._upload_started,
            EventKind.UPLOAD_FINALIZED: self._upload_finalized,
            EventKind.MEDIA_VALIDATED: self._media_validated,
            EventKind.MEDIA_ACTION_RESOLVED: self._media_action_resolved,
            EventKind.ANALYSIS_STARTED: self._analysis_started,
            EventKind.ANALYSIS_RETRY_RECORDED: self._analysis_retry_recorded,
            EventKind.ANALYSIS_COMPLETED: self._analysis_completed,
            EventKind.ANALYSIS_FAILED: self._analysis_failed,
            EventKind.CONTRACTOR_PACKET_READY_RECORDED: self._packet_ready,
            EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED: self._packet_delivery_started,
            EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED: self._packet_delivery_receipt,
            EventKind.DELIVERY_POLICY_RECEIPT_RECORDED: self._delivery_policy_receipt,
            EventKind.CUSTOMER_DELIVERY_STARTED: self._customer_delivery_started,
            EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED: self._customer_delivery_receipt,
            EventKind.CASE_CLOSED: self._case_closed,
            EventKind.CASE_CANCELLED: self._case_cancelled,
            EventKind.CASE_EXPIRED: self._case_expired,
            EventKind.DELETION_REQUESTED: self._deletion_requested,
            EventKind.DELETION_RETRY_RECORDED: self._deletion_retry_recorded,
            EventKind.DELETION_VERIFIED: self._deletion_verified,
        }
        return handlers[event.kind](event)

    @staticmethod
    def _value(payload: dict[str, Any], key: str) -> Any:
        if key not in payload:
            raise TransitionRejected("missing_payload")
        return payload[key]

    @classmethod
    def _text(cls, payload: dict[str, Any], key: str) -> str:
        value = cls._value(payload, key)
        if not isinstance(value, str) or not value:
            raise TransitionRejected("invalid_payload_code")
        return value

    @classmethod
    def _enum(cls, payload: dict[str, Any], key: str, enum_type: Any) -> Any:
        value = cls._text(payload, key)
        try:
            return enum_type(value)
        except ValueError as error:
            raise TransitionRejected("invalid_payload_code") from error

    def _require_case(self) -> None:
        if self._case_id is None:
            raise TransitionRejected("aggregate_not_found")

    def _require_not_deleting(self) -> None:
        if self._state.deletion is not DeletionStatus.NOT_REQUESTED:
            raise TransitionRejected("deletion_precedence")

    def _require_active(self) -> None:
        if self._state.case is not CaseStatus.ACTIVE:
            raise TransitionRejected("case_not_active")

    def _require_no_pending(self) -> None:
        if self._pending is not None:
            raise TransitionRejected("customer_action_pending")

    def _with_state(self, **changes: Any) -> None:
        values = self._state.model_dump()
        values.update(changes)
        self._state = StateVector(**values)

    def _action_from_payload(
        self, payload: dict[str, Any], kind: ActionKind, event_time: datetime
    ) -> PendingCustomerAction:
        options = self._value(payload, "response_option_codes") if "response_option_codes" in payload else ()
        if isinstance(options, list):
            options = tuple(options)
        if not isinstance(options, tuple):
            raise TransitionRejected("invalid_payload_code")
        return PendingCustomerAction(
            request_id=self._text(payload, "request_id"),
            kind=kind,
            budget_bucket=self._text(payload, "budget_bucket"),
            status=ActionStatus.ISSUED,
            copy_ref=payload.get("copy_ref"),
            response_option_codes=options,
            locale=payload.get("locale", "und"),
            optionality=payload.get("optionality", "optional"),
            safe_refusal_code=payload.get("safe_refusal_code"),
            decision_ref=payload.get("decision_ref"),
            evidence_ref=payload.get("evidence_ref"),
            issued_at=event_time,
            expires_at=self._parse_action_expiry(payload.get("expires_at"), event_time),
        )

    @staticmethod
    def _parse_action_expiry(value: Any, event_time: datetime) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TransitionRejected("invalid_action_expiry")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise TransitionRejected("invalid_action_expiry") from error
        if parsed.tzinfo is None or parsed <= event_time:
            raise TransitionRejected("invalid_action_expiry")
        return parsed

    def _case_created(self, event: VisualTriageEvent) -> str:
        if self._case_id is not None:
            raise TransitionRejected("case_already_exists")
        if set(event.payload) - {"scenario", "source_ref"}:
            raise TransitionRejected("invalid_case_payload")
        scenario = event.payload.get("scenario", self._supported_scenario)
        if not isinstance(scenario, str) or not scenario or "." not in scenario:
            raise TransitionRejected("invalid_case_payload")
        if scenario != scenario.casefold() or not all(
            part and part.replace("_", "").isalnum() for part in scenario.split(".")
        ):
            raise TransitionRejected("invalid_case_payload")
        source_ref = event.payload.get("source_ref")
        if source_ref is not None:
            if not isinstance(source_ref, str) or not OPAQUE_REF_PATTERN.fullmatch(source_ref):
                raise TransitionRejected("invalid_case_payload")
        self._case_id = event.case_id
        self._contractor_id = event.contractor_id
        self._call_sid_ref = source_ref
        self._supported_scenario = scenario
        self._created_at = event.event_time
        return "case_created"

    def _consent_requested(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        if self._state.case is not CaseStatus.CREATED or self._state.consent is not ConsentStatus.NOT_REQUESTED:
            raise TransitionRejected("consent_request_not_allowed")
        self._require_no_pending()
        self._with_state(consent=ConsentStatus.AWAITING_DECISION)
        return "consent_requested"

    def _consent_granted(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        if self._state.case is not CaseStatus.CREATED or self._state.consent is not ConsentStatus.AWAITING_DECISION:
            raise TransitionRejected("consent_grant_not_allowed")
        self._with_state(
            case=CaseStatus.ACTIVE,
            consent=ConsentStatus.GRANTED,
            media=MediaStatus.AWAITING_REQUIRED,
        )
        return "consent_granted"

    def _consent_declined(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        if self._state.case is not CaseStatus.CREATED or self._state.consent is not ConsentStatus.AWAITING_DECISION:
            raise TransitionRejected("consent_decline_not_allowed")
        self._with_state(case=CaseStatus.CANCELLED, consent=ConsentStatus.DECLINED)
        self._pending = None
        return "consent_declined"

    def _consent_withdrawn(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        if self._state.case is not CaseStatus.ACTIVE or self._state.consent is not ConsentStatus.GRANTED:
            raise TransitionRejected("consent_withdrawal_not_allowed")
        self._with_state(case=CaseStatus.CANCELLED, consent=ConsentStatus.WITHDRAWN)
        self._pending = None
        return "consent_withdrawn"

    def _media_action_issued(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        kind = self._enum(event.payload, "action_kind", ActionKind)
        if kind not in _MEDIA_ACTION_KINDS:
            raise TransitionRejected("invalid_media_action_kind")
        if self._text(event.payload, "receipt_ref") != event.event_id:
            raise TransitionRejected("action_receipt_binding_mismatch")
        expected_bucket = {
            ActionKind.RATING_PLATE: "rating_plate",
            ActionKind.TARGETED_RECAPTURE: "recapture",
        }[kind]
        if self._text(event.payload, "budget_bucket") != expected_bucket:
            raise TransitionRejected("action_budget_binding_mismatch")
        if kind is ActionKind.RATING_PLATE:
            if (
                self._rating_plate_count >= 1
                or self._state.media is not MediaStatus.VALIDATED
                or self._state.analysis not in (AnalysisStatus.COMPLETE, AnalysisStatus.ABSTAINED)
                or not self._has_validated_role(MediaRole.SYMPTOM_VIDEO)
            ):
                raise TransitionRejected("rating_plate_budget_exhausted")
            self._rating_plate_count += 1
        else:
            if self._recapture_count >= 1 or self._state.media is not MediaStatus.REJECTED:
                raise TransitionRejected("recapture_not_reachable")
            self._recapture_count += 1
        self._pending = self._action_from_payload(event.payload, kind, event.event_time)
        self._with_state(analysis=AnalysisStatus.AWAITING_CUSTOMER_ACTION)
        return "media_action_issued"

    def _diagnostic_question_issued(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        if self._state.analysis not in {AnalysisStatus.COMPLETE, AnalysisStatus.ABSTAINED}:
            raise TransitionRejected("analysis_not_question_ready")
        if any(
            action.kind is ActionKind.DIAGNOSTIC_QUESTION
            and action.status in {
                ActionStatus.DECLINED,
                ActionStatus.CANNOT_SAFELY_COMPLETE,
                ActionStatus.EXPIRED,
                ActionStatus.CANCELLED,
            }
            for action in self._resolved_customer_actions
        ):
            raise TransitionRejected("question_prompt_closed")
        if self._enum(event.payload, "action_kind", ActionKind) is not ActionKind.DIAGNOSTIC_QUESTION:
            raise TransitionRejected("invalid_question_action_kind")
        if self._text(event.payload, "budget_bucket") != "question":
            raise TransitionRejected("action_budget_binding_mismatch")
        if self._text(event.payload, "receipt_ref") != event.event_id:
            raise TransitionRejected("action_receipt_binding_mismatch")
        self._text(event.payload, "copy_ref")
        options = event.payload.get("response_option_codes")
        if not isinstance(options, list) or not options:
            raise TransitionRejected("question_options_required")
        request_id = self._text(event.payload, "request_id")
        copy_ref = self._text(event.payload, "copy_ref")
        if any(
            action.request_id == request_id or action.copy_ref == copy_ref
            for action in self._resolved_customer_actions
        ):
            raise TransitionRejected("question_identity_reused")
        if self._question_count >= 2:
            raise TransitionRejected("question_budget_exhausted")
        self._question_count += 1
        self._pending = self._action_from_payload(
            event.payload, ActionKind.DIAGNOSTIC_QUESTION, event.event_time
        )
        self._with_state(analysis=AnalysisStatus.AWAITING_CUSTOMER_ACTION)
        return "diagnostic_question_issued"

    def _customer_action_resolved(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._pending is None or self._pending.kind is not ActionKind.DIAGNOSTIC_QUESTION:
            raise TransitionRejected("no_matching_question")
        request_id = self._text(event.payload, "request_id")
        if request_id != self._pending.request_id:
            raise TransitionRejected("action_binding_mismatch")
        if self._enum(event.payload, "action_kind", ActionKind) is not ActionKind.DIAGNOSTIC_QUESTION:
            raise TransitionRejected("action_binding_mismatch")
        if self._text(event.payload, "locale") != self._pending.locale:
            raise TransitionRejected("action_binding_mismatch")
        status = self._enum(event.payload, "status", ActionStatus)
        if status not in {
            ActionStatus.ANSWERED,
            ActionStatus.NOT_SURE,
            ActionStatus.DECLINED,
            ActionStatus.CANNOT_SAFELY_COMPLETE,
            ActionStatus.EXPIRED,
            ActionStatus.CANCELLED,
        }:
            raise TransitionRejected("invalid_question_resolution")
        option_code = event.payload.get("response_option_code")
        if status is ActionStatus.ANSWERED:
            if not isinstance(option_code, str) or option_code not in self._pending.response_option_codes:
                raise TransitionRejected("response_binding_mismatch")
        elif option_code is not None:
            raise TransitionRejected("response_binding_mismatch")
        if status in {ActionStatus.EXPIRED, ActionStatus.CANCELLED}:
            if self._pending.status is not ActionStatus.ISSUED:
                raise TransitionRejected("action_not_expirable")
            if status is ActionStatus.EXPIRED and (
                self._pending.expires_at is None or event.event_time < self._pending.expires_at
            ):
                raise TransitionRejected("action_not_expired")
        resolved = self._pending.model_copy(
            update={"status": status, "resolved_at": event.event_time, "response_option_code": option_code}
        )
        self._resolved_customer_actions.append(resolved)
        del self._resolved_customer_actions[:-4]
        self._pending = None
        self._with_state(
            analysis=AnalysisStatus.READY
            if status in {ActionStatus.ANSWERED, ActionStatus.NOT_SURE}
            else AnalysisStatus.ABSTAINED
        )
        return "customer_action_resolved"

    def _asset_from_payload(self, payload: dict[str, Any], role: MediaRole) -> MediaAsset:
        media_type = self._text(payload, "media_type")
        if not re.fullmatch(r"[a-z0-9][a-z0-9+./-]{0,31}", media_type.casefold()):
            raise TransitionRejected("invalid_media_type")
        return MediaAsset(
            asset_id=self._text(payload, "asset_id"),
            role=role,
            media_type=media_type,
            byte_size=payload.get("byte_size", 0),
            duration_ms=payload.get("duration_ms"),
            width=payload.get("width"),
            height=payload.get("height"),
            digest=self._text(payload, "digest"),
            validation=MediaValidation.PENDING,
        )

    def _role_for_action(self, kind: ActionKind) -> MediaRole:
        return {
            ActionKind.RATING_PLATE: MediaRole.RATING_PLATE,
            ActionKind.TARGETED_RECAPTURE: MediaRole.TARGETED_RECAPTURE,
        }[kind]

    def _upload_started(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        asset_id = self._text(event.payload, "asset_id")
        if asset_id in self._media_assets:
            raise TransitionRejected("duplicate_upload")
        if self._pending is None:
            if self._state.media is not MediaStatus.AWAITING_REQUIRED:
                raise TransitionRejected("initial_upload_not_allowed")
            role = MediaRole.SYMPTOM_VIDEO
        else:
            if self._pending.kind not in _MEDIA_ACTION_KINDS or self._pending.status is not ActionStatus.ISSUED:
                raise TransitionRejected("upload_not_allowed")
            role = self._role_for_action(self._pending.kind)
        self._media_assets[asset_id] = self._asset_from_payload(event.payload, role)
        if self._pending is not None:
            self._pending = self._pending.model_copy(update={"status": ActionStatus.UPLOADING})
        self._with_state(media=MediaStatus.UPLOAD_PENDING)
        return "upload_started"

    def _matching_asset(self, payload: dict[str, Any]) -> MediaAsset:
        asset_id = self._text(payload, "asset_id")
        asset = self._media_assets.get(asset_id)
        if asset is None:
            raise TransitionRejected("asset_binding_mismatch")
        return asset

    def _has_validated_role(self, role: MediaRole) -> bool:
        return any(asset.role is role and asset.validation is MediaValidation.VALIDATED for asset in self._media_assets.values())

    def _has_validated_symptom_evidence(self) -> bool:
        return self._has_validated_role(MediaRole.SYMPTOM_VIDEO) or self._has_validated_role(
            MediaRole.TARGETED_RECAPTURE
        )

    def _upload_finalized(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        asset = self._matching_asset(event.payload)
        if self._state.media is not MediaStatus.UPLOAD_PENDING:
            raise TransitionRejected("upload_not_pending")
        if self._pending is not None:
            if self._pending.status is not ActionStatus.UPLOADING:
                raise TransitionRejected("upload_not_pending")
            if asset.role.value != self._role_for_action(self._pending.kind).value:
                raise TransitionRejected("action_binding_mismatch")
            self._pending = self._pending.model_copy(update={"status": ActionStatus.SUBMITTED})
        self._with_state(media=MediaStatus.UPLOADED_QUARANTINED)
        return "upload_finalized"

    def _media_validated(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        asset = self._matching_asset(event.payload)
        if self._state.media is not MediaStatus.UPLOADED_QUARANTINED:
            raise TransitionRejected("media_not_quarantined")
        validation = self._enum(event.payload, "validation", MediaValidation)
        if validation is MediaValidation.PENDING:
            raise TransitionRejected("validation_not_final")
        self._media_assets[asset.asset_id] = asset.model_copy(update={"validation": validation})
        media_state = {
            MediaValidation.VALIDATED: MediaStatus.VALIDATED,
            MediaValidation.REJECTED: MediaStatus.REJECTED,
            MediaValidation.UNAVAILABLE: MediaStatus.UNAVAILABLE,
        }[validation]
        symptom_evidence_present = self._has_validated_symptom_evidence()
        effective_media_state = (
            MediaStatus.VALIDATED
            if asset.role is not MediaRole.SYMPTOM_VIDEO
            and symptom_evidence_present
            else media_state
        )
        self._with_state(
            media=effective_media_state,
            analysis=(
                AnalysisStatus.READY
                if validation is MediaValidation.VALIDATED and self._pending is None
                else AnalysisStatus.ABSTAINED
                if validation is not MediaValidation.VALIDATED and self._pending is None
                else AnalysisStatus.AWAITING_CUSTOMER_ACTION
            ),
        )
        return "media_validated"

    def _media_action_resolved(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._pending is None or self._pending.kind not in _MEDIA_ACTION_KINDS:
            raise TransitionRejected("no_matching_media_action")
        request_id = self._text(event.payload, "request_id")
        if request_id != self._pending.request_id:
            raise TransitionRejected("action_binding_mismatch")
        action_kind = self._enum(event.payload, "action_kind", ActionKind)
        if action_kind is not self._pending.kind:
            raise TransitionRejected("action_binding_mismatch")
        expected_role = self._role_for_action(self._pending.kind)
        media_role = self._enum(event.payload, "media_role", MediaRole)
        if media_role is not expected_role:
            raise TransitionRejected("action_binding_mismatch")
        status = self._enum(event.payload, "status", ActionStatus)
        if status not in {
            ActionStatus.FULFILLED,
            ActionStatus.UNAVAILABLE,
            ActionStatus.DECLINED,
            ActionStatus.CANNOT_SAFELY_COMPLETE,
            ActionStatus.EXPIRED,
            ActionStatus.CANCELLED,
        }:
            raise TransitionRejected("invalid_media_resolution")
        if status is ActionStatus.EXPIRED:
            if self._pending.expires_at is None or event.event_time < self._pending.expires_at:
                raise TransitionRejected("action_not_expired")
        if status in {ActionStatus.EXPIRED, ActionStatus.CANCELLED} and self._pending.status not in {
            ActionStatus.ISSUED,
            ActionStatus.UPLOADING,
            ActionStatus.SUBMITTED,
        }:
            raise TransitionRejected("action_not_resolvable")
        if status is ActionStatus.FULFILLED:
            if self._pending.status is not ActionStatus.SUBMITTED:
                raise TransitionRejected("media_action_not_submitted")
            asset = self._matching_asset(event.payload)
            validation = self._enum(event.payload, "validation", MediaValidation)
            if validation is not MediaValidation.VALIDATED:
                raise TransitionRejected("media_fulfillment_binding_mismatch")
            if asset.role is not expected_role or asset.validation is not validation:
                raise TransitionRejected("media_fulfillment_binding_mismatch")
        elif self._pending.status is ActionStatus.ISSUED:
            pass
        elif self._pending.status is ActionStatus.SUBMITTED:
            asset = self._matching_asset(event.payload)
            validation = self._enum(event.payload, "validation", MediaValidation)
            if validation is MediaValidation.PENDING or asset.validation is not validation:
                raise TransitionRejected("media_resolution_binding_mismatch")
            if asset.role is not expected_role:
                raise TransitionRejected("media_resolution_binding_mismatch")
        elif status in {ActionStatus.EXPIRED, ActionStatus.CANCELLED} and self._pending.status is ActionStatus.UPLOADING:
            pass
        else:
            raise TransitionRejected("media_action_not_resolvable")
        resolved = self._pending.model_copy(
            update={"status": status, "resolved_at": event.event_time}
        )
        self._resolved_customer_actions.append(resolved)
        del self._resolved_customer_actions[:-4]
        self._pending = None
        media_after_abort = (
            MediaStatus.VALIDATED
            if self._has_validated_symptom_evidence()
            else MediaStatus.REJECTED
        )
        self._with_state(
            media=media_after_abort
            if status in {ActionStatus.EXPIRED, ActionStatus.CANCELLED}
            and self._state.media is MediaStatus.UPLOAD_PENDING
            else self._state.media,
            analysis=AnalysisStatus.READY
            if status is ActionStatus.FULFILLED
            or (status in {
                ActionStatus.DECLINED,
                ActionStatus.CANNOT_SAFELY_COMPLETE,
                ActionStatus.UNAVAILABLE,
                ActionStatus.EXPIRED,
                ActionStatus.CANCELLED,
            }
                and self._has_validated_symptom_evidence())
            else AnalysisStatus.ABSTAINED
        )
        return "media_action_resolved"

    def _analysis_started(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        if (
            self._state.analysis is not AnalysisStatus.READY
            or self._state.media is not MediaStatus.VALIDATED
            or not self._has_validated_symptom_evidence()
        ):
            raise TransitionRejected("analysis_not_ready")
        self._with_state(analysis=AnalysisStatus.PROCESSING)
        return "analysis_started"

    def _analysis_retry_recorded(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._state.analysis is not AnalysisStatus.FAILED_RETRIABLE:
            raise TransitionRejected("analysis_retry_not_allowed")
        if event.retry_stage != "analysis":
            raise TransitionRejected("retry_stage_invalid")
        attempt = self._value(event.payload, "attempt")
        if (
            not isinstance(attempt, int)
            or event.retry_attempt != attempt
            or attempt != self._analysis_retry_count + 1
            or attempt > 3
        ):
            raise TransitionRejected("retry_attempt_invalid")
        self._analysis_retry_count = attempt
        self._with_state(analysis=AnalysisStatus.READY)
        return "analysis_retry_recorded"

    def _analysis_completed(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._state.analysis is not AnalysisStatus.PROCESSING:
            raise TransitionRejected("analysis_not_processing")
        outcome = self._text(event.payload, "outcome")
        if outcome not in {"complete", "abstained"}:
            raise TransitionRejected("invalid_analysis_outcome")
        self._with_state(
            analysis=AnalysisStatus.COMPLETE if outcome == "complete" else AnalysisStatus.ABSTAINED
        )
        return "analysis_completed"

    def _analysis_failed(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._state.analysis is not AnalysisStatus.PROCESSING:
            raise TransitionRejected("analysis_not_processing")
        failure_state = self._text(event.payload, "failure_state")
        if failure_state not in {"failed_retriable", "failed_terminal"}:
            raise TransitionRejected("invalid_analysis_failure")
        self._with_state(analysis=AnalysisStatus(failure_state))
        return "analysis_failed"

    def _packet_ready(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        if self._state.contractor_packet is not PacketStatus.NOT_READY:
            raise TransitionRejected("packet_already_ready")
        if self._state.analysis not in (AnalysisStatus.COMPLETE, AnalysisStatus.ABSTAINED):
            raise TransitionRejected("packet_not_ready")
        self._with_state(contractor_packet=PacketStatus.READY)
        return "contractor_packet_ready_recorded"

    def _packet_delivery_started(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        if self._state.contractor_packet is not PacketStatus.READY:
            raise TransitionRejected("packet_delivery_not_ready")
        self._with_state(contractor_packet=PacketStatus.DELIVERY_PENDING)
        return "contractor_packet_delivery_started"

    def _packet_delivery_receipt(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._state.contractor_packet is not PacketStatus.DELIVERY_PENDING:
            raise TransitionRejected("packet_delivery_not_pending")
        status = self._text(event.payload, "status")
        if status not in {"delivered", "failed"}:
            raise TransitionRejected("invalid_delivery_status")
        self._with_state(
            contractor_packet=PacketStatus.DELIVERED
            if status == "delivered"
            else PacketStatus.DELIVERY_FAILED
        )
        return "contractor_packet_delivery_receipt_recorded"

    def _delivery_policy_receipt(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        if self._state.customer_delivery is not DeliveryStatus.NOT_EVALUATED:
            raise TransitionRejected("policy_receipt_already_recorded")
        if self._state.analysis not in (AnalysisStatus.COMPLETE, AnalysisStatus.ABSTAINED):
            raise TransitionRejected("policy_receipt_not_ready")
        receipt_ref = self._text(event.payload, "receipt_ref")
        if receipt_ref != event.event_id or not OPAQUE_REF_PATTERN.fullmatch(receipt_ref):
            raise TransitionRejected("invalid_receipt_ref")
        self._policy_receipt_ref = event.event_id
        self._with_state(customer_delivery=DeliveryStatus.POLICY_RECEIPT_RECORDED)
        return "delivery_policy_receipt_recorded"

    def _customer_delivery_started(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        self._require_no_pending()
        if self._state.customer_delivery is not DeliveryStatus.POLICY_RECEIPT_RECORDED:
            raise TransitionRejected("customer_delivery_not_ready")
        if self._text(event.payload, "receipt_ref") != self._policy_receipt_ref:
            raise TransitionRejected("policy_receipt_binding_mismatch")
        self._with_state(customer_delivery=DeliveryStatus.DELIVERY_PENDING)
        return "customer_delivery_started"

    def _customer_delivery_receipt(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        self._require_active()
        if self._state.customer_delivery is not DeliveryStatus.DELIVERY_PENDING:
            raise TransitionRejected("customer_delivery_not_pending")
        status = self._text(event.payload, "status")
        if status not in {"delivered", "failed"}:
            raise TransitionRejected("invalid_delivery_status")
        self._with_state(
            customer_delivery=DeliveryStatus.DELIVERED
            if status == "delivered"
            else DeliveryStatus.DELIVERY_FAILED
        )
        return "customer_delivery_receipt_recorded"

    def _case_closed(self, event: VisualTriageEvent) -> str:
        self._require_case()
        self._require_not_deleting()
        if self._state.case is not CaseStatus.ACTIVE:
            raise TransitionRejected("case_close_not_allowed")
        if self._pending is not None or self._state.media in {
            MediaStatus.UPLOAD_PENDING,
            MediaStatus.UPLOADED_QUARANTINED,
        }:
            raise TransitionRejected("case_not_quiescent")
        if self._state.analysis in {
            AnalysisStatus.PROCESSING,
            AnalysisStatus.FAILED_RETRIABLE,
            AnalysisStatus.AWAITING_CUSTOMER_ACTION,
        }:
            raise TransitionRejected("case_not_quiescent")
        if self._state.contractor_packet is PacketStatus.DELIVERY_PENDING or self._state.customer_delivery is DeliveryStatus.DELIVERY_PENDING:
            raise TransitionRejected("case_not_quiescent")
        self._completed_at = event.event_time
        self._with_state(case=CaseStatus.CLOSED)
        return "case_closed"

    def _case_cancelled(self, event: VisualTriageEvent) -> str:
        return self._terminal_case(event, CaseStatus.CANCELLED, "case_cancelled")

    def _case_expired(self, event: VisualTriageEvent) -> str:
        return self._terminal_case(event, CaseStatus.EXPIRED, "case_expired")

    def _terminal_case(self, event: VisualTriageEvent, status: CaseStatus, code: str) -> str:
        self._require_case()
        self._require_not_deleting()
        if self._state.case not in {CaseStatus.CREATED, CaseStatus.ACTIVE}:
            raise TransitionRejected("case_terminal_not_allowed")
        self._pending = None
        self._completed_at = event.event_time
        if status is CaseStatus.EXPIRED:
            self._expires_at = event.event_time
        self._with_state(case=status)
        return code

    def _deletion_requested(self, event: VisualTriageEvent) -> str:
        self._require_case()
        if self._state.deletion is not DeletionStatus.NOT_REQUESTED:
            raise TransitionRejected("deletion_already_requested")
        self._pending = None
        self._with_state(deletion=DeletionStatus.PENDING)
        return "deletion_requested"

    def _deletion_retry_recorded(self, event: VisualTriageEvent) -> str:
        self._require_case()
        if self._state.deletion is not DeletionStatus.PENDING:
            raise TransitionRejected("deletion_not_pending")
        if event.retry_stage != "deletion":
            raise TransitionRejected("retry_stage_invalid")
        attempt = self._value(event.payload, "attempt")
        if (
            not isinstance(attempt, int)
            or event.retry_attempt != attempt
            or attempt != self._deletion_retry_count + 1
            or attempt > 3
        ):
            raise TransitionRejected("deletion_retry_invalid")
        self._deletion_retry_count = attempt
        return "deletion_retry_recorded"

    def _deletion_verified(self, event: VisualTriageEvent) -> str:
        self._require_case()
        if self._state.deletion is not DeletionStatus.PENDING:
            raise TransitionRejected("deletion_not_pending")
        self._deleted_at = event.event_time
        self._with_state(deletion=DeletionStatus.VERIFIED)
        return "deletion_verified"


__all__ = ["VisualTriageStateMachine", "TransitionRejected", "EVIDENCE_SCOPE"]
