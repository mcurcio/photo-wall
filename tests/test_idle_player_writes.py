"""An idle Player costs Central reads, never writes, through the real routes on real PostgreSQL.

On 2026-11-16 one idle Pi kept Central committing under the fleet-wide Coordination lock about
six times a second: readiness re-posted every half second (a new sequence, a readiness
upsert and a grant pass each), the session socket re-sending an unchanged state every half second
(a delivery under the lock each), and every state answered by an acknowledgment that row-locked
the Player (`SELECT ... FOR UPDATE`), however often that delivery had been acknowledged before.

Here each is sent and written only on a change: a re-sent readiness (the Player's retry) and a
re-acknowledged delivery write nothing; the Player never asks for its state, which the socket
pushes when it changed (an event, never a timer); liveness is the socket's open and close, written
then and never between (decision 0034). A real change still reaches the other side.

`Watched` is the real Database with one look at the end of each transaction: whether it took a
transaction id (any row written or row-locked: the WAL flush a commit then waits for) and whether
it held the Coordination lock.
"""
from __future__ import annotations

import concurrent.futures
import time
from contextlib import contextmanager

from fastapi.testclient import TestClient
from support.player_state import player_state
from test_coordination import report, schedule, setup_players
from test_registry import ADMIN, hello

from central.app import create_app
from central.db import Database
from central.transaction_locks import COORDINATION_LOCK

IDLE_MINUTE = 120       # half-second turns in a minute


class Watched(Database):
    """The real Database; `log` holds (wrote, held Coordination) for each committed transaction."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn, application_name="photo-wall-test")
        self.log: list[tuple[bool, bool]] = []

    @contextmanager
    def transaction(self):
        with super().transaction() as conn:
            yield conn
            row = conn.execute(
                "SELECT pg_current_xact_id_if_assigned() IS NOT NULL AS wrote, EXISTS(SELECT 1 "
                "FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted "
                "AND classid=0 AND objid=%s::oid AND objsubid=1) AS coordination",
                (COORDINATION_LOCK,)).fetchone()
            self.log.append((row["wrote"], row["coordination"]))

    def since(self, mark: int) -> dict[str, int]:
        entries = self.log[mark:]
        return {"transactions": len(entries),
                "writes": sum(wrote for wrote, _ in entries),
                "coordination": sum(held for _, held in entries),
                "coordination_writes": sum(wrote and held for wrote, held in entries)}


def _granted(registry):
    """One bound Player holding a plan, its readiness accepted, a grant delivered and applied."""
    player = setup_players(registry, count=1)[0]
    hello(registry, player)
    watched = Watched(registry.db.dsn)
    app = create_app(watched, registry.clock, ADMIN)
    return player, watched, app


def _ready_and_applied(client, app, registry, player, headers):
    coordinator = app.state.coordinator
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(6)
    readiness = report(coordinator, player, sequence=1)
    answer = client.post("/v1/player/readiness", headers=headers,
                         json=readiness.model_dump(mode="json"))
    assert answer.json() == {"accepted": True}, answer.text
    state = player_state(client, player["token"])
    assert state["commits"], state
    acked = client.post("/v1/player/control-acks", headers=headers, json={
        "authority_epoch": player["authority_epoch"], "delivery_id": state["delivery_id"],
        "result": "applied"})
    assert acked.json()["accepted"] is True and acked.json()["receipt"], acked.text
    return readiness, state, acked.json()


def _heard(client, player_id):
    inventory = client.get("/v1/operator/inventory", headers={"Authorization": "Bearer " + ADMIN})
    (entry,) = [p for p in inventory.json()["players"] if p["id"] == player_id]
    return entry["last_report_at"], inventory.json()["read_at"]


def test_an_idle_player_minute_writes_nothing(registry):
    """A minute of an idle Player's traffic (its socket open, a clock probe every half second, its
    delivery acknowledged again, its unchanged readiness re-sent after a lost answer) commits no
    write and row-locks nothing, and the console reads the Player as heard now. Mutation probes:
    take `FOR UPDATE` on the ack's replay path -> a write per turn; write a contact on every
    authenticated request (the old `heard_in`) -> writes."""
    player, watched, app = _granted(registry)
    headers = {"Authorization": "Bearer " + player["token"]}
    try:
        with TestClient(app) as client:
            readiness, state, acked = _ready_and_applied(client, app, registry, player, headers)
            with client.websocket_connect("/v1/player/session", headers=headers) as connection:
                assert _receive(connection, 5)["delivery_id"] == state["delivery_id"]
                time.sleep(.3)      # its open's contact write, and the wake it may cause
                mark = len(watched.log)
                for turn in range(IDLE_MINUTE):
                    registry.clock.advance(.5)
                    assert client.get("/v1/player/time", headers=headers).status_code == 200
                    reacked = client.post("/v1/player/control-acks", headers=headers, json={
                    "authority_epoch": player["authority_epoch"],
                        "delivery_id": state["delivery_id"], "result": "applied"})
                    assert reacked.json() == acked, turn
                    if turn % 40 == 0:      # the Player's retry of a report whose answer it lost
                        resent = client.post("/v1/player/readiness", headers=headers,
                                             json=readiness.model_dump(mode="json"))
                        assert resent.json() == {"accepted": False}, resent.text
                counts = watched.since(mark)
                assert counts["writes"] == 0, counts
                heard, read_at = _heard(client, player["player_id"])
                assert heard == read_at, (heard, read_at)
    finally:
        watched.close()


def test_a_re_acknowledged_delivery_takes_no_row_lock_and_a_new_one_is_accepted(registry):
    """An acknowledgment of a delivery already handled is answered with its receipt and writes
    nothing (no `FOR UPDATE` on the Player's row, which node admission's `FOR SHARE` waited on); a
    new delivery's acknowledgment still records it and answers a new receipt. Mutation probe: drop
    the replay path -> the re-ack writes."""
    player, watched, app = _granted(registry)
    headers = {"Authorization": "Bearer " + player["token"]}
    try:
        with TestClient(app) as client:
            _, state, acked = _ready_and_applied(client, app, registry, player, headers)
            mark = len(watched.log)
            again = client.post("/v1/player/control-acks", headers=headers, json={
                "authority_epoch": player["authority_epoch"], "delivery_id": state["delivery_id"],
                "result": "applied"})
            assert again.json() == acked
            assert watched.since(mark)["writes"] == 0, watched.since(mark)
            # A real change: the Player no longer holds its grant, so a new delivery is issued.
            lost = report(app.state.coordinator, player, sequence=2, prepared=False)
            assert client.post("/v1/player/readiness", headers=headers,
                               json=lost.model_dump(mode="json")).json() == {"accepted": True}
            changed = player_state(client, player["token"])
            assert changed["delivery_id"] != state["delivery_id"]
            mark = len(watched.log)
            new = client.post("/v1/player/control-acks", headers=headers, json={
                "authority_epoch": player["authority_epoch"],
                "delivery_id": changed["delivery_id"], "result": "applied"}).json()
            assert new["accepted"] is True
            assert new["receipt"]["delivery_id"] == changed["delivery_id"]
            assert watched.since(mark)["writes"] == 1
    finally:
        watched.close()


def _receive(connection, seconds: float) -> dict:
    """The socket's next message, failing (never hanging) when none comes within `seconds`."""
    pool = concurrent.futures.ThreadPoolExecutor(1)
    try:
        return pool.submit(connection.receive_json).result(timeout=seconds)
    finally:
        pool.shutdown(wait=False)


