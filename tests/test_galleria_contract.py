"""Galleria's generated contract (custom_components/galleria/contract.py) is `contracts`' own: the file
is what scripts/galleria_contract.py writes now, it needs only the stdlib, and each of its codecs
reads what the repository's twin writes (and the reverse), as strictly."""
from __future__ import annotations

import ast
import importlib.util
import sys
from types import ModuleType
from typing import Any

import pytest

from contracts import central_api as c
from contracts import home_assistant as h
from contracts import node_link, node_state
from contracts import room_light as r
from scripts import galleria_contract

CONTRACT = galleria_contract.GENERATED


def _contract() -> ModuleType:
    """contract.py loaded by path: the integration's package imports Home Assistant, which the
    repository's environment does not hold."""
    spec = importlib.util.spec_from_file_location("galleria_contract_under_test", CONTRACT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _contract()

_TAG = "v0.35.0"
_ENTRY = r.ReportedEntry(colour=r.KelvinColour(kelvin=2712.5), state=r.Dimmer(on=True, brightness=0.5), doubt=None)

# One sample per wire value Galleria reads, each under the repository's own encoder.
CENTRAL_STATES: dict[str, Any] = {
    c.BIRTH_KEY: c.CentralBirth(version="0.35.0"),
    c.INSTALLATION_KEY: c.Installation(
        frames=(c.FrameListing(id="frame-1", name="Hall", driven_by=c.DrivenBy(
                    serial="75628d0c", output_id="HDMI-A-1", generation=3)),
                c.FrameListing(id="frame-2", name="Stairs", driven_by=None)),
        players=(c.PlayerListing(serial="75628d0c", name="Hall Pi", address="192.168.1.20"),
                 c.PlayerListing(serial="aa11", name="Spare", address=None))),
    c.LISTENING_KEY: c.Listening(entities=("light.test", "sensor.lux")),
    c.ROOM_LIGHTS_KEY: c.RoomLightsListing(change=4, lights=(
        c.RoomLightListing(id="overheads", name="Overheads", entry=_ENTRY),
        c.RoomLightListing(id="lamp", name="Lamp", entry=r.ReportedEntry(
            colour=r.RgbColour(rgb=(1.0, 0.6, 0.2)), state=r.NotReported(), doubt=None)),
        c.RoomLightListing(id="daylight", name="Daylight", entry=r.MeasuredEntry(
            colour=r.KelvinColour(kelvin=34000.0), state=r.Measured(lux=812.5), doubt=None)),
        c.RoomLightListing(id="sconce", name="Sconce", entry=r.FixedEntry(colour=r.KelvinColour(kelvin=3000))))),
    c.RELEASES_KEY: c.Releases(releases=(
        c.ReleaseListing(version="v0.36.0-rc.1", stable=False, readiness="ready"),
        c.ReleaseListing(version=_TAG, stable=True, readiness="downloading"))),
    c.VERSIONS_KEY: c.Versions(pis={"75628d0c": c.PiVersion(version=_TAG, chosen=True, install="restarting"),
                                    "aa11": c.PiVersion(version=None, chosen=False, install=None)}),
    c.STATES_EPOCH_KEY: c.StatesEpoch(epoch="0123456789abcdef0123456789abcdef"),
}
HOME_ASSISTANT_STATES = (
    h.HomeAssistantLight(name="Test", on=True, brightness=128, colour=h.HomeAssistantKelvin(kelvin=2700)),
    h.HomeAssistantLight(name="Hue", on=True, brightness=40, colour=h.HomeAssistantRgb(rgb=(255, 200, 10))),
    h.HomeAssistantLight(name="Off", on=False, brightness=None, colour=None),
    h.HomeAssistantIlluminance(name="Lux", lux=12.5),
    h.HomeAssistantIlluminance(name="Lux", lux=None),
    h.HomeAssistantColourTemperature(name="Sky", kelvin=4321.5),
    h.HomeAssistantColourTemperature(name="Sky", kelvin=None),
    h.HomeAssistantUnavailable(entity_kind="light", name="Gone"),
    h.HomeAssistantUnavailable(entity_kind="colour-temperature", name="Gone"),
)
BIRTH = node_state.Birth(component="player", version="1.0.0", pipe="show", release_digest="sha256:ab",
                         schema_majors={"player": 1}, slice_digest="0" * 64)
FRAME_REPORTS = node_state.FrameReports(frames=(
    node_state.FrameReport(frame_id="frame-1", generation=3, output_id="HDMI-A-1", showing=True, light_lux=41.0),
    node_state.FrameReport(frame_id="frame-2", generation=1, output_id="HDMI-A-2", showing=False, light_lux=None)))
ANSWERS = (c.InstallStarted(serial="75628d0c", version=_TAG, install="downloading"),
           c.InstallRefused(refusal=c.InstallRefusal.UNKNOWN_VERSION))


def _fields(name: str) -> set[str]:
    return set(getattr(contract, name).__annotations__)


def test_the_committed_contract_is_what_the_generator_writes() -> None:
    assert galleria_contract.main(["--check"]) == 0


def test_the_contract_imports_only_the_stdlib() -> None:
    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported - {"__future__"} <= set(sys.stdlib_module_names)


@pytest.mark.parametrize("serial", ["75628d0c", "aa11", "A:b_c.d-e", "x" * 128])
def test_bus_server_name_is_the_node_links(serial: str) -> None:
    assert contract.bus_server_name(serial) == node_link.bus_server_name(serial)


@pytest.mark.parametrize("serial", ["", "a b", "x" * 129, "é"])
def test_bus_server_name_refuses_what_the_node_link_refuses(serial: str) -> None:
    with pytest.raises(ValueError):
        node_link.bus_server_name(serial)
    with pytest.raises(ValueError, match="node_link_serial"):
        contract.bus_server_name(serial)


@pytest.mark.parametrize("key", sorted(CENTRAL_STATES))
def test_central_state_round_trips(key: str) -> None:
    value = CENTRAL_STATES[key]
    decoded = contract.decode_central_state(key, c.encode_central_state(key, value))
    assert decoded == value.model_dump(mode="json")
    assert set(decoded) == _fields(type(value).__name__)


@pytest.mark.parametrize("state", HOME_ASSISTANT_STATES, ids=lambda state: f"{state.kind}")
def test_home_assistant_state_round_trips_both_ways(state: Any) -> None:
    wire = state.model_dump(mode="json")
    assert set(wire) == _fields(type(state).__name__)
    assert contract.decode_home_assistant_state(h.encode_home_assistant_state(state)) == wire
    assert contract.encode_home_assistant_state(wire) == h.encode_home_assistant_state(state)


def test_pi_reads_round_trip() -> None:
    reading = r.LightSensorReading(reading=r.Measured(lux=3.5))
    assert contract.decode_birth(node_state.encode_birth(BIRTH)) == BIRTH.model_dump(mode="json")
    assert contract.decode_host_base(node_state.encode_host_base(node_state.HostBase(base_tag=_TAG))) == {
        "base_tag": _TAG}
    assert contract.decode_frame_reports(node_state.encode_frame_reports(FRAME_REPORTS)) == (
        FRAME_REPORTS.model_dump(mode="json"))
    assert contract.decode_light_sensor(r.encode_light_sensor(reading)) == reading.model_dump(mode="json")
    unavailable = r.LightSensorReading(reading=r.Unavailable())
    assert contract.decode_light_sensor(r.encode_light_sensor(unavailable)) == {"reading": {"kind": "unavailable"}}
    assert set(BIRTH.model_dump()) == _fields("Birth")
    assert set(FRAME_REPORTS.frames[0].model_dump()) == _fields("FrameReport")


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda answer: answer.kind)
def test_install_round_trips_both_ways(answer: Any) -> None:
    assert contract.decode_install_answer(c.encode_install_answer(answer)) == answer.model_dump(mode="json")
    request = {"serial": "75628d0c", "version": _TAG}
    assert c.decode_install_player(contract.encode_install_player(request)) == c.InstallPlayer(**request)
    assert contract.INSTALL_REFUSALS == tuple(refusal.value for refusal in c.InstallRefusal)


