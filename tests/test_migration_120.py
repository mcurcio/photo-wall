"""120 (decision 0034, slice S2d) over rows seeded at 119: the room-light setup becomes one
`singletons` document that validates as `_RoomLightSetup` (every light with its position, every
Sensor and colour sensor, the change number); facts move into `room_light_facts` for the references
the setup names only; refused reports become `events`; Galleria's birth survives as a `singletons`
document; paper profiles gain their audit fields; the ten tables are gone.

Mutation probe: drop the `WHERE EXISTS` naming filter from 120's light copy -> the unnamed light's
facts are copied and the facts case fails.
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from central.db import Database
from central.infra.events import refusals_in
from central.infra.room_light_rows import ROOM_LIGHT_SETUP, lights_in
from central.infra.room_light_store import facts_in, galleria_birth_in
from central.room_light.model import (
    BulbColour,
    ColourSensor,
    ColourSensorRef,
    Doubt,
    DoubtReason,
    FixedLight,
    Kept,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    SensedColour,
    Sensor,
    SensorRef,
    TypedColour,
)
from contracts.home_assistant import GalleriaBirth, GalleriaConnection
from contracts.room_light import Dimmer, KelvinColour, Measured

pytestmark = pytest.mark.db

MIGRATIONS = Path(__file__).parents[1] / "central/migrations"
HA = "home-assistant"
DROPPED = ("room_lights", "sensors", "colour_sensors", "room_lights_change", "light_words", "reading_words",
           "colour_words", "room_lights_drawn", "report_refusals", "galleria_birth")
SPOT = ReportedLight(name="Spot", reporter=ReporterRef(source=HA, address="light.spot"),
                     colour=BulbColour(typed_kelvin=2700))
WINDOW = MeasuredLight(name="Window", sensor="lux", colour=TypedColour(kelvin=6500))
SUN = FixedLight(name="Sun", colour=SensedColour(sensor="sky", typed_kelvin=5500))
LUX = Sensor(name="Lux", at=SensorRef(source=HA, address="sensor.lux"))
SKY = ColourSensor(name="Sky", at=ColourSensorRef(source=HA, address="sensor.sky"))
BIRTH = GalleriaBirth(version="0.45.0", kinds=("light",), connection=GalleriaConnection(server="NHUB", client=3))


def _applied(db: Database, stop: str) -> None:
    """Every migration before `stop`, as Database.migrate applies them."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                     "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name >= stop:
                break
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                         (path.name, sha256(path.read_bytes()).hexdigest()))


def _audit(at: int, by: str) -> tuple:
    return at, by, at + 1, by


def _seed(conn) -> int:
    for light_id, position, light in (("spot", 1, SPOT), ("window", 2, WINDOW), ("sun", 3, SUN)):
        conn.execute("INSERT INTO room_lights(id, light, position) VALUES (%s, %s, %s)",
                     (light_id, json.dumps(light.model_dump(mode="json")), position))
    conn.execute("INSERT INTO sensors(id, sensor) VALUES ('lux', %s)", (json.dumps(LUX.model_dump(mode="json")),))
    conn.execute("INSERT INTO colour_sensors(id, sensor, created_at, created_by, changed_at, changed_by) "
                 "VALUES ('sky', %s, to_timestamp(10), 'operator', to_timestamp(11), 'operator')",
                 (json.dumps(SKY.model_dump(mode="json")),))
    words = ("INSERT INTO light_words(source, address, state, state_at, colour, colour_at, doubt_reason, doubt_since, "
             "heard_at, created_at, created_by, changed_at, changed_by) VALUES (%s, %s, %s, to_timestamp(%s), %s, "
             "to_timestamp(%s), %s, to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), %s, to_timestamp(%s), %s)")
    conn.execute(words, (HA, "light.spot", json.dumps({"kind": "dimmer", "on": True, "brightness": 0.5}), 100,
                         json.dumps({"kind": "kelvin", "kelvin": 2200.0}), 110, None, None, 120, *_audit(90, HA)))
    conn.execute(words, (HA, "light.unnamed", json.dumps({"kind": "dimmer", "on": True, "brightness": 1.0}), 100,
                         None, None, None, None, 120, *_audit(90, HA)))
    conn.execute("INSERT INTO reading_words(source, address, reading, reading_at, doubt_reason, doubt_since, heard_at, "
                 "created_at, created_by, changed_at, changed_by) VALUES "
                 "(%s, 'sensor.lux', %s, to_timestamp(200), 'link-closed', to_timestamp(250), to_timestamp(210), "
                 "to_timestamp(190), %s, to_timestamp(250), 'central'), "
                 "(%s, 'sensor.gone', NULL, NULL, NULL, NULL, to_timestamp(210), to_timestamp(190), %s, "
                 "to_timestamp(191), %s)",
                 (HA, json.dumps({"kind": "measured", "lux": 80.0}), HA, HA, HA, HA))
    conn.execute("INSERT INTO colour_words(source, address, colour, colour_at, heard_at, created_at, created_by, "
                 "changed_at, changed_by) VALUES (%s, 'sensor.sky', %s, to_timestamp(300), to_timestamp(310), "
                 "to_timestamp(300), %s, to_timestamp(310), %s)",
                 (HA, json.dumps({"kind": "kelvin", "kelvin": 3456.5}), HA, HA))
    conn.execute("INSERT INTO galleria_birth(one, birth, heard_at, created_at, created_by, changed_at, changed_by) "
                 "VALUES (true, %s, to_timestamp(400), to_timestamp(400), 'galleria', to_timestamp(401), 'galleria')",
                 (json.dumps(BIRTH.model_dump(mode="json")),))
    conn.execute("INSERT INTO report_refusals(source, address, reason, at) VALUES "
                 "(%s, 'Not_An_Entity', 'unknown_address', to_timestamp(500)), ('pi', 'p-1', 'unreadable', "
                 "to_timestamp(501))", (HA,))
    conn.execute("INSERT INTO paper_profiles(profile_sha256, name, profile, added_at) VALUES (%s, 'Paper', %s, 1000)",
                 ("a" * 64, bytes(128)))
    conn.execute("UPDATE room_lights_change SET change = 7 WHERE one")
    return 7


