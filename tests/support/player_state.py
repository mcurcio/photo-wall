"""The tests' one way to read a Player's state: the first message Central's session socket sends
after it opens (`central/app.py` `session`). The Player never asks for its state (decision 0034,
S3b): there is no state route, only the socket, which pushes it first and then on each change."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from central.infra.player_contact_store import closed_in, opened_in


def player_state(client: TestClient, token: str) -> dict:
    """The state the socket sends first after it opens (`{"type": "state", ...}` without "type")."""
    with client.websocket_connect("/v1/player/session",
                                  headers={"Authorization": "Bearer " + token}) as socket:
        message = socket.receive_json()
        assert message.pop("type") == "state", message
        # Closed and waited for in here, while the session's event loop still runs: a client used
        # outside `with` ends that loop as the socket's block ends, and a close still queued for
        # a worker thread is then cancelled and never written (seen under load, run 824).
        socket.close()
        socket_closed(client, message["configuration"]["player_id"])
    return message


def socket_closed(client: TestClient, player_id: str, seconds: float = 5) -> None:
    """Wait until Central wrote the socket's close (`closed_in`): the session writes it on its
    own thread a moment after the socket closes, which a test counting writes must not see. The
    session's event loop must still be running: inside the socket's block after `close()`, or
    under `with TestClient(...)`."""
    deadline = time.monotonic() + seconds
    while True:
        with client.app.state.registry.db.transaction() as conn:
            row = conn.execute("SELECT connected FROM player_contacts WHERE player_id=%s",
                               (player_id,)).fetchone()
        if row is None or not row["connected"]:
            return
        assert time.monotonic() < deadline, "the session's close was never written"
        time.sleep(.01)




def heard_in(conn, player_id: str, authority_epoch: int, now: float) -> None:
    """Central heard from the Player at `now`: its session socket opened and closed then, in the
    caller's transaction (central/infra/player_contact_store.py, the one writer)."""
    opened_in(conn, player_id, authority_epoch, now)
    closed_in(conn, player_id, authority_epoch, now)


__all__ = ["heard_in", "player_state", "socket_closed"]
