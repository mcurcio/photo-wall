"""Room lights' rules (central/room_light/policy.py): what each room light reads as from its
reference's last word, which House › Lights changes are refused and with which status, and how a
Player's drawn report reads. Pure."""
from __future__ import annotations

import pytest

from central.room_light.model import (
    FixedLight,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    RoomLightRefusal,
    Sensor,
    SensorRef,
    Words,
)
from central.room_light.policy import (
    check_room_light,
    check_room_light_removal,
    check_sensor,
    check_sensor_removal,
    drawn_state,
    entry_of,
    room_lights_document,
)
from contracts.room_light import (
    ROOM_LIGHTS_DRAWN_LEASE_SECONDS,
    ROOM_LIGHTS_MAX,
    ColourDimmer,
    ColourEntry,
    Dimmer,
    FixedColour,
    FixedEntry,
    Measured,
    MeasuredEntry,
    NotReported,
    ReportedColour,
    RoomLightsDrawn,
    Unavailable,
    WhiteEntry,
)

WHITE = FixedColour(kelvin=4000)
SHARES = ReportedColour(channel_shares=(0.3, 0.4, 0.3))
SPOT_REF = ReporterRef(source="home-assistant", address="light.spot")
METER_REF = SensorRef(source="pi", address="p1")
SPOT = ReportedLight(name="Spot", reporter=SPOT_REF, colour=WHITE)
BULB = ReportedLight(name="Bulb", reporter=ReporterRef(source="home-assistant", address="light.bulb"), colour=SHARES)
ROOM = FixedLight(name="Room", colour=FixedColour(kelvin=6500))
WINDOW = MeasuredLight(name="Window", sensor="meter", colour=WHITE)
METER = Sensor(name="Meter", at=METER_REF)
LIGHTS = {"spot": SPOT, "bulb": BULB, "room": ROOM, "window": WINDOW}
SENSORS = {"meter": METER}
NONE = Words(lights={}, readings={})


def test_each_light_reads_its_references_last_word():
    """Mutation probe M1: entry_of ignoring the word -> every reported light reads not reported."""
    words = Words(lights={SPOT_REF: ColourDimmer(on=True, brightness=0.5, rgb=(1.0, 0.5, 0.0)),
                          BULB.reporter: Dimmer(on=True, brightness=0.25)},
                  readings={METER_REF: Measured(lux=240.0)})
    assert entry_of(ROOM, words, SENSORS) == FixedEntry(colour=ROOM.colour)
    assert entry_of(SPOT, words, SENSORS) == WhiteEntry(colour=WHITE, state=Dimmer(on=True, brightness=0.5))
    assert entry_of(BULB, words, SENSORS) == ColourEntry(
        colour=SHARES, state=ColourDimmer(on=True, brightness=0.25, rgb=(1.0, 1.0, 1.0)))
    assert entry_of(WINDOW, words, SENSORS) == MeasuredEntry(colour=WHITE, state=Measured(lux=240.0))
    unavailable = Words(lights={SPOT_REF: Unavailable()}, readings={METER_REF: Unavailable()})
    assert entry_of(SPOT, unavailable, SENSORS).state == Unavailable()
    assert entry_of(WINDOW, unavailable, SENSORS).state == Unavailable()


def test_no_word_or_no_sensor_is_not_reported():
    for light in (SPOT, BULB, WINDOW):
        assert entry_of(light, NONE, SENSORS).state == NotReported()
    assert entry_of(WINDOW, Words(lights={}, readings={METER_REF: Measured(lux=9.0)}), {}).state == NotReported()


def test_the_document_carries_every_light_by_id():
    document = room_lights_document(LIGHTS, SENSORS, NONE, 7)
    assert document.change == 7 and sorted(document.lights) == sorted(LIGHTS)


