"""Central's errors, pure (central/central_errors/compose.py), and what each source's adapter says to
do (central/infra/home_assistant/advice.py, central/infra/light_sensor_judge.py `PiMeterAdvice`):
which references are in doubt and which Frames they reach, which side to update, what a Frame's
Player said, and Central's real release (central/version.py). The stores and routes over PostgreSQL
are tests/test_central_errors_store.py's."""
from __future__ import annotations

import pytest

from central.central_errors.compose import impediment, in_doubt
from central.central_errors.views import (
    FrameClear,
    FrameImpeded,
    FrameNotReported,
    SourceAttentionView,
)
from central.infra.home_assistant.advice import HomeAssistantAdvice, galleria_advice
from central.infra.home_assistant.settings import galleria_view
from central.infra.light_sensor_judge import PI_SOURCE, PiMeterAdvice
from central.room_light.model import (
    BulbColour,
    ColourFacts,
    ColourSensor,
    ColourSensorRef,
    Doubt,
    DoubtReason,
    Facts,
    FixedLight,
    LightFacts,
    MeasuredLight,
    ReadingFacts,
    ReportedLight,
    ReporterRef,
    SensedColour,
    Sensor,
    SensorRef,
    TypedColour,
)
from central.room_light.views import DoubtView
from central.version import RELEASE_ENV, central_version
from contracts.home_assistant import GalleriaBirth, GalleriaConnection
from contracts.node_impeded import Impeded, ImpededFrames
from contracts.room_light import CentralWords

HA = "home-assistant"
FAMILY = ReporterRef(source=HA, address="light.family_room")
LAMP = ReporterRef(source=HA, address="light.lamp")
METER = SensorRef(source=PI_SOURCE, address="p-hall")
SKY = ColourSensorRef(source=HA, address="sensor.sky")
LIGHTS = {"family": ReportedLight(name="Family Room Lights", reporter=FAMILY, colour=BulbColour(typed_kelvin=2700)),
          "lamp": ReportedLight(name="Lamp", reporter=LAMP, colour=TypedColour(kelvin=2700)),
          "window": MeasuredLight(name="Window", sensor="meter", colour=SensedColour(sensor="sky", typed_kelvin=5500)),
          "room": FixedLight(name="Room", colour=SensedColour(sensor="sky", typed_kelvin=5500))}
SENSORS = {"meter": Sensor(name="Hall meter", at=METER)}
COLOUR_SENSORS = {"sky": ColourSensor(name="Sky", at=SKY)}
UNREAD = Doubt(DoubtReason.CANT_READ, 100.0)


def _birth(version: str) -> GalleriaBirth:
    return GalleriaBirth(version=version, kinds=("light",), connection=GalleriaConnection(server="NHUB", client=1))


class Advice:
    def __init__(self, source: str) -> None:
        self.source = source

    def what_to_do(self, reference, reason) -> str:
        assert reference.source == self.source   # dispatched by source
        return f"{reason.value} {reference.address}"


def test_every_named_reference_in_doubt_with_its_lights_frames_and_what_to_do():
    """Mutation probe: dropping the colour sensor's lights -> "sky" reaches no Frame."""
    facts = Facts(lights={FAMILY: LightFacts(state=None, colour=None, doubt=UNREAD, heard_at=100.0),
                          LAMP: LightFacts(state=None, colour=None, doubt=None, heard_at=1.0),
                          ReporterRef(source=HA, address="light.unnamed"): LightFacts(
                              state=None, colour=None, doubt=UNREAD, heard_at=100.0)},
                  readings={METER: ReadingFacts(reading=None, doubt=Doubt(DoubtReason.CANT_TELL, 50.0), heard_at=50.0)},
                  colours={SKY: ColourFacts(colour=None, doubt=Doubt(DoubtReason.LINK_CLOSED, 70.0), heard_at=1.0)})
    naming = {"family": ("hall", "stairs"), "window": ("hall",), "room": ("study",)}
    view = in_doubt(LIGHTS, SENSORS, COLOUR_SENSORS, facts, naming, {HA: Advice(HA), PI_SOURCE: Advice(PI_SOURCE)})
    assert view.in_doubt == (
        SourceAttentionView(source=HA, address="light.family_room", doubt=DoubtView(reason="cant-read", since=100.0),
                            lights=("family",), frames=("hall", "stairs"), what_to_do="cant-read light.family_room"),
        SourceAttentionView(source=PI_SOURCE, address="p-hall", doubt=DoubtView(reason="cant-tell", since=50.0),
                            lights=("window",), frames=("hall",), what_to_do="cant-tell p-hall"),
        SourceAttentionView(source=HA, address="sensor.sky", doubt=DoubtView(reason="link-closed", since=70.0),
                            lights=("window", "room"), frames=("hall", "study"), what_to_do="link-closed sensor.sky"))
    assert in_doubt(LIGHTS, SENSORS, COLOUR_SENSORS, Facts(lights={}, readings={}, colours={}), naming, {}).in_doubt == ()


