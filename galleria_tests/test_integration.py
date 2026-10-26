"""Galleria in Home Assistant, at its transport seams (conftest.py): the devices Central's state names,
what it publishes, Install, and the Frames read from the Pi Central names."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import EPOCH, LATEST, ORIGIN, FakeDoor, FakePis, birth, wire
from homeassistant import config_entries
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.galleria import contract
from custom_components.galleria.const import CONF_ORIGIN, DOMAIN
from custom_components.galleria.door import CentralDoor, DoorUnavailable


async def settle(hass: HomeAssistant) -> None:
    """Every tracked task done, and the publisher's queue (a background task) drained."""
    await hass.async_block_till_done()
    for _ in range(5):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found is not None, unique_id
    return found


def state_of(hass: HomeAssistant, platform: str, unique_id: str) -> Any:
    return hass.states.get(entity_id(hass, platform, unique_id))


async def read_all(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    runtime = entry.runtime_data
    await runtime.central.async_refresh()
    for reader in runtime.pis.values():
        await reader.async_refresh()
    await settle(hass)


# ---- I1: the devices ----

async def test_an_entry_creates_central_the_installation_a_player_per_pi_and_a_frame_per_frame(
        hass: HomeAssistant, entry: MockConfigEntry) -> None:
    devices = dr.async_get(hass)
    central = devices.async_get_device(identifiers={(DOMAIN, f"central:{ORIGIN}")})
    installation = devices.async_get_device(identifiers={(DOMAIN, f"installation:{ORIGIN}")})
    assert central is not None and installation is not None
    for identifier in ("player:p1", "player:p2", "frame:frame-1", "frame:frame-2", "frame:frame-3"):
        device = devices.async_get_device(identifiers={(DOMAIN, identifier)})
        assert device is not None, identifier
        assert device.via_device_id == installation.id
    assert devices.async_get_device(identifiers={(DOMAIN, "player:p1")}).name == "Hall Pi"
    assert state_of(hass, "sensor", f"galleria_{ORIGIN}_central_version").state == "0.35.0"
    assert state_of(hass, "binary_sensor", f"galleria_{ORIGIN}_hub_connected").state == STATE_ON
    overheads = state_of(hass, "sensor", f"galleria_{ORIGIN}_room_light_overheads")
    assert (overheads.state, overheads.attributes["level"], overheads.attributes["kelvin"]) == ("on", 0.5, 2700)
    assert state_of(hass, "sensor", f"galleria_{ORIGIN}_room_light_lamp").state == "not_reported"
    # a light meter only on the Pi whose host writes one
    assert float(state_of(hass, "sensor", "galleria_player_p1_light_meter").state) == 120.0
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "galleria_player_p2_light_meter") is None


async def test_a_pi_central_lists_later_gets_its_devices(hass: HomeAssistant, entry: MockConfigEntry,
                                                         door: FakeDoor, pis: FakePis) -> None:
    door.state[contract.INSTALLATION_KEY] = wire(contract.INSTALLATION_KEY, {
        "frames": [{"id": "frame-9", "name": "New", "driven_by": {"serial": "p9", "output_id": "HDMI-A-2",
                                                                   "generation": 1}}],
        "players": [{"serial": "p9", "name": "New Pi", "address": "10.0.0.9"}]})
    pis.readings["p9"] = pis.readings["p1"]
    await read_all(hass, entry)
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, "player:p9")}) is not None
    assert devices.async_get_device(identifiers={(DOMAIN, "frame:frame-9")}) is not None
    assert ("p9", "10.0.0.9") in pis.calls


# ---- I2: what Galleria publishes ----

async def test_a_light_turned_on_is_published_under_its_entity_id(hass: HomeAssistant, entry: MockConfigEntry,
                                                                   door: FakeDoor) -> None:
    hass.states.async_set("light.test", STATE_ON, {"brightness": 128, "color_mode": "color_temp",
                                                   "color_temp_kelvin": 2700, "rgb_color": (255, 180, 100),
                                                   "friendly_name": "Test"})
    hass.states.async_set("sensor.lux", "12.5", {"device_class": "illuminance", "friendly_name": "Lux"})
    hass.states.async_set("sensor.temperature", "21", {"device_class": "temperature"})
    hass.states.async_set("switch.fan", STATE_ON)
    await settle(hass)
    assert ("light.test", {"kind": "light", "name": "Test", "on": True, "brightness": 128,
                           "colour": {"kind": "kelvin", "kelvin": 2700.0}}) in door.published
    assert ("sensor.lux", {"kind": "illuminance", "name": "Lux", "lux": 12.5}) in door.published
    published = {entity for entity, _ in door.published}
    assert "sensor.temperature" not in published and "switch.fan" not in published

    door.published.clear()
    hass.states.async_set("light.test", STATE_OFF, {"friendly_name": "Test"})
    hass.states.async_set("sensor.lux", STATE_UNAVAILABLE, {"device_class": "illuminance", "friendly_name": "Lux"})
    await settle(hass)
    assert door.published == [
        ("light.test", {"kind": "light", "name": "Test", "on": False, "brightness": None, "colour": None}),
        ("sensor.lux", {"kind": "unavailable", "entity_kind": "illuminance", "name": "Lux"})]


