"""The Home Assistant adapter (central/infra/home_assistant/): its references, what a state says as a
room-light word or reading (Home Assistant's 0..255 scales), and what the listener records for each
newest value of Home Assistant's states bucket. No hub here: the path through Central's hub is
tests/integration/test_central_room_light_tracer.py's."""
from __future__ import annotations

import asyncio

import pytest

from central.infra.home_assistant import messages, references
from central.infra.home_assistant.states import HomeAssistantListener
from central.room_light.model import LightStated, ReporterRef, ReportRefusal, SensorRef
from contracts.home_assistant import (
    HomeAssistantIlluminance,
    HomeAssistantKelvin,
    HomeAssistantLight,
    HomeAssistantRgb,
    HomeAssistantUnavailable,
    encode_home_assistant_state,
)
from contracts.room_light import Dimmer, KelvinColour, Measured, RgbColour, Unavailable
from nodeapi.hub import Heard


def test_a_reference_names_an_entity():
    assert references.is_entity("light.spot") and references.is_entity("sensor.hall_lux")
    for other in ("Light.Spot", "light.", "light", "light." + "x" * 255, "light.x y"):
        assert not references.is_entity(other), other
    assert references.light_reporter("light.spot") == ReporterRef(source=references.SOURCE, address="light.spot")
    assert references.illuminance_sensor("sensor.lux") == SensorRef(source=references.SOURCE, address="sensor.lux")


def _light(on=True, brightness=None, colour=None) -> HomeAssistantLight:
    return HomeAssistantLight(name="Spot", on=on, brightness=brightness, colour=colour)


def _stated(on: bool, brightness: float, colour=None) -> LightStated:
    return LightStated(state=Dimmer(on=on, brightness=brightness), colour=colour)


@pytest.mark.parametrize("state, report", [
    (_light(brightness=255), _stated(True, 1.0)),
    (_light(), _stated(True, 1.0)),
    (_light(on=False), _stated(False, 0.0)),
    (_light(brightness=51, colour=HomeAssistantRgb(rgb=(255, 0, 51))), _stated(True, 0.2, RgbColour(rgb=(1.0, 0.0, 0.2)))),
    (_light(brightness=51, colour=HomeAssistantKelvin(kelvin=2700)), _stated(True, 0.2, KelvinColour(kelvin=2700))),
    (HomeAssistantUnavailable(entity_kind="light", name="Spot"), Unavailable()),
])
def test_a_light_state_reads_as_its_report(state, report):
    assert messages.light_report(state) == (report, None)


def test_a_colour_out_of_range_is_refused_and_the_brightness_kept():
    report, refused = messages.light_report(_light(brightness=51, colour=HomeAssistantKelvin(kelvin=50_000)))
    assert report == _stated(True, 0.2)
    assert refused is not None and refused.reason is ReportRefusal.OUT_OF_RANGE


def test_an_illuminance_state_reads_as_its_reading():
    assert messages.reading(HomeAssistantIlluminance(name="Hall", lux=240)) == Measured(lux=240.0)
    assert messages.reading(HomeAssistantIlluminance(name="Hall", lux=None)) == Unavailable()
    assert messages.reading(HomeAssistantUnavailable(entity_kind="illuminance", name="Hall")) == Unavailable()
    with pytest.raises(messages.MessageRefused) as refused:
        messages.reading(HomeAssistantIlluminance(name="Hall", lux=10_000_000))
    assert refused.value.reason is ReportRefusal.OUT_OF_RANGE


class Reports:
    """A RoomLightReports and HeardEntities that keep what they were told."""

    def __init__(self) -> None:
        self.told: list[tuple] = []

    def record_light_state(self, reporter, state):
        self.told.append(("light", reporter, state))
        return "recorded"

    def record_reading(self, sensor, reading):
        self.told.append(("reading", sensor, reading))
        return "recorded"

    def record_refused(self, source, address, reason):
        self.told.append(("refused", source, address, reason))

    def record_heard(self, entity, kind, name):
        self.told.append(("heard", entity, kind, name))


def test_each_state_is_recorded_under_its_kinds_reference_or_refused():
    """Mutation probe: the listener records a light under SensorRef -> this fails."""
    reports = Reports()
    listener = HomeAssistantListener(reports, reports)   # type: ignore[arg-type]
    asyncio.run(listener.heard("e" * 32, (
        Heard("light.spot", encode_home_assistant_state(_light(brightness=255))),
        Heard("sensor.lux", encode_home_assistant_state(HomeAssistantIlluminance(name="Lux", lux=12))),
        Heard("light.gone", encode_home_assistant_state(HomeAssistantUnavailable(entity_kind="light", name="Gone"))),
        Heard("sensor.lux", encode_home_assistant_state(_light())),   # a light state for a sensor
        Heard("light.spot", b'{"state": {"kind": "dim"}}'),
        Heard("Not.An Entity", b"{}"),
        Heard("light.removed", None))))
    source = references.SOURCE
    assert reports.told == [
        ("light", ReporterRef(source=source, address="light.spot"), _stated(True, 1.0)),
        ("heard", "light.spot", "light", "Spot"),
        ("reading", SensorRef(source=source, address="sensor.lux"), Measured(lux=12.0)),
        ("heard", "sensor.lux", "illuminance", "Lux"),
        ("light", ReporterRef(source=source, address="light.gone"), Unavailable()),
        ("heard", "light.gone", "light", "Gone"),
        ("refused", source, "sensor.lux", ReportRefusal.UNREADABLE),
        ("refused", source, "light.spot", ReportRefusal.UNREADABLE),
        ("refused", source, "Not.An Entity", ReportRefusal.UNKNOWN_ADDRESS)]