@pytest.mark.parametrize("birth, central, advice", [
    (None, "0.45.0", "update-galleria"),
    (_birth("0.40.1"), "0.45.0", "update-galleria"),
    (_birth("0.45.0"), "0.45.0", "matched"),
    (_birth("v0.45.0"), "0.45.0", "matched"),
    (_birth("0.46.0"), "0.45.0", "update-central"),
    (_birth("0.45.0-rc.1"), "0.45.0", "update-galleria"),
    (_birth("not a version"), "0.45.0", "update-galleria"),
])
def test_which_side_to_update(birth, central, advice):
    assert galleria_advice(birth, central) == advice


def test_settings_asks_to_update_galleria_by_the_same_rule():
    """The kinds test is retired: a Galleria publishing every kind but older than Central is asked to update."""
    assert galleria_view(None, "0.45.0").update is True
    assert galleria_view((_birth("0.40.1"), 1.0), "0.45.0").update is True
    assert galleria_view((_birth("0.45.0"), 1.0), "0.45.0").update is False
    assert galleria_view((_birth("0.46.0"), 1.0), "0.45.0").update is False


def test_what_to_do_about_a_home_assistant_reference():
    births: list[GalleriaBirth | None] = [None]
    advice = HomeAssistantAdvice(lambda: births[0], "0.45.0")
    assert advice.what_to_do(FAMILY, DoubtReason.CANT_READ) == (
        "Update Galleria in Home Assistant (HACS) to its newest release")
    births[0] = _birth("0.46.0")
    assert advice.what_to_do(FAMILY, DoubtReason.CANT_READ) == "Update Photo Wall (Central)"
    births[0] = _birth("0.45.0")
    assert advice.what_to_do(FAMILY, DoubtReason.CANT_READ) == "Photo Wall can't read this Galleria: report a bug"
    assert advice.what_to_do(FAMILY, DoubtReason.CANT_TELL) == "Check light.family_room in Home Assistant"
    assert advice.what_to_do(FAMILY, DoubtReason.LINK_CLOSED) == "Check Home Assistant and its Galleria integration"


def test_what_to_do_about_a_pi_s_meter():
    assert PiMeterAdvice().what_to_do(METER, DoubtReason.CANT_READ) == "Install the current release on Player p-hall"
    assert PiMeterAdvice().what_to_do(METER, DoubtReason.CANT_TELL) == "Check the light meter on Player p-hall"


WORDS = CentralWords(line1="Photo Wall can't read Family Room Lights · since 29 Oct 18:05",
                     line2="Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall")
HALL = Impeded(frame_id="hall", output_id="HDMI-A-1", grant_id="0" * 8 + "-0000-4000-8000-" + "0" * 12,
               lights=("family",), words=WORDS)


def test_what_a_frame_s_player_said():
    assert impediment("hall", "HDMI-A-1", None) == FrameNotReported()
    assert impediment("hall", "HDMI-A-1", ImpededFrames(frames=())) == FrameClear()
    assert impediment("hall", "HDMI-A-1", ImpededFrames(frames=(HALL,))) == FrameImpeded(by=HALL)
    assert impediment("stairs", "HDMI-A-1", ImpededFrames(frames=(HALL,))) == FrameClear()
    assert impediment("hall", "HDMI-A-2", ImpededFrames(frames=(HALL,))) == FrameClear()   # another Output's


def test_central_reports_the_release_it_was_built_as(monkeypatch):
    monkeypatch.setenv(RELEASE_ENV, "v0.45.0")
    assert central_version() == "0.45.0"
    monkeypatch.setenv(RELEASE_ENV, "0.45.0")   # no tag: refused, never a hand-written number
    with pytest.raises(ValueError):
        central_version()
