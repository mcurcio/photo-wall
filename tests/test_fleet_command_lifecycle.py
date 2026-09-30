"""Internal command composition stays atomic and unmounted until D14/D17."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from test_fleet_attempts import (
    DEVICE_ID,
    FALLBACK_SHA,
    TARGET_SHA,
    _principal,
    _seed,
)
from test_fleet_rollout_gate import _gate
from test_registry import enroll

from central.fleet.command_lifecycle import FleetCommandLifecycle
from central.fleet.models import FleetError, MaintenanceRequestWrite
from central.fleet.principal import PrincipalError
from central.fleet.service import FleetService
from central.registry import RegistryError
from contracts.app_process_proof import ProcessIdentity
from contracts.os_command import LocalCommandContext, parse_activate_app_command
from contracts.os_stop_permit import (
    ReadyToStop,
    parse_stop_permit,
    require_current_stop_permit,
)


def _setup(registry):
    _seed(registry)
    player, _, _ = enroll(registry, device_id=DEVICE_ID)
    request_id = uuid4()
    FleetService(registry.db, registry.clock).request_maintenance(
        DEVICE_ID,
        MaintenanceRequestWrite(
            request_id=request_id, expected_device_generation=1,
            expected_policy_source="explicit", expected_policy_revision=1,
            expected_target_sha256=TARGET_SHA, ttl_seconds=300,
        ),
    )
    gate, _ = _gate(registry)
    generation = gate.open(expected_revision=0).generation
    return FleetCommandLifecycle(registry.db, registry.clock, gate), request_id, player, generation


def _ready(command):
    parsed = parse_activate_app_command(command.command_bytes)
    return ReadyToStop(
        attempt_id=command.attempt_id, command_id=command.command_id,
        drain_id=command.drain_id, command_sha256=command.command_sha256,
        ready_nonce="1" * 64,
        target_sha256=parsed.target.sha256, target_size=parsed.target.size,
        fallback_sha256=parsed.fallback.sha256, fallback_size=parsed.fallback.size,
        base_abi=parsed.target.base_abi,
        staged_target_sha256=parsed.target.sha256,
        staged_fallback_sha256=parsed.fallback.sha256,
        selected_sha256=parsed.fallback.sha256,
        process=ProcessIdentity(pid=4321, start_ticks=9001,
                                invocation_id="2" * 32,
                                cgroup_unit="photo-wall-player.service"),
        capacity_checked=True, sampled_boottime_ms=1000,
    )


def test_unbound_dispatch_and_permit_are_atomic_idempotent_and_exact(registry) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    principal = _principal()
    command = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    assert parse_activate_app_command(command.command_bytes).fallback.sha256 == FALLBACK_SHA
    assert hashlib.sha256(command.command_bytes).hexdigest() == command.command_sha256
    assert lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    ) == command
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT status FROM fleet_maintenance_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()["status"] == "dispatched"
        assert conn.execute(
            "SELECT phase,fleet_drain_id FROM equipment_drains WHERE player_id=%s",
            (player["player_id"],),
        ).fetchone() == {"phase": "prepared", "fleet_drain_id": None}

    ready = _ready(command)
    permit = lifecycle.authorize_stop_unbound(
        principal, ready, expected_gate_generation=generation,
    )
    permit_doc = parse_stop_permit(permit.permit_bytes)
    assert permit_doc.ready_sha256
    require_current_stop_permit(
        permit_doc, parse_activate_app_command(command.command_bytes),
        LocalCommandContext(
            principal.installation_audience, principal.device_id,
            principal.device_generation, principal.kernel_boot_id,
            principal.offer_id, principal.command_session_id,
            ready.base_abi, registry.clock.utc(), principal.expires_at,
        ), ready, command_bytes=command.command_bytes,
        now_utc=registry.clock.utc(),
    )
    assert lifecycle.authorize_stop_unbound(
        principal, ready, expected_gate_generation=generation,
    ) == permit
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT phase,fleet_drain_id FROM equipment_drains WHERE player_id=%s",
            (player["player_id"],),
        ).fetchone() == {"phase": "stop_committed", "fleet_drain_id": command.drain_id}
        assert conn.execute(
            "SELECT phase FROM fleet_app_attempts WHERE attempt_id=%s",
            (command.attempt_id,),
        ).fetchone()["phase"] == "stop_committed"


def test_invalid_ready_leaves_prepared_drain_and_no_permit(registry) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    principal = _principal()
    command = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    bad = _ready(command).model_copy(update={"command_sha256": "0" * 64})
    with pytest.raises(FleetError, match="ready_to_stop_attempt_mismatch"):
        lifecycle.authorize_stop_unbound(
            principal, bad, expected_gate_generation=generation,
        )
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT phase,fleet_drain_id FROM equipment_drains WHERE player_id=%s",
            (player["player_id"],),
        ).fetchone() == {"phase": "prepared", "fleet_drain_id": None}
        assert conn.execute(
            "SELECT count(*) AS n FROM fleet_app_stop_permits",
        ).fetchone()["n"] == 0


def test_dispatch_refusal_rolls_back_attempt_roots_request_and_drain(registry) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    with registry.db.transaction() as conn:
        conn.execute("DELETE FROM outputs WHERE player_id=%s", (player["player_id"],))
    with pytest.raises(RegistryError, match="no_observed_outputs"):
        lifecycle.dispatch_unbound(
            _principal(), request_id=request_id, player_id=player["player_id"],
            expected_gate_generation=generation,
        )
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT status FROM fleet_maintenance_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()["status"] == "queued"
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM equipment_drains").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM asset_references WHERE owner LIKE 'fleet-attempt:%'",
        ).fetchone()["n"] == 0


def test_stop_snapshot_change_rolls_back_fleet_id_and_permit(registry) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    principal = _principal()
    command = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE outputs SET observation=jsonb_set(observation,'{width_px}',"
            "'1024'::jsonb) WHERE player_id=%s AND output_id='HDMI-A-1'",
            (player["player_id"],),
        )
    with pytest.raises(RegistryError, match="unbound_drain_snapshot_changed"):
        lifecycle.authorize_stop_unbound(
            principal, _ready(command), expected_gate_generation=generation,
        )
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT phase,fleet_drain_id FROM equipment_drains WHERE player_id=%s",
            (player["player_id"],),
        ).fetchone() == {"phase": "prepared", "fleet_drain_id": None}
        assert conn.execute(
            "SELECT phase FROM fleet_app_attempts WHERE attempt_id=%s",
            (command.attempt_id,),
        ).fetchone()["phase"] == "prepared"
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_stop_permits").fetchone()["n"] == 0


def test_permit_replay_survives_new_desired_policy_but_not_expiry(registry) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    principal = _principal()
    command = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    ready = _ready(command)
    permit = lifecycle.authorize_stop_unbound(
        principal, ready, expected_gate_generation=generation,
    )
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE fleet_app_policy SET revision=2,target_tag=NULL,"
            "target_sha256=NULL,target_size=NULL,changed_at=1001 WHERE singleton=TRUE"
        )
    assert lifecycle.authorize_stop_unbound(
        principal, ready, expected_gate_generation=generation,
    ) == permit
    registry.clock.advance(permit.expires_at - registry.clock.utc() + .01)
    with pytest.raises(FleetError, match="stop_permit_unavailable"):
        lifecycle.authorize_stop_unbound(
            principal, ready, expected_gate_generation=generation,
        )


def test_permit_replay_rechecks_expiry_after_drain_row_wait(registry, monkeypatch) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    principal = _principal()
    command = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    ready = _ready(command)
    permit = lifecycle.authorize_stop_unbound(
        principal, ready, expected_gate_generation=generation,
    )
    reached = Event()
    original = FleetCommandLifecycle._require_permit_replay

    def signal_after_first_deadline_check(*args, **kwargs):
        original(*args, **kwargs)
        reached.set()

    monkeypatch.setattr(FleetCommandLifecycle, "_require_permit_replay",
                        staticmethod(signal_after_first_deadline_check))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT 1 FROM equipment_drains WHERE player_id=%s FOR UPDATE",
                            (player["player_id"],))
            future = pool.submit(lifecycle.authorize_stop_unbound, principal, ready,
                                 expected_gate_generation=generation)
            assert reached.wait(timeout=3)
            assert not future.done()
            registry.clock.advance(permit.expires_at - registry.clock.utc() + .01)
        with pytest.raises(FleetError, match="stop_permit_unavailable"):
            future.result(timeout=5)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fleet_app_stop_permits").fetchone()["n"] == 1


def test_command_replay_rechecks_session_after_attempt_row_wait(registry, monkeypatch) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    principal = _principal()
    command = lifecycle.dispatch_unbound(
        principal, request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    import central.fleet.command_lifecycle as module

    reached = Event()
    original = module.require_current_principal_in

    def signal_after_session_lock(conn, value, *, clock):
        result = original(conn, value, clock=clock)
        reached.set()
        return result

    monkeypatch.setattr(module, "require_current_principal_in", signal_after_session_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.db.transaction() as blocker:
            blocker.execute("SELECT 1 FROM fleet_app_attempts WHERE attempt_id=%s FOR UPDATE",
                            (command.attempt_id,))
            future = pool.submit(lifecycle.dispatch_unbound, principal,
                                 request_id=request_id, player_id=player["player_id"],
                                 expected_gate_generation=generation)
            assert reached.wait(timeout=3)
            assert not future.done()
            registry.clock.advance(101)
        with pytest.raises(PrincipalError, match="os_command_session_unavailable"):
            future.result(timeout=5)