async def test_the_sun_s_sky_colour_temperature_is_published_as_a_colour_temperature(
        hass: HomeAssistant, entry: MockConfigEntry, door: FakeDoor) -> None:
    """Home Assistant's real shape: a template sensor in K with no device class and a measurement
    state class. Its kelvin goes as stated (never bounded or rounded); a temperature in K is not one."""
    sky = {"unit_of_measurement": "K", "state_class": "measurement", "friendly_name": "Sun Sky color temperature"}
    hass.states.async_set("sensor.sun_sky_color_temperature", "4321.5", sky)
    hass.states.async_set("sensor.hot", "45000", {"unit_of_measurement": "K", "friendly_name": "Hot"})
    hass.states.async_set("sensor.kiln", "1500", {"unit_of_measurement": "K", "device_class": "temperature"})
    await settle(hass)
    assert ("sensor.sun_sky_color_temperature", {"kind": "colour-temperature", "name": "Sun Sky color temperature",
                                                 "kelvin": 4321.5}) in door.published
    assert ("sensor.hot", {"kind": "colour-temperature", "name": "Hot", "kelvin": 45000.0}) in door.published
    assert "sensor.kiln" not in {entity for entity, _ in door.published}
    door.published.clear()
    hass.states.async_set("sensor.sun_sky_color_temperature", "not a number", sky)
    hass.states.async_set("sensor.hot", "-3", {"unit_of_measurement": "K", "friendly_name": "Hot"})
    await settle(hass)
    hass.states.async_set("sensor.sun_sky_color_temperature", STATE_UNAVAILABLE, sky)
    await settle(hass)
    assert door.published == [
        ("sensor.sun_sky_color_temperature", {"kind": "colour-temperature", "name": "Sun Sky color temperature",
                                              "kelvin": None}),
        ("sensor.hot", {"kind": "colour-temperature", "name": "Hot", "kelvin": None}),
        ("sensor.sun_sky_color_temperature", {"kind": "unavailable", "entity_kind": "colour-temperature",
                                              "name": "Sun Sky color temperature"})]


LIFX = {"supported_color_modes": ["color_temp", "hs"], "min_color_temp_kelvin": 1500,
        "max_color_temp_kelvin": 9000, "friendly_name": "Window lamp"}


async def test_a_bulb_states_its_colour_by_its_colour_mode(hass: HomeAssistant, entry: MockConfigEntry,
                                                           door: FakeDoor) -> None:
    """A LIFX bulb fills every colour attribute; Galleria states only its mode's: kelvin in color_temp
    (the rgb Home Assistant works out is left), rgb in hs, none when off."""
    hass.states.async_set("light.fam_window", STATE_ON, {
        **LIFX, "color_mode": "color_temp", "brightness": 255, "color_temp_kelvin": 3500,
        "hs_color": (27.0, 47.0), "rgb_color": (255, 196, 135), "xy_color": (0.43, 0.38)})
    await settle(hass)
    hass.states.async_set("light.fam_window", STATE_ON, {
        **LIFX, "color_mode": "hs", "brightness": 255, "color_temp_kelvin": None,
        "hs_color": (240.0, 100.0), "rgb_color": (0, 0, 255), "xy_color": (0.136, 0.04)})
    await settle(hass)
    hass.states.async_set("light.fam_window", STATE_OFF, {
        **LIFX, "color_mode": None, "brightness": None, "color_temp_kelvin": None, "hs_color": None,
        "rgb_color": None, "xy_color": None})
    await settle(hass)
    assert [state for entity, state in door.published if entity == "light.fam_window"] == [
        {"kind": "light", "name": "Window lamp", "on": True, "brightness": 255,
         "colour": {"kind": "kelvin", "kelvin": 3500.0}},
        {"kind": "light", "name": "Window lamp", "on": True, "brightness": 255, "colour": {"kind": "rgb", "rgb": [0, 0, 255]}},
        {"kind": "light", "name": "Window lamp", "on": False, "brightness": None, "colour": None}]


