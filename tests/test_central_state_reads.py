"""Central's state keys as its hub serves them (central/infra/central_state.py PgCentralStateReads), on
real PostgreSQL: what `installation`, `listening` and `room-lights` contain, from the services the
console's routes call. The keys' transport (the door, a hub restart) is the integration tier's
(tests/integration/test_central_leaf_bridge.py, test_central_worker_hub.py).

Mutation probes: `listening` returning no entity, `room_lights` filtering every light, or the
installation listing no Pi's address -> each test here fails."""
from __future__ import annotations

from test_fleet_attempts import SERIAL
from test_node_boot import ALL_READY
from test_node_fleet_hosts import Host, _rig
from test_node_host_facts_ingest import _facts, _send

from central.fleet.node_commands import NodeCommands
from central.infra.central_state import PgCentralStateReads
from central.infra.frame_lighting_store import PgHouseLock
from central.infra.player_versions import PgPlayerVersions
from central.infra.room_light_store import PgRoomLights
from central.room_light.model import (
    FixedLight,
    MeasuredLight,
    ReportedLight,
    ReporterRef,
    Sensor,
    SensorRef,
    TypedColour,
)


def _reads(registry, sessions=None) -> tuple[PgCentralStateReads, PgRoomLights]:
    lights = PgRoomLights(registry.db, PgHouseLock())
    if sessions is None:
        _, sessions = _rig(registry)
    versions = PgPlayerVersions(registry.db, registry.clock, ALL_READY, None, NodeCommands(sessions))
    return PgCentralStateReads(registry.db, registry.clock, versions, lights, lambda: None), lights


def test_the_installation_lists_each_pi_at_the_address_its_host_reported(registry):
    boots, sessions = _rig(registry)
    host = Host(registry, sessions, boots)
    reads, _ = _reads(registry, sessions)
    assert [(each.serial, each.address) for each in reads.installation().players] == [(SERIAL, None)]
    _send(host, _facts(host.grant.producer, 2, address="192.168.1.40"))
    assert [(each.serial, each.address) for each in reads.installation().players] == [(SERIAL, "192.168.1.40")]


def test_listening_names_every_home_assistant_entity_the_setup_names_and_nothing_else(registry):
    reads, lights = _reads(registry)
    assert reads.listening().entities == ()
    lights.save_room_light("spot", ReportedLight(name="Spot", colour=TypedColour(kelvin=4000),
                                                 reporter=ReporterRef(source="home-assistant", address="light.spot")))
    lights.save_sensor("meter", Sensor(name="Meter", at=SensorRef(source="home-assistant", address="sensor.lux")))
    lights.save_room_light("window", MeasuredLight(name="Window", colour=TypedColour(kelvin=6500), sensor="meter"))
    assert reads.listening().entities == ("light.spot", "sensor.lux")


def test_room_lights_list_each_light_by_name_with_the_entry_the_wall_draws(registry):
    reads, lights = _reads(registry)
    lights.save_room_light("lamp", FixedLight(name="Lamp", colour=TypedColour(kelvin=3000)))
    document = lights.current()
    listed = reads.room_lights()
    assert listed.change == document.change
    assert {(each.id, each.name) for each in listed.lights} >= {("lamp", "Lamp")}
    assert {each.id: each.entry for each in listed.lights} == dict(document.lights)
