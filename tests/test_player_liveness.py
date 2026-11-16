"""Player liveness facts on the operator inventory (console pass 2, slice 1, bead 1).

Liveness is the Central-clock time Central last heard from a Player on its current authority
epoch: any request the Player authenticates, written at most every HEARD_WRITE_SECONDS
(central/infra/player_contact_store.py). It is its own domain: a readiness report is not liveness
(readiness is sent once per change), and enrollment is not contact. Real PostgreSQL, explicit
clocks."""

import time

from fastapi.testclient import TestClient
from test_coordination import report, schedule, setup_players
from test_registry import ADMIN, enroll, hello

import contracts.liveness as liveness
import player.central_link
import player.service
from central.app import create_app
from central.coordination import COORDINATION_LOCK, Coordinator
from central.infra.player_contact_store import player_reports_in

OPERATOR = {"Authorization": "Bearer " + ADMIN}


def players_by_id(inventory):
    return {entry.id: entry for entry in inventory.players}


def reports(registry):
    with registry.db.transaction() as conn:
        return player_reports_in(conn, registry.clock.utc())


def inventory(registry):
    return registry.inventory().with_liveness(reports(registry))


def heard(registry, identity):
    """One request the Player authenticates."""
    assert registry.authenticate(identity["token"])["id"] == identity["player_id"]


def test_silence_threshold_is_derived_from_the_players_own_loop_constants():
    assert liveness.SILENT_AFTER_SECONDS == (
        2 * liveness.REQUEST_TIMEOUT + liveness.SESSION_BACKOFF[0] + liveness.REPORT_INTERVAL
        + liveness.HEARD_WRITE_SECONDS
    ) == 36.5
    # The Player binds the same objects, so its loop cannot drift from the threshold.
    assert player.service.BACKOFF is liveness.SESSION_BACKOFF
    assert player.service.REPORT_INTERVAL is liveness.REPORT_INTERVAL
    assert player.central_link.REQUEST_TIMEOUT is liveness.REQUEST_TIMEOUT


def test_a_request_sets_last_report_at_from_centrals_clock_at_most_every_heard_write(registry):
    """Mutation probe: write the contact on every request -> 1003 after the second."""
    identity, _, _ = enroll(registry)
    registry.clock.advance(7)   # heard_at must differ from last_seen (enrollment time)
    heard(registry, identity)
    seen = players_by_id(inventory(registry))[identity["player_id"]]
    assert (seen.last_report_at, seen.last_seen) == (1007, 1000)
    registry.clock.advance(liveness.HEARD_WRITE_SECONDS - 1)
    heard(registry, identity)
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at == 1007
    registry.clock.advance(1)
    heard(registry, identity)
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at == (
        1007 + liveness.HEARD_WRITE_SECONDS)


def test_unbound_player_is_heard_over_http(registry):
    identity, _, _ = enroll(registry)
    hello(registry, identity)
    player_headers = {"Authorization": "Bearer " + identity["token"]}
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        before = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert before["players"][0]["last_report_at"] is None
        registry.clock.advance(2)
        client.app.state.coordinator.advance()
        plan = client.get("/v1/player/state", headers=player_headers).json()["plan"]
        assert plan is not None and plan["layers"] == [] and plan["bindings"] == []
        after = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert after["players"][0]["last_report_at"] == 1002


def test_a_readiness_report_is_not_liveness(registry):
    """One concept, one domain: an accepted readiness report writes no contact. Mutation probe:
    read liveness from player_feedback again -> 1016."""
    players = setup_players(registry, count=1)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(6)
    assert coordinator.readiness(players[0]["player_id"], report(coordinator, players[0]))
    assert players_by_id(inventory(registry))[players[0]["player_id"]].last_report_at is None


def test_enrolled_player_without_contact_has_last_seen_but_no_last_report_at(registry):
    identity, _, _ = enroll(registry)
    seen = players_by_id(inventory(registry))[identity["player_id"]]
    assert seen.last_report_at is None
    assert seen.last_seen == 1000


def test_reenrollment_hides_the_contact_from_the_previous_authority_epoch(registry):
    identity, key, _ = enroll(registry)
    heard(registry, identity)
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at == 1000
    registry.clock.advance(3)
    fresh, _, _ = enroll(registry, key)
    assert fresh["player_id"] == identity["player_id"] and fresh["authority_epoch"] == 2
    seen = players_by_id(inventory(registry))[identity["player_id"]]
    assert seen.last_report_at is None
    assert seen.last_seen == 1003
    heard(registry, fresh)      # the new epoch is heard at once, however recent the old contact
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at == 1003


def test_a_retired_player_has_no_report(registry):
    identity, _, _ = enroll(registry)
    heard(registry, identity)
    registry.retire(identity["player_id"])
    assert reports(registry).reports == {}
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at is None


def test_ages_are_never_negative_after_the_wall_clock_steps_backward(registry):
    def assert_bounded(seen):
        for entry in seen.players:
            assert entry.last_seen <= seen.read_at
            assert entry.last_report_at is None or entry.last_report_at <= seen.read_at

    reporting, _, _ = enroll(registry)
    registry.clock.advance(10)
    heard(registry, reporting)                          # heard_at 1010, after every last_seen
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


def test_inventory_serves_the_liveness_constants_and_read_time(registry):
    enroll(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        body = client.get("/v1/operator/inventory", headers=OPERATOR).json()
    assert body["silent_after_seconds"] == liveness.SILENT_AFTER_SECONDS
    # The console's grace before a fresh enrollment counts as a to-do is derived from this.
    assert body["report_interval_seconds"] == liveness.REPORT_INTERVAL
    assert body["read_at"] == 1000
