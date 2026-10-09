"""The fleet status's projections of T0 serial claims and app control; no route is exercised."""

from uuid import UUID

from central.fleet.check_in_status import (
    app_control_status,
    app_observation_status,
    base_state,
    boot_claim_status,
)


def _claim(*, received_at: float, boot: int) -> dict:
    return {"received_at": received_at, "fault_code": None, "phase": "base_ready",
            "kernel_boot_id": UUID(int=boot), "attempted_app_sha256": None,
            "observation_schema": 1}


def test_delayed_old_boot_report_does_not_establish_current_physical_boot() -> None:
    old, new = _claim(received_at=101, boot=1), _claim(received_at=100, boot=2)
    status = boot_claim_status(observations=[new, old], read_at=102)
    assert status["state"] == "ambiguous_boot_claims"
    assert status["current_physical_boot"] == "unknown"
    assert status["claimed_boot_ids"] == [str(UUID(int=1)), str(UUID(int=2))]
    installed, running = app_observation_status(observations=[new, old], read_at=102)
    assert installed["boot_ambiguity"] is running["boot_ambiguity"] is True


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
    assert base_state(observation=None, read_at=100) == {
        "state": "os_telemetry_unavailable", "source": "none", "age_seconds": None,
        "assurance": "none"}
    observed = _claim(received_at=99, boot=1)
    current = base_state(observation=observed, read_at=100)
    assert current["state"] == "base_heard_recently"
    assert current["assurance"] == "t0_unverified"
    assert base_state(observation=observed, read_at=159)["state"] == "base_heard_recently"
    assert base_state(observation=observed, read_at=160)["state"] == "base_last_heard"
    assert base_state(observation=observed, read_at=200)["state"] == "base_last_heard"
