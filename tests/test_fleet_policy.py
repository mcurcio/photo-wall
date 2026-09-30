"""Conservative fleet selection/status checks; no command or HTTP route is exercised."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from central.fleet.models import FleetError, OfferRequest
from central.fleet.policy import (
    app_control_status,
    attempt_outcome,
    base_state,
    boot_claim_status,
    effective_app,
    fallback_classification,
)
from central.fleet.service import DisabledCommandAuthority, FleetService


def test_override_precedence_and_tombstone() -> None:
    fleet = {"source": "legacy_promotion", "revision": 8,
             "target": {"tag": "v1.0.0", "sha256": "a" * 64, "size": 10}}
    override = {"revision": 9, "target": {"tag": "v1.1.0", "sha256": "b" * 64, "size": 11}}
    assert effective_app(fleet, override) == {"source": "override", "revision": 9,
                                               "target": override["target"]}
    assert effective_app(fleet, {"revision": 10, "target": None}) == {
        "source": "legacy_promotion", "revision": 8, "target": fleet["target"]}


def test_delayed_old_boot_report_does_not_establish_current_physical_boot() -> None:
    old = {"received_at": 101, "fault_code": None, "phase": "base_ready",
           "kernel_boot_id": UUID(int=1), "offer_id": UUID(int=1),
           "attempted_app_sha256": None}
    new = {"received_at": 100, "fault_code": None, "phase": "base_ready",
           "kernel_boot_id": UUID(int=2), "offer_id": UUID(int=2),
           "attempted_app_sha256": None}
    status = boot_claim_status(observations=[new, old],
                               offer={"kernel_boot_id": UUID(int=2)}, legacy=None,
                               read_at=102)
    assert status["state"] == "ambiguous_boot_claims"
    assert status["current_physical_boot"] == "unknown"
    assert status["latest_offered_boot_id"] == str(UUID(int=2))


def test_latest_app_rejection_does_not_borrow_older_applied_ack() -> None:
    player = {"authority_epoch": 7}
    session = {"authority_epoch": 7, "last_result": "rejected",
               "last_result_at": 90.0, "last_delivery_id": "new",
               "last_result_sequence": 3, "last_result_digest": "b" * 64,
               "applied_at": 10.0, "applied_delivery_id": "old",
               "applied_digest": "a" * 64}
    status = app_control_status(player=player, session=session, read_at=100)
    assert status["state"] == "rejected"
    assert status["age_seconds"] == 10
    assert status["historical_applied"]["at"] == 10
    assert status["last_result"]["delivery_id"] == "new"
    assert app_control_status(player={"authority_epoch": 8}, session=session,
                              read_at=100)["state"] == "enrolled_boot_linkage_unknown"


def test_serial_claim_is_not_base_acceptance_or_failure_from_silence() -> None:
    assert base_state(observation=None, legacy=None, read_at=100)["state"] == (
        "os_telemetry_unavailable")
    legacy = {"known_good_tag": "v1.0.0", "boot_outcome": "failed"}
    assert base_state(observation=None, legacy=legacy, read_at=100)["assurance"] == (
        "fallback_unverified")
    observed = {"received_at": 99, "fault_code": None, "phase": "base_ready",
                "kernel_boot_id": UUID(int=1), "offer_id": None,
                "attempted_app_sha256": None}
    current = base_state(observation=observed, legacy=legacy, read_at=100)
    assert current["state"] == "base_heard_recently"
    assert current["assurance"] == "t0_unverified"
    assert base_state(observation=observed, legacy=legacy, read_at=200)["state"] == (
        "base_last_heard")
    assert fallback_classification(accepted_digest=None, obtainable=True, compatible=True,
                                   legacy_tag="v1.0.0") == "fallback_unverified"
    assert fallback_classification(accepted_digest="a" * 64, obtainable=False, compatible=True,
                                   legacy_tag=None) == "recovery_required"


def test_offer_nonce_and_serial_are_bounded() -> None:
    good = OfferRequest(schema=1, kind="pi", serial="abcdef1234567890",
                        kernel_boot_id=UUID(int=1), boot_nonce="a" * 32)
    assert good.validated_serial() == "abcdef1234567890"
    with pytest.raises(ValidationError):
        OfferRequest(schema=1, kind="pi", serial="abcdef1234567890",
                     kernel_boot_id=UUID(int=1), boot_nonce="bad/nonce")
    bad_serial = OfferRequest(schema=1, kind="pi", serial="bad/path",
                              kernel_boot_id=UUID(int=1), boot_nonce="a" * 32)
    with pytest.raises(FleetError, match="invalid_serial"):
        bad_serial.validated_serial()


def test_t0_command_authority_is_disabled() -> None:
    with pytest.raises(FleetError, match="command_trust_unapproved"):
        DisabledCommandAuthority().authorize(device_id="device", boot_id=UUID(int=1),
                                             attempt_id=UUID(int=2), policy_revision=1,
                                             drain_id=UUID(int=3))


def test_unknown_offer_asset_kind_never_reaches_storage() -> None:
    service = FleetService(db=None, clock=None)  # type: ignore[arg-type]
    with pytest.raises(FleetError, match="boot_offer_asset_not_found"):
        service.offer_asset(UUID(int=1), "command")


def test_partition_after_install_is_unknown_not_observed_failure() -> None:
    assert attempt_outcome(accepted_at=None, observed_fault_at=None,
                           deadline_elapsed=False) == "pending"
    assert attempt_outcome(accepted_at=None, observed_fault_at=None,
                           deadline_elapsed=True) == "expired_unknown"
    assert attempt_outcome(accepted_at=None, observed_fault_at=15,
                           deadline_elapsed=True) == "observed_failed"
    assert attempt_outcome(accepted_at=16, observed_fault_at=15,
                           deadline_elapsed=True) == "operational"
