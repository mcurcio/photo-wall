"""Room lights' rules (central/room_light/policy.py): what each room light reads as from its
references' kept facts, which House › Lights changes are refused and with which status, and how a
Player's drawn report reads. Pure."""
from __future__ import annotations

import pytest

from central.room_light.model import (
    COLOUR_SENSORS_MAX,
    BulbColour,
    ColourFacts,
    ColourSensor,
    ColourSensorRef,
    Facts,
    FixedLight,
    Kept,
    LightFacts,
    LightStated,
    MeasuredLight,
    ReadingFacts,
    ReportedLight,
    ReporterRef,
    RoomLightRefusal,
    SensedColour,
    Sensor,
    SensorRef,
    TypedColour,
)
from central.room_light.policy import (
    check_colour_sensor,
    check_colour_sensor_removal,
    check_room_light,
    check_room_light_removal,
    check_sensor,
    check_sensor_removal,
    colour_facts_after,
    colour_line,
    drawn_state,
    entry_of,
    followed_by,
    light_facts_after,
    reading_facts_after,
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
    Unavailable,
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


# ---- The two holds (owner, 2026-10-25, Q1 and Q3): a stated value stands until another is stated ----

HALF = Dimmer(on=True, brightness=0.5)
OFF = Dimmer(on=False, brightness=0.0)
WARM = KelvinColour(kelvin=2700.0)
RED = RgbColour(rgb=(1.0, 0.0, 0.0))


def test_a_light_keeps_its_brightness_and_colour_through_cant_tell():
    """Mutation probe: light_facts_after answering Unavailable with state=None fails here."""
    stated = light_facts_after(None, LightStated(state=HALF, colour=WARM), 10.0)
    assert stated == LightFacts(state=Kept(HALF, 10.0), colour=Kept(WARM, 10.0), cant_tell_since=None, heard_at=10.0)
    away = light_facts_after(stated, Unavailable(), 20.0)
    assert away == LightFacts(state=Kept(HALF, 10.0), colour=Kept(WARM, 10.0), cant_tell_since=20.0, heard_at=20.0)
    still = light_facts_after(away, Unavailable(), 30.0)
    assert still.cant_tell_since == 20.0 and still.heard_at == 30.0 and still.state == Kept(HALF, 10.0)
    back = light_facts_after(still, LightStated(state=HALF, colour=None), 40.0)
    assert back == LightFacts(state=Kept(HALF, 10.0), colour=Kept(WARM, 10.0), cant_tell_since=None, heard_at=40.0)
    assert entry_of(SPOT, Facts(lights={SPOT_REF: away}, readings={}, colours={}), SENSORS, {}).state == HALF


def test_a_repeated_value_keeps_when_it_was_first_stated():
    """A re-read (Central restarting, Galleria publishing all again) states the same values again:
    when each was stated stays; heard_at moves, and a new value is stamped when it comes."""
    light = light_facts_after(None, LightStated(state=HALF, colour=WARM), 1.0)
    again = light_facts_after(light, LightStated(state=HALF, colour=WARM), 5.0)
    assert again == LightFacts(state=Kept(HALF, 1.0), colour=Kept(WARM, 1.0), cant_tell_since=None, heard_at=5.0)
    assert light_facts_after(again, LightStated(state=OFF, colour=RED), 6.0) == LightFacts(
        state=Kept(OFF, 6.0), colour=Kept(RED, 6.0), cant_tell_since=None, heard_at=6.0)
    read = reading_facts_after(reading_facts_after(None, Measured(lux=80.0), 1.0), Measured(lux=80.0), 5.0)
    assert (read.reading, read.heard_at) == (Kept(Measured(lux=80.0), 1.0), 5.0)
    sky = colour_facts_after(colour_facts_after(None, KelvinColour(kelvin=4100.5), 1.0), KelvinColour(kelvin=4100.5), 5.0)
    assert (sky.colour, sky.heard_at) == (Kept(KelvinColour(kelvin=4100.5), 1.0), 5.0)


def test_off_is_stated_and_a_report_without_colour_leaves_the_colour():
    kept = light_facts_after(light_facts_after(None, LightStated(state=HALF, colour=RED), 1.0),
                             LightStated(state=OFF, colour=None), 2.0)
    assert kept.state == Kept(OFF, 2.0) and kept.colour == Kept(RED, 1.0)
    assert light_facts_after(kept, LightStated(state=HALF, colour=WARM), 3.0).colour == Kept(WARM, 3.0)


def test_never_stated_then_cant_tell_keeps_nothing():
    facts = light_facts_after(None, Unavailable(), 5.0)
    assert facts == LightFacts(state=None, colour=None, cant_tell_since=5.0, heard_at=5.0)
    assert entry_of(SPOT, Facts(lights={SPOT_REF: facts}, readings={}, colours={}), SENSORS, {}).state == NotReported()


def test_a_sensor_and_a_colour_sensor_keep_their_last_stated_value():
    read = reading_facts_after(reading_facts_after(None, Measured(lux=80.0), 1.0), Unavailable(), 2.0)
    assert read == ReadingFacts(reading=Kept(Measured(lux=80.0), 1.0), cant_tell_since=2.0, heard_at=2.0)
    assert reading_facts_after(read, Measured(lux=3.0), 4.0).cant_tell_since is None
    colour = colour_facts_after(colour_facts_after(None, KelvinColour(kelvin=4100.5), 1.0), Unavailable(), 2.0)
    assert colour == ColourFacts(colour=Kept(KelvinColour(kelvin=4100.5), 1.0), cant_tell_since=2.0, heard_at=2.0)
    assert colour_facts_after(colour, Unavailable(), 3.0).cant_tell_since == 2.0


# ---- Which colour a light takes ----

SKY_REF = ColourSensorRef(source="home-assistant", address="sensor.sun_sky_color_temperature")
SKY = ColourSensor(name="Sky", at=SKY_REF)
FOLLOWS_SKY = SensedColour(sensor="sky", typed_kelvin=5500)
DAYLIGHT = MeasuredLight(name="Daylight", sensor="meter", colour=FOLLOWS_SKY)


def test_a_colour_line_by_source():
    typed = colour_line(SPOT, NONE, {})
    assert (typed.colour, typed.source, typed.colour_from, typed.sensor) == (KELVIN, "typed", "typed", None)
    never = colour_line(BULB, NONE, {})
    assert (never.colour, never.source, never.colour_from) == (KelvinColour(kelvin=2700), "its-bulb", "never-stated")
    bulb = Facts(lights={BULB.reporter: LightFacts(state=Kept(OFF, 1.0), colour=Kept(RED, 1.0), cant_tell_since=9.0,
                                                   heard_at=9.0)}, readings={}, colours={})
    following = colour_line(BULB, bulb, {})
    assert (following.colour, following.colour_from, following.stated_at, following.cant_tell_since) == (
        RED, "following", 1.0, 9.0)


def test_a_light_follows_its_colour_sensor_and_stays_typed_without_one():
    stated = KelvinColour(kelvin=4321.25)
    facts = Facts(lights={}, readings={}, colours={SKY_REF: ColourFacts(colour=Kept(stated, 3.0), cant_tell_since=None,
                                                                        heard_at=3.0)})
    line = colour_line(DAYLIGHT, facts, {"sky": SKY})
    assert (line.colour, line.source, line.sensor, line.colour_from, line.stated_at) == (
        stated, "colour-sensor", "sky", "following", 3.0)
    assert entry_of(DAYLIGHT, facts, SENSORS, {"sky": SKY}).colour == stated     # unbounded, unrounded
    gone = colour_line(DAYLIGHT, facts, {})
    assert (gone.colour, gone.colour_from) == (KelvinColour(kelvin=5500), "never-stated")
    assert colour_line(FixedLight(name="Lamp", colour=FOLLOWS_SKY), NONE, {"sky": SKY}).colour_from == "never-stated"


def test_colour_sensors_saved_and_removed():
    lights = {**LIGHTS, "daylight": DAYLIGHT.model_copy(update={"sensor": "other"})}
    assert followed_by(lights, "sky") == ("daylight",)
    unknown = check_room_light("lamp", FixedLight(name="Lamp", colour=SensedColour(sensor="nope", typed_kelvin=5000)),
                               LIGHTS, SENSORS, {"sky": SKY}, ())
    assert unknown is not None and unknown.code is RoomLightRefusal.UNKNOWN_COLOUR_SENSOR
    assert check_room_light("lamp", FixedLight(name="Lamp", colour=FOLLOWS_SKY), LIGHTS, SENSORS, {"sky": SKY}, ()) is None
    assert check_colour_sensor("sky", SKY, {}, LIGHTS, SENSORS) is None
    assert check_colour_sensor("sky", SKY.model_copy(update={"name": "Sun"}), {"sky": SKY}, LIGHTS, SENSORS) is None
    for at in (SKY_REF, SPOT_REF, METER_REF):
        taken = check_colour_sensor("other", ColourSensor(name="Other", at=ColourSensorRef(
            source=at.source, address=at.address)), {"sky": SKY}, LIGHTS, SENSORS)
        assert taken is not None and taken.code is RoomLightRefusal.REFERENCE_TAKEN
    full = {f"c{index}": ColourSensor(name="C", at=ColourSensorRef(source="home-assistant", address=f"sensor.c{index}"))
            for index in range(COLOUR_SENSORS_MAX)}
    too_many = check_colour_sensor("sky", SKY, full, {}, {})
    assert too_many is not None and too_many.code is RoomLightRefusal.TOO_MANY
    followed = check_colour_sensor_removal("sky", {"sky": SKY}, lights)
    assert followed is not None and followed.code is RoomLightRefusal.COLOUR_SENSOR_FOLLOWED
    gone = check_colour_sensor_removal("nope", {"sky": SKY}, lights)
    assert gone is not None and gone.code is RoomLightRefusal.UNKNOWN_COLOUR_SENSOR
    assert check_colour_sensor_removal("sky", {"sky": SKY}, LIGHTS) is None
    taken = check_sensor("other", Sensor(name="Other", at=SensorRef(source=SKY_REF.source, address=SKY_REF.address)),
                         SENSORS, LIGHTS, {"sky": SKY})       # a Sensor at a colour sensor's reference
    assert taken is not None and taken.code is RoomLightRefusal.REFERENCE_TAKEN
