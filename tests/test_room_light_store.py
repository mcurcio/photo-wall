"""Room lights over PostgreSQL (central/infra/room_light_store.py, central/infra/light_sensor_judge.py,
the entities Home Assistant published, and the WALL marks): every word is kept by its reference and
resolved when the document is built, the Pi sensor's judge never rolls back the host line, the Home
Assistant page reads when Central last heard each entity, and ensuring WALL only wakes the room-lights
writer."""
from __future__ import annotations

import asyncio

import pytest
from psycopg.conninfo import make_conninfo
from test_registry import enroll

from central.content_catalog.catalog import device_id_for_serial
from central.db import Database
from central.infra import room_light_store
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.home_assistant.settings import PgHomeAssistantSettings
from central.infra.light_sensor_judge import HOST_STATE_STREAM, PI_SOURCE, LightSensorJudge
from central.infra.node_link_store import PgLinkStores, PgWallMarks
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    ADDRESS_MAX,
    BulbColour,
    ColourSensor,
    ColourSensorRef,
    FixedLight,
    LightStated,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    ReportRefusal,
    RoomLightRefusal,
    RoomLightRefused,
    SensedColour,
    Sensor,
    SensorRef,
    TypedColour,
)
from contracts.home_assistant import GalleriaBirth, GalleriaConnection
from contracts.node_link import Pipe
from contracts.room_light import (
    LIGHT_SENSOR_KEY,
    Dimmer,
    KelvinColour,
    LightSensorReading,
    Measured,
    MeasuredEntry,
    NotReported,
    ReportedEntry,
    RgbColour,
    Unavailable,
    encode_light_sensor,
)
from nodeapi.buffers import state_prefix
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

X = ReporterRef(source="home-assistant", address="light.x")
Y = ReporterRef(source="home-assistant", address="light.y")
WHITE = TypedColour(kelvin=4000)
KELVIN = KelvinColour(kelvin=4000)
_CONNECTION = GalleriaConnection(server="NHUB", client=1)


def _said(on: bool, brightness: float) -> LightStated:
    return LightStated(state=Dimmer(on=on, brightness=brightness), colour=None)
SERIAL = "10000000c0ffee01"


