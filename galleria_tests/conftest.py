"""Galleria under Home Assistant's own test harness (pytest-homeassistant-custom-component), with fakes
at the integration's two transport seams: CentralDoor (Central's hub) and read_pi (a Pi's door). The
doors themselves are proved against a real nats-server in test_doors.py."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the repository root holds custom_components/

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.galleria import contract  # noqa: E402
from custom_components.galleria.const import CONF_ORIGIN, DOMAIN  # noqa: E402
from custom_components.galleria.door import CentralDoor, DoorUnavailable  # noqa: E402
from custom_components.galleria.pi import PiReading  # noqa: E402

ORIGIN = "http://central.test:8000"
EPOCH = "0" * 32
LATEST = "v0.35.0"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Iterator[None]:
    yield


def wire(key: str, value: dict[str, Any]) -> bytes:
    """`value` as Central writes it under `key`, checked by the contract's own decoder."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    contract.decode_central_state(key, raw)
    return raw


def central_state(epoch: str = EPOCH) -> dict[str, bytes]:
    """Central with two Pis and three Frames: frame-1 on Pi p1 (generation 3), frame-2 on Pi p2
    (generation 1), frame-3 on none."""
    return {
        contract.CENTRAL_BIRTH_KEY: wire(contract.CENTRAL_BIRTH_KEY, {"component": "central", "version": "0.35.0"}),
        contract.INSTALLATION_KEY: wire(contract.INSTALLATION_KEY, {
            "frames": [
                {"id": "frame-1", "name": "Hall", "driven_by": {"serial": "p1", "output_id": "HDMI-A-1",
                                                                 "generation": 3}},
                {"id": "frame-2", "name": "Stairs", "driven_by": {"serial": "p2", "output_id": "HDMI-A-1",
                                                                   "generation": 1}},
                {"id": "frame-3", "name": "Spare", "driven_by": None}],
            "players": [{"serial": "p1", "name": "Hall Pi", "address": "10.0.0.1"},
                        {"serial": "p2", "name": "Stairs Pi", "address": "10.0.0.2"}]}),
        contract.ROOM_LIGHTS_KEY: wire(contract.ROOM_LIGHTS_KEY, {"change": 2, "lights": [
            {"id": "overheads", "name": "Overheads", "entry": {
                "kind": "reported", "colour": {"kind": "kelvin", "kelvin": 2700},
                "state": {"kind": "dimmer", "on": True, "brightness": 0.5}}},
            {"id": "lamp", "name": "Lamp", "entry": {
                "kind": "reported", "colour": {"kind": "kelvin", "kelvin": 3000}, "state": {"kind": "not-reported"}}}]}),
        contract.RELEASES_KEY: wire(contract.RELEASES_KEY, {"releases": [
            {"version": "v0.36.0-rc.1", "stable": False, "readiness": "ready"},
            {"version": "v0.36.0", "stable": True, "readiness": "downloading"},
            {"version": LATEST, "stable": True, "readiness": "ready"},
            {"version": "v0.34.0", "stable": True, "readiness": "ready"}]}),
        contract.VERSIONS_KEY: wire(contract.VERSIONS_KEY, {"pis": {
            "p1": {"version": "v0.34.0", "chosen": True, "install": None},
            "p2": {"version": "v0.34.0", "chosen": False, "install": None}}}),
        contract.STATES_EPOCH_KEY: wire(contract.STATES_EPOCH_KEY, {"epoch": epoch}),
    }


class FakeDoor:
    """CentralDoor's seam: Central's state from a dict; publishes and installs recorded."""

    def __init__(self) -> None:
        self.state = central_state()
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.installs: list[tuple[str, str]] = []
        self.answer: dict[str, Any] = {"kind": "started", "serial": "p1", "version": LATEST, "install": "downloading"}
        self.connected = True
        self.down = False

    async def read(self, key: str) -> bytes | None:
        if self.down:
            raise DoorUnavailable("down")
        return self.state.get(key)

    async def publish(self, entity_id: str, state: dict[str, Any]) -> None:
        if self.down:
            raise DoorUnavailable("down")
        contract.encode_home_assistant_state(state)   # the contract's own check of what goes on the wire
        self.published.append((entity_id, state))

    async def install_player(self, serial: str, version: str) -> dict[str, Any]:
        self.installs.append((serial, version))
        return self.answer

    async def close(self) -> None:
        pass


def birth(major: int = contract.PLAYER_SCHEMA_MAJOR) -> dict[str, Any]:
    return {"component": "player", "version": "1.0.0", "pipe": "show", "release_digest": "sha256:ab",
            "schema_majors": {"player": major}, "slice_digest": "0" * 64}


def frame_report(frame_id: str, generation: int, showing: bool = True, lux: float | None = 41.0) -> dict[str, Any]:
    return {"frame_id": frame_id, "generation": generation, "output_id": "HDMI-A-1", "showing": showing,
            "light_lux": lux}


class FakePis:
    """read_pi's seam: one reading per serial ("pi_unreachable" when absent)."""

    def __init__(self) -> None:
        self.readings: dict[str, PiReading | str] = {
            "p1": PiReading(base_tag="v0.34.0", light_meter={"reading": {"kind": "measured", "lux": 120.0}},
                            player_birth=birth(), frames=(frame_report("frame-1", 3),)),
            # frame-2's report carries generation 2; Central's binding is generation 1
            "p2": PiReading(base_tag="v0.34.0", light_meter=None, player_birth=birth(),
                            frames=(frame_report("frame-2", 2, showing=True, lux=80.0),)),
        }
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, serial: str, address: str) -> PiReading | str:
        self.calls.append((serial, address))
        return self.readings.get(serial, "pi_unreachable")

    def set(self, serial: str, **changes: Any) -> None:
        self.readings[serial] = replace(self.readings[serial], **changes)


@pytest.fixture
def door() -> FakeDoor:
    return FakeDoor()


@pytest.fixture
def pis() -> FakePis:
    return FakePis()


@pytest.fixture
def seams(door: FakeDoor, pis: FakePis) -> Iterator[None]:
    with (patch.object(CentralDoor, "connect", AsyncMock(return_value=door)),
          patch("custom_components.galleria.coordinator.read_pi", pis)):
        yield


@pytest.fixture
async def entry(hass: Any, seams: None) -> MockConfigEntry:
    """Galleria set up for ORIGIN, every reader read once."""
    config_entry = MockConfigEntry(domain=DOMAIN, unique_id=ORIGIN, data={CONF_ORIGIN: ORIGIN}, title="Galleria")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
