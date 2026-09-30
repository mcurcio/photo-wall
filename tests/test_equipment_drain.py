"""Runtime maintenance admission stays fenced across deadlines and restart."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from test_registry import ADMIN, enroll, frame

from central.app import create_app
from central.coordination import Coordinator
from central.equipment_drain import EquipmentDrain
from central.media_repository import MediaRepository
from central.media_store import MediaStore, MediaStoreError
from central.player_control_protocol import project_state, state_digest
from central.registry import RegistryError
from central.runtime import Contribution, Program, Scene
from contracts.models import Calibration, Readiness
from contracts.player_control import ControlAck, ControlHello


def bound_player(registry):
    player, _, request = enroll(registry)
    frame(registry, "drain-frame")
    registry.bind("drain-frame", player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate("drain-frame", "commit", 1, Calibration(), expected_generation=1)
    return player, request


def future_run(coordinator):
    coordinator.runtime.command("set_scene", Scene(
        scene_id="future", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:drain-frame", kind="black"),),
    ))
    coordinator.runtime.command("set_program", Program(
        program_id="future", scene_id="future", starts_at=1010, ends_at=1100,
    ))
    coordinator.advance()


def test_prepared_drain_cancels_old_group_and_blocks_new_delivery(registry):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    future_run(coordinator)
    before = coordinator.delivery(player["player_id"], player["authority_epoch"])
    assert before["plan"] and before["plan"].layers

    drain = EquipmentDrain(coordinator)
    result = drain.prepare_idle(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1015,
    )
    assert result.status == "prepared"
    assert {output["output_id"] for output in result.snapshot["outputs"]} == {
        "HDMI-A-1", "HDMI-A-2",
    }
    assert result.snapshot["outputs"][0]["binding_generation"] == 1

    pending = coordinator.delivery(player["player_id"], player["authority_epoch"])
    assert pending["commits"] == ()
    assert pending["revocations"] and all(
        revocation.mode == "cancel" for revocation in pending["revocations"]
    )
    with pytest.raises(RegistryError, match="equipment_draining"):
        coordinator.readiness(player["player_id"], Readiness(
            plan_id=before["plan"].plan_id, revision=before["plan"].revision,
            authority_epoch=player["authority_epoch"], sequence=1,
            observed_at=registry.clock.utc(), clock_uncertainty=.01,
            capacity_ok=True,
        ))
    coordinator.advance()
    after = coordinator.delivery(player["player_id"], player["authority_epoch"])
    assert after["plan"] is None or not after["plan"].layers
    assert after["commits"] == ()
    with pytest.raises(RegistryError, match="equipment_draining"):
        registry.calibrate("drain-frame", "commit", 2, Calibration(), expected_generation=1)
    with pytest.raises(RegistryError, match="equipment_draining"):
        registry.unbind("drain-frame", expected_generation=1)


def test_stop_committed_never_time_releases_and_retries_are_typed(registry):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    drain = EquipmentDrain(coordinator)
    registry.calibrate("drain-frame", "preview", 2, Calibration(gain=.8),
                       expected_generation=1)
    with registry.db.transaction() as conn:
        preview_revision = conn.execute(
            "SELECT configuration_revision FROM frames WHERE id='drain-frame'"
        ).fetchone()["configuration_revision"]
    prepared = drain.prepare_idle(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1005,
    )
    assert prepared.snapshot["outputs"][0]["configuration_revision"] == preview_revision + 1
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT preview FROM frames WHERE id='drain-frame'"
        ).fetchone()["preview"] is None
    assert drain.prepare_idle(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1300,
    ).status == "already_prepared"
    assert drain.commit_stop(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"],
    ).status == "stop_committed"
    registry.clock.advance(100)
    restarted = EquipmentDrain(Coordinator(registry.db, registry.clock))
    assert restarted.commit_stop(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"],
    ).status == "already_committed"
    assert restarted.prepare_idle(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1300,
    ).status == "already_committed"
    with pytest.raises(RegistryError, match="equipment_drain_conflict"):
        restarted.prepare_idle(
            player["player_id"], "attempt-2", request.boot_id,
            player["authority_epoch"], authorization_expires_at=1300,
        )
    assert prepared.snapshot == restarted.commit_stop(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"],
    ).snapshot
    with pytest.raises(RegistryError, match="stop_committed_requires_reconciliation"):
        restarted.abort_prepared(player["player_id"], "attempt-1", request.boot_id,
                                player["authority_epoch"])


def test_expired_preparation_stays_fenced_and_cannot_commit_stop(registry):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    drain = EquipmentDrain(coordinator)
    drain.prepare_idle(player["player_id"], "attempt-1", request.boot_id,
                       player["authority_epoch"], authorization_expires_at=1001)
    registry.clock.advance(2)
    with pytest.raises(RegistryError, match="drain_authorization_expired"):
        drain.commit_stop(player["player_id"], "attempt-1", request.boot_id,
                          player["authority_epoch"])
    with pytest.raises(RegistryError, match="equipment_draining"):
        registry.unbind("drain-frame", expected_generation=1)


def test_active_run_requires_declared_interruption_policy(registry):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="live", loop=True,
        contributions=(Contribution(target="frame:drain-frame", kind="black"),),
    ))
    coordinator.runtime.command("activate", "live", "live-1", registry.clock.utc())
    with pytest.raises(RegistryError, match="active_run_requires_interruption_policy"):
        EquipmentDrain(coordinator).prepare_idle(
            player["player_id"], "attempt-1", request.boot_id,
            player["authority_epoch"], authorization_expires_at=1010,
        )
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM equipment_drains").fetchone()["n"] == 0


@pytest.mark.parametrize("advance_before_commit", [False, True])
def test_program_entering_after_prepare_refuses_stop(registry, advance_before_commit):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="scheduled", loop=True,
        contributions=(
            Contribution(target="frame:drain-frame", kind="black"),
            Contribution(target="actuator:lamp", kind="actuator", ramp_to=1),
        ),
    ))
    coordinator.runtime.command("set_program", Program(
        program_id="scheduled", scene_id="scheduled", starts_at=1005, ends_at=1100,
    ))
    drain = EquipmentDrain(coordinator)
    drain.prepare_idle(
        player["player_id"], "attempt-scheduled", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1020,
    )
    registry.clock.advance(5)
    if advance_before_commit:
        coordinator.advance()
    # The Program is active at the stop cut even if a scheduler tick has not
    # persisted it yet. Its Actuator is part of the same logical Run.
    active = [run for run in coordinator.runtime.read().project(registry.clock.utc()).runs
              if run.ended_at is None and "frame:drain-frame" in run.participants]
    assert len(active) == 1
    assert "actuator:lamp" in active[0].participants
    with pytest.raises(RegistryError, match="active_run_requires_interruption_policy") as exc:
        drain.commit_stop(player["player_id"], "attempt-scheduled", request.boot_id,
                          player["authority_epoch"])
    assert exc.value.details["run_ids"] == [active[0].run_id]
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT phase,stop_committed_at FROM equipment_drains "
                           "WHERE player_id=%s", (player["player_id"],)).fetchone()
    assert row["phase"] == "prepared"
    assert row["stop_committed_at"] is None


@pytest.mark.parametrize("expires_at,expected_code", [
    (1020, "active_run_requires_interruption_policy"),
    (1002, "invalid_drain_request"),
])
def test_prepare_samples_time_after_waiting_for_coordination_lock(
    registry, monkeypatch, expires_at, expected_code,
):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="due-during-lock-wait", loop=True,
        contributions=(Contribution(target="frame:drain-frame", kind="black"),
                       Contribution(target="actuator:lamp", kind="actuator", ramp_to=1)),
    ))
    coordinator.runtime.command("set_program", Program(
        program_id="due-during-lock-wait", scene_id="due-during-lock-wait",
        starts_at=1005, ends_at=1100,
    ))
    drain = EquipmentDrain(coordinator)
    original_transaction = coordinator._transaction
    entering = Event()

    @contextmanager
    def notified_transaction():
        # The old implementation had already read clock.utc() by this point.
        entering.set()
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(coordinator, "_transaction", notified_transaction)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with original_transaction():
            pending = workers.submit(
                drain.prepare_idle, player["player_id"], "attempt-cut", request.boot_id,
                player["authority_epoch"], authorization_expires_at=expires_at,
            )
            assert entering.wait(4)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.1)
            registry.clock.advance(5)
        due = [run for run in coordinator.runtime.read().project(registry.clock.utc()).runs
               if run.ended_at is None and "frame:drain-frame" in run.participants]
        assert len(due) == 1 and "actuator:lamp" in due[0].participants
        with pytest.raises(RegistryError, match=expected_code):
            pending.result(timeout=4)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM equipment_drains").fetchone()["n"] == 0


@pytest.mark.parametrize("expires_at,expected_code", [
    (1020, "active_run_requires_interruption_policy"),
    (1002, "invalid_drain_request"),
])
def test_prepare_samples_time_after_waiting_for_binding_row(
    registry, monkeypatch, expires_at, expected_code,
):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(
        scene_id="due-during-binding-wait", loop=True,
        contributions=(Contribution(target="frame:drain-frame", kind="black"),
                       Contribution(target="actuator:lamp", kind="actuator", ramp_to=1)),
    ))
    coordinator.runtime.command("set_program", Program(
        program_id="due-during-binding-wait", scene_id="due-during-binding-wait",
        starts_at=1005, ends_at=1100,
    ))
    drain = EquipmentDrain(coordinator)
    original_transaction = coordinator._transaction
    entered = Event()
    backend_pid = {}

    @contextmanager
    def identified_transaction():
        with original_transaction() as conn:
            backend_pid["pid"] = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            entered.set()
            yield conn

    monkeypatch.setattr(coordinator, "_transaction", identified_transaction)
    with ThreadPoolExecutor(max_workers=1) as workers:
        # A raw row holder exercises a wait below the advisory lock cut. Normal
        # equipment writers also take Coordination, so cannot cause this wait.
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT 1 FROM bindings WHERE frame_id='drain-frame' FOR UPDATE")
            pending = workers.submit(
                drain.prepare_idle, player["player_id"], "attempt-binding", request.boot_id,
                player["authority_epoch"], authorization_expires_at=expires_at,
            )
            assert entered.wait(4)
            deadline = monotonic() + 3
            while monotonic() < deadline:
                with registry.db.transaction() as observer:
                    activity = observer.execute(
                        "SELECT wait_event_type,query FROM pg_stat_activity WHERE pid=%s",
                        (backend_pid["pid"],),
                    ).fetchone()
                if (activity and activity["wait_event_type"] == "Lock"
                        and "FROM bindings b JOIN frames f" in activity["query"]):
                    break
                sleep(.01)
            else:
                pytest.fail("prepare never waited on the binding row lock")
            registry.clock.advance(5)
        due = [run for run in coordinator.runtime.read().project(registry.clock.utc()).runs
               if run.ended_at is None and "frame:drain-frame" in run.participants]
        assert len(due) == 1 and "actuator:lamp" in due[0].participants
        with pytest.raises(RegistryError, match=expected_code):
            pending.result(timeout=4)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM equipment_drains").fetchone()["n"] == 0


def test_stop_projection_and_recorded_timestamp_share_one_cut(registry, monkeypatch):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    drain = EquipmentDrain(coordinator)
    drain.prepare_idle(player["player_id"], "attempt-cut", request.boot_id,
                       player["authority_epoch"], authorization_expires_at=1010)
    samples = []

    def ticking_utc():
        samples.append(registry.clock.wall)
        registry.clock.wall += 1
        return samples[-1]

    monkeypatch.setattr(registry.clock, "utc", ticking_utc)
    assert drain.commit_stop(player["player_id"], "attempt-cut", request.boot_id,
                             player["authority_epoch"]).status == "stop_committed"
    with registry.db.transaction() as conn:
        stamped = conn.execute("SELECT stop_committed_at FROM equipment_drains WHERE player_id=%s",
                               (player["player_id"],)).fetchone()["stop_committed_at"]
    assert samples == [1000]
    assert stamped == samples[0]


def test_media_grant_and_frame_placement_are_fenced(registry, tmp_path):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    EquipmentDrain(coordinator).prepare_idle(
        player["player_id"], "attempt-1", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1010,
    )
    store = MediaStore(MediaRepository(registry.db, registry.clock), tmp_path / "media")
    with pytest.raises(MediaStoreError, match="equipment_draining"):
        store.open_read(player["token"], "a" * 64)
    from central.registry import FramePlacement

    with pytest.raises(RegistryError, match="equipment_draining"):
        registry.place_frame("drain-frame", FramePlacement(x_mm=12))


def test_reenrollment_cannot_clear_fence_or_commit_old_epoch(registry):
    player, key, request = enroll(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    drain = EquipmentDrain(coordinator)
    drain.prepare_idle(player["player_id"], "attempt-1", request.boot_id,
                       player["authority_epoch"], authorization_expires_at=1010)
    replacement, _, _ = enroll(registry, key=key, device_id=request.device_id)
    assert replacement["authority_epoch"] == player["authority_epoch"] + 1
    with pytest.raises(RegistryError, match="stale_authority"):
        drain.commit_stop(player["player_id"], "attempt-1", request.boot_id,
                          player["authority_epoch"])
    from central.equipment_drain import fenced_players_in

    with registry.db.transaction() as conn:
        assert player["player_id"] in fenced_players_in(conn)


def test_prepared_abort_needs_expiry_and_fresh_applied_control(registry):
    player, request = bound_player(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    selection = registry.control_hello(player["player_id"], ControlHello(
        authority_epoch=player["authority_epoch"], schemas=(1, 2), capabilities=(),
    ))
    old_delivery = registry.issue_control_delivery_record(
        player["player_id"], player["authority_epoch"], "a" * 64,
    )
    drain = EquipmentDrain(coordinator)
    drain.prepare_idle(player["player_id"], "attempt-1", request.boot_id,
                       player["authority_epoch"], authorization_expires_at=1001)
    with pytest.raises(RegistryError, match="drain_authorization_open"):
        drain.abort_prepared(player["player_id"], "attempt-1", request.boot_id,
                             player["authority_epoch"])
    registry.clock.advance(4)
    with pytest.raises(RegistryError, match="drain_recovery_unverified"):
        drain.abort_prepared(player["player_id"], "attempt-1", request.boot_id,
                             player["authority_epoch"])
    assert registry.control_ack(player["player_id"], ControlAck(
        authority_epoch=player["authority_epoch"],
        delivery_id=old_delivery["delivery_id"], result="applied",
    ))
    with pytest.raises(RegistryError, match="drain_recovery_unverified"):
        drain.abort_prepared(player["player_id"], "attempt-1", request.boot_id,
                             player["authority_epoch"])
    delivery = coordinator.delivery(player["player_id"], player["authority_epoch"])
    digest = state_digest(project_state(delivery, selection))
    issued = registry.issue_control_delivery_record(
        player["player_id"], player["authority_epoch"], digest,
    )
    assert registry.control_ack(player["player_id"], ControlAck(
        authority_epoch=player["authority_epoch"],
        delivery_id=issued["delivery_id"], result="applied",
    ))
    assert drain.abort_prepared(player["player_id"], "attempt-1", request.boot_id,
                                player["authority_epoch"]).status == "aborted"
    assert drain.abort_prepared(player["player_id"], "attempt-1", request.boot_id,
                                player["authority_epoch"]).status == "already_aborted"
    registry.unbind("drain-frame", expected_generation=1)


def test_control_delivery_challenge_and_drain_floor_are_one_lock_cut(registry, monkeypatch):
    player, request = bound_player(registry)
    player_id, epoch = player["player_id"], player["authority_epoch"]
    registry.control_hello(player_id, ControlHello(
        authority_epoch=epoch, schemas=(1, 2), capabilities=()))
    app = create_app(registry.db, registry.clock, ADMIN)
    entered, release, drain_started = Event(), Event(), Event()
    original = app.state.registry.issue_control_delivery_record_in

    def paused_issue(conn, *args):
        entered.set()
        assert release.wait(4)
        return original(conn, *args)

    monkeypatch.setattr(app.state.registry, "issue_control_delivery_record_in", paused_issue)
    headers = {"Authorization": "Bearer " + player["token"]}
    drain = EquipmentDrain(app.state.coordinator)

    def prepare():
        drain_started.set()
        return drain.prepare_idle(player_id, "attempt-race", request.boot_id, epoch,
                                  authorization_expires_at=registry.clock.utc() + 15)

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as workers:
        pending_state = workers.submit(client.get, "/v1/player/state", headers=headers)
        assert entered.wait(4)
        pending_drain = workers.submit(prepare)
        assert drain_started.wait(4)
        try:
            with pytest.raises(TimeoutError):
                pending_drain.result(timeout=.1)
        finally:
            release.set()
        state = pending_state.result(timeout=4)
        prepared = pending_drain.result(timeout=4)
        assert state.status_code == 200
        first = state.json()
        floor = prepared.snapshot["control_floor_sequence"]
        assert first["delivery_sequence"] <= floor
        later = client.get("/v1/player/state", headers=headers).json()
        assert later["delivery_sequence"] > floor
        assert later["delivery_id"] != first["delivery_id"]


def test_expired_prepared_drain_recovers_after_new_epoch_applies_fenced_state(registry):
    old, key, request = enroll(registry)
    frame(registry, "reenroll-frame")
    registry.bind("reenroll-frame", old["player_id"], "HDMI-A-1", expected_generation=0)
    coordinator = Coordinator(registry.db, registry.clock)
    drain = EquipmentDrain(coordinator)
    prepared = drain.prepare_idle(old["player_id"], "attempt-reenroll", request.boot_id,
                                  old["authority_epoch"], authorization_expires_at=1005)
    assert prepared.status == "prepared"

    current, _, _ = enroll(registry, key=key, device_id=request.device_id)
    assert current["player_id"] == old["player_id"]
    assert current["authority_epoch"] > old["authority_epoch"]
    registry.clock.advance(1)
    registry.control_hello(current["player_id"], ControlHello(
        authority_epoch=current["authority_epoch"], schemas=(1, 2), capabilities=()))
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        response = client.get("/v1/player/state", headers={
            "Authorization": "Bearer " + current["token"]})
        assert response.status_code == 200
        delivery = response.json()
    assert registry.control_ack(current["player_id"], ControlAck(
        authority_epoch=current["authority_epoch"],
        delivery_id=delivery["delivery_id"], result="applied"))
    registry.clock.advance(17)
    assert drain.abort_prepared(old["player_id"], "attempt-reenroll", request.boot_id,
                                old["authority_epoch"]).status == "aborted"
