"""Room lights' rules (central/room_light/policy.py): what each room light reads as from its
references' kept facts, which House › Lights changes are refused and with which status, and how a
Player's drawn report reads. Pure."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from central.house_time.model import HouseClock
from central.room_light.model import (
    COLOUR_SENSORS_MAX,
    BulbColour,
    ColourFacts,
    ColourSensor,
    ColourSensorRef,
    Doubt,
    DoubtReason,
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
    doubt_after,
    doubt_words,
    doubted,
    drawn_state,
    entry_of,
    followed_by,
    light_facts_after,
    reading_facts_after,
    room_lights_document,
)
from central.room_light.views import DoubtView
from contracts.room_light import (
    CENTRAL_LINE_MAX,
    ROOM_LIGHTS_DRAWN_LEASE_SECONDS,
    ROOM_LIGHTS_MAX,
    CentralWords,
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
ZONE = HouseClock(ZoneInfo("Europe/London"), "24h")   # the house's clock the words are dated on


def _cant(since: float) -> Doubt:
    return Doubt(DoubtReason.CANT_TELL, since)


def _light(state: Dimmer | None, colour: KelvinColour | RgbColour | None = None) -> LightFacts:
    return LightFacts(state=None if state is None else Kept(state, 1.0), colour=None if colour is None else Kept(colour, 1.0),
                      doubt=None, heard_at=1.0)


def test_each_light_reads_its_references_kept_facts():
    """Mutation probe M1: entry_of ignoring the facts -> every reported light reads not reported."""
    facts = Facts(lights={SPOT_REF: _light(Dimmer(on=True, brightness=0.5), RgbColour(rgb=(1.0, 0.5, 0.0))),
                          BULB.reporter: _light(Dimmer(on=True, brightness=0.25), RgbColour(rgb=(1.0, 0.5, 0.0)))},
                  readings={METER_REF: ReadingFacts(reading=Kept(Measured(lux=240.0), 1.0), doubt=None,
                                                    heard_at=1.0)},
                  colours={})
    assert entry_of(ROOM, facts, SENSORS, {}, ZONE) == FixedEntry(colour=KelvinColour(kelvin=6500))
    assert entry_of(SPOT, facts, SENSORS, {}, ZONE) == ReportedEntry(colour=KELVIN, state=Dimmer(on=True, brightness=0.5), doubt=None)
    assert entry_of(BULB, facts, SENSORS, {}, ZONE) == ReportedEntry(
        colour=RgbColour(rgb=(1.0, 0.5, 0.0)), state=Dimmer(on=True, brightness=0.25), doubt=None)
    assert entry_of(WINDOW, facts, SENSORS, {}, ZONE) == MeasuredEntry(colour=KELVIN, state=Measured(lux=240.0), doubt=None)


def test_no_word_or_no_sensor_is_not_reported():
    for light in (SPOT, BULB, WINDOW):
        assert entry_of(light, NONE, SENSORS, {}, ZONE).state == NotReported()
    kept = Facts(lights={}, readings={METER_REF: ReadingFacts(reading=Kept(Measured(lux=9.0), 1.0), doubt=None,
                                                             heard_at=1.0)}, colours={})
    assert entry_of(WINDOW, kept, {}, {}, ZONE).state == NotReported()
    assert entry_of(BULB, NONE, SENSORS, {}, ZONE).colour == KelvinColour(kelvin=2700)     # typed until the bulb says


def test_the_document_carries_every_light_by_id():
    document = room_lights_document(LIGHTS, SENSORS, {}, NONE, 7, ZONE)
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
    assert stated == LightFacts(state=Kept(HALF, 10.0), colour=Kept(WARM, 10.0), doubt=None, heard_at=10.0)
    away = light_facts_after(stated, Unavailable(), 20.0)
    assert away == LightFacts(state=Kept(HALF, 10.0), colour=Kept(WARM, 10.0), doubt=_cant(20.0), heard_at=20.0)
    still = light_facts_after(away, Unavailable(), 30.0)
    assert still.doubt == _cant(20.0) and still.heard_at == 30.0 and still.state == Kept(HALF, 10.0)
    back = light_facts_after(still, LightStated(state=HALF, colour=None), 40.0)
    assert back == LightFacts(state=Kept(HALF, 10.0), colour=Kept(WARM, 10.0), doubt=None, heard_at=40.0)
    assert entry_of(SPOT, Facts(lights={SPOT_REF: away}, readings={}, colours={}), SENSORS, {}, ZONE).state == HALF


def test_a_repeated_value_keeps_when_it_was_first_stated():
    """A re-read (Central restarting, Galleria publishing all again) states the same values again:
    when each was stated stays; heard_at moves, and a new value is stamped when it comes."""
    light = light_facts_after(None, LightStated(state=HALF, colour=WARM), 1.0)
    again = light_facts_after(light, LightStated(state=HALF, colour=WARM), 5.0)
    assert again == LightFacts(state=Kept(HALF, 1.0), colour=Kept(WARM, 1.0), doubt=None, heard_at=5.0)
    assert light_facts_after(again, LightStated(state=OFF, colour=RED), 6.0) == LightFacts(
        state=Kept(OFF, 6.0), colour=Kept(RED, 6.0), doubt=None, heard_at=6.0)
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
    assert facts == LightFacts(state=None, colour=None, doubt=_cant(5.0), heard_at=5.0)
    assert entry_of(SPOT, Facts(lights={SPOT_REF: facts}, readings={}, colours={}), SENSORS, {}, ZONE).state == NotReported()


def test_a_sensor_and_a_colour_sensor_keep_their_last_stated_value():
    read = reading_facts_after(reading_facts_after(None, Measured(lux=80.0), 1.0), Unavailable(), 2.0)
    assert read == ReadingFacts(reading=Kept(Measured(lux=80.0), 1.0), doubt=_cant(2.0), heard_at=2.0)
    assert reading_facts_after(read, Measured(lux=3.0), 4.0).doubt is None
    colour = colour_facts_after(colour_facts_after(None, KelvinColour(kelvin=4100.5), 1.0), Unavailable(), 2.0)
    assert colour == ColourFacts(colour=Kept(KelvinColour(kelvin=4100.5), 1.0), doubt=_cant(2.0), heard_at=2.0)
    assert colour_facts_after(colour, Unavailable(), 3.0).doubt == _cant(2.0)


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
    bulb = Facts(lights={BULB.reporter: LightFacts(state=Kept(OFF, 1.0), colour=Kept(RED, 1.0), doubt=_cant(9.0),
                                                   heard_at=9.0)}, readings={}, colours={})
    following = colour_line(BULB, bulb, {})
    assert (following.colour, following.colour_from, following.stated_at, following.doubt) == (
        RED, "following", 1.0, DoubtView(reason=DoubtReason.CANT_TELL, since=9.0))


def test_a_light_follows_its_colour_sensor_and_stays_typed_without_one():
    stated = KelvinColour(kelvin=4321.25)
    facts = Facts(lights={}, readings={}, colours={SKY_REF: ColourFacts(colour=Kept(stated, 3.0), doubt=None,
                                                                        heard_at=3.0)})
    line = colour_line(DAYLIGHT, facts, {"sky": SKY})
    assert (line.colour, line.source, line.sensor, line.colour_from, line.stated_at) == (
        stated, "colour-sensor", "sky", "following", 3.0)
    assert entry_of(DAYLIGHT, facts, SENSORS, {"sky": SKY}, ZONE).colour == stated     # unbounded, unrounded
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


# ---- Doubt and its words ----

def _at(day: int, hour: int, minute: int) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE.zone).timestamp()


def test_the_newest_reason_wins_and_a_repeat_keeps_since():
    """Mutation probe: doubt_after always new -> "since" moves at every repeat."""
    first = doubt_after(None, DoubtReason.CANT_READ, 10.0)
    assert first == Doubt(DoubtReason.CANT_READ, 10.0)
    assert doubt_after(first, DoubtReason.CANT_READ, 20.0) == first
    assert doubt_after(first, DoubtReason.LINK_CLOSED, 30.0) == Doubt(DoubtReason.LINK_CLOSED, 30.0)
    assert doubt_after(first, DoubtReason.CANT_TELL, 40.0) == Doubt(DoubtReason.CANT_TELL, 40.0)


def test_a_doubt_without_a_report_keeps_the_last_word_and_a_stated_report_clears_it():
    stated = light_facts_after(None, LightStated(state=Dimmer(on=False, brightness=0.0), colour=None), 5.0)
    empty = LightFacts(state=None, colour=None, doubt=None, heard_at=60.0)
    unread = doubted(stated, DoubtReason.CANT_READ, 60.0, empty)
    assert unread == LightFacts(state=stated.state, colour=None, doubt=Doubt(DoubtReason.CANT_READ, 60.0), heard_at=60.0)
    closed = doubted(unread, DoubtReason.LINK_CLOSED, 70.0, empty)   # not heard: heard_at stands
    assert closed == LightFacts(state=stated.state, colour=None, doubt=Doubt(DoubtReason.LINK_CLOSED, 70.0),
                                heard_at=60.0)
    assert doubted(None, DoubtReason.CANT_READ, 60.0, empty).doubt == Doubt(DoubtReason.CANT_READ, 60.0)
    again = light_facts_after(closed, LightStated(state=Dimmer(on=False, brightness=0.0), colour=None), 80.0)
    assert again.doubt is None and again.state == stated.state   # a restated value keeps when it was stated
    reading = ReadingFacts(reading=Kept(Measured(lux=3.0), 1.0), doubt=None, heard_at=1.0)
    assert reading_facts_after(doubted(reading, DoubtReason.CANT_READ, 2.0, reading), Measured(lux=3.0), 3.0).doubt is None


def test_doubt_words_name_the_light_its_last_word_and_since_when_in_the_house_s_zone():
    off = Kept(Dimmer(on=False, brightness=0.0), _at(26, 15, 35))
    assert doubt_words("Family Room Lights", Doubt(DoubtReason.CANT_READ, _at(29, 18, 5)), off, ZONE) == CentralWords(
        line1="Photo Wall can't read Family Room Lights · since 29 Oct 18:05",
        line2="Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall")
    dark = Kept(Measured(lux=0.0), _at(26, 18, 2))
    assert doubt_words("Daylight (windows)", Doubt(DoubtReason.CANT_TELL, _at(26, 18, 2)), dark, ZONE) == CentralWords(
        line1="Daylight (windows): its sensor can't tell · since 26 Oct 18:02",
        line2="Its last word: 0 lx (26 Oct 18:02) · see Needs attention in Photo Wall")
    half = Kept(Dimmer(on=True, brightness=0.5), _at(26, 9, 0))
    closed = doubt_words("Lamp", Doubt(DoubtReason.LINK_CLOSED, _at(27, 7, 1)), half, ZONE)
    assert closed.line1 == "Photo Wall lost the link to Lamp · since 27 Oct 07:01"
    assert closed.line2.startswith("Its last word: on at 50% (26 Oct 09:00)")
    assert doubt_words("Lamp", Doubt(DoubtReason.CANT_TELL, 1.0), None, ZONE).line2.startswith("Its last word: none yet")


def test_doubt_words_are_dated_on_the_house_s_12_hour_clock_when_it_has_one():
    """Mutation probe: the words ignoring the house's clock format -> "18:05" on a 12-hour house."""
    twelve = HouseClock(ZoneInfo("Europe/London"), "12h")
    off = Kept(Dimmer(on=False, brightness=0.0), _at(26, 0, 35))
    words = doubt_words("Family Room Lights", Doubt(DoubtReason.CANT_READ, _at(29, 18, 5)), off, twelve)
    assert words.line1 == "Photo Wall can't read Family Room Lights · since 29 Oct 6:05 pm"
    assert words.line2.startswith("Its last word: off (26 Oct 12:35 am)")
    assert twelve.instant_words(_at(26, 12, 0)) == "26 Oct 12:00 pm"


