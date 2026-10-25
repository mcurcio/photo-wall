"""109 over rows seeded at 108 (099's shapes): each light's colour becomes a colour source (a
reported light follows its bulb with its old kelvin typed, 6500 for an old reported colour; a
measured or fixed light's kelvin becomes typed), each light word and reading word becomes its
reference's facts (a stated state and when, a colour dimmer's rgb as its stated colour, "unavailable"
keeping nothing and can't tell since it was heard), and the document the store builds from them is
the one the new rules give.
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from central.db import Database
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.room_light_store import PgRoomLights, document_in
from central.room_light.model import (
    BulbColour,
    FixedLight,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    RoomLight,
    TypedColour,
)
from contracts.room_light import (
    Dimmer,
    FixedEntry,
    KelvinColour,
    Measured,
    MeasuredEntry,
    NotReported,
    ReportedEntry,
    RgbColour,
)

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
HA = "home-assistant"


def _at_108(db: Database) -> None:
    """Every migration up to 108, as Database.migrate applies them."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                     "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= "109":
                break
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                         (path.name, sha256(path.read_bytes()).hexdigest()))


def _light(conn, light_id: str, position: int, light: dict) -> None:
    conn.execute("INSERT INTO room_lights(id, light, position) VALUES (%s, %s, %s) "
                 "ON CONFLICT (id) DO UPDATE SET light=EXCLUDED.light", (light_id, json.dumps(light), position))


def _word(conn, table: str, address: str, word: dict, heard_at: int) -> None:
    conn.execute(f"INSERT INTO {table}(source, address, word, heard_at) VALUES (%s, %s, %s, to_timestamp(%s))",  # noqa: S608
                 (HA, address, json.dumps(word), heard_at))


def _reported(address: str, colour: dict) -> dict:
    return {"kind": "reported", "name": address, "reporter": {"source": HA, "address": address}, "colour": colour}


def test_109_upgrades_lights_and_words_to_colour_sources_and_facts(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo)
        try:
            _at_108(db)
            with db.transaction() as conn:
                _light(conn, "white", 10, _reported("light.white", {"kind": "fixed", "kelvin": 5000}))
                _light(conn, "colour", 11, _reported("light.colour", {"kind": "reported"}))
                _light(conn, "away", 12, _reported("light.away", {"kind": "fixed", "kelvin": 2700}))
                _light(conn, "window", 13, {"kind": "measured", "name": "Window", "sensor": "lux",
                                            "colour": {"kind": "fixed", "kelvin": 5500}})
                conn.execute("INSERT INTO sensors(id, sensor) VALUES ('lux', %s)",
                             (json.dumps({"name": "Lux", "at": {"source": HA, "address": "sensor.lux"}}),))
                _word(conn, "light_words", "light.white", {"kind": "dimmer", "on": True, "brightness": 0.5}, 100)
                _word(conn, "light_words", "light.colour",
                      {"kind": "colour-dimmer", "on": True, "brightness": 0.25, "rgb": [1.0, 0.5, 0.0]}, 200)
                _word(conn, "light_words", "light.dark",
                      {"kind": "colour-dimmer", "on": False, "brightness": 0.0, "rgb": [0.0, 0.0, 0.0]}, 250)
                _word(conn, "light_words", "light.away", {"kind": "unavailable"}, 300)
                _word(conn, "reading_words", "sensor.lux", {"kind": "measured", "lux": 80.0}, 400)
                _word(conn, "reading_words", "sensor.gone", {"kind": "unavailable"}, 500)
                change = conn.execute("SELECT change FROM room_lights_change WHERE one").fetchone()["change"]
            db.migrate()
            with db.transaction() as conn:
                lights = {row["id"]: TypeAdapter(RoomLight).validate_python(row["light"])
                          for row in conn.execute("SELECT id, light FROM room_lights").fetchall()}
                assert lights["white"] == ReportedLight(name="light.white", reporter=ReporterRef(
                    source=HA, address="light.white"), colour=BulbColour(typed_kelvin=5000))
                assert lights["colour"].colour == BulbColour(typed_kelvin=6500)
                assert lights["away"].colour == BulbColour(typed_kelvin=2700)
                assert lights["window"] == MeasuredLight(name="Window", sensor="lux", colour=TypedColour(kelvin=5500))
                assert isinstance(lights["room"], FixedLight) and lights["room"].colour == TypedColour(kelvin=6500)
                words = {row["address"]: row for row in conn.execute(
                    "SELECT address, state, colour, EXTRACT(EPOCH FROM state_at) AS state_at, "
                    "EXTRACT(EPOCH FROM colour_at) AS colour_at, EXTRACT(EPOCH FROM cant_tell_since) AS cant_tell, "
                    "EXTRACT(EPOCH FROM heard_at) AS heard_at FROM light_words").fetchall()}
                assert (words["light.white"]["state"], words["light.white"]["colour"], words["light.white"]["state_at"]) == (
                    {"kind": "dimmer", "on": True, "brightness": 0.5}, None, 100)
                assert (words["light.colour"]["colour"], words["light.colour"]["colour_at"]) == (
                    {"kind": "rgb", "rgb": [1.0, 0.5, 0.0]}, 200)
                assert words["light.dark"]["colour"] is None      # all channels 0: no colour stated
                assert (words["light.away"]["state"], words["light.away"]["cant_tell"],
                        words["light.away"]["heard_at"]) == (None, 300, 300)
                readings = {row["address"]: row for row in conn.execute(
                    "SELECT address, reading, EXTRACT(EPOCH FROM cant_tell_since) AS cant_tell FROM reading_words")}
                assert (readings["sensor.lux"]["reading"], readings["sensor.lux"]["cant_tell"]) == (
                    {"kind": "measured", "lux": 80.0}, None)
                assert (readings["sensor.gone"]["reading"], readings["sensor.gone"]["cant_tell"]) == (None, 500)
                document = document_in(conn)
            assert document.change == change + 1
            assert document.lights["white"] == ReportedEntry(colour=KelvinColour(kelvin=5000),
                                                             state=Dimmer(on=True, brightness=0.5))
            assert document.lights["colour"] == ReportedEntry(colour=RgbColour(rgb=(1.0, 0.5, 0.0)),
                                                              state=Dimmer(on=True, brightness=0.25))
            assert document.lights["away"] == ReportedEntry(colour=KelvinColour(kelvin=2700), state=NotReported())
            assert document.lights["window"] == MeasuredEntry(colour=KelvinColour(kelvin=5500), state=Measured(lux=80.0))
            assert document.lights["room"] == FixedEntry(colour=KelvinColour(kelvin=6500))
            view = {each.id: each for each in PgRoomLights(db, PgHouseLock()).lights().lights}
            assert view["away"].cant_tell_since == 300 and view["white"].cant_tell_since is None
            assert view["colour"].colour.colour_from == "following" and view["white"].colour.colour_from == "never-stated"
        finally:
            db.close()