async def test_a_bulb_not_in_a_colour_mode_or_off_states_no_colour(hass: HomeAssistant, entry: MockConfigEntry,
                                                                   door: FakeDoor) -> None:
    """Only color_temp states a kelvin and only a colour mode an rgb, whatever else Home Assistant
    fills: brightness and onoff modes state none, and so does an off bulb that still names a mode.
    A colour mode's rgb goes as Home Assistant states it, all zeros too: Central judges its range."""
    filled = {"color_temp_kelvin": 3500, "hs_color": (27.0, 47.0), "rgb_color": (255, 196, 135),
              "xy_color": (0.43, 0.38)}
    for brightness, mode in ((100, "brightness"), (120, "onoff")):
        hass.states.async_set("light.fam_window", STATE_ON, {**LIFX, "color_mode": mode, "brightness": brightness,
                                                            **filled})
        await settle(hass)
    hass.states.async_set("light.fam_window", STATE_OFF, {**LIFX, "color_mode": "color_temp", "brightness": None,
                                                         **filled})
    await settle(hass)
    hass.states.async_set("light.fam_window", STATE_ON, {**LIFX, "color_mode": "rgb", "brightness": 50,
                                                        **filled, "rgb_color": (0, 0, 0)})
    await settle(hass)
    assert [state for entity, state in door.published if entity == "light.fam_window"] == [
        {"kind": "light", "name": "Window lamp", "on": True, "brightness": 100, "colour": None},
        {"kind": "light", "name": "Window lamp", "on": True, "brightness": 120, "colour": None},
        {"kind": "light", "name": "Window lamp", "on": False, "brightness": None, "colour": None},
        {"kind": "light", "name": "Window lamp", "on": True, "brightness": 50, "colour": {"kind": "rgb", "rgb": [0, 0, 0]}}]


async def test_a_birth_the_hub_did_not_take_publishes_nothing_until_the_next_read(
        hass: HomeAssistant, entry: MockConfigEntry, door: FakeDoor) -> None:
    """Galleria's birth goes first; when the hub does not take it, no state goes into that epoch and
    none is kept as published, so the next read publishes the birth and every state again."""
    hass.states.async_set("light.test", STATE_ON, {"brightness": 10, "friendly_name": "Test"})
    await settle(hass)
    door.published.clear()
    door.births.clear()
    door.state[contract.STATES_EPOCH_KEY] = wire(contract.STATES_EPOCH_KEY, {"epoch": "f" * 32})
    door.births_refused = 1
    await read_all(hass, entry)
    assert (door.published, door.births) == ([], [])
    assert entry.runtime_data.publisher.published_epoch is None
    await read_all(hass, entry)
    assert len(door.births) == 1 and [entity for entity, _ in door.published] == ["light.test"]
    assert entry.runtime_data.publisher.published_epoch == "f" * 32


async def test_galleria_s_birth_goes_with_every_publish_of_all_states(hass: HomeAssistant, entry: MockConfigEntry,
                                                                      door: FakeDoor) -> None:
    """On start and with every publish of all states: its manifest's version and every kind it publishes."""
    manifest = (await async_get_integration(hass, DOMAIN)).version
    expected = {"component": "galleria", "version": str(manifest),
                "kinds": ["light", "illuminance", "colour-temperature"]}
    assert door.births == [expected]
    door.state[contract.STATES_EPOCH_KEY] = wire(contract.STATES_EPOCH_KEY, {"epoch": "e" * 32})
    await read_all(hass, entry)
    assert door.births == [expected, expected]


