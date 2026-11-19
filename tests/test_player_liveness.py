"""Player liveness on the operator inventory (console pass 2, slice 1, bead 1; decision 0034).

A Player is live while its session socket is open, and for SILENT_AFTER_SECONDS after it closes:
Central writes its contact (migrations 115, 125, central/infra/player_contact_store.py) when the
socket opens and when it closes, never between, and an authenticated request writes nothing. It is
its own domain: a readiness report is not liveness, and enrollment is not contact. Real PostgreSQL,
explicit clocks."""

import time

from fastapi.testclient import TestClient
from support.player_state import socket_closed
from test_coordination import report, schedule, setup_players
from test_registry import ADMIN, enroll, hello

import contracts.liveness as liveness
import player.central_link
import player.service
from central.app import create_app
from central.coordination import COORDINATION_LOCK, Coordinator
from central.infra.player_contact_store import closed_in, opened_in, player_reports_in

OPERATOR = {"Authorization": "Bearer " + ADMIN}


def players_by_id(inventory):
    return {entry.id: entry for entry in inventory.players}


def reports(registry):
    with registry.db.transaction() as conn:
        return player_reports_in(conn, registry.clock.utc())


def inventory(registry):
    return registry.inventory().with_liveness(reports(registry))


def heard(registry, identity):
    """A session socket opened and closed now: heard now, not connected."""
    with registry.db.transaction() as conn:
        for write in (opened_in, closed_in):
            write(conn, identity["player_id"], identity["authority_epoch"], registry.clock.utc())


def contact(registry, player_id):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT heard_at, connected FROM player_contacts WHERE player_id=%s",
                            (player_id,)).fetchone()


def test_silence_threshold_is_derived_from_the_players_own_session_constants():
    assert liveness.SILENT_AFTER_SECONDS == (
        2 * liveness.KEEPALIVE_SECONDS + liveness.SESSION_BACKOFF[0]
        + 2 * liveness.REQUEST_TIMEOUT
    ) == 51
    # The Player binds the same objects, so its session cannot drift from the threshold.
    assert player.service.BACKOFF is liveness.SESSION_BACKOFF
    assert player.central_link.REQUEST_TIMEOUT is liveness.REQUEST_TIMEOUT
    options = player.central_link.CentralLink.WEBSOCKET_OPTIONS
    assert options["ping_interval"] == options["ping_timeout"] == liveness.KEEPALIVE_SECONDS


def test_an_authenticated_request_writes_no_contact(registry):
    """Mutation probe: write the contact on every authenticated request (the old `heard_in`) ->
    a contact row."""
    identity, _, _ = enroll(registry)
    registry.clock.advance(7)
    assert registry.authenticate(identity["token"])["id"] == identity["player_id"]
    assert contact(registry, identity["player_id"]) is None
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at is None


def test_a_player_with_an_open_socket_reads_live_and_silent_after_its_close(registry):
    """The Player's socket open, then no request at all for 60 s: it reads heard now. Its socket
    closes at T: it reads heard at T, and silent once SILENT_AFTER_SECONDS have passed. Mutation
    probe: write `connected` false at open -> heard at the open, 60 s ago."""
    identity, _, _ = enroll(registry)
    hello(registry, identity)
    headers = {"Authorization": "Bearer " + identity["token"]}
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        before = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert before["players"][0]["last_report_at"] is None
        with client.websocket_connect("/v1/player/session", headers=headers) as socket:
            assert socket.receive_json()["type"] == "state"
            registry.clock.advance(60)
            live = client.get("/v1/operator/inventory", headers=OPERATOR).json()
            assert live["players"][0]["last_report_at"] == live["read_at"] == 1060
            assert contact(registry, identity["player_id"])["connected"] is True
        socket_closed(client, identity["player_id"])
        closed = contact(registry, identity["player_id"])
        assert (closed["heard_at"], closed["connected"]) == (1060, False)
        registry.clock.advance(liveness.SILENT_AFTER_SECONDS)
        quiet = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert quiet["read_at"] - quiet["players"][0]["last_report_at"] == (
            liveness.SILENT_AFTER_SECONDS)                          # not yet past the limit
        registry.clock.advance(1)
        silent = client.get("/v1/operator/inventory", headers=OPERATOR).json()
        assert silent["read_at"] - silent["players"][0]["last_report_at"] > (
            silent["silent_after_seconds"])


def test_a_dead_centrals_sockets_never_read_live_after_it_starts_again(registry):
    """A Central that stopped without closing its sockets left `connected`: the API process's start
    clears it (one process holds every Player socket), so the Player reads heard when that Central
    last wrote, not now. Mutation probe: skip the reset at start -> it reads heard now."""
    identity, _, _ = enroll(registry)
    with registry.db.transaction() as conn:
        opened_in(conn, identity["player_id"], identity["authority_epoch"], registry.clock.utc())
    registry.clock.advance(120)
    assert players_by_id(inventory(registry))[identity["player_id"]].last_report_at == 1120
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        body = client.get("/v1/operator/inventory", headers=OPERATOR).json()
    assert contact(registry, identity["player_id"])["connected"] is False
    assert body["players"][0]["last_report_at"] == 1000
    assert body["read_at"] - 1000 > body["silent_after_seconds"]


def test_a_readiness_report_is_not_liveness(registry):
    """One concept, one domain: an accepted readiness report writes no contact. Mutation probe:
    read liveness from the readiness receipt (player_show_state) again -> 1016."""
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
    registry.retire(identity["player_id"], by="console")
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
    assert body["socket_keepalive_seconds"] == liveness.KEEPALIVE_SECONDS
    assert body["read_at"] == 1000
