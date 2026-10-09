"""Pure projections of T0 serial claims and app control for the fleet status read; no serial
claim grants authority."""

from __future__ import annotations

from typing import Any


def base_state(*, observation: dict[str, Any] | None, read_at: float,
               fresh_seconds: float = 60.0) -> dict[str, Any]:
    """Conservative T0 label. Missing evidence is unknown, never failure/acceptance."""
    if observation is None:
        return {"state": "os_telemetry_unavailable", "source": "none",
                "age_seconds": None, "assurance": "none"}
    age = max(0.0, read_at - observation["received_at"])
    return {
        "state": "reported_fault_unverified" if observation["fault_code"] is not None else
                 ("base_heard_recently" if age <= fresh_seconds else "base_last_heard"),
        "source": "serial_claim", "age_seconds": age, "assurance": "t0_unverified",
        "boot_linkage": "claim_only",
        "phase": observation["phase"], "fault_code": observation["fault_code"],
        "boot_id": str(observation["kernel_boot_id"]),
        "attempted_app_sha256": observation["attempted_app_sha256"],
    }


def _boot_claim_context(observations: list[dict[str, Any]]) -> tuple[
        dict[str, Any] | None, list[str], bool]:
    """Choose one received claim for all OS facets; arrival ID resolves clock ties."""
    ordered = sorted(observations, key=lambda row: (
        row["received_at"], row.get("arrival_id") or 0), reverse=True)
    boot_ids = [str(row["kernel_boot_id"]) for row in ordered[:3]]
    return (ordered[0] if ordered else None, boot_ids, len(ordered) > 1)


def boot_claim_status(*, observations: list[dict[str, Any]],
                      read_at: float) -> dict[str, Any]:
    """Latest T0 claim is diagnostic; no arrival order proves a physical current boot."""
    latest, boot_ids, ambiguous = _boot_claim_context(observations)
    if latest is None:
        result = base_state(observation=None, read_at=read_at)
        result["current_physical_boot"] = "unknown"
        return result
    result = base_state(observation=latest, read_at=read_at)
    if ambiguous:
        result["state"] = "ambiguous_boot_claims"
    result["latest_claim_state"] = base_state(observation=latest, read_at=read_at)["state"]
    result["claimed_boot_ids"] = boot_ids
    result["current_physical_boot"] = "unknown"
    return result


def app_observation_status(*, observations: list[dict[str, Any]],
                           read_at: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project installed/process claims from the exact row selected for base status."""
    latest, boot_ids, ambiguous = _boot_claim_context(observations)
    shared: dict[str, Any] = {
        "source": "serial_claim" if latest is not None else "none",
        "assurance": "t0_unverified" if latest is not None else "none",
        "age_seconds": max(0.0, read_at - latest["received_at"]) if latest else None,
        "boot_id": str(latest["kernel_boot_id"]) if latest else None,
        "boot_linkage": "claim_only" if latest is not None else "unknown",
        "boot_ambiguity": ambiguous,
        "claimed_boot_ids": boot_ids,
        "current_physical_boot": "unknown",
    }
    if latest is None:
        unknown = {**shared, "state": "unknown", "digest": None,
                   "reason": "os_telemetry_unavailable"}
        return unknown, {**unknown, "process": None}
    if latest["observation_schema"] == 1:
        installed_digest = running_digest = None
        installed_reason = running_reason = "schema_one_no_app_evidence"
    else:
        installed_digest = latest["app_installed_sha256"]
        running_digest = latest["app_running_sha256"]
        installed_reason = latest["app_installed_reason"]
        running_reason = latest["app_running_reason"]
    installed = {**shared, "state": "reported" if installed_digest else "unknown",
                 "digest": installed_digest}
    running = {**shared, "state": "reported" if running_digest else "unknown",
               "digest": running_digest,
               "process": ({"pid": latest["app_running_pid"],
                            "start_ticks": latest["app_running_start_ticks"],
                            "invocation_id": latest["app_running_invocation_id"]}
                           if running_digest else None)}
    if installed_digest is None:
        installed["reason"] = installed_reason
    if running_digest is None:
        running["reason"] = running_reason
    return installed, running


def app_control_status(*, player: dict[str, Any] | None,
                       session: dict[str, Any] | None, read_at: float) -> dict[str, Any]:
    """Latest control result is diagnostic; an older applied ack remains dated history."""
    if player is None:
        return {"state": "not_observed", "source": "none", "age_seconds": None}
    result = {"state": "enrolled_boot_linkage_unknown", "source": "app_auth",
              "age_seconds": None, "authority_epoch": player["authority_epoch"]}
    if session is None or session["authority_epoch"] != player["authority_epoch"]:
        return result
    if session["last_result"] is None:
        return result
    latest_at = session["last_result_at"]
    return {"state": ("control_applied_boot_linkage_unknown"
                      if session["last_result"] == "applied" else session["last_result"]),
            "source": "app_control", "authority_epoch": player["authority_epoch"],
            "age_seconds": max(0, read_at - latest_at) if latest_at is not None else None,
            "last_result": {"result": session["last_result"], "at": latest_at,
                            "delivery_id": session["last_delivery_id"],
                            "sequence": session["last_result_sequence"],
                            "digest": session["last_result_digest"]},
            "historical_applied": ({"at": session["applied_at"],
                                     "delivery_id": session["applied_delivery_id"],
                                     "digest": session["applied_digest"]}
                                    if session["applied_at"] is not None else None),
            "boot_linkage": "unknown"}