@pytest.mark.parametrize("raw", [
    b'{"state":{"kind":"light","name":"x","on":1,"brightness":null,"colour":null}}',               # int for bool
    b'{"state":{"kind":"light","name":"x","on":true,"brightness":true,"colour":null}}',            # bool for int
    b'{"state":{"kind":"light","name":"x","on":true,"brightness":256,"colour":null}}',             # past LEVEL_MAX
    b'{"state":{"kind":"light","name":"x","on":true,"brightness":1,"colour":{"kind":"rgb","rgb":[1,2]}}}',  # short rgb
    b'{"state":{"kind":"light","name":"x","on":true,"brightness":1,"colour":{"kind":"kelvin","kelvin":0}}}',  # not > 0
    b'{"state":{"kind":"colour-temperature","name":"x","kelvin":-1}}',                             # not > 0
    b'{"state":{"kind":"illuminance","name":"x","lux":1,"extra":1}}',                               # unknown field
    b'{"state":{"kind":"illuminance","name":"x"}}',                                                 # missing field
    b'{"state":{"kind":"illuminance","name":"","lux":1}}',                                          # empty name
    b'{"state":{"kind":"illuminance","name":"x","lux":NaN}}',                                       # NaN
    b'{"state":{"kind":"unavailable","entity_kind":"switch","name":"x"}}',                          # outside the enum
    b'{"state":{"kind":"illuminance","name":"x","lux":1},"state":{"kind":"illuminance","name":"y","lux":1}}',
])
def test_the_copy_refuses_what_the_contract_refuses(raw: bytes) -> None:
    with pytest.raises(ValueError):
        h.decode_home_assistant_state(raw)
    with pytest.raises(ValueError, match="home_assistant_state"):
        contract.decode_home_assistant_state(raw)


def test_a_serial_key_past_its_pattern_and_an_unknown_state_key_are_refused() -> None:
    raw = b'{"pis":{"a b":{"version":null,"chosen":false,"install":null}}}'
    with pytest.raises(ValueError):
        c.decode_central_state(c.VERSIONS_KEY, raw)
    with pytest.raises(ValueError, match="central_state"):
        contract.decode_central_state(c.VERSIONS_KEY, raw)
    with pytest.raises(ValueError, match="central_state"):
        contract.decode_central_state("not-a-key", b"{}")
