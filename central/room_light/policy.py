"""Room lights' rules, each with one home. Pure: no I/O, no clock (an instant is an argument).

What a report keeps (`light_facts_after`, `reading_facts_after`, `colour_facts_after`: the two holds,
owner 2026-10-25 Q1 and Q3: a stated value stands until the reference states another; "can't tell"
leaves it and doubts it), why Central can't stand behind a reference's last word (`doubt_after`,
`doubted`: "can't tell", "can't read", "link closed", the newest reason winning) and the words that
travel for it (`doubt_words`), which colour each light takes (`colour_of`, `colour_line`), what
each room light reads as now (`entry_of`, `room_lights_document`), which House › Lights changes are
refused (`check_*`), and how a Player's drawn report reads against the document now (`drawn_state`).

Central never bounds a stated colour to what a screen draws (the Pi does, player/geometry.py
`drawn_kelvin`), and no colour value ever costs a light its brightness.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from central.house_time.model import HouseClock
from central.room_light.model import (
    COLOUR_SENSORS_MAX,
    SENSORS_MAX,
    BulbColour,
    ColourFacts,
    ColourReading,
    ColourSensor,
    ColourSensorId,
    Doubt,
    DoubtReason,
    Facts,
    FixedLight,
    Kept,
    LightFacts,
    LightReport,
    LightStated,
    MeasuredLight,
    ReadingFacts,
    ReportedLight,
    RoomLight,
    RoomLightId,
    RoomLightRefusal,
    RoomLightRefused,
    SensedColour,
    Sensor,
    SensorId,
    TypedColour,
)
from central.room_light.views import ColourLineView, DoubtView, DrawnState
from contracts.room_light import (
    CENTRAL_LINE_MAX,
    ROOM_LIGHTS_DRAWN_LEASE_SECONDS,
    ROOM_LIGHTS_MAX,
    CentralWords,
    Dimmer,
    FixedEntry,
    KelvinColour,
    LightColour,
    Measured,
    MeasuredEntry,
    NotReported,
    Reading,
    ReportedEntry,
    RoomLightEntry,
    RoomLightsDocument,
    RoomLightsDrawn,
)

# ---- What a report keeps: the two holds ----

def light_facts_after(before: LightFacts | None, report: LightReport, at: float) -> LightFacts:
    """A light reference's facts after `report`, heard at `at`: a stated state replaces the last and
    clears its doubt; a stated colour replaces the last, a report stating none leaves it (a repeat
    of a kept value keeps when it was first stated, `_kept`);
    Unavailable leaves both and doubts it "can't tell" (`doubt_after`). Pure."""
    if isinstance(report, LightStated):
        colour = None if before is None else before.colour
        if report.colour is not None:
            colour = _kept(colour, report.colour, at)
        return LightFacts(state=_kept(None if before is None else before.state, report.state, at), colour=colour,
                          doubt=None, heard_at=at)
    return LightFacts(state=None if before is None else before.state, colour=None if before is None else before.colour,
                      doubt=_cant_tell(before, at), heard_at=at)


def reading_facts_after(before: ReadingFacts | None, reading: Reading, at: float) -> ReadingFacts:
    """A Sensor reference's facts after `reading`, heard at `at`: a Measured replaces the last and
    clears its doubt; Unavailable leaves it and doubts it "can't tell". Pure."""
    if isinstance(reading, Measured):
        return ReadingFacts(reading=_kept(None if before is None else before.reading, reading, at), doubt=None,
                            heard_at=at)
    return ReadingFacts(reading=None if before is None else before.reading, doubt=_cant_tell(before, at),
                        heard_at=at)


def colour_facts_after(before: ColourFacts | None, reading: ColourReading, at: float) -> ColourFacts:
    """A colour sensor reference's facts after `reading`, heard at `at`: a KelvinColour replaces the
    last and clears its doubt; Unavailable leaves it and doubts it "can't tell". Pure."""
    if isinstance(reading, KelvinColour):
        return ColourFacts(colour=_kept(None if before is None else before.colour, reading, at), doubt=None,
                           heard_at=at)
    return ColourFacts(colour=None if before is None else before.colour, doubt=_cant_tell(before, at),
                       heard_at=at)


def _kept[T](before: Kept[T] | None, value: T, at: float) -> Kept[T]:
    """`value` as stated at `at`; a repeat of the kept value keeps when it was first stated, so a
    re-read (a hub restart, Galleria publishing all again) never moves "when it was stated"."""
    return before if before is not None and before.value == value else Kept(value, at)


def _cant_tell(before: LightFacts | ReadingFacts | ColourFacts | None, at: float) -> Doubt:
    return doubt_after(None if before is None else before.doubt, DoubtReason.CANT_TELL, at)


# ---- Doubt: why Central can't stand behind a reference's last word, and its words ----

def doubt_after(before: Doubt | None, reason: DoubtReason, at: float) -> Doubt:
    """The doubt after an event of `reason` at `at` (Central's clock): the newest reason wins; a
    repeat of the same reason keeps `before.since`. A stated report clears a doubt (its caller keeps
    None); `out_of_range` and `unknown_address` refusals never reach here (log only)."""
    return before if before is not None and before.reason == reason else Doubt(reason=reason, since=at)


def doubted[F: (LightFacts, ReadingFacts, ColourFacts)](before: F | None, reason: DoubtReason, at: float,
                                                        empty: F) -> F:
    """A reference's facts after an event that doubts it without a report (an unreadable report:
    "can't read", heard at `at`; its source's link closed: "link closed", not heard): its last word
    stands, its doubt is `doubt_after`'s. `empty` stands for a reference with no facts yet (never
    stated). Pure."""
    facts = empty if before is None else before
    heard_at = at if reason == DoubtReason.CANT_READ else facts.heard_at
    return replace(facts, doubt=doubt_after(facts.doubt, reason, at), heard_at=heard_at)


def doubt_words(name: str, doubt: Doubt, last: Kept[Dimmer] | Kept[Measured] | None,
                clock: HouseClock) -> CentralWords:
    """Central's two lines for a light whose brightness reference is in `doubt` (DESIGN-r8 §7):
    line 1 what is wrong and since when ("Photo Wall can't read Family Room Lights · since 29 Oct
    18:05"; "Family Room Lights: its light can't tell · since …"; "Photo Wall lost the link to
    Family Room Lights · since …"), line 2 the last word the Frame is drawn with and where to look
    ("Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall"). A light whose last
    word is a reading says "its sensor". Dates on the house's `clock` (its zone, 12 or 24 hours); a name too long for its line
    is shortened, so "since" always shows. Names the light, never an integration. Pure."""
    return _words(name, doubt, last, clock, sensor=last is not None and isinstance(last.value, Measured))


def _words(name: str, doubt: Doubt, last: Kept[Dimmer] | Kept[Measured] | None, clock: HouseClock, *,
           sensor: bool) -> CentralWords:
    since = f" · since {clock.instant_words(doubt.since)}"
    before, after = {DoubtReason.CANT_READ: ("Photo Wall can't read ", ""),
                     DoubtReason.CANT_TELL: ("", f": its {'sensor' if sensor else 'light'} can't tell"),
                     DoubtReason.LINK_CLOSED: ("Photo Wall lost the link to ", "")}[doubt.reason]
    room = CENTRAL_LINE_MAX - len(before) - len(after) - len(since)
    said = "none yet" if last is None else f"{_said(last.value)} ({clock.instant_words(last.at)})"
    return CentralWords(line1=f"{before}{_fit(name, room)}{after}{since}",
                        line2=f"Its last word: {said} · see Needs attention in Photo Wall")


def _said(value: Dimmer | Measured) -> str:
    if isinstance(value, Measured):
        return f"{value.lux:g} lx" if value.lux < 10 else f"{value.lux:.0f} lx"
    return f"on at {round(value.brightness * 100)}%" if value.on else "off"


def _fit(name: str, room: int) -> str:
    """`name` with every unprintable character a space, cut to `room` characters (ending "…")."""
    text = "".join(each if each.isprintable() else " " for each in name)
    return text if len(text) <= room else text[:room - 1] + "…"


# ---- Which colour a light takes ----

def colour_line(light: RoomLight, facts: Facts,
                colour_sensors: Mapping[ColourSensorId, ColourSensor]) -> ColourLineView:
    """`light`'s colour now and why: typed by choice; its source's last stated colour ("following",
    with when it was stated and its source's doubt); or its typed kelvin while the source never stated
    one ("never-stated"). A colour sensor that is gone reads as never stated. Pure."""
    source = light.colour
    if isinstance(source, TypedColour):
        return ColourLineView(colour=KelvinColour(kelvin=source.kelvin), source="typed", sensor=None,
                              colour_from="typed", stated_at=None, doubt=None)
    stated: Kept[LightColour] | Kept[KelvinColour] | None = None
    doubt: Doubt | None = None
    if isinstance(source, BulbColour):
        assert isinstance(light, ReportedLight)
        bulb = facts.lights.get(light.reporter)
        if bulb is not None:
            stated, doubt = bulb.colour, bulb.doubt
        kind, sensor = "its-bulb", None
    else:
        assert isinstance(source, SensedColour)
        found = colour_sensors.get(source.sensor)
        read = None if found is None else facts.colours.get(found.at)
        if read is not None:
            stated, doubt = read.colour, read.doubt
        kind, sensor = "colour-sensor", source.sensor
    if stated is None:
        return ColourLineView(colour=KelvinColour(kelvin=source.typed_kelvin), source=kind, sensor=sensor,
                              colour_from="never-stated", stated_at=None, doubt=doubt_view(doubt))
    return ColourLineView(colour=stated.value, source=kind, sensor=sensor, colour_from="following",
                          stated_at=stated.at, doubt=doubt_view(doubt))


def doubt_view(doubt: Doubt | None) -> DoubtView | None:
    """`doubt` as House › Lights shows it. Pure."""
    return None if doubt is None else DoubtView(reason=doubt.reason, since=doubt.since)


def colour_of(light: RoomLight, facts: Facts, colour_sensors: Mapping[ColourSensorId, ColourSensor]) -> LightColour:
    """The colour `light`'s entry carries: `colour_line`'s colour. Pure."""
    return colour_line(light, facts, colour_sensors).colour


# ---- What each light reads as now ----

def entry_of(light: RoomLight, facts: Facts, sensors: Mapping[SensorId, Sensor],
             colour_sensors: Mapping[ColourSensorId, ColourSensor], clock: HouseClock) -> RoomLightEntry:
    """`light` as the document's entry of its kind, with its references' kept facts: a FixedLight is
    always on, at full; a ReportedLight takes its reporter's last stated Dimmer; a MeasuredLight its
    Sensor's last stated reading; each at `colour_of`. Never stated (or no such Sensor): NotReported.
    The doubt travels only from the reference its brightness comes from (the reporter, the Sensor),
    worded on the house's `clock` (`doubt_words`); a colour doubt never does. A colour never decides
    a state. Pure."""
    colour = colour_of(light, facts, colour_sensors)
    if isinstance(light, FixedLight):
        assert isinstance(colour, KelvinColour)
        return FixedEntry(colour=colour)
    if isinstance(light, MeasuredLight):
        assert isinstance(colour, KelvinColour)
        sensor = sensors.get(light.sensor)
        kept = None if sensor is None else facts.readings.get(sensor.at)
        reading = None if kept is None else kept.reading
        return MeasuredEntry(colour=colour, state=NotReported() if reading is None else reading.value,
                             doubt=None if kept is None or kept.doubt is None
                             else _words(light.name, kept.doubt, reading, clock, sensor=True))
    kept_light = facts.lights.get(light.reporter)
    state = None if kept_light is None else kept_light.state
    return ReportedEntry(colour=colour, state=NotReported() if state is None else state.value,
                         doubt=None if kept_light is None or kept_light.doubt is None
                         else doubt_words(light.name, kept_light.doubt, state, clock))


def room_lights_document(lights: Mapping[RoomLightId, RoomLight], sensors: Mapping[SensorId, Sensor],
                         colour_sensors: Mapping[ColourSensorId, ColourSensor], facts: Facts,
                         change: int, clock: HouseClock) -> RoomLightsDocument:
    """The document every Pi draws with: every room light's entry (`entry_of`, worded on the house's
    `clock`). The same for every Pi."""
    return RoomLightsDocument(change=change, lights={
        light_id: entry_of(light, facts, sensors, colour_sensors, clock) for light_id, light in sorted(lights.items())})


def measured_by(lights: Mapping[RoomLightId, RoomLight], sensor_id: SensorId) -> RoomLightId | None:
    """The room light that measures Sensor `sensor_id`, or None (a Sensor measures at most one)."""
    return next((light_id for light_id, light in lights.items()
                 if isinstance(light, MeasuredLight) and light.sensor == sensor_id), None)


def followed_by(lights: Mapping[RoomLightId, RoomLight], sensor_id: ColourSensorId) -> tuple[RoomLightId, ...]:
    """The room lights whose colour follows colour sensor `sensor_id`, in the house's order."""
    return tuple(light_id for light_id, light in lights.items()
                 if isinstance(light.colour, SensedColour) and light.colour.sensor == sensor_id)


def _references(lights: Mapping[RoomLightId, RoomLight], sensors: Mapping[SensorId, Sensor],
                colour_sensors: Mapping[ColourSensorId, ColourSensor]) -> set[tuple[str, str]]:
    """Every (source, address) a room light's reporter, a Sensor or a colour sensor names."""
    taken = {(light.reporter.source, light.reporter.address) for light in lights.values()
             if isinstance(light, ReportedLight)}
    return (taken | {(sensor.at.source, sensor.at.address) for sensor in sensors.values()}
            | {(sensor.at.source, sensor.at.address) for sensor in colour_sensors.values()})


def check_room_light(light_id: RoomLightId, light: RoomLight, lights: Mapping[RoomLightId, RoomLight],
                     sensors: Mapping[SensorId, Sensor], colour_sensors: Mapping[ColourSensorId, ColourSensor],
                     named_by: tuple[str, ...]) -> RoomLightRefused | None:
    """Whether saving `light` as `light_id` (new, or replacing lights[light_id]) is refused, in this
    order: a change between MeasuredLight and any other kind while a Frame names it (`named_by`: its
    Frame lights' references would no longer fit), NAMED_BY_A_FRAME; a MeasuredLight naming no Sensor
    (UNKNOWN_SENSOR) or a Sensor another light measures (SENSOR_REPORTS_A_LIGHT); a colour source
    naming no colour sensor (UNKNOWN_COLOUR_SENSOR); a reporter another light, a Sensor or a colour
    sensor already names (REFERENCE_TAKEN); a new light past ROOM_LIGHTS_MAX (TOO_MANY). None when
    it may be saved."""
    previous = lights.get(light_id)
    others = {key: other for key, other in lights.items() if key != light_id}
    if (previous is not None and named_by
            and isinstance(previous, MeasuredLight) != isinstance(light, MeasuredLight)):
        return RoomLightRefused(RoomLightRefusal.NAMED_BY_A_FRAME)
    if isinstance(light, MeasuredLight):
        if light.sensor not in sensors:
            return RoomLightRefused(RoomLightRefusal.UNKNOWN_SENSOR)
        if measured_by(others, light.sensor) is not None:
            return RoomLightRefused(RoomLightRefusal.SENSOR_REPORTS_A_LIGHT)
    if isinstance(light.colour, SensedColour) and light.colour.sensor not in colour_sensors:
        return RoomLightRefused(RoomLightRefusal.UNKNOWN_COLOUR_SENSOR)
    if isinstance(light, ReportedLight) and (
            (light.reporter.source, light.reporter.address) in _references(others, sensors, colour_sensors)):
        return RoomLightRefused(RoomLightRefusal.REFERENCE_TAKEN)
    if previous is None and len(lights) >= ROOM_LIGHTS_MAX:
        return RoomLightRefused(RoomLightRefusal.TOO_MANY)
    return None


def check_room_light_removal(light_id: RoomLightId, lights: Mapping[RoomLightId, RoomLight],
                             named_by: tuple[str, ...]) -> RoomLightRefused | None:
    """UNKNOWN_ROOM_LIGHT when there is no such light; NAMED_BY_A_FRAME while a Frame's lighting
    names it; else None."""
    if light_id not in lights:
        return RoomLightRefused(RoomLightRefusal.UNKNOWN_ROOM_LIGHT)
    if named_by:
        return RoomLightRefused(RoomLightRefusal.NAMED_BY_A_FRAME)
    return None


def check_sensor(sensor_id: SensorId, sensor: Sensor, sensors: Mapping[SensorId, Sensor],
                 lights: Mapping[RoomLightId, RoomLight],
                 colour_sensors: Mapping[ColourSensorId, ColourSensor]) -> RoomLightRefused | None:
    """Whether saving `sensor` as `sensor_id` is refused: its reference already names another Sensor,
    a colour sensor or a room light's reporter (REFERENCE_TAKEN: a second Sensor on one Pi is this); a new Sensor past
    SENSORS_MAX (TOO_MANY). None when it may be saved."""
    others = {key: other for key, other in sensors.items() if key != sensor_id}
    if (sensor.at.source, sensor.at.address) in _references(lights, others, colour_sensors):
        return RoomLightRefused(RoomLightRefusal.REFERENCE_TAKEN)
    if sensor_id not in sensors and len(sensors) >= SENSORS_MAX:
        return RoomLightRefused(RoomLightRefusal.TOO_MANY)
    return None


def check_sensor_removal(sensor_id: SensorId, sensors: Mapping[SensorId, Sensor],
                         lights: Mapping[RoomLightId, RoomLight]) -> RoomLightRefused | None:
    """UNKNOWN_SENSOR when there is no such Sensor; SENSOR_REPORTS_A_LIGHT while a room light
    is measured by it; else None."""
    if sensor_id not in sensors:
        return RoomLightRefused(RoomLightRefusal.UNKNOWN_SENSOR)
    if measured_by(lights, sensor_id) is not None:
        return RoomLightRefused(RoomLightRefusal.SENSOR_REPORTS_A_LIGHT)
    return None


def check_colour_sensor(sensor_id: ColourSensorId, sensor: ColourSensor,
                        colour_sensors: Mapping[ColourSensorId, ColourSensor], lights: Mapping[RoomLightId, RoomLight],
                        sensors: Mapping[SensorId, Sensor]) -> RoomLightRefused | None:
    """Whether saving `sensor` as `sensor_id` is refused: its reference already names another colour
    sensor, a Sensor or a room light's reporter (REFERENCE_TAKEN); a new one past COLOUR_SENSORS_MAX
    (TOO_MANY). None when it may be saved. (Its source and address form are ReportSources'.)"""
    others = {key: other for key, other in colour_sensors.items() if key != sensor_id}
    if (sensor.at.source, sensor.at.address) in _references(lights, sensors, others):
        return RoomLightRefused(RoomLightRefusal.REFERENCE_TAKEN)
    if sensor_id not in colour_sensors and len(colour_sensors) >= COLOUR_SENSORS_MAX:
        return RoomLightRefused(RoomLightRefusal.TOO_MANY)
    return None


def check_colour_sensor_removal(sensor_id: ColourSensorId, colour_sensors: Mapping[ColourSensorId, ColourSensor],
                                lights: Mapping[RoomLightId, RoomLight]) -> RoomLightRefused | None:
    """UNKNOWN_COLOUR_SENSOR when there is no such colour sensor; COLOUR_SENSOR_FOLLOWED while a room
    light follows it; else None."""
    if sensor_id not in colour_sensors:
        return RoomLightRefused(RoomLightRefusal.UNKNOWN_COLOUR_SENSOR)
    if followed_by(lights, sensor_id):
        return RoomLightRefused(RoomLightRefusal.COLOUR_SENSOR_FOLLOWED)
    return None


def drawn_state(report: RoomLightsDrawn | None, age_seconds: float | None, digest_now: str) -> DrawnState:
    """A Player's drawn report against the document now (`digest_now`): "not-reported" with no report
    or one older than ROOM_LIGHTS_DRAWN_LEASE_SECONDS on Central's clock; "not-following" when its
    reader is not following WALL; "drawn" when it drew the document now; else "behind"."""
    if report is None or age_seconds is None or age_seconds > ROOM_LIGHTS_DRAWN_LEASE_SECONDS:
        return "not-reported"
    if not report.following:
        return "not-following"
    return "drawn" if report.drawn == digest_now else "behind"


__all__ = ["check_colour_sensor", "check_colour_sensor_removal", "check_room_light", "check_room_light_removal",
           "check_sensor", "check_sensor_removal", "colour_facts_after", "colour_line", "colour_of", "doubt_after",
           "doubt_view", "doubt_words", "doubted", "drawn_state",
           "entry_of", "followed_by", "light_facts_after", "measured_by", "reading_facts_after", "room_lights_document"]
