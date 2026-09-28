"""Player liveness facts on the operator inventory (console pass 2, slice 1, bead 1).

Liveness is the Central-clock time of the last readiness report Central accepted on a Player's
current authority epoch; enrollment is not a report. Real PostgreSQL, explicit clocks."""

import time

import pytest
from fastapi.testclient import TestClient
from test_coordination import report, schedule, setup_players
from test_registry import ADMIN, enroll

import contracts.liveness as liveness
import player.central_link
import player.service
from central.app import create_app
from central.coordination import COORDINATION_LOCK, CoordinationError, Coordinator
from contracts.models import Readiness

OPERATOR = {"Authorization": "Bearer " + ADMIN}


def players_by_id(inventory):
    return {entry.id: entry for entry in inventory.players}


def inventory(registry):
    return registry.inventory().with_liveness(
        Coordinator(registry.db, registry.clock).player_reports_lock_free())


def accepted_report(coordinator, identity, *, sequence=1):
    assert coordinator.readiness(identity["player_id"], report(coordinator, identity,
                                                               sequence=sequence))


def test_silence_threshold_is_derived_from_the_players_own_loop_constants():
    assert liveness.SILENT_AFTER_SECONDS == (
        2 * liveness.REQUEST_TIMEOUT + liveness.SESSION_BACKOFF[0] + liveness.REPORT_INTERVAL
    ) == 31.5
    # The Player binds the same objects, so its loop cannot drift from the threshold.
    assert player.service.BACKOFF is liveness.SESSION_BACKOFF
    assert player.service.REPORT_INTERVAL is liveness.REPORT_INTERVAL
    assert player.central_link.REQUEST_TIMEOUT is liveness.REQUEST_TIMEOUT


def test_an_accepted_report_sets_last_report_at_from_centrals_clock(registry):
    identity, _, _ = enroll(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.advance()
    registry.clock.advance(7)   # received_at must differ from last_seen (enrollment time)
    accepted_report(coordinator, identity)
    seen = players_by_id(inventory(registry))[identity["player_id"]]
    assert seen.last_report_at == 1007
    assert seen.last_seen == 1000


def test_unbound_player_is_offered_an_empty_plan_and_its_report_is_heard_over_http(registry):
    identity, _, _ = enroll(registry)
    player_headers = {"Authorization": "Bearer " + identity["token"]}
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        before = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert before["players"][0]["last_report_at"] is None
        registry.clock.advance(2)
        client.app.state.coordinator.advance()
        plan = client.get("/v1/player/state", headers=player_headers).json()["plan"]
        assert plan is not None and plan["layers"] == [] and plan["bindings"] == []
        readiness = Readiness(plan_id=plan["plan_id"], revision=plan["revision"],
                              authority_epoch=plan["authority_epoch"], sequence=1,
                              clock_uncertainty=.01, capacity_ok=True,
                              observed_at=registry.clock.utc())
        response = client.post("/v1/player/readiness", headers=player_headers,
                               json=readiness.model_dump(mode="json"))
        assert response.json() == {"accepted": True}
        after = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert after["players"][0]["last_report_at"] == 1002


def test_enrolled_player_without_a_report_has_last_seen_but_no_last_report_at(registry):
    identity, _, _ = enroll(registry)
    seen = players_by_id(inventory(registry))[identity["player_id"]]
    assert seen.last_report_at is None
    assert seen.last_seen == 1000


def test_reenrollment_hides_the_report_from_the_previous_authority_epoch(registry):
    identity, key, _ = enroll(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.advance()
    accepted_report(coordinator, identity)
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at == 1000
    registry.clock.advance(3)
    fresh, _, _ = enroll(registry, key)
    assert fresh["player_id"] == identity["player_id"] and fresh["authority_epoch"] == 2
    seen = players_by_id(inventory(registry))[identity["player_id"]]
    assert seen.last_report_at is None
    assert seen.last_seen == 1003


def test_a_retired_player_has_no_report(registry):
    identity, _, _ = enroll(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.advance()
    accepted_report(coordinator, identity)
    registry.retire(identity["player_id"])
    assert coordinator.player_reports_lock_free().reports == {}
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at is None


def test_a_rejected_report_leaves_last_report_at_unchanged(registry):
    players = setup_players(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(6)
    first = report(coordinator, players[0])
    assert first.secured and coordinator.readiness(players[0]["player_id"], first)
    registry.bind("frame-0", players[1]["player_id"], "HDMI-A-2", expected_generation=1)
    registry.clock.advance(1)
    newer = first.model_copy(update={"sequence": 2, "observed_at": registry.clock.utc()})
    with pytest.raises(CoordinationError, match="stale_binding"):
        coordinator.readiness(players[0]["player_id"], newer)
    assert players_by_id(inventory(registry))[players[0]["player_id"]].last_report_at == 1006


def test_ages_are_never_negative_after_the_wall_clock_steps_backward(registry):
    def assert_bounded(seen):
        for entry in seen.players:
            assert entry.last_seen <= seen.read_at
            assert entry.last_report_at is None or entry.last_report_at <= seen.read_at

    reporting, _, _ = enroll(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.advance()
    registry.clock.advance(10)
    accepted_report(coordinator, reporting)             # received_at 1010, after every last_seen
    registry.clock.step_utc(-500)                       # Central's clock now reads 510
    seen = inventory(registry)
    assert seen.read_at == 1010
    assert_bounded(seen)
    registry.clock.step_utc(510)
    enroll(registry)                                    # last_seen 1020, after every report
    registry.clock.step_utc(-500)
    seen = inventory(registry)
    assert seen.read_at == 1020
    assert_bounded(seen)
    assert players_by_id(seen)[reporting["player_id"]].last_report_at == 1010


def test_inventory_answers_while_the_coordination_lock_is_held(registry):
    enroll(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        with registry.db.transaction() as holder:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))
            started = time.monotonic()
            response = client.get("/v1/operator/inventory", headers=OPERATOR)
            elapsed = time.monotonic() - started
        assert response.status_code == 200
        # Well within the 5 s lock_timeout the read would wait out if it took the lock.
        assert elapsed < 2


def test_inventory_serves_the_silence_threshold_and_read_time(registry):
    enroll(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        body = client.get("/v1/operator/inventory", headers=OPERATOR).json()
    assert body["silent_after_seconds"] == liveness.SILENT_AFTER_SECONDS
    assert body["read_at"] == 1000