async def test_galleria_s_own_entities_are_never_published(hass: HomeAssistant, entry: MockConfigEntry,
                                                            door: FakeDoor, pis: FakePis) -> None:
    pis.set("p1", light_meter={"reading": {"kind": "measured", "lux": 300.0}},
            frames=(contract_report := {"frame_id": "frame-1", "generation": 3, "output_id": "HDMI-A-1",
                                        "showing": True, "light_lux": 99.0},))
    await read_all(hass, entry)
    assert float(state_of(hass, "sensor", "galleria_player_p1_light_meter").state) == 300.0
    assert float(state_of(hass, "sensor", "galleria_frame_frame-1_light").state) == contract_report["light_lux"]
    door.state[contract.STATES_EPOCH_KEY] = wire(contract.STATES_EPOCH_KEY, {"epoch": "1" * 32})
    await read_all(hass, entry)   # publish_all too
    registry = er.async_get(hass)
    own = {entity.entity_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert own, "Galleria made entities"
    assert not own & {entity for entity, _ in door.published}


async def test_a_new_states_epoch_publishes_every_state_again(hass: HomeAssistant, entry: MockConfigEntry,
                                                              door: FakeDoor) -> None:
    hass.states.async_set("light.test", STATE_ON, {"brightness": 10, "friendly_name": "Test"})
    hass.states.async_set("sensor.lux", "3", {"device_class": "illuminance", "friendly_name": "Lux"})
    await settle(hass)
    door.published.clear()
    await read_all(hass, entry)
    assert door.published == [], "the same epoch publishes nothing again"
    door.state[contract.STATES_EPOCH_KEY] = wire(contract.STATES_EPOCH_KEY, {"epoch": "f" * 32})
    await read_all(hass, entry)
    assert {entity for entity, _ in door.published} == {"light.test", "sensor.lux"}
    assert entry.runtime_data.publisher.published_epoch == "f" * 32


async def test_a_publish_the_hub_did_not_take_publishes_every_state_at_the_next_read(
        hass: HomeAssistant, entry: MockConfigEntry, door: FakeDoor) -> None:
    hass.states.async_set("light.test", STATE_ON, {"brightness": 10, "friendly_name": "Test"})
    await settle(hass)
    door.down = True
    hass.states.async_set("light.test", STATE_ON, {"brightness": 200, "friendly_name": "Test"})
    await settle(hass)
    assert entry.runtime_data.publisher.published_epoch is None
    door.down = False
    door.published.clear()
    await read_all(hass, entry)
    assert ("light.test", {"kind": "light", "name": "Test", "on": True, "brightness": 200,
                           "colour": None}) in door.published
    assert entry.runtime_data.publisher.published_epoch == EPOCH


# ---- I3: Install ----

async def test_install_asks_central_for_the_latest_release_on_that_pi_only(
        hass: HomeAssistant, entry: MockConfigEntry, door: FakeDoor) -> None:
    update = state_of(hass, "update", "galleria_player_p1_update")
    assert (update.attributes["installed_version"], update.attributes["latest_version"]) == ("v0.34.0", LATEST)
    await hass.services.async_call("update", "install", {"entity_id": update.entity_id}, blocking=True)
    assert door.installs == [("p1", LATEST)]


async def test_a_refused_install_names_the_refusal(hass: HomeAssistant, entry: MockConfigEntry,
                                                   door: FakeDoor) -> None:
    door.answer = {"kind": "refused", "refusal": "unknown_version"}
    update = entity_id(hass, "update", "galleria_player_p2_update")
    with pytest.raises(HomeAssistantError, match="unknown_version"):
        await hass.services.async_call("update", "install", {"entity_id": update}, blocking=True)
    assert door.installs == [("p2", LATEST)]


async def test_an_install_in_progress_reads_from_central(hass: HomeAssistant, entry: MockConfigEntry,
                                                        door: FakeDoor) -> None:
    door.state[contract.VERSIONS_KEY] = wire(contract.VERSIONS_KEY, {"pis": {
        "p1": {"version": LATEST, "chosen": True, "install": "restarting"}}})
    await read_all(hass, entry)
    assert state_of(hass, "update", "galleria_player_p1_update").attributes["in_progress"] is True
    assert state_of(hass, "update", "galleria_player_p2_update").attributes["in_progress"] is False


# ---- I4: a Frame's state, from the Pi Central names, at the binding's generation ----

async def test_a_frame_reads_only_the_report_carrying_its_binding_s_generation(
        hass: HomeAssistant, entry: MockConfigEntry, pis: FakePis) -> None:
    assert state_of(hass, "binary_sensor", "galleria_frame_frame-1_showing").state == STATE_ON
    assert float(state_of(hass, "sensor", "galleria_frame_frame-1_light").state) == 41.0
    # frame-2: Pi p2 reports generation 2, Central's binding is generation 1
    assert state_of(hass, "binary_sensor", "galleria_frame_frame-2_showing").state == STATE_UNAVAILABLE
    assert state_of(hass, "sensor", "galleria_frame_frame-2_light").state == STATE_UNAVAILABLE
    # frame-3: no Pi drives it
    assert state_of(hass, "sensor", "galleria_frame_frame-3_light").state == STATE_UNAVAILABLE

    pis.set("p2", frames=({"frame_id": "frame-2", "generation": 1, "output_id": "HDMI-A-1", "showing": False,
                           "light_lux": 80.0},))
    await read_all(hass, entry)
    assert state_of(hass, "binary_sensor", "galleria_frame_frame-2_showing").state == STATE_OFF
    assert float(state_of(hass, "sensor", "galleria_frame_frame-2_light").state) == 80.0


# ---- I5: a Pi that cannot be read ----

async def test_every_entity_of_an_unreachable_pi_reads_unavailable(hass: HomeAssistant, entry: MockConfigEntry,
                                                                   pis: FakePis) -> None:
    pis.readings["p1"] = "pi_unreachable"
    await read_all(hass, entry)
    for platform, unique_id in (("update", "galleria_player_p1_update"),
                                ("sensor", "galleria_player_p1_light_meter"),
                                ("sensor", "galleria_frame_frame-1_light"),
                                ("binary_sensor", "galleria_frame_frame-1_showing")):
        assert state_of(hass, platform, unique_id).state == STATE_UNAVAILABLE, unique_id
    assert state_of(hass, "update", "galleria_player_p2_update").state != STATE_UNAVAILABLE


async def test_a_player_of_another_schema_major_raises_a_repair_issue(hass: HomeAssistant, entry: MockConfigEntry,
                                                                     pis: FakePis) -> None:
    pis.set("p1", player_birth="unreadable", frames="unreadable")
    await read_all(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "unreadable_player_p1") is not None
    assert state_of(hass, "sensor", "galleria_frame_frame-1_light").state == STATE_UNAVAILABLE
    pis.set("p1", player_birth=birth(), frames=())
    await read_all(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "unreadable_player_p1") is None


async def test_central_away_leaves_its_facts_unavailable_and_frames_read_from_the_pi_it_last_named(
        hass: HomeAssistant, entry: MockConfigEntry, door: FakeDoor) -> None:
    light = state_of(hass, "sensor", "galleria_frame_frame-1_light").state
    assert light != STATE_UNAVAILABLE
    door.down = True
    door.connected = False
    await read_all(hass, entry)
    assert state_of(hass, "sensor", f"galleria_{ORIGIN}_central_version").state == STATE_UNAVAILABLE
    assert state_of(hass, "binary_sensor", f"galleria_{ORIGIN}_hub_connected").state == STATE_OFF
    # Only Central changes a binding: the Frame still reads from the Pi Central last named.
    assert state_of(hass, "sensor", "galleria_frame_frame-1_light").state == light


async def test_unloading_stops_publishing(hass: HomeAssistant, entry: MockConfigEntry, door: FakeDoor) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await settle(hass)
    hass.states.async_set("light.after", STATE_ON, {"friendly_name": "After"})
    await settle(hass)
    assert "light.after" not in {entity for entity, _ in door.published}


# ---- The config flow ----

async def test_the_flow_adds_one_entry_per_central(hass: HomeAssistant, seams: None) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ORIGIN: "Central.Test:8000/"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ORIGIN: ORIGIN}
    assert result["result"].unique_id == ORIGIN
    await hass.async_block_till_done()
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    again = await hass.config_entries.flow.async_configure(again["flow_id"], {CONF_ORIGIN: "http://central.test:8000"})
    assert again["type"] is FlowResultType.ABORT and again["reason"] == "already_configured"


@pytest.mark.parametrize(("origin", "error"), [("ftp://central.test", {CONF_ORIGIN: "invalid_origin"}),
                                               ("http://central.test/console", {CONF_ORIGIN: "invalid_origin"})])
async def test_the_flow_refuses_what_is_not_an_origin(hass: HomeAssistant, origin: str, error: dict) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ORIGIN: origin})
    assert result["type"] is FlowResultType.FORM and result["errors"] == error


async def test_the_flow_says_when_the_door_does_not_answer_or_is_not_central(hass: HomeAssistant,
                                                                             door: FakeDoor) -> None:
    from unittest.mock import AsyncMock, patch
    with patch.object(CentralDoor, "connect", AsyncMock(side_effect=DoorUnavailable("refused"))):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ORIGIN: ORIGIN})
    assert result["errors"] == {"base": "cannot_connect"}
    door.state.pop(contract.CENTRAL_BIRTH_KEY)
    with patch.object(CentralDoor, "connect", AsyncMock(return_value=door)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ORIGIN: ORIGIN})
    assert result["errors"] == {"base": "not_central"}
