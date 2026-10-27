"""Doubt over PostgreSQL (central/infra/room_light_store.py) and Central's errors read from what Central
keeps (central/infra/central_errors_store.py, its routes): an unreadable report doubts its reference
"can't read" and the doubt travels in the room-lights document; a closed link doubts every
reference of its source; a stated report clears either; Needs attention's rows; what a Frame's
Player said on its line."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from test_registry import ADMIN, enroll, frame, hello

from central.app import create_app
from central.central_errors.views import (
    CentralErrorsView,
    FrameImpeded,
    FrameImpedimentView,
    FrameNotReported,
)
from central.central_errors_routes import CENTRAL_ERRORS, FRAME_CENTRAL_ERROR
from central.content_catalog.catalog import device_id_for_serial
from central.infra.central_errors_store import IMPEDED_SUBJECT, PLAYER_STATE_STREAM, PgCentralErrors
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.home_assistant.advice import HomeAssistantAdvice
from central.infra.light_sensor_judge import (
    HOST_STATE_STREAM,
    PI_SOURCE,
    LightSensorJudge,
    PiMeterAdvice,
)
from central.infra.node_link_store import PgLinkStores
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    ColourSensor,
    ColourSensorRef,
    FixedLight,
    LightStated,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    ReportRefusal,
    SensedColour,
    Sensor,
    SensorRef,
    TypedColour,
)
from contracts.node_impeded import Impeded, ImpededFrames, encode_impeded
from contracts.node_link import Pipe
from contracts.room_light import (
    LIGHT_SENSOR_KEY,
    Dimmer,
    KelvinColour,
    Measured,
    NotReported,
    ReportedEntry,
)
from nodeapi.buffers import state_prefix
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

HA = "home-assistant"
FAMILY = ReporterRef(source=HA, address="light.family_room")
LAMP = ReporterRef(source=HA, address="light.lamp")
SKY = ColourSensorRef(source=HA, address="sensor.sky")
WHITE = TypedColour(kelvin=4000)
OFF = LightStated(state=Dimmer(on=False, brightness=0.0), colour=None)
SERIAL = "10000000c0ffee02"
GRANT = "11111111-2222-4333-8444-555555555555"


def _house(registry) -> PgRoomLights:
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.save_room_light("family", ReportedLight(name="Family Room Lights", reporter=FAMILY, colour=WHITE))
    lights.save_room_light("lamp", ReportedLight(name="Lamp", reporter=LAMP, colour=WHITE))
    return lights


def test_an_unreadable_report_doubts_its_reference_and_a_stated_one_clears_it(registry):
    """Mutation probe: record_refused keeping only the log line -> the document never changes."""
    lights = _house(registry)
    lights.record_light_state(FAMILY, OFF)
    change = lights.current().change
    lights.record_refused(HA, FAMILY.address, ReportRefusal.UNREADABLE)
    document = lights.current()
    family = document.lights["family"]
    assert document.change == change + 1
    assert isinstance(family, ReportedEntry) and family.state == Dimmer(on=False, brightness=0.0)   # the last word stands
    assert family.doubt is not None and family.doubt.line1.startswith("Photo Wall can't read Family Room Lights · since")
    assert family.doubt.line2.startswith("Its last word: off (")
    view = {each.id: each for each in lights.lights().lights}
    assert view["family"].doubt is not None and view["family"].doubt.reason == "cant-read"
    assert view["lamp"].doubt is None
    lights.record_refused(HA, FAMILY.address, ReportRefusal.UNREADABLE)   # a repeat keeps "since"
    lights.record_refused(HA, FAMILY.address, ReportRefusal.OUT_OF_RANGE)  # log only
    assert lights.current().change == change + 1
    assert lights.record_light_state(FAMILY, OFF) == "recorded"          # restated: cleared
    assert lights.current().lights["family"].doubt is None


def test_an_out_of_range_or_unknown_address_refusal_is_log_only(registry):
    """Mutation probe: every refusal doubting its reference (not only an unreadable one) -> a light
    whose colour is out of range, its brightness recorded, reads "can't read"."""
    lights = _house(registry)
    lights.record_light_state(FAMILY, OFF)
    before = lights.current()
    for reason in (ReportRefusal.OUT_OF_RANGE, ReportRefusal.UNKNOWN_ADDRESS):
        lights.record_refused(HA, FAMILY.address, reason)
    assert lights.current() == before
    assert {each.id: each for each in lights.lights().lights}["family"].doubt is None
    assert [each.reason for each in lights.refused().refused][:2] == ["unknown_address", "out_of_range"]


def test_each_word_row_says_who_created_and_last_changed_it_and_when(registry):
    """Audit fields: a reference's source for what it stated, Central for a doubt it set itself."""
    lights = _house(registry)
    lights.record_light_state(FAMILY, OFF)
    lights.record_reading(SensorRef(source=HA, address="sensor.window"), Measured(lux=3.0))

    def audit(table: str, address: str) -> tuple:
        with registry.db.transaction() as conn:
            row = conn.execute(f"SELECT created_by, changed_by, created_at, changed_at FROM {table} "  # noqa: S608
                               "WHERE address=%s", (address,)).fetchone()
        return row["created_by"], row["changed_by"], row["created_at"], row["changed_at"]

    created_by, changed_by, created_at, changed_at = audit("light_words", FAMILY.address)
    assert (created_by, changed_by, created_at) == (HA, HA, changed_at)
    lights.record_refused(HA, FAMILY.address, ReportRefusal.UNREADABLE)
    lights.record_link_closed(HA)
    assert audit("light_words", FAMILY.address)[:3] == (HA, "central", created_at)
    assert audit("light_words", FAMILY.address)[3] > changed_at
    assert audit("reading_words", "sensor.window")[:2] == (HA, "central")
    lights.record_light_state(FAMILY, OFF)                # restated: its source changed it last
    assert audit("light_words", FAMILY.address)[:2] == (HA, HA)


