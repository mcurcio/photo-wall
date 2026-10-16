"""Room lights' rules (decision 0028; central/room_light/policy.py): what a Home Assistant message
says, who may report what, whether a Frame's lighting fits the room lights, the document every Pi
draws with and a Frame's lighting on upgrade. Pure."""
from __future__ import annotations

import math

import pytest

from central.room_light.model import (
    FixedReporter,
    FrameLightingRefusal,
    HomeAssistantLight,
    HomeAssistantSensor,
    IlluminanceMessage,
    LightStateMessage,
    OnPlayer,
    RoomLight,
    Sensor,
    SensorReporter,
    SetupRefusal,
)
from central.room_light.policy import (
    check_frame_lighting,
    check_room_light,
    check_room_light_removal,
    check_sensor,
    check_sensor_removal,
    light_state,
    parse_illuminance_message,
    parse_light_message,
    room_lights_document,
    topic_entity,
    upgrade_lighting,
)
from contracts.frame_lighting import FrameLighting, Paper, WashLight
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.room_light import (
    FULL_ON,
    Dimmer,
    FixedColour,
    Measured,
    NotReported,
    ReportedColour,
    Unavailable,
)

WHITE = FixedColour(kelvin=4000)
SPOT = RoomLight(id="bookshelf-spot", name="Spot", reporter=HomeAssistantLight(entity="light.spot"), colour=WHITE)
ROOM = RoomLight(id="room", name="Room", reporter=FixedReporter(), colour=FixedColour(kelvin=6500))
WINDOW = RoomLight(id="window", name="Window", reporter=SensorReporter(sensor="meter"), colour=WHITE)
METER = Sensor(id="meter", name="Meter", at=HomeAssistantSensor(entity="sensor.lux"))
LIGHTS = {light.id: light for light in (SPOT, ROOM, WINDOW)}
SENSORS = {"meter": METER}
PAPER = Paper(whiteness=0.9, ink_density=2.3)


def wash(light: str, reference) -> FrameLighting:
    return FrameLighting(mat_depth_mm=0, lights=(WashLight(light=light, lux=500.0, reference=reference),), paper=PAPER)


def test_topics_name_an_entity_or_nothing():
    assert topic_entity("photowall/light/light.bookshelf_spot") == ("light", "light.bookshelf_spot")
    assert topic_entity("photowall/illuminance/sensor.hall_lux") == ("illuminance", "sensor.hall_lux")
    for topic in ("photowall/light/", "photowall/light/Light.Spot", "photowall/other/light.spot", "light/light.a"):
        assert topic_entity(topic) is None


def test_home_assistant_light_messages():
    assert parse_light_message(b'{"state":"unknown","friendly_name":"x"}') == LightStateMessage("unavailable", None, None)
    assert parse_light_message(b'{"state":"off","brightness":null}') == LightStateMessage("off", None, None)
    assert parse_light_message(b'{"state":"on","brightness":51,"rgb":[255,0,10]}') == LightStateMessage(
        "on", 51, (255, 0, 10))
    for raw in (b'{"state":"dim"}', b'{"state":"on","brightness":256}', b'{"state":"on","brightness":true}',
                b'{"state":"on","rgb":[1,2]}', b'[1]', b'{"state":"on","state":"off"}', b"x" * 1025):
        with pytest.raises(ValueError, match="out-of-range"):
            parse_light_message(raw)
    assert light_state(LightStateMessage("on", None, None)) == FULL_ON
    assert light_state(LightStateMessage("off", None, None)) == Dimmer(on=False, brightness=0.0)
    assert light_state(LightStateMessage("on", 51, (255, 0, 51))) == Dimmer(on=True, brightness=0.2, rgb=(1.0, 0.0, 0.2))
    assert light_state(LightStateMessage("unavailable", None, None)) == Unavailable()


def test_illuminance_messages():
    assert parse_illuminance_message(b'{"lux": 120}') == IlluminanceMessage(120.0)
    assert parse_illuminance_message(b'{"state": "unavailable"}') == IlluminanceMessage(None)
    for raw in (b'{"lux": -1}', b'{"lux": "12"}', b'{"lux": 1e9}', b'{"state": "on"}'):
        with pytest.raises(ValueError, match="out-of-range"):
            parse_illuminance_message(raw)