def test_120_moves_the_setup_the_named_facts_refusals_and_galleria_s_birth(database_provisioner):
    with database_provisioner.empty() as conninfo:
        db = Database(conninfo, application_name="photo-wall-test")
        try:
            _applied(db, "120")
            with db.transaction() as conn:
                change = _seed(conn)
            db.migrate()
            with db.transaction() as conn:
                stored = ROOM_LIGHT_SETUP.read_in(conn)
                assert stored is not None and stored.revision == 1 and stored.created_by == "unknown"
                setup = stored.value
                assert setup.change == change
                assert list(lights_in(conn).items()) == [
                    ("room", FixedLight(name="Room", colour=TypedColour(kelvin=6500))), ("spot", SPOT),
                    ("window", WINDOW), ("sun", SUN)]
                assert (setup.sensors, setup.colour_sensors) == ({"lux": LUX}, {"sky": SKY})

                facts = facts_in(conn)
                assert set(facts.lights) == {SPOT.reporter}          # light.unnamed is not copied
                spot = facts.lights[SPOT.reporter]
                assert (spot.state, spot.colour, spot.doubt, spot.heard_at) == (
                    Kept(Dimmer(on=True, brightness=0.5), 100.0), Kept(KelvinColour(kelvin=2200.0), 110.0), None, 120.0)
                assert set(facts.readings) == {LUX.at}                # sensor.gone is not copied
                lux = facts.readings[LUX.at]
                assert (lux.reading, lux.doubt) == (Kept(Measured(lux=80.0), 200.0),
                                                    Doubt(DoubtReason.LINK_CLOSED, 250.0))
                assert facts.colours[SKY.at].colour == Kept(KelvinColour(kelvin=3456.5), 300.0)
                audit = conn.execute("SELECT created_at, created_by, changed_at, changed_by FROM room_light_facts "
                                     "WHERE kind='reading'").fetchone()
                assert (audit["created_at"], audit["created_by"], audit["changed_at"], audit["changed_by"]) == (
                    190.0, HA, 250.0, "central")

                assert galleria_birth_in(conn) == (BIRTH, 400.0)
                birth = conn.execute("SELECT created_by, changed_at FROM singletons WHERE name='galleria_birth'"
                                     ).fetchone()
                assert (birth["created_by"], birth["changed_at"]) == ("galleria", 401.0)

                assert [(each.source, each.address, each.reason, each.recorded_at)
                        for each in refusals_in(conn, None, 10)] == [
                    ("pi", "p-1", "unreadable", 501.0), (HA, "Not_An_Entity", "unknown_address", 500.0)]

                profile = conn.execute("SELECT created_at, created_by, changed_at, changed_by FROM paper_profiles"
                                       ).fetchone()
                assert (profile["created_at"], profile["created_by"], profile["changed_at"], profile["changed_by"]) == (
                    1000.0, "unknown", 1000.0, "unknown")
                nullable = conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                                        "table_name='paper_profiles' AND column_name LIKE ANY "
                                        "(ARRAY['created_%%', 'changed_%%']) AND is_nullable='YES'").fetchall()
                assert nullable == []

                tables = {row["table_name"] for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
                assert not set(DROPPED) & tables and "room_light_facts" in tables
        finally:
            db.close()
