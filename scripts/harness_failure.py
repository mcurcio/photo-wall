"""Closed public failure vocabulary for isolated acceptance helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

FAILURE_TYPE = "photo_wall_helper_failure"

PROVENANCE_FAILURE_STAGES = {
    "provenance_manifest_invalid": "manifest",
    "provenance_manifest_unreadable": "manifest",
    "provenance_application_invalid": "application",
    "provenance_application_unreadable": "application",
    "provenance_bundle_invalid": "bundle",
    "provenance_bundle_unreadable": "bundle",
    "provenance_bundle_changed": "bundle",
    "provenance_bundle_closure": "bundle",
    "provenance_result_invalid": "result",
    "provenance_internal": "internal",
}

_CODES = (
    *PROVENANCE_FAILURE_STAGES,
    "absolute_state_required", "active_before_outage_timeout", "already_deleted",
    "already_evolved", "baseline_timeout",
    "cache_not_rebuilt", "capture_time_mismatch", "central_observation_missing",
    "central_recovery_timeout", "central_startup_timeout", "cleanup_failed",
    "complete_group_commit_missing",
    "core_dirty", "core_image_source_mismatch", "core_images_must_be_paired",
    "core_revision_mismatch", "core_revision_unavailable", "deleted_asset_download_succeeded",
    "deleted_asset_wrong_failure", "deleted_refresh_timeout", "deleted_secured_not_presented",
    "demo_failed",
    "demo_missing", "demo_path_mismatch", "docker_command_failed", "docker_command_timeout",
    "docker_output_limit", "empty_query_failed", "enrollment_pending", "evidence_bound",
    "exact_central_image_required", "exact_revision_required", "exact_worker_image_required",
    "fault_returned_partial_membership", "fault_status_mismatch", "file_bound",
    "fixture_action_required", "fixture_base_changed", "fixture_containers_missing",
    "fixture_document_bound", "fixture_document_invalid", "fixture_path_mismatch",
    "fixture_http_400", "fixture_http_401", "fixture_http_403", "fixture_http_404",
    "fixture_http_409", "fixture_http_422", "fixture_http_429", "fixture_http_500",
    "fixture_http_502", "fixture_http_503", "fixture_http_504",
    "forwarding_enabled", "forwarding_not_disabled", "harness_bundle_mismatch",
    "harness_image_mismatch", "host_port_exposed", "image_not_prepared",
    "immutable_fixture_base_required", "invalid_core_images", "invalid_demo_marker",
    "invalid_demo_revision", "invalid_fixture_marker", "invalid_page_size",
    "invalid_role_result", "invalid_scenario", "key_scope_mismatch", "linux_conversion_missing",
    "live_lock_proof_empty", "live_membership_timeout", "live_run_changed",
    "local_bytes_missing", "local_download_changed_after_upstream_deletion",
    "media_types_not_presented", "metadata_convergence_timeout", "missing_fixture_inputs",
    "operator_body_bound", "operator_http_400", "operator_http_401", "operator_http_403",
    "operator_http_404", "operator_http_409", "operator_http_422", "operator_http_429",
    "operator_http_500", "operator_http_502", "operator_http_503", "operator_http_504",
    "new_media_not_presented", "operator_transport", "original_geometry_mismatch",
    "original_hash_mismatch",
    "original_integrity_mismatch", "outage_fallback_missing", "outage_lease_overrun",
    "outage_report_stale", "output_not_drawn", "output_state_missing",
    "pagination_not_exercised", "player_boundary", "player_environment_leak",
    "player_network_leak", "player_requirements_mismatch", "player_revision_mismatch",
    "player_volume_leak", "player_wheel_mismatch", "players_not_registered",
    "permission_not_reported", "permission_recovery_timeout", "player_rejoin_timeout",
    "portrait_not_secured", "readiness_commit_proof", "refresh_pending",
    "rejoin_not_stateless", "release_not_centrally_accepted", "restart_run_changed",
    "revision_requires_core_images", "run_ended_before_faults_completed",
    "runtime_mutation_not_denied", "runtime_provenance_invalid", "runtime_upload_not_denied", "secured_assignment_changed",
    "source_configuration_invalid", "source_limit_not_enforced",
    "source_or_player_startup_timeout", "state_path_must_be_absolute",
    "synthetic_metadata_convergence", "synthetic_video_failed", "unexpected_harness_failure",
    "unexpected_network_membership", "unexpected_player_bytes", "unknown_fixture_action",
    "unknown_operator_action", "unknown_upstream_action", "unsupported_state_path",
    "upstream_already_initialized", "upstream_host_port_exposed", "upstream_outage_not_reported",
    "upstream_owner_mismatch", "upstream_recovery_timeout", "upstream_scope",
    "upstream_startup_timeout", "upstream_version_mismatch",
    "video_not_prepared", "wheel_path",
)

FailureCode = StrEnum("FailureCode", {value.upper(): value for value in _CODES})
FailureRole = StrEnum("FailureRole", {value.upper().replace("-", "_"): value for value in (
    "host", "harness", "docker-compose", "init", "upstream-tools", "operator", "setup",
    "verify", "serve",
)})
FailureAction = StrEnum("FailureAction", {value.upper().replace("-", "_"): value for value in (
    "run", "cleanup", "build_images", "initialize", "start_central", "configure_source",
    "request_refresh", "start_runtime", "health", "source", "refresh", "start", "snapshot",
    "evolve", "delete", "deny", "restore", "status", "live", "initial", "deleted", "source_audit",
    "outage", "recovered", "serve", "unknown",
)})
FailurePhase = StrEnum("FailurePhase", {value.upper(): value for value in (
    "role_action", "setup_build", "setup_volumes", "setup_upstream", "setup_central",
    "setup_source", "setup_refresh", "setup_runtime", "source_audit", "demo", "cleanup",
)})


class CodedFailure(ValueError):
    """An exception whose public code is always drawn from FailureCode."""

    def __init__(self, code: str | FailureCode):
        try:
            self.code = FailureCode(code)
        except (TypeError, ValueError):
            self.code = FailureCode.DEMO_FAILED
        super().__init__(self.code.value)


@dataclass(frozen=True)
class FailureEnvelope:
    phase: FailurePhase
    role: FailureRole
    action: FailureAction
    code: FailureCode

    @classmethod
    def create(cls, phase: str, role: str, action: str,
               code: str | FailureCode) -> Self:
        try:
            selected_phase = FailurePhase(phase)
        except (TypeError, ValueError):
            selected_phase = FailurePhase.DEMO
        try:
            selected_role = FailureRole(role)
        except (TypeError, ValueError):
            selected_role = FailureRole.HARNESS
        try:
            selected_action = FailureAction(action)
        except (TypeError, ValueError):
            selected_action = FailureAction.UNKNOWN
        try:
            selected_code = FailureCode(code)
        except (TypeError, ValueError):
            selected_code = FailureCode.DEMO_FAILED
        if (selected_phase.value != phase or selected_role.value != role
                or selected_action.value != action):
            selected_code = FailureCode.DEMO_FAILED
        return cls(selected_phase, selected_role, selected_action, selected_code)

    def as_dict(self) -> dict[str, object]:
        return {"type": FAILURE_TYPE, "schema": 1, "phase": self.phase.value,
                "role": self.role.value, "action": self.action.value, "code": self.code.value}

    def public_payload(self) -> dict[str, object]:
        return {"error": self.code.value, "failure": self.as_dict()}

    @classmethod
    def parse_public(cls, value: Any, *, expected_role: str,
                     expected_action: str) -> Self | None:
        if not isinstance(value, dict) or set(value) != {"error", "failure"}:
            return None
        failure = value.get("failure")
        if not isinstance(failure, dict) or set(failure) != {
                "type", "schema", "phase", "role", "action", "code"}:
            return None
        if (failure.get("type") != FAILURE_TYPE or type(failure.get("schema")) is not int
                or failure["schema"] != 1):
            return None
        try:
            parsed = cls(FailurePhase(failure["phase"]), FailureRole(failure["role"]),
                         FailureAction(failure["action"]), FailureCode(failure["code"]))
        except (KeyError, TypeError, ValueError):
            return None
        if (parsed.phase is not FailurePhase.ROLE_ACTION
                or parsed.role.value != expected_role
                or parsed.action.value != expected_action
                or value["error"] != parsed.code.value):
            return None
        return parsed