def test_a_long_name_is_shortened_so_since_always_shows():
    words = doubt_words("X" * 64, Doubt(DoubtReason.CANT_READ, _at(29, 18, 5)), None, ZONE)
    assert len(words.line1) == CENTRAL_LINE_MAX and words.line1.endswith("… · since 29 Oct 18:05")
    tabbed = doubt_words("Hall\tlamp", Doubt(DoubtReason.CANT_READ, _at(29, 18, 5)), None, ZONE)
    assert tabbed.line1.startswith("Photo Wall can't read Hall lamp")


def test_only_the_brightness_reference_s_doubt_travels_in_the_entry():
    """Mutation probe: entry_of dropping the doubt -> the Pi never learns Central can't stand behind it."""
    unread = Doubt(DoubtReason.CANT_READ, _at(29, 18, 5))
    facts = Facts(lights={SPOT_REF: LightFacts(state=Kept(Dimmer(on=False, brightness=0.0), _at(26, 15, 35)), colour=None,
                                               doubt=unread, heard_at=_at(29, 18, 5))},
                  readings={METER_REF: ReadingFacts(reading=None, doubt=_cant(_at(26, 18, 2)), heard_at=1.0)},
                  colours={SKY_REF: ColourFacts(colour=None, doubt=_cant(1.0), heard_at=1.0)})
    spot = entry_of(SPOT, facts, SENSORS, {}, ZONE)
    assert isinstance(spot, ReportedEntry) and spot.doubt == doubt_words("Spot", unread, facts.lights[SPOT_REF].state, ZONE)
    window = entry_of(WINDOW, facts, SENSORS, {}, ZONE)
    assert isinstance(window, MeasuredEntry) and window.state == NotReported()
    assert window.doubt is not None and window.doubt.line1 == "Window: its sensor can't tell · since 26 Oct 18:02"
    follows_sky = FixedLight(name="Daylight", colour=SensedColour(sensor="sky", typed_kelvin=5000))
    assert entry_of(follows_sky, facts, SENSORS, {"sky": SKY}, ZONE) == FixedEntry(colour=KelvinColour(kelvin=5000))
    trusted = Facts(lights={SPOT_REF: LightFacts(state=Kept(HALF, 1.0), colour=None, doubt=None, heard_at=1.0)},
                    readings={}, colours={SKY_REF: ColourFacts(colour=None, doubt=_cant(1.0), heard_at=1.0)})
    sky_spot = SPOT.model_copy(update={"colour": BulbColour(typed_kelvin=4000)})
    assert entry_of(sky_spot, trusted, SENSORS, {"sky": SKY}, ZONE).doubt is None   # a colour doubt never travels