def test_the_session_socket_pushes_a_state_only_when_it_changed(registry):
    """The socket sends the state once, then nothing while it stays unchanged (no delivery under the
    Coordination lock either: no timer re-reads it); a change written by any request (here a
    readiness posted over HTTP, whose grant changes the state) reaches the socket at once.
    Mutation probes: restore the half-second re-send -> deliveries while idle; drop the commit's
    NOTIFY -> the grant never reaches the socket."""
    player, watched, app = _granted(registry)
    headers = {"Authorization": "Bearer " + player["token"]}
    try:
        with TestClient(app) as client:
            coordinator = app.state.coordinator
            schedule(coordinator, ["frame-0"])
            registry.clock.advance(6)
            deadline = time.monotonic() + 5
            while app.state.player_state_wakes.count() == 0:    # its LISTEN is connected
                assert time.monotonic() < deadline
                time.sleep(.01)
            with client.websocket_connect("/v1/player/session", headers=headers) as connection:
                first = _receive(connection, 5)
                assert first["type"] == "state" and first["plan"] and not first["commits"]
                # Its first delivery was issued (a write), whose own wake reads it once more.
                time.sleep(.3)
                mark = len(watched.log)
                time.sleep(1.5)     # three of the old half-second re-sends
                idle = watched.since(mark)
                assert idle["coordination"] == 0, idle
                readiness = report(coordinator, player, sequence=1)
                assert client.post("/v1/player/readiness", headers=headers,
                                   json=readiness.model_dump(mode="json")).json() == {
                                       "accepted": True}
                granted = _receive(connection, 5)
                assert granted["commits"][0]["readiness_sequence"] == 1
                assert granted["delivery_id"] != first["delivery_id"]
    finally:
        watched.close()
