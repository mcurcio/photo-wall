"""Pure selection and evidence classifications; no serial claim grants authority."""

from __future__ import annotations

from typing import Any


def effective_app(fleet: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """A tombstone is absence; a real override wins, even if incompatible later."""
    if override is not None and override["target"] is not None:
        return {"source": "override", "revision": override["revision"],
                "target": override["target"]}
    return {"source": fleet["source"], "revision": fleet["revision"],
            "target": fleet["target"]}


def base_state(*, observation: dict[str, Any] | None, legacy: dict[str, Any] | None,
               read_at: float, fresh_seconds: float = 30.0) -> dict[str, Any]:
    """Conservative F6/T0 label. Missing evidence is unknown, never failure/acceptance."""
    if observation is None:
        if legacy is None or (legacy.get("boot_outcome") is None
                              and legacy.get("known_good_tag") is None):
            return {"state": "os_telemetry_unavailable", "source": "none",
                    "age_seconds": None, "assurance": "none"}
        return {"state": "legacy_tag_only", "source": "legacy", "age_seconds": None,
                "assurance": "fallback_unverified", "legacy_boot_outcome": legacy.get("boot_outcome")}
    age = max(0.0, read_at - observation["received_at"])
    return {
        "state": "reported_fault_unverified" if observation["fault_code"] is not None else
                 ("base_heard_recently" if age <= fresh_seconds else "base_last_heard"),
        "source": "serial_claim", "age_seconds": age, "assurance": "t0_unverified",
        "boot_linkage": "claim_only",
        "phase": observation["phase"], "fault_code": observation["fault_code"],
        "boot_id": str(observation["kernel_boot_id"]),
        "offer_id": str(observation["offer_id"]) if observation["offer_id"] else None,
        "attempted_app_sha256": observation["attempted_app_sha256"],
    }


def boot_claim_status(*, observations: list[dict[str, Any]], offer: dict[str, Any] | None,
                      legacy: dict[str, Any] | None, read_at: float) -> dict[str, Any]:
    """Latest T0 claim is diagnostic; no arrival order proves a physical current boot."""
    if not observations:
        result = base_state(observation=None, legacy=legacy, read_at=read_at)
        result["current_physical_boot"] = "unknown"
        return result
    ordered = sorted(observations, key=lambda row: row["received_at"], reverse=True)
    latest = ordered[0]
    result = base_state(observation=latest, legacy=legacy, read_at=read_at)
    boot_ids = [str(row["kernel_boot_id"]) for row in ordered[:3]]
    offered_boot = str(offer["kernel_boot_id"]) if offer else None
    if len(ordered) > 1 or (offered_boot is not None and offered_boot != boot_ids[0]):
        result["state"] = "ambiguous_boot_claims"
    result["latest_claim_state"] = base_state(observation=latest, legacy=legacy,
                                               read_at=read_at)["state"]
    result["claimed_boot_ids"] = boot_ids
    result["latest_offered_boot_id"] = offered_boot
    result["current_physical_boot"] = "unknown"
    return result


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


def fallback_classification(*, accepted_digest: str | None, obtainable: bool,
                            compatible: bool, legacy_tag: str | None) -> str:
    """A legacy alias and an unknown timeout cannot qualify rollback bytes."""
    if accepted_digest is not None and obtainable and compatible:
        return "exact_accepted"
    if legacy_tag is not None:
        return "fallback_unverified"
    return "recovery_required"


def attempt_outcome(*, accepted_at: float | None, observed_fault_at: float | None,
                    deadline_elapsed: bool) -> str:
    """Use only evidence already matched to the exact boot/attempt/digest by the caller.

    A partition after installation supplies neither acceptance nor observed failure. It may
    expire into an unknown safety fence, never a claim that the package bytes failed.
    """
    if observed_fault_at is not None and (
        accepted_at is None or observed_fault_at >= accepted_at
    ):
        return "observed_failed"
    if accepted_at is not None:
        return "operational"
    return "expired_unknown" if deadline_elapsed else "pending"
