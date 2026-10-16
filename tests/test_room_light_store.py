"""Room lights over PostgreSQL (central/infra/room_light_store.py, central/infra/light_sensor_judge.py,
the Home Assistant link's heartbeat, and the WALL marks): every word is kept by its reference and
resolved when the document is built, the Pi sensor's judge never rolls back the host line, the
link's health is judged by its beat's age on the database's clock, and ensuring WALL only wakes the
room-lights writer."""
from __future__ import annotations

import asyncio

from test_registry import enroll

from central.content_catalog.catalog import device_id_for_serial
from central.infra import room_light_store
from central.infra.home_assistant.health import LEASE_SECONDS, PgLinkHealth
from central.infra.light_sensor_judge import HOST_STATE_STREAM, PI_SOURCE, LightSensorJudge
from central.infra.node_link_store import PgLinkStores, PgWallMarks
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    ADDRESS_MAX,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    ReportRefusal,
    Sensor,
    SensorRef,
)
from contracts.node_link import Pipe
from contracts.room_light import (
    LIGHT_SENSOR_KEY,
    Dimmer,
    FixedColour,
    LightSensorReading,
    Measured,
    MeasuredEntry,
    NotReported,
    WhiteEntry,
    encode_light_sensor,
)
from nodeapi.buffers import state_prefix
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

X = ReporterRef(source="home-assistant", address="light.x")
Y = ReporterRef(source="home-assistant", address="light.y")
WHITE = FixedColour(kelvin=4000)
SERIAL = "10000000c0ffee01"


def _sql(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def test_a_word_heard_before_its_light_exists_shows_at_once(registry):
    """A2. Mutation probe M1: entry_of ignoring the word -> the light reads not reported."""
    lights = PgRoomLights(registry.db)
    change = lights.current().change
    assert lights.record_light_state(X, Dimmer(on=True, brightness=0.5)) == "unchanged"     # no light names it yet
    assert lights.current().change == change
    view = lights.save_room_light("x", ReportedLight(name="X", reporter=X, colour=WHITE))
    assert view.entry == WhiteEntry(colour=WHITE, state=Dimmer(on=True, brightness=0.5)) and view.heard_at is not None
    assert lights.current().lights["x"] == view.entry and lights.current().change == change + 1
    assert lights.record_light_state(X, Dimmer(on=True, brightness=0.5)) == "unchanged"     # a repeat
    assert lights.record_light_state(X, Dimmer(on=False, brightness=0.0)) == "recorded"
    assert lights.current().change == change + 2


def test_a_pi_reading_heard_before_its_sensor_exists_shows_at_once(registry):
    """A2, the Pi's half, through the host line's judge as the link store calls it."""
    player = _pi(registry)
    _record_host_line(registry, LightSensorJudge(PgRoomLights(registry.db)), encode_light_sensor(
        LightSensorReading(reading=Measured(lux=180.0))))
    lights = PgRoomLights(registry.db)
    sensor = lights.save_sensor("pi-meter", Sensor(name="Pi meter", at=SensorRef(source=PI_SOURCE,
                                                                                address=player["player_id"])))
    assert sensor.reading == Measured(lux=180.0) and sensor.heard_at is not None and sensor.reports is None
    window = lights.save_room_light("window", MeasuredLight(name="Window", sensor="pi-meter", colour=WHITE))
    assert window.entry == MeasuredEntry(colour=WHITE, state=Measured(lux=180.0))
    assert next(each for each in lights.lights().sensors if each.id == "pi-meter").reports == "window"


def test_a_changed_reporter_reads_its_new_references_last_word(registry):
    """A3: no reset; the light takes whatever its new reference last said."""
    lights = PgRoomLights(registry.db)
    lights.record_light_state(Y, Dimmer(on=True, brightness=0.25))
    lights.save_room_light("x", ReportedLight(name="X", reporter=X, colour=WHITE))
    assert lights.current().lights["x"].state == NotReported()
    lights.save_room_light("x", ReportedLight(name="X", reporter=Y, colour=WHITE))
    assert lights.current().lights["x"].state == Dimmer(on=True, brightness=0.25)


def test_refused_reports_are_one_log_cut_and_kept_per_source(registry, monkeypatch):
    monkeypatch.setattr(room_light_store, "REFUSALS_KEPT", 2)
    lights = PgRoomLights(registry.db)
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

    monkeypatch.setattr(room_light_store, "_word_in", failing)
    _record_host_line(registry, LightSensorJudge(PgRoomLights(registry.db)),
                      encode_light_sensor(LightSensorReading(reading=Measured(lux=5.0))))
    assert _sql(registry, "SELECT seq FROM node_link_records WHERE stream=%s", (HOST_STATE_STREAM,)) == [{"seq": 1}]
    monkeypatch.undo()
    assert _sql(registry, "SELECT word FROM reading_words WHERE source=%s", (PI_SOURCE,)) == []
    lights = PgRoomLights(registry.db)
    lights.current()
    assert _sql(registry, "SELECT address, word FROM reading_words WHERE source=%s", (PI_SOURCE,)) == [
        {"address": player["player_id"], "word": {"kind": "measured", "lux": 5.0}}]
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
    _record_host_line(registry, LightSensorJudge(PgRoomLights(registry.db)), b"not a reading")
    assert _sql(registry, "SELECT seq FROM node_link_records WHERE stream=%s", (HOST_STATE_STREAM,)) == [{"seq": 1}]


def test_the_links_health_is_judged_by_its_beats_age_on_the_databases_clock(registry):
    """A7. Mutation probe M5: judged ignoring the age -> the old beat reads connected."""
    health = PgLinkHealth(registry.db)
    assert health.health().state == "never-run"
    health.beat("connecting")
    health.beat("connected")
    connected = health.health()
    assert connected.state == "connected" and connected.since is not None
    health.beat("connected")
    assert health.health().since == connected.since                 # since moves only with the state
    _sql(registry, "UPDATE home_assistant_link SET beat_at = clock_timestamp() - make_interval(secs => %s)",
         (LEASE_SECONDS + 1,))
    assert health.health().state == "away"


def test_ensuring_wall_only_wakes_the_room_lights_writer():
    """Mutation probe M3: wall_documents handing the document -> WallWriter would write the key too."""
    woken: list[int] = []
    marks = PgWallMarks(None, lambda: woken.append(1))   # type: ignore[arg-type]
    assert asyncio.run(marks.wall_documents()) == {} and woken == [1]


def test_words_are_kept_per_source_newest_first(registry, monkeypatch):
    monkeypatch.setattr(room_light_store, "WORDS_KEPT_PER_SOURCE", 2)
    lights = PgRoomLights(registry.db)
    for name in ("a", "b", "c"):
        lights.record_light_state(ReporterRef(source="home-assistant", address=f"light.{name}"), Dimmer(on=True, brightness=1.0))
    lights.record_light_state(ReporterRef(source="other", address="light.a"), Dimmer(on=True, brightness=1.0))
    kept = _sql(registry, "SELECT source, address FROM light_words ORDER BY source, address")
    assert [(row["source"], row["address"]) for row in kept] == [
        ("home-assistant", "light.b"), ("home-assistant", "light.c"), ("other", "light.a")]
