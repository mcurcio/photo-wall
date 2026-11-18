"""Central's state keys as its hub serves them (central/infra/central_state.py PgCentralStateReads), on
real PostgreSQL: what `installation`, `listening` and `room-lights` contain, from the services the
console's routes call. The keys' transport (the door, a hub restart) is the integration tier's
(tests/integration/test_central_leaf_bridge.py, test_central_worker_hub.py).

Mutation probes: `listening` returning no entity, `room_lights` filtering every light, or the
installation listing no Pi's address -> each test here fails."""
from __future__ import annotations

from test_fleet_attempts import SERIAL
from test_node_boot import ALL_READY
from test_node_fleet_hosts import FACTS, Host, _rig, on_line

from central.infra.central_state import PgCentralStateReads
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.player_versions import PgPlayerVersions
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    ColourSensor,
    ColourSensorRef,
    FixedLight,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    Sensor,
    SensorRef,
    TypedColour,
)
from contracts.node_host_facts import encode_host_facts_state
from contracts.node_state import HOST_FACTS_KEY


def _reads(registry, sessions=None) -> tuple[PgCentralStateReads, PgRoomLights]:
    lights = PgRoomLights(registry.db, PgHouseLock())
    if sessions is None:
        _, sessions = _rig(registry)
    versions = PgPlayerVersions(registry.db, registry.clock, ALL_READY, None)
    return PgCentralStateReads(registry.db, registry.clock, versions, lights, lambda: None), lights


def test_the_installation_lists_each_pi_at_the_address_its_host_line_states(registry):
    boots, sessions = _rig(registry)
    Host(registry, sessions, boots)
    reads, _ = _reads(registry, sessions)
    assert [(each.serial, each.address) for each in reads.installation().players] == [(SERIAL, None)]
    on_line(registry, SERIAL, HOST_FACTS_KEY, encode_host_facts_state(FACTS), 1)
    assert [(each.serial, each.address) for each in reads.installation().players] == [(SERIAL, "192.168.1.40")]


def test_listening_names_every_home_assistant_entity_the_setup_names_and_nothing_else(registry):
    reads, lights = _reads(registry)
    assert reads.listening().entities == ()
    lights.save_room_light("spot", ReportedLight(name="Spot", colour=TypedColour(kelvin=4000),
                                                 reporter=ReporterRef(source="home-assistant", address="light.spot")), by="console")
    lights.save_sensor("meter", Sensor(name="Meter", at=SensorRef(source="home-assistant", address="sensor.lux")), by="console")
    lights.save_room_light("window", MeasuredLight(name="Window", colour=TypedColour(kelvin=6500), sensor="meter"), by="console")
    lights.save_colour_sensor("sky", ColourSensor(name="Sky", at=ColourSensorRef(
        source="home-assistant", address="sensor.sun_sky_color_temperature")), by="console")
    assert reads.listening().entities == ("light.spot", "sensor.lux", "sensor.sun_sky_color_temperature")


def test_room_lights_list_each_light_by_name_with_the_entry_the_wall_draws(registry):
    reads, lights = _reads(registry)
    lights.save_room_light("lamp", FixedLight(name="Lamp", colour=TypedColour(kelvin=3000)), by="console")
    document = lights.current()
    listed = reads.room_lights()
    assert listed.change == document.change
    assert {(each.id, each.name) for each in listed.lights} >= {("lamp", "Lamp")}
    assert {each.id: each.entry for each in listed.lights} == dict(document.lights)