def test_a_settings_house_save_that_moves_the_words_raises_the_document_s_change(registry):
    """The words are dated on the house's clock, so a save of its zone or clock format is an event
    for the document too. Mutation probe: PgHouse not calling house_clock_changing_within -> the
    same change number carries other words, and the WALL copy keeps the old ones."""
    lights = _house(registry)
    lights.record_light_state(FAMILY, OFF)
    lights.record_refused(HA, FAMILY.address, ReportRefusal.UNREADABLE)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        headers = {"Authorization": "Bearer " + ADMIN}

        def save(zone: str, clock: str) -> None:
            response = client.put("/v1/operator/house", headers=headers, json={
                "time": {"zone": zone, "place": None}, "units": "cm", "clock": clock})
            assert response.status_code == 200, response.text

        save("Europe/London", "24h")
        before = lights.current()
        save("Europe/London", "24h")                         # nothing moved: no change
        assert lights.current() == before
        save("Asia/Tokyo", "12h")
        after = lights.current()
    assert after.change == before.change + 1
    assert after.lights["family"].doubt != before.lights["family"].doubt
    assert after.lights["family"].doubt.line1.endswith(("am", "pm"))


def test_an_unreadable_report_of_a_named_reference_never_heard_and_of_one_nothing_names(registry):
    lights = _house(registry)
    lights.record_refused(HA, LAMP.address, ReportRefusal.UNREADABLE)   # named, never stated
    lamp = lights.current().lights["lamp"]
    assert lamp.state == NotReported() and lamp.doubt is not None
    assert lamp.doubt.line2.startswith("Its last word: none yet")
    lights.record_refused(HA, "light.garbage", ReportRefusal.UNREADABLE)  # nothing names it, no facts: no row
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT 1 FROM light_words WHERE address='light.garbage'").fetchone() is None


def test_a_closed_link_doubts_every_reference_of_its_source_with_facts(registry):
    """Mutation probe: record_link_closed marking another source -> the Pi's reading is doubted."""
    lights = _house(registry)
    lights.save_sensor("meter", Sensor(name="Meter", at=SensorRef(source=PI_SOURCE, address="p-hall")))
    lights.save_room_light("window", MeasuredLight(name="Window", sensor="meter", colour=WHITE))
    lights.record_light_state(FAMILY, OFF)
    lights.record_reading(SensorRef(source=PI_SOURCE, address="p-hall"), Measured(lux=12.0))
    assert lights.record_link_closed(HA) == "recorded"
    assert lights.record_link_closed(HA) == "unchanged"
    document = lights.current()
    assert document.lights["family"].doubt is not None
    assert document.lights["family"].doubt.line1.startswith("Photo Wall lost the link to Family Room Lights")
    assert document.lights["lamp"].doubt is None        # never heard: no facts to doubt
    assert document.lights["window"].doubt is None      # another source
    lights.record_refused(HA, FAMILY.address, ReportRefusal.UNREADABLE)   # the newest reason wins
    assert {each.id: each for each in lights.lights().lights}["family"].doubt.reason == "cant-read"
    lights.record_light_state(FAMILY, OFF)              # restated after reconnecting
    assert lights.current().lights["family"].doubt is None


