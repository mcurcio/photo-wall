"""The Home Assistant adapter (central/infra/home_assistant/): its topics and the shipped automation
that publishes them, what a payload says as a room-light state or reading, the link's health from
its heartbeat's age, and what the link does with each message. No broker here: the end-to-end path
through a real broker is the e2e lane's."""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import pytest

from central.infra.home_assistant import health, messages, topics
from central.infra.home_assistant.link import HomeAssistantLink, build_home_assistant_link
from central.room_light.model import ReporterRef, ReportRefusal, SensorRef
from contracts.room_light import ColourDimmer, Dimmer, Measured, Unavailable

YAML = Path(__file__).parents[1] / "integrations/home-assistant/photo-wall-room-lights.yaml"


def test_the_shipped_automation_publishes_the_adapters_topics():
    """Mutation probe M9: LIGHT_TOPIC_PREFIX changed in topics.py only -> the automation's topics differ."""
    published = re.findall(r'topic:\s*"([^"{]*)\{\{\s*repeat\.item\s*\}\}"', YAML.read_text())
    assert published == [topics.LIGHT_TOPIC_PREFIX, topics.ILLUMINANCE_TOPIC_PREFIX]
    assert all(prefix.startswith(topics.SUBSCRIPTION.removesuffix("#")) for prefix in published)


def test_a_topic_names_an_entity_of_its_kind():
    assert topics.topic_of("photowall/light/light.spot") == topics.Topic("light", "light.spot")
    assert topics.topic_of("photowall/illuminance/sensor.hall_lux") == topics.Topic("illuminance", "sensor.hall_lux")
    for other in ("photowall/light/Light.Spot", "photowall/light/", "photowall/other/light.x", "light.x",
                  "photowall/light/light." + "x" * 255):
        assert topics.topic_of(other) is None
    assert topics.is_entity("light.spot") and not topics.is_entity("Not An Entity")
    assert topics.light_reporter("light.spot") == ReporterRef(source=topics.SOURCE, address="light.spot")
    assert topics.illuminance_sensor("sensor.lux") == SensorRef(source=topics.SOURCE, address="sensor.lux")


@pytest.mark.parametrize("payload, state", [
    (b'{"state": "on", "brightness": 255}', Dimmer(on=True, brightness=1.0)),
    (b'{"state": "on"}', Dimmer(on=True, brightness=1.0)),
    (b'{"state": "off", "brightness": null, "rgb": null}', Dimmer(on=False, brightness=0.0)),
    (b'{"state": "on", "brightness": 51, "rgb": [255, 0, 51], "extra": 1}',
     ColourDimmer(on=True, brightness=0.2, rgb=(1.0, 0.0, 0.2))),
    (b'{"state": "unknown"}', Unavailable()),
    (b'{"state": "unavailable"}', Unavailable()),
])
def test_a_light_payload_reads_as_its_state(payload, state):
    assert messages.light_state(payload) == state


@pytest.mark.parametrize("payload, reason", [
    (b'{"state": "dim"}', ReportRefusal.UNREADABLE), (b"not json", ReportRefusal.UNREADABLE),
    (b'{"state": "on", "brightness": 256}', ReportRefusal.OUT_OF_RANGE),
    (b'{"state": "on", "brightness": true}', ReportRefusal.OUT_OF_RANGE),
    (b'{"state": "on", "rgb": [1, 2]}', ReportRefusal.OUT_OF_RANGE),
    (b'{"state": "on", "pad": "' + b"x" * 1024 + b'"}', ReportRefusal.UNREADABLE),
])
def test_a_light_payload_refused(payload, reason):
    with pytest.raises(messages.MessageRefused) as refused:
        messages.light_state(payload)
    assert refused.value.reason is reason


def test_an_illuminance_payload():
    assert messages.illuminance(b'{"lux": 240}') == Measured(lux=240.0)
    assert messages.illuminance(b'{"state": "unknown"}') == Unavailable()
    for payload, reason in ((b'{"lux": -1}', ReportRefusal.OUT_OF_RANGE), (b'{"lux": "1"}', ReportRefusal.UNREADABLE),
                            (b'{"state": "on"}', ReportRefusal.UNREADABLE)):
        with pytest.raises(messages.MessageRefused) as refused:
            messages.illuminance(payload)
        assert refused.value.reason is reason


def test_the_links_health_is_judged_by_its_beats_age():
    """Mutation probe M5: judged ignoring the age -> an old beat reads connected."""
    assert health.judged(None, None, None).state == "never-run"
    assert health.judged("connected", 100.0, 1.0) == health.LinkHealthView(state="connected", since=100.0)
    assert health.judged("connected", 100.0, health.LEASE_SECONDS + 1).state == "away"
    assert health.judged("not-configured", 5.0, 0.0).state == "not-configured"


class Reports:
    """A RoomLightReports that keeps what it was told."""

    def __init__(self, answer: str = "recorded") -> None:
        self.answer, self.told = answer, []

    def record_light_state(self, reporter, state):
        self.told.append(("light", reporter, state))
        return self.answer

    def record_reading(self, sensor, reading):
        self.told.append(("reading", sensor, reading))
        return self.answer

    def record_refused(self, source, address, reason):
        self.told.append(("refused", source, address, reason))


class Health:
    def __init__(self) -> None:
        self.beats: list[str] = []
        self.first = threading.Event()

    def beat(self, beat: str) -> None:
        self.beats.append(beat)
        self.first.set()


def test_each_message_is_recorded_or_refused_and_a_change_wakes_the_writer():
    reports, woken = Reports(), []
    link = HomeAssistantLink(None, reports, Health(), lambda: woken.append(1))
    link.record("photowall/light/light.spot", b'{"state": "on", "brightness": 255}')
    link.record("photowall/illuminance/sensor.lux", b'{"lux": 12}')
    link.record("photowall/light/light.spot", b'{"state": "dim"}')
    link.record("photowall/other/x", b"{}")
    assert reports.told == [
        ("light", ReporterRef(source="home-assistant", address="light.spot"), Dimmer(on=True, brightness=1.0)),
        ("reading", SensorRef(source="home-assistant", address="sensor.lux"), Measured(lux=12.0)),
        ("refused", "home-assistant", "photowall/light/light.spot", ReportRefusal.UNREADABLE),
        ("refused", "home-assistant", "photowall/other/x", ReportRefusal.UNKNOWN_ADDRESS)]
    assert woken == [1, 1]
    quiet = Reports("unchanged")
    HomeAssistantLink(None, quiet, Health(), lambda: woken.append(2)).record("photowall/light/light.a", b'{"state": "off"}')
    assert woken == [1, 1]


def test_without_a_broker_the_link_beats_not_configured_until_stopped():
    beats = Health()
    link = build_home_assistant_link({}, Reports(), beats, lambda: None)   # type: ignore[arg-type]

    async def run() -> None:
        stop = asyncio.Event()
        running = asyncio.create_task(link.run(stop))
        assert await asyncio.to_thread(beats.first.wait, 5)
        stop.set()
        await asyncio.wait_for(running, 5)

    asyncio.run(run())
    assert beats.beats == ["not-configured"]