@pytest.mark.parametrize("light_id, light, named_by, code, status", [
    ("window", SPOT, ("hall",), RoomLightRefusal.NAMED_BY_A_FRAME, 409),           # measured -> reported while named
    ("room", WINDOW.model_copy(update={"sensor": "meter"}), ("hall",), RoomLightRefusal.NAMED_BY_A_FRAME, 409),
    ("lamp", MeasuredLight(name="Lamp", sensor="nope", colour=WHITE), (), RoomLightRefusal.UNKNOWN_SENSOR, 422),
    ("lamp", MeasuredLight(name="Lamp", sensor="meter", colour=WHITE), (), RoomLightRefusal.SENSOR_REPORTS_A_LIGHT, 409),
    ("lamp", SPOT.model_copy(update={"name": "Lamp"}), (), RoomLightRefusal.REFERENCE_TAKEN, 409),
    ("lamp", ReportedLight(name="Lamp", reporter=ReporterRef(source="pi", address="p1"), colour=WHITE), (),
     RoomLightRefusal.REFERENCE_TAKEN, 409),                                          # a Sensor's reference
])
def test_a_room_light_refused(light_id, light, named_by, code, status):
    refused = check_room_light(light_id, light, LIGHTS, SENSORS, named_by)
    assert refused is not None and (refused.code, refused.status) == (code, status)


def test_a_room_light_saved():
    assert check_room_light("spot", SPOT.model_copy(update={"name": "Spotlight"}), LIGHTS, SENSORS, ("hall",)) is None
    assert check_room_light("room", SPOT.model_copy(update={"reporter": ReporterRef(
        source="home-assistant", address="light.room")}), LIGHTS, SENSORS, ("hall",)) is None    # fixed -> reported
    full = {f"l{index}": ROOM for index in range(ROOM_LIGHTS_MAX)}
    refused = check_room_light("one-more", ROOM, full, {}, ())
    assert refused is not None and refused.code is RoomLightRefusal.TOO_MANY
    assert check_room_light("l0", ROOM, full, {}, ()) is None


def test_removals_and_sensors():
    unknown = check_room_light_removal("nope", LIGHTS, ())
    assert unknown is not None and (unknown.code, unknown.status) == (RoomLightRefusal.UNKNOWN_ROOM_LIGHT, 404)
    named = check_room_light_removal("spot", LIGHTS, ("hall",))
    assert named is not None and (named.code, named.status) == (RoomLightRefusal.NAMED_BY_A_FRAME, 409)
    assert check_room_light_removal("spot", LIGHTS, ()) is None
    second = check_sensor("other", Sensor(name="Other", at=METER_REF), SENSORS, LIGHTS)    # a second Sensor on one Pi
    assert second is not None and (second.code, second.status) == (RoomLightRefusal.REFERENCE_TAKEN, 409)
    taken = check_sensor("other", Sensor(name="Other", at=SensorRef(source="home-assistant", address="light.spot")),
                         SENSORS, LIGHTS)
    assert taken is not None and taken.code is RoomLightRefusal.REFERENCE_TAKEN
    assert check_sensor("meter", METER.model_copy(update={"name": "Hall"}), SENSORS, LIGHTS) is None
    measured = check_sensor_removal("meter", SENSORS, LIGHTS)
    assert measured is not None and (measured.code, measured.status) == (RoomLightRefusal.SENSOR_REPORTS_A_LIGHT, 409)
    gone = check_sensor_removal("nope", SENSORS, LIGHTS)
    assert gone is not None and (gone.code, gone.status) == (RoomLightRefusal.UNKNOWN_SENSOR, 404)
    assert check_sensor_removal("meter", SENSORS, {"spot": SPOT}) is None


def test_a_players_drawn_report_against_the_document_now():
    digest = "a" * 64
    drew = RoomLightsDrawn(drawn=digest, following=True)
    assert drawn_state(None, None, digest) == "not-reported"
    assert drawn_state(drew, ROOM_LIGHTS_DRAWN_LEASE_SECONDS + 1, digest) == "not-reported"
    assert drawn_state(RoomLightsDrawn(drawn=digest, following=False), 1.0, digest) == "not-following"
    assert drawn_state(drew, 1.0, digest) == "drawn"
    assert drawn_state(drew, 1.0, "b" * 64) == "behind"
    assert drawn_state(RoomLightsDrawn(drawn=None, following=True), 1.0, digest) == "behind"