def test_one_reporter_per_light_and_per_entity():
    assert check_room_light(SPOT, LIGHTS, SENSORS, ()) is None          # replacing itself
    rgb_fixed = RoomLight(id="lamp", name="Lamp", reporter=FixedReporter(), colour=ReportedColour(channel_shares=(1, 1, 1)))
    assert check_room_light(rgb_fixed, LIGHTS, SENSORS, ()) is SetupRefusal.REPORTED_COLOUR_NEEDS_HOME_ASSISTANT
    twin = RoomLight(id="twin", name="Twin", reporter=HomeAssistantLight(entity="light.spot"), colour=WHITE)
    assert check_room_light(twin, LIGHTS, SENSORS, ()) is SetupRefusal.ENTITY_REPORTS_ANOTHER
    by_meter = RoomLight(id="lamp", name="Lamp", reporter=SensorReporter(sensor="meter"), colour=WHITE)
    assert check_room_light(by_meter, LIGHTS, SENSORS, ()) is SetupRefusal.SENSOR_REPORTS_A_LIGHT
    no_sensor = RoomLight(id="lamp", name="Lamp", reporter=SensorReporter(sensor="gone"), colour=WHITE)
    assert check_room_light(no_sensor, LIGHTS, SENSORS, ()) is SetupRefusal.UNKNOWN_SENSOR
    full = {f"l{index}": ROOM.model_copy(update={"id": f"l{index}"}) for index in range(32)}
    assert check_room_light(ROOM.model_copy(update={"id": "one-more"}), full, {}, ()) is SetupRefusal.TOO_MANY
    assert check_room_light_removal("room", LIGHTS, ("hall",)) is SetupRefusal.NAMED_BY_A_FRAME
    assert check_room_light_removal("nope", LIGHTS, ()) is SetupRefusal.UNKNOWN_ROOM_LIGHT
    assert check_room_light_removal("room", LIGHTS, ()) is None


def test_sensors():
    pi = Sensor(id="pi-meter", name="Pi", at=OnPlayer(player_id="p1"))
    assert check_sensor(pi, SENSORS, LIGHTS) is None
    assert check_sensor(pi.model_copy(update={"id": "second"}), {**SENSORS, "pi-meter": pi}, LIGHTS) \
        is SetupRefusal.ENTITY_REPORTS_ANOTHER
    on_spot = Sensor(id="x", name="X", at=HomeAssistantSensor(entity="light.spot"))
    assert check_sensor(on_spot, SENSORS, LIGHTS) is SetupRefusal.ENTITY_REPORTS_ANOTHER
    assert check_sensor_removal("meter", SENSORS, LIGHTS) is SetupRefusal.SENSOR_REPORTS_A_LIGHT
    assert check_sensor_removal("meter", SENSORS, {"room": ROOM}) is None
    assert check_sensor_removal("gone", SENSORS, LIGHTS) is SetupRefusal.UNKNOWN_SENSOR


def test_a_frames_lighting_names_room_lights_with_their_reference():
    """Mutation probe: check_frame_lighting passes a reference mismatch -> the second assert fails."""
    assert check_frame_lighting(wash("bookshelf-spot", "dimmer-at-full"), LIGHTS) is None
    assert check_frame_lighting(wash("window", "dimmer-at-full"), LIGHTS) is FrameLightingRefusal.REFERENCE_MISMATCH
    assert check_frame_lighting(wash("room", 300.0), LIGHTS) is FrameLightingRefusal.REFERENCE_MISMATCH
    assert check_frame_lighting(wash("window", 300.0), LIGHTS) is None
    assert check_frame_lighting(wash("cellar", "dimmer-at-full"), LIGHTS) is FrameLightingRefusal.UNKNOWN_ROOM_LIGHT


def test_the_document_every_pi_draws_with():
    """Mutation probe: room_lights_document stops mapping FixedReporter to FULL_ON -> `room` reads
    the stored state (not reported here) and the first assert fails."""
    document = room_lights_document(LIGHTS, {"room": NotReported(), "window": Measured(lux=40.0)}, 7)
    assert document.lights["room"].state == FULL_ON
    assert document.lights["bookshelf-spot"].state == NotReported()
    assert document.lights["window"].state == Measured(lux=40.0) and document.change == 7
    assert document.lights["bookshelf-spot"].colour == WHITE


def test_upgrade_lighting_matches_the_panel():
    lighting = upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 2.5)
    assert lighting.mat_depth_mm == 2.5
    assert lighting.lights == (WashLight(light="room", lux=1000.0, reference="dimmer-at-full"),)
    assert lighting.paper.whiteness == 0.9 and lighting.paper.profile is None
    assert math.isclose(lighting.paper.ink_density, -math.log10(0.0003))
