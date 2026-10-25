"""Room lights' rules (central/room_light/policy.py): what each room light reads as from its
references' kept facts, which House › Lights changes are refused and with which status, and how a
Player's drawn report reads. Pure."""
from __future__ import annotations

import pytest

from central.room_light.model import (
    BulbColour,
    Facts,
    FixedLight,
    Kept,
    LightFacts,
    MeasuredLight,
    ReadingFacts,
    ReportedLight,
    ReporterRef,
    RoomLightRefusal,
    Sensor,
    SensorRef,
    TypedColour,
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
    Dimmer,
    FixedEntry,
    KelvinColour,
    Measured,
    MeasuredEntry,
    NotReported,
    ReportedEntry,
    RgbColour,
    RoomLightsDrawn,
)

WHITE = TypedColour(kelvin=4000)
SHARES = BulbColour(typed_kelvin=2700)
SPOT_REF = ReporterRef(source="home-assistant", address="light.spot")
METER_REF = SensorRef(source="pi", address="p1")
SPOT = ReportedLight(name="Spot", reporter=SPOT_REF, colour=WHITE)
BULB = ReportedLight(name="Bulb", reporter=ReporterRef(source="home-assistant", address="light.bulb"), colour=SHARES)
ROOM = FixedLight(name="Room", colour=TypedColour(kelvin=6500))
WINDOW = MeasuredLight(name="Window", sensor="meter", colour=WHITE)
METER = Sensor(name="Meter", at=METER_REF)
LIGHTS = {"spot": SPOT, "bulb": BULB, "room": ROOM, "window": WINDOW}
SENSORS = {"meter": METER}
NONE = Facts(lights={}, readings={}, colours={})
KELVIN = KelvinColour(kelvin=4000)


def _light(state: Dimmer | None, colour: KelvinColour | RgbColour | None = None) -> LightFacts:
    return LightFacts(state=None if state is None else Kept(state, 1.0), colour=None if colour is None else Kept(colour, 1.0),
                      cant_tell_since=None, heard_at=1.0)


def test_each_light_reads_its_references_kept_facts():
    """Mutation probe M1: entry_of ignoring the facts -> every reported light reads not reported."""
    facts = Facts(lights={SPOT_REF: _light(Dimmer(on=True, brightness=0.5), RgbColour(rgb=(1.0, 0.5, 0.0))),
                          BULB.reporter: _light(Dimmer(on=True, brightness=0.25), RgbColour(rgb=(1.0, 0.5, 0.0)))},
                  readings={METER_REF: ReadingFacts(reading=Kept(Measured(lux=240.0), 1.0), cant_tell_since=None,
                                                    heard_at=1.0)},
                  colours={})
    assert entry_of(ROOM, facts, SENSORS, {}) == FixedEntry(colour=KelvinColour(kelvin=6500))
    assert entry_of(SPOT, facts, SENSORS, {}) == ReportedEntry(colour=KELVIN, state=Dimmer(on=True, brightness=0.5))
    assert entry_of(BULB, facts, SENSORS, {}) == ReportedEntry(
        colour=RgbColour(rgb=(1.0, 0.5, 0.0)), state=Dimmer(on=True, brightness=0.25))
    assert entry_of(WINDOW, facts, SENSORS, {}) == MeasuredEntry(colour=KELVIN, state=Measured(lux=240.0))


def test_no_word_or_no_sensor_is_not_reported():
    for light in (SPOT, BULB, WINDOW):
        assert entry_of(light, NONE, SENSORS, {}).state == NotReported()
    kept = Facts(lights={}, readings={METER_REF: ReadingFacts(reading=Kept(Measured(lux=9.0), 1.0), cant_tell_since=None,
                                                             heard_at=1.0)}, colours={})
    assert entry_of(WINDOW, kept, {}, {}).state == NotReported()
    assert entry_of(BULB, NONE, SENSORS, {}).colour == KelvinColour(kelvin=2700)     # typed until the bulb says


def test_the_document_carries_every_light_by_id():
    document = room_lights_document(LIGHTS, SENSORS, {}, NONE, 7)
    assert document.change == 7 and sorted(document.lights) == sorted(LIGHTS)


@pytest.mark.parametrize("light_id, light, named_by, code", [
    ("window", SPOT, ("hall",), RoomLightRefusal.NAMED_BY_A_FRAME),           # measured -> reported while named
    ("room", WINDOW.model_copy(update={"sensor": "meter"}), ("hall",), RoomLightRefusal.NAMED_BY_A_FRAME),
    ("lamp", MeasuredLight(name="Lamp", sensor="nope", colour=WHITE), (), RoomLightRefusal.UNKNOWN_SENSOR),
    ("lamp", MeasuredLight(name="Lamp", sensor="meter", colour=WHITE), (), RoomLightRefusal.SENSOR_REPORTS_A_LIGHT),
    ("lamp", SPOT.model_copy(update={"name": "Lamp"}), (), RoomLightRefusal.REFERENCE_TAKEN),
    ("lamp", ReportedLight(name="Lamp", reporter=ReporterRef(source="pi", address="p1"), colour=WHITE), (),
     RoomLightRefusal.REFERENCE_TAKEN),                                          # a Sensor's reference
])
def test_a_room_light_refused(light_id, light, named_by, code):
    refused = check_room_light(light_id, light, LIGHTS, SENSORS, {}, named_by)
    assert refused is not None and refused.code is code


def test_a_room_light_saved():
    assert check_room_light("spot", SPOT.model_copy(update={"name": "Spotlight"}), LIGHTS, SENSORS, {}, ("hall",)) is None
    assert check_room_light("room", SPOT.model_copy(update={"reporter": ReporterRef(
        source="home-assistant", address="light.room")}), LIGHTS, SENSORS, {}, ("hall",)) is None    # fixed -> reported
    full = {f"l{index}": ROOM for index in range(ROOM_LIGHTS_MAX)}
    refused = check_room_light("one-more", ROOM, full, {}, {}, ())
    assert refused is not None and refused.code is RoomLightRefusal.TOO_MANY
    assert check_room_light("l0", ROOM, full, {}, {}, ()) is None


def test_removals_and_sensors():
    unknown = check_room_light_removal("nope", LIGHTS, ())
    assert unknown is not None and unknown.code is RoomLightRefusal.UNKNOWN_ROOM_LIGHT
    named = check_room_light_removal("spot", LIGHTS, ("hall",))
    assert named is not None and named.code is RoomLightRefusal.NAMED_BY_A_FRAME
    assert check_room_light_removal("spot", LIGHTS, ()) is None
    second = check_sensor("other", Sensor(name="Other", at=METER_REF), SENSORS, LIGHTS, {})    # a second Sensor on one Pi
    assert second is not None and second.code is RoomLightRefusal.REFERENCE_TAKEN
    taken = check_sensor("other", Sensor(name="Other", at=SensorRef(source="home-assistant", address="light.spot")),
                         SENSORS, LIGHTS, {})
    assert taken is not None and taken.code is RoomLightRefusal.REFERENCE_TAKEN
    assert check_sensor("meter", METER.model_copy(update={"name": "Hall"}), SENSORS, LIGHTS, {}) is None
    measured = check_sensor_removal("meter", SENSORS, LIGHTS)
    assert measured is not None and measured.code is RoomLightRefusal.SENSOR_REPORTS_A_LIGHT
    gone = check_sensor_removal("nope", SENSORS, LIGHTS)
    assert gone is not None and gone.code is RoomLightRefusal.UNKNOWN_SENSOR
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