def _pi(registry, serial: str = SERIAL) -> dict:
    device = device_id_for_serial(serial)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device, serial))
    return enroll(registry, device_id=device)[0]


def _record(registry, stream: str, subject: str, value: bytes, seq: int, judges=None, serial: str = SERIAL) -> None:
    stores = PgLinkStores(registry.db, judges or {})
    read = Read(Token("epoch", seq), subject, {}, value)
    asyncio.run(stores.store(serial, Pipe.FLEET).commit(stream, Batch((read,), Token("epoch", seq), False)))


def test_a_pi_s_unreadable_reading_doubts_its_sensor_without_rolling_back_the_host_line(registry):
    player = _pi(registry)
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.save_sensor("meter", Sensor(name="Meter", at=SensorRef(source=PI_SOURCE, address=player["player_id"])))
    lights.save_room_light("window", MeasuredLight(name="Window", sensor="meter", colour=WHITE))
    _record(registry, HOST_STATE_STREAM, state_prefix("host") + LIGHT_SENSOR_KEY, b"not a reading", 1,
            {HOST_STATE_STREAM: LightSensorJudge(lights)})
    window = lights.current().lights["window"]
    assert window.doubt is not None and window.doubt.line1.startswith("Photo Wall can't read Window")
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_link_records WHERE stream=%s",
                            (HOST_STATE_STREAM,)).fetchone()["n"] == 1


def test_needs_attention_and_the_frame_page_read_what_central_keeps(registry):
    """Needs attention: every named reference in doubt with the Frames it reaches and what to do;
    the Frame page: what its bound Player last said on its line, through the routes."""
    lights = _house(registry)
    lights.save_colour_sensor("sky", ColourSensor(name="Sky", at=SKY))
    lights.save_room_light("room", FixedLight(name="Room", colour=SensedColour(sensor="sky", typed_kelvin=5500)))
    frame(registry, "hall")                                      # a new Frame names the house's first fixed light
    lights.record_colour_reading(SKY, KelvinColour(kelvin=5000.0))
    lights.record_refused(HA, FAMILY.address, ReportRefusal.UNREADABLE)
    lights.record_link_closed(HA)                                # the sky's colour too (its facts exist)
    errors = PgCentralErrors(registry.db, PgHouseLock(), {
        HA: HomeAssistantAdvice(lights.galleria_birth, "0.45.0"), PI_SOURCE: PiMeterAdvice()}, registry)
    rows = {row.address: row for row in errors.in_doubt().in_doubt}
    assert set(rows) == {FAMILY.address, SKY.address}
    assert (rows[FAMILY.address].doubt.reason, rows[FAMILY.address].lights, rows[FAMILY.address].frames) == (
        "link-closed", ("family",), ())
    assert (rows[SKY.address].lights, rows[SKY.address].frames) == (("room",), ("hall",))
    assert rows[SKY.address].what_to_do == "Check Home Assistant and its Galleria integration"

    player = _pi(registry)
    hello(registry, player)
    registry.bind("hall", player["player_id"], "HDMI-A-1", expected_generation=0)
    assert errors.impeded("hall").impediment == FrameNotReported()
    entry = Impeded(frame_id="hall", output_id="HDMI-A-1", grant_id=GRANT, lights=("room",),
                    words=lights.current().lights["family"].doubt)
    _record(registry, PLAYER_STATE_STREAM, IMPEDED_SUBJECT, encode_impeded(ImpededFrames(frames=(entry,))), 1)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False, central_version="0.45.0")) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        attention = CentralErrorsView.model_validate(client.get(CENTRAL_ERRORS, headers=headers).json())
        assert {row.address for row in attention.in_doubt} == {FAMILY.address, SKY.address}
        page = FrameImpedimentView.model_validate(
            client.get(FRAME_CENTRAL_ERROR.format(frame_id="hall"), headers=headers).json())
        assert page.impediment == FrameImpeded(by=entry)
        assert client.get(CENTRAL_ERRORS).status_code in (401, 403)
    _record(registry, PLAYER_STATE_STREAM, IMPEDED_SUBJECT, encode_impeded(ImpededFrames(frames=())), 2)
    assert errors.impeded("hall").impediment.kind == "clear"
    # Another Pi's report (its device sorts first) is never the bound Pi's.
    other = "10000000c0ffee12"
    assert device_id_for_serial(other) < device_id_for_serial(SERIAL)
    _pi(registry, other)
    _record(registry, PLAYER_STATE_STREAM, IMPEDED_SUBJECT, encode_impeded(ImpededFrames(frames=(entry,))), 3,
            serial=other)
    assert errors.impeded("hall").impediment.kind == "clear"