def _sql(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def test_a_word_heard_before_its_light_exists_shows_at_once(registry):
    """A2. Mutation probe M1: entry_of ignoring the word -> the light reads not reported."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    change = lights.current().change
    assert lights.record_light_state(X, _said(True, 0.5)) == "unchanged"     # no light names it yet
    assert lights.current().change == change
    view = lights.save_room_light("x", ReportedLight(name="X", reporter=X, colour=WHITE))
    assert view.entry == ReportedEntry(colour=KELVIN, state=Dimmer(on=True, brightness=0.5), doubt=None) and view.heard_at is not None
    assert lights.current().lights["x"] == view.entry and lights.current().change == change + 1
    assert lights.record_light_state(X, _said(True, 0.5)) == "unchanged"     # a repeat
    assert lights.record_light_state(X, _said(False, 0.0)) == "recorded"
    assert lights.current().change == change + 2


def test_a_pi_reading_heard_before_its_sensor_exists_shows_at_once(registry):
    """A2, the Pi's half, through the host line's judge as the link store calls it."""
    player = _pi(registry)
    _record_host_line(registry, LightSensorJudge(PgRoomLights(registry.db, PgHouseLock())), encode_light_sensor(
        LightSensorReading(reading=Measured(lux=180.0))))
    lights = PgRoomLights(registry.db, PgHouseLock())
    sensor = lights.save_sensor("pi-meter", Sensor(name="Pi meter", at=SensorRef(source=PI_SOURCE,
                                                                                address=player["player_id"])))
    assert sensor.reading == Measured(lux=180.0) and sensor.heard_at is not None and sensor.reports is None
    window = lights.save_room_light("window", MeasuredLight(name="Window", sensor="pi-meter", colour=WHITE))
    assert window.entry == MeasuredEntry(colour=KELVIN, state=Measured(lux=180.0), doubt=None)
    assert next(each for each in lights.lights().sensors if each.id == "pi-meter").reports == "window"


def test_a_changed_reporter_reads_its_new_references_last_word(registry):
    """A3: no reset; the light takes whatever its new reference last said."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.record_light_state(Y, _said(True, 0.25))
    lights.save_room_light("x", ReportedLight(name="X", reporter=X, colour=WHITE))
    assert lights.current().lights["x"].state == NotReported()
    lights.save_room_light("x", ReportedLight(name="X", reporter=Y, colour=WHITE))
    assert lights.current().lights["x"].state == Dimmer(on=True, brightness=0.25)


def test_refused_reports_are_one_log_cut_and_kept_per_source(registry, monkeypatch):
    monkeypatch.setattr(room_light_store, "REFUSALS_KEPT", 2)
    lights = PgRoomLights(registry.db, PgHouseLock())
    for index in range(3):
        lights.record_refused("home-assistant", f"photowall/x/{index}", ReportRefusal.UNREADABLE)
    lights.record_refused("pi", "p" * (ADDRESS_MAX + 10), ReportRefusal.OUT_OF_RANGE)
    refused = lights.refused().refused
    assert [(each.source, each.address) for each in refused] == [
        ("pi", "p" * ADDRESS_MAX), ("home-assistant", "photowall/x/2"), ("home-assistant", "photowall/x/1")]


def _pi(registry) -> dict:
    """A Pi enrolled with the device id its serial names (the link store's key)."""
    device = device_id_for_serial(SERIAL)
    _sql(registry, "INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)", (device, SERIAL))
    return enroll(registry, device_id=device)[0]


def _record_host_line(registry, judge, value: bytes, seq: int = 1) -> None:
    """One host-line record, committed by the real link store with `judge` as the host line's judge."""
    stores = PgLinkStores(registry.db, {HOST_STATE_STREAM: judge})
    read = Read(Token("epoch", seq), state_prefix("host") + LIGHT_SENSOR_KEY, {}, value)
    asyncio.run(stores.store(SERIAL, Pipe.FLEET).commit(HOST_STATE_STREAM, Batch((read,), Token("epoch", seq), False)))


def test_a_reading_that_fails_never_rolls_back_the_host_lines_record(registry, monkeypatch):
    """A8. Mutation probe M4: no savepoint in record_reading_within -> the batch's commit fails. The
    record stays the source: the next build of the document takes the reading the failed savepoint
    lost (mutation: `current` without `missed_readings_in` -> the Pi's reading is never kept)."""
    player = _pi(registry)

    def failing(conn, *_):
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(room_light_store, "_reading_in", failing)
    _record_host_line(registry, LightSensorJudge(PgRoomLights(registry.db, PgHouseLock())),
                      encode_light_sensor(LightSensorReading(reading=Measured(lux=5.0))))
    assert _sql(registry, "SELECT seq FROM node_link_records WHERE stream=%s", (HOST_STATE_STREAM,)) == [{"seq": 1}]
    monkeypatch.undo()
    assert _sql(registry, "SELECT reading FROM reading_words WHERE source=%s", (PI_SOURCE,)) == []
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.current()
    assert _sql(registry, "SELECT address, reading FROM reading_words WHERE source=%s", (PI_SOURCE,)) == [
        {"address": player["player_id"], "reading": {"kind": "measured", "lux": 5.0}}]
    change = lights.current().change
    assert lights.current().change == change           # taken once: the word is now newer than the record
    _record_host_line(registry, LightSensorJudge(lights), b"not a reading", seq=2)
    [refused] = lights.refused().refused
    assert (refused.source, refused.address, refused.reason) == (PI_SOURCE, player["player_id"], ReportRefusal.UNREADABLE)


def test_a_refusal_that_fails_never_rolls_back_the_host_lines_record(registry, monkeypatch):
    """Mutation: record_refused_within outside the judge's try, or without its savepoint -> the
    batch's commit fails."""
    _pi(registry)

    def failing(conn, *_):
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(room_light_store, "_refusal_in", failing)
    _record_host_line(registry, LightSensorJudge(PgRoomLights(registry.db, PgHouseLock())), b"not a reading")
    assert _sql(registry, "SELECT seq FROM node_link_records WHERE stream=%s", (HOST_STATE_STREAM,)) == [{"seq": 1}]


def test_the_home_assistant_page_reads_when_each_entity_was_heard_and_its_name(registry):
    """"States last heard": the newest heard_at; every entity Home Assistant published, by its name,
    newest heard first; a room light's entity never published reads with no name and no time."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    settings = PgHomeAssistantSettings(registry.db, "0.40.0")
    assert settings.view().heard_at is None
    lights.save_room_light("spot", ReportedLight(name="Spot", reporter=ReporterRef(source="home-assistant",
                                                                                   address="light.unheard"),
                                                 colour=TypedColour(kelvin=4000)))
    lights.record_heard("light.hall", "light", "Hall")
    lights.record_heard("sensor.lux", "illuminance", "Hall lux")
    view = settings.view()
    assert [(each.kind, each.entity, each.name, each.reports) for each in view.entities] == [
        ("illuminance", "sensor.lux", "Hall lux", None), ("light", "light.hall", "Hall", None),
        ("light", "light.unheard", None, "spot")]
    assert view.heard_at == view.entities[0].heard_at and view.entities[2].heard_at is None


def test_ensuring_wall_only_wakes_the_room_lights_writer():
    """Mutation probe M3: wall_documents handing the document -> WallWriter would write the key too."""
    woken: list[int] = []
    marks = PgWallMarks(None, lambda: woken.append(1))   # type: ignore[arg-type]
    assert asyncio.run(marks.wall_documents()) == {} and woken == [1]


def test_words_are_kept_per_source_newest_first(registry, monkeypatch):
    monkeypatch.setattr(room_light_store, "WORDS_KEPT_PER_SOURCE", 2)
    lights = PgRoomLights(registry.db, PgHouseLock())
    for name in ("a", "b", "c"):
        lights.record_light_state(ReporterRef(source="home-assistant", address=f"light.{name}"), _said(True, 1.0))
    lights.record_light_state(ReporterRef(source="other", address="light.a"), _said(True, 1.0))
    kept = _sql(registry, "SELECT source, address FROM light_words ORDER BY source, address")
    assert [(row["source"], row["address"]) for row in kept] == [
        ("home-assistant", "light.b"), ("home-assistant", "light.c"), ("other", "light.a")]



def test_a_named_reference_is_never_trimmed(registry, monkeypatch):
    """A room light's, Sensor's or colour sensor's reference keeps its facts however many others speak."""
    monkeypatch.setattr(room_light_store, "WORDS_KEPT_PER_SOURCE", 1)
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.save_room_light("x", ReportedLight(name="X", reporter=X, colour=WHITE))
    lights.save_sensor("lux", Sensor(name="Lux", at=SensorRef(source="home-assistant", address="sensor.lux")))
    lights.save_colour_sensor("sky", ColourSensor(name="Sky", at=SKY_AT))
    lights.record_light_state(X, _said(True, 0.5))
    lights.record_reading(SensorRef(source="home-assistant", address="sensor.lux"), Measured(lux=70.0))
    lights.record_colour_reading(SKY_AT, KelvinColour(kelvin=4100.0))
    for name in ("a", "b", "c"):
        lights.record_light_state(ReporterRef(source="home-assistant", address=f"light.{name}"), _said(True, 1.0))
        lights.record_reading(SensorRef(source="home-assistant", address=f"sensor.{name}"), Measured(lux=1.0))
        lights.record_colour_reading(ColourSensorRef(source="home-assistant", address=f"sensor.k{name}"),
                                     KelvinColour(kelvin=3000.0))
    assert [row["address"] for row in _sql(registry, "SELECT address FROM light_words ORDER BY address")] == [
        "light.c", "light.x"]
    assert [row["address"] for row in _sql(registry, "SELECT address FROM reading_words ORDER BY address")] == [
        "sensor.c", "sensor.lux"]
    assert [row["address"] for row in _sql(registry, "SELECT address FROM colour_words ORDER BY address")] == [
        "sensor.kc", "sensor.sun_sky_color_temperature"]
    view = lights.lights()
    assert next(each for each in view.lights if each.id == "x").entry.state == Dimmer(on=True, brightness=0.5)
    assert view.sensors[0].reading == Measured(lux=70.0) and view.colour_sensors[0].colour == KelvinColour(kelvin=4100.0)


SKY_AT = ColourSensorRef(source="home-assistant", address="sensor.sun_sky_color_temperature")


def test_kept_brightness_and_colour_survive_cant_tell_and_a_restart(registry):
    """Owner, 2026-10-25 Q1 and Q3: the last stated brightness and colour stand through "unavailable",
    and a Central restart (a new PgRoomLights over the same database) keeps them."""
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.save_colour_sensor("sky", ColourSensor(name="Sky", at=SKY_AT))
    lights.save_room_light("x", ReportedLight(name="X", reporter=X, colour=BulbColour(typed_kelvin=2700)))
    lights.save_room_light("sun", FixedLight(name="Sun", colour=SensedColour(sensor="sky", typed_kelvin=5500)))
    red = RgbColour(rgb=(1.0, 0.0, 0.0))
    assert lights.record_light_state(X, LightStated(state=Dimmer(on=True, brightness=0.4), colour=red)) == "recorded"
    assert lights.record_colour_reading(SKY_AT, KelvinColour(kelvin=3456.5)) == "recorded"
    assert lights.record_light_state(X, Unavailable()) == "recorded"   # its doubt travels
    assert lights.record_colour_reading(SKY_AT, Unavailable()) == "unchanged"
    restarted = PgRoomLights(registry.db, PgHouseLock())
    document = restarted.current()
    entry = document.lights["x"]
    assert isinstance(entry, ReportedEntry) and (entry.colour, entry.state) == (red, Dimmer(on=True, brightness=0.4))
    assert entry.doubt is not None and entry.doubt.line1.startswith("X: its light can't tell · since")
    assert document.lights["sun"].colour == KelvinColour(kelvin=3456.5)
    view = {each.id: each for each in restarted.lights().lights}
    assert view["x"].doubt is not None and view["x"].doubt.reason == "cant-tell"
    assert view["x"].colour.doubt is not None
    assert (view["x"].colour.colour_from, view["sun"].colour.colour_from) == ("following", "following")
    assert restarted.lights().colour_sensors[0].doubt is not None
    # off is stated (adds no light) and keeps the colour; a stated value clears "can't tell"
    assert restarted.record_light_state(X, _said(False, 0.0)) == "recorded"
    after = {each.id: each for each in restarted.lights().lights}["x"]
    assert after.entry == ReportedEntry(colour=red, state=Dimmer(on=False, brightness=0.0), doubt=None)
    assert after.doubt is None


def test_colour_sensors_are_saved_and_kept_while_followed(registry):
    lights = PgRoomLights(registry.db, PgHouseLock())
    saved = lights.save_colour_sensor("sky", ColourSensor(name="Sky", at=SKY_AT))
    assert (saved.colour, saved.heard_at, saved.followed_by) == (None, None, ())
    lights.save_room_light("sun", FixedLight(name="Sun", colour=SensedColour(sensor="sky", typed_kelvin=5500)))
    with pytest.raises(RoomLightRefused) as followed:
        lights.remove_colour_sensor("sky")
    assert followed.value.code is RoomLightRefusal.COLOUR_SENSOR_FOLLOWED
    with pytest.raises(RoomLightRefused) as taken:
        lights.save_colour_sensor("other", ColourSensor(name="Other", at=SKY_AT))
    assert taken.value.code is RoomLightRefusal.REFERENCE_TAKEN
    with pytest.raises(RoomLightRefused) as unknown:
        lights.save_room_light("lamp", FixedLight(name="Lamp", colour=SensedColour(sensor="nope", typed_kelvin=5500)))
    assert unknown.value.code is RoomLightRefusal.UNKNOWN_COLOUR_SENSOR
    lights.save_room_light("sun", FixedLight(name="Sun", colour=TypedColour(kelvin=5500)))
    lights.remove_colour_sensor("sky")
    assert lights.lights().colour_sensors == ()
    with pytest.raises(RoomLightRefused) as gone:
        lights.remove_colour_sensor("sky")
    assert gone.value.code is RoomLightRefusal.UNKNOWN_COLOUR_SENSOR


def test_galleria_s_birth_reaches_the_settings_page(registry):
    lights = PgRoomLights(registry.db, PgHouseLock())
    settings = PgHomeAssistantSettings(registry.db, "0.40.0")
    before = settings.view().galleria
    assert (before.version, before.update, before.central_version) == (None, True, "0.40.0")
    lights.record_galleria_birth(GalleriaBirth(version="0.39.0", kinds=("light", "illuminance"), connection=_CONNECTION))
    old = settings.view().galleria
    assert (old.version, old.update) == ("0.39.0", True) and old.heard_at is not None
    lights.record_galleria_birth(GalleriaBirth(version="0.41.0", kinds=("light", "illuminance", "colour-temperature"),
                                               connection=_CONNECTION))
    new = settings.view().galleria
    assert (new.version, new.kinds, new.update) == ("0.41.0", ("light", "illuminance", "colour-temperature"), False)


@pytest.fixture
def ticking_db(registry):
    """The registry's database seen through a `clock_timestamp()` that moves a whole second at every
    call (a schema ahead of pg_catalog on the search path), so a statement that reads the clock twice
    stamps two instants every time, never by timing luck."""
    with registry.db.transaction() as conn:
        conn.execute("CREATE SCHEMA ticking")
        conn.execute("CREATE SEQUENCE ticking.tick")
        conn.execute("CREATE FUNCTION ticking.clock_timestamp() RETURNS timestamptz VOLATILE LANGUAGE sql AS "
                     "$$ SELECT pg_catalog.clock_timestamp() + nextval('ticking.tick') * interval '1 second' $$")
    db = Database(make_conninfo(registry.db.dsn, options="-c search_path=ticking,pg_catalog,public"))
    try:
        yield db
    finally:
        db.close()


def test_the_new_tables_carry_their_audit_fields(ticking_db):
    """A colour sensor, a colour sensor's facts and Galleria's birth each keep when and by whom they
    were created and last changed, a new record's two times one instant (one clock read per write,
    held under a clock that moves at every read): a second write moves only when it changed and by whom."""
    lights = PgRoomLights(ticking_db, PgHouseLock())

    def audit(table: str) -> dict:
        with ticking_db.transaction() as conn:
            return conn.execute(f"SELECT created_at, created_by, changed_at, changed_by FROM {table}").fetchone()  # noqa: S608

    writes = {
        "colour_sensors": lambda name: lights.save_colour_sensor("sky", ColourSensor(name=name, at=SKY_AT)),
        "colour_words": lambda name: lights.record_colour_reading(SKY_AT, KelvinColour(kelvin=len(name) * 1000.0)),
        "galleria_birth": lambda name: lights.record_galleria_birth(GalleriaBirth(version=name, kinds=("light",),
                                                                                     connection=_CONNECTION)),
    }
    by = {"colour_sensors": "operator", "colour_words": "home-assistant", "galleria_birth": "galleria"}
    for table, write in writes.items():
        write("Sky")
        first = audit(table)
        assert first["created_at"] == first["changed_at"] and first["created_by"] == first["changed_by"] == by[table]
        write("Daylight")
        second = audit(table)
        assert second["created_at"] == first["created_at"] and second["changed_at"] > first["changed_at"], table
        assert (second["created_by"], second["changed_by"]) == (by[table], by[table])
