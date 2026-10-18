"""What each Player drew with arrives on its own bus line (central/infra/drawn_report_judge.py): a
`room-lights-drawn` value of the Player line, committed by the real link store, is that Player's
drawn report on House › Lights (judged against the document now); a value that does not decode, or a
Node with no player, records nothing and never rolls back the line's record; the Player's former HTTP
route is gone. The same path over a real Node bus is tests/integration/test_central_room_light_tracer.py's.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from test_registry import ADMIN, enroll

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra import room_light_store
from central.infra.drawn_report_judge import PLAYER_STATE_STREAM, DrawnReportJudge
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.node_link_store import PgLinkStores
from central.infra.room_light_store import PgRoomLights
from contracts.node_link import Pipe
from contracts.node_state import PLAYER_LINE, ROOM_LIGHTS_DRAWN_KEY
from contracts.room_light import ROOM_LIGHTS_DRAWN_PATH, RoomLightsDrawn, room_lights_digest
from contracts.wire_codec import encode
from nodeapi.buffers import state_prefix
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

SERIAL = "10000000d4a3e001"
HEADERS = {"Authorization": "Bearer " + ADMIN}


def _commit(registry, value: bytes, seq: int, key: str = ROOM_LIGHTS_DRAWN_KEY) -> None:
    """One Player-line record, committed by the real link store with the drawn report's judge."""
    stores = PgLinkStores(registry.db, {PLAYER_STATE_STREAM: DrawnReportJudge(PgRoomLights(registry.db, PgHouseLock()))})
    read = Read(Token("epoch", seq), state_prefix(PLAYER_LINE) + key, {}, value)
    asyncio.run(stores.store(SERIAL, Pipe.SHOW).commit(PLAYER_STATE_STREAM, Batch((read,), Token("epoch", seq), False)))


def _records(registry) -> list[int]:
    with registry.db.transaction() as conn:
        return [row["seq"] for row in conn.execute(
            "SELECT seq FROM node_link_records WHERE stream=%s ORDER BY seq", (PLAYER_STATE_STREAM,)).fetchall()]


def test_a_drawn_report_on_the_player_line_is_that_players_on_house_lights(registry, monkeypatch):
    """C4. Mutation probe: the judge matches no key -> House › Lights lists no Player."""
    device = device_id_for_serial(SERIAL)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device, SERIAL))
    _commit(registry, b'{"drawn": null, "following": true}', 1)   # no player enrolled yet: nothing
    player = enroll(registry, device_id=device)[0]
    lights = PgRoomLights(registry.db, PgHouseLock())
    digest = room_lights_digest(lights.current())
    _commit(registry, encode(RoomLightsDrawn(drawn=digest, following=True).model_dump(mode="json"), 256, "x"), 2)
    _commit(registry, b"not a report", 3)
    _commit(registry, b'{"frames": []}', 4, key="frames")   # another key of the line: not a report
    with TestClient(create_app(registry.db, registry.clock, ADMIN)) as client:
        drawn = client.get("/v1/operator/house/lights/drawn", headers=HEADERS)
        assert drawn.status_code == 200, drawn.text
        [reported] = drawn.json()["players"]
        assert (reported["player_id"], reported["state"]) == (player["player_id"], "drawn")
        # The Player's former HTTP route is gone.
        assert client.put(ROOM_LIGHTS_DRAWN_PATH, json={"drawn": None, "following": True}).status_code in (404, 405)
    assert _records(registry) == [1, 2, 3, 4]

    # A report that fails to record rolls back only itself: the line's record still commits.
    def failing(*_args):
        raise RuntimeError("database trouble")
    monkeypatch.setattr(room_light_store.PgRoomLights, "record_drawn_within", failing)
    _commit(registry, b'{"drawn": null, "following": false}', 5)
    assert _records(registry) == [1, 2, 3, 4, 5]
